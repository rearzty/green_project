"""CLI: входной чертёж -> план озеленения отдельным слоем + отчёт с обоснованиями.

Это тот самый пайплайн, вокруг которого построено ТЗ: «пайплайн DXF ->
обработка -> DXF (результат на отдельном слое)», и «UI на демо необязателен:
допустим запуск из терминала». Работает без базы и без веб-сервера — только
geo_engine + ml_scoring.

    python -m scripts.plan_dxf --input <чертёж.dxf|.dwg|каталог> --output plan.dxf

Каталог на входе — обычный случай для реальных данных: проектный чертёж не
самодостаточен, граница участка и сети лежат в соседней папке Xrefs/ (см.
geo_engine.io.dxf_reader.read_dxf_bundle).
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

from geo_engine.compliance import explain_items, report_payload, unverified_sources, write_trace_csv
from geo_engine.io.dxf_reader import (
    COMBINED_LAYER_MAP,
    BundleResolutionError,
    read_document,
    read_dxf_bundle,
    resolve_bundle_inputs,
)
from geo_engine.io.dxf_writer import RESULT_LAYER_PREFIX, write_dxf
from geo_engine.norms import load_norms
from geo_engine.planner import (
    CROWN_SPACING_TYPES,
    ACCENT_RATIONALE_PREFIX,
    GROUP_RATIONALE_PREFIX,
    PLACEMENT_PATTERNS,
    ROW_RATIONALE_PREFIX,
    plan_items,
)
from geo_engine.species import load_catalogue
from geo_engine.territory import MissingTerritoryError, territory_polygon
from ml_scoring.heuristic_scorer import HeuristicScorer
from ml_scoring.ml_scorer import MLScorer, ModelNotTrainedError

PLANTING_TYPES = ("tree", "shrub", "lawn")


def resolve_inputs(source: Path, workdir: Path) -> tuple[Path, list[Path]]:
    """(главный чертёж, все файлы бандла) — с конвертацией DWG при необходимости.

    Тонкая обёртка над `dxf_reader.resolve_bundle_inputs()` — тем же кодом, что
    и веб-загрузка: рекурсивный обход папки проекта (сети по подпапкам заказов
    на съёмку, не только Xrefs/ссылки), подсказка со списком подпапок, когда
    чертежа нет прямо в корне. `BundleResolutionError` — единственная точка,
    где CLI переводит это в `SystemExit`; предупреждения о пропущенных файлах
    печатаются здесь же, а не проглатываются.
    """
    try:
        main, bundle, warnings = resolve_bundle_inputs(source, workdir)
    except BundleResolutionError as error:
        raise SystemExit(str(error)) from error
    for warning in warnings:
        print(f"  ! {warning}", file=sys.stderr)
    return main, bundle


def _open_and_count(path: Path) -> tuple[Path, int | None, str | None]:
    """Opens one bundle file and counts its modelspace entities, or reports
    why it couldn't. Module-level and picklable so it can run in a worker
    process (see pick_base_drawing) -- returns the failure as data rather
    than raising, since a worker process has no way to call back into the
    caller's own stderr-printing loop."""
    try:
        doc = read_document(path)
    except Exception as error:  # noqa: BLE001 — годится любой открывающийся
        return path, None, type(error).__name__
    return path, sum(1 for _ in doc.modelspace()), None


def pick_base_drawing(candidates: list[Path]) -> Path | None:
    """Самый крупный чертёж, который реально открывается.

    Исходник нужен только как холст: результат пишется в его копию отдельным
    слоем, чтобы эксперт открыл файл и увидел свою подоснову с включаемым
    слоем плана. Значит подойдёт любой читаемый файл бандла, и незачем терять
    весь прогон из-за того, что самый большой повреждён.

    Случай не гипотетический: на Измайловской площади главный чертёж (76 МБ)
    не открывается ни `readfile`, ни `recover` ни в одном режиме обработки
    ошибок — внутри испорченная юникод-escape-последовательность вида
    "backslash-U-plus", на которой падает декодер ezdxf. Остальные файлы бандла при этом читаются, и план по
    ним строится полностью.

    Каждое открытие независимо от остальных — то же самое чтение, что уже
    параллелится в `read_dxf_bundle`, просто здесь бандл перечитывается
    заново только ради подсчёта сущностей. Раньше это был последовательный
    цикл — замерено на реальном бандле (33 файла, «1. Олимпийская деревня»):
    81.2с, самый большой необъяснённый кусок времени всего CLI-прогона (см.
    docs/worklog.md), при том что параллельное чтение того же бандла в
    read_dxf_bundle заняло 61с. `ProcessPoolExecutor.map` возвращает
    результаты в порядке `ordered`, не в порядке завершения — порядок
    перебора (и, значит, какой файл выигрывает при равном числе сущностей)
    остаётся ровно таким же, как в последовательной версии.
    """
    ordered = sorted(set(candidates), key=lambda p: p.stat().st_size, reverse=True)
    if not ordered:
        return None

    if len(ordered) > 1:
        worker_count = min(len(ordered), os.cpu_count() or 1)
        with ProcessPoolExecutor(max_workers=worker_count) as pool:
            results = list(pool.map(_open_and_count, ordered))
    else:
        results = [_open_and_count(p) for p in ordered]

    best, best_count = None, -1
    for path, count, error_name in results:
        if count is None:
            print(f"  ! как основу не использовать {path.name}: {error_name}", file=sys.stderr)
            continue
        # Не первый открывшийся, а самый содержательный: у бандла бывают
        # файлы-заглушки в пару объектов, и копия такой заглушки со слоем
        # результата формально проходит, но эксперт открывает её и не видит
        # своей подосновы — ровно то, ради чего результат и пишется поверх.
        if count > best_count:
            best, best_count = path, count
    return best


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.plan_dxf",
        description="Генерация плана озеленения из чертежа: DXF/DWG -> DXF с результатом на отдельном слое + отчёт с обоснованиями по НПА.",
    )
    # Не required: --list-species печатает каталог и выходит, требовать при
    # этом чертёж было бы бессмысленно. Проверяются вручную ниже.
    parser.add_argument("--input", type=Path, help="Чертёж (.dxf/.dwg) или каталог с чертежом и Xrefs/")
    parser.add_argument("--output", type=Path, help="Куда записать итоговый DXF")
    parser.add_argument("--report", type=Path, help="Куда записать JSON-отчёт (по умолчанию — рядом с --output)")
    parser.add_argument(
        "--csv",
        type=Path,
        nargs="?",
        const=Path("-"),
        help="Дополнительно выгрузить плоскую трассировку «посадка → норма → пункт» в CSV "
        "(без значения — рядом с --output)",
    )
    parser.add_argument(
        "--types",
        default="tree,shrub",
        help="Типы посадок через запятую: tree,shrub,lawn (по умолчанию tree,shrub)",
    )
    parser.add_argument("--scoring", choices=("heuristic", "ml"), default="heuristic", help="Способ ранжирования")
    parser.add_argument("--tree-spacing", type=float, help="Интервал между деревьями, м (по умолчанию из нормативов)")
    parser.add_argument("--shrub-spacing", type=float, help="Интервал между кустами, м")
    parser.add_argument("--prefix", default=RESULT_LAYER_PREFIX, help="Префикс слоёв результата")
    parser.add_argument("--plan-key", default="cli", help="Ключ детерминированной расстановки (одинаковый ключ -> тот же план)")
    parser.add_argument(
        "--species",
        action="append",
        metavar="ТИП=ПОРОДА",
        help="Задать породу явно, например --species tree=«Липа мелколистная». "
        "Без этого порода выбирается детерминированно по --plan-key. "
        "Породу видно в выводе; список — в geo_engine/config/species.yaml",
    )
    parser.add_argument("--list-species", action="store_true", help="Показать доступные породы и выйти")
    parser.add_argument(
        "--density",
        action="append",
        metavar="ТИП=ШТ_НА_ГА",
        help="Плотность посадки, например --density tree=120 --density shrub=400. "
        "Без неё алгоритм заполняет каждое легально доступное место — это «сколько влезает», "
        "а не «сколько нужно». Нормативной величины в доступных актах нет, поэтому значения "
        "по умолчанию нет тоже",
    )
    parser.add_argument(
        "--pattern",
        choices=PLACEMENT_PATTERNS,
        default="auto",
        help="Схема расстановки: auto — ряд вдоль проездов/тротуаров/границы участка плюс россыпь "
        "в остатке (по умолчанию); scatter — только россыпь; row — только ряды",
    )
    return parser


def _parse_species_overrides(raw: list[str] | None) -> dict[str, str]:
    """`--species tree=Липа` -> {"tree": "Липа"}. Имя сверяется с каталогом.

    Сверка здесь, а не внутри planner: опечатка в названии породы иначе просто
    не нашлась бы в каталоге и молча откатилась к автоматическому выбору, и
    пользователь получил бы не ту породу, которую просил, без единого слова.
    """
    overrides: dict[str, str] = {}
    catalogue = load_catalogue()
    for entry in raw or []:
        planting_type, _, name = entry.partition("=")
        planting_type, name = planting_type.strip(), name.strip().strip("«»\"'")
        if not name:
            raise SystemExit(f"Ожидалось ТИП=ПОРОДА, получено: {entry!r}")
        # Перечень проверяется ПЕРВЫМ, до наличия в каталоге: инвазивных видов
        # в каталоге и нет, и сообщение «не найдена» скрыло бы настоящую
        # причину — именно так выглядели бы «Дёрен белый» и «Пузыреплодник»,
        # которые сервис предлагал до сверки с 369-ПП.
        invasive = catalogue.is_invasive(name)
        if invasive is not None:
            raise SystemExit(
                f"Порода «{name}» внесена в перечень инвазивных видов "
                f"(369-ПП от 03.03.2026, приложение 1, группа {invasive.group}) "
                f"и не может быть предложена к посадке."
            )
        if catalogue.get(name) is None:
            available = ", ".join(catalogue.names_for_type(planting_type)) or "нет пород для этого типа"
            raise SystemExit(f"Порода «{name}» не найдена в каталоге. Доступны для {planting_type}: {available}")
        overrides[planting_type] = name
    return overrides


def _parse_densities(raw: list[str] | None) -> dict[str, float]:
    """`--density tree=120` -> {"tree": 120.0}."""
    out: dict[str, float] = {}
    for entry in raw or []:
        planting_type, _, value = entry.partition("=")
        try:
            density = float(value)
        except ValueError:
            raise SystemExit(f"Ожидалось ТИП=ЧИСЛО, получено: {entry!r}") from None
        if density <= 0:
            raise SystemExit(f"Плотность должна быть положительной, получено: {entry!r}")
        out[planting_type.strip()] = density
    return out


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.list_species:
        catalogue = load_catalogue()
        for planting_type in ("tree", "shrub"):
            print(f"{planting_type}:")
            for species in catalogue.for_type(planting_type):
                spacing = catalogue.spacing_for_crown(species.crown)
                # Интервал по классу кроны применяется только к деревьям —
                # у кустарника нормативные значения относятся к групповой
                # посадке, которой генератор пока не умеет (см. planner.py).
                interval = f"{spacing} м" if planting_type in CROWN_SPACING_TYPES else "интервал из planting_norms.yaml"
                print(f"   «{species.name}» — крона {species.crown}, {interval}, диаметр {species.crown_diameter_m} м")
        return 0
    missing = [flag for flag, value in (("--input", args.input), ("--output", args.output)) if value is None]
    if missing:
        raise SystemExit(f"Не заданы обязательные аргументы: {', '.join(missing)}")
    species_overrides = _parse_species_overrides(args.species)
    densities = _parse_densities(args.density)
    planting_types = [t.strip() for t in args.types.split(",") if t.strip()]
    unknown = [t for t in planting_types if t not in PLANTING_TYPES]
    if unknown:
        raise SystemExit(f"Неизвестные типы посадок: {', '.join(unknown)}. Допустимы: {', '.join(PLANTING_TYPES)}")

    norms = load_norms()
    keep_spacing_for = []
    if args.tree_spacing:
        norms = norms.with_spacing_override("tree", args.tree_spacing)
        keep_spacing_for.append("tree")
    if args.shrub_spacing:
        norms = norms.with_spacing_override("shrub", args.shrub_spacing)
        keep_spacing_for.append("shrub")

    with tempfile.TemporaryDirectory(prefix="greenplan-") as tmp:
        workdir = Path(tmp)
        print(f"1/5 Чтение чертежа: {args.input}")
        main_drawing, bundle = resolve_inputs(args.input, workdir)
        print(f"     файлов в бандле: {len(bundle)}")
        utilities, zones = read_dxf_bundle(
            bundle, layer_map=COMBINED_LAYER_MAP, stitch_dashes=True, drop_origin=True
        )
        by_type = Counter(u.object_type for u in utilities)
        print(f"     сетей: {sum(by_type.values())} ({', '.join(f'{k}: {v}' for k, v in by_type.most_common())})")

        try:
            territory = territory_polygon(zones)
        except MissingTerritoryError as error:
            raise SystemExit(f"\nОШИБКА: {error}") from error
        print(f"2/5 Граница участка: {territory.geom_type}, площадь {territory.area:,.0f} м²".replace(",", " "))

        existing_greenery = [z.geometry for z in zones if z.zone_type == "existing_greenery"]
        if args.scoring == "ml":
            try:
                scorer = MLScorer(norms, existing_greenery=existing_greenery)
            except ModelNotTrainedError as error:
                raise SystemExit(f"\nОШИБКА: {error}") from error
        else:
            scorer = HeuristicScorer(norms, existing_greenery=existing_greenery)

        print(f"3/5 Генерация ({args.scoring}, схема: {args.pattern}, типы: {', '.join(planting_types)})...")
        items = plan_items(
            args.plan_key,
            utilities,
            zones,
            territory,
            planting_types,
            scorer.as_score_fn(),
            norms,
            keep_spacing_for=keep_spacing_for,
            species_overrides=species_overrides,
            pattern=args.pattern,
            density_per_ha=densities,
        )
        counts = Counter(i.planting_type for i in items)
        print(f"     посадок: {len(items)} ({', '.join(f'{k}: {v}' for k, v in counts.most_common())})")
        in_rows = sum(1 for i in items if i.rationale.startswith(ROW_RATIONALE_PREFIX))
        in_groups = sum(1 for i in items if i.rationale.startswith(GROUP_RATIONALE_PREFIX))
        accents = sum(1 for i in items if i.rationale.startswith(ACCENT_RATIONALE_PREFIX))
        loose = len(items) - in_rows - in_groups - accents
        parts = [
            f"{label}: {count}"
            for label, count in (
                ("рядом", in_rows),
                ("куртинами", in_groups),
                ("солитерами", accents),
                ("россыпью", loose),
            )
            if count
        ]
        if parts:
            print(f"     схемы посадки — {', '.join(parts)}")
        catalogue = load_catalogue()
        # Пород на тип теперь несколько: ряд одной, куртины разными. Печатать
        # первую попавшуюся значило бы скрывать состав плана.
        used: dict[str, Counter] = {}
        for item in items:
            used.setdefault(item.planting_type, Counter())[item.species] += 1
        for planting_type in sorted(used):
            print(f"     {planting_type}:")
            for species_name, count in used[planting_type].most_common():
                picked = catalogue.get(species_name)
                if picked is None:
                    print(f"        «{species_name}» — {count}")
                    continue
                if planting_type in keep_spacing_for:
                    note = "интервал задан вручную"
                elif planting_type in CROWN_SPACING_TYPES:
                    note = f"интервал {catalogue.spacing_for_crown(picked.crown)} м по классу кроны"
                else:
                    # Для кустарника нормативные 0,3-1,0 м относятся к
                    # групповой посадке, а не к россыпи.
                    note = f"интервал {norms.spacing_for(planting_type).min_distance_m} м из planting_norms.yaml"
                print(f"        «{species_name}» — {count}, крона {picked.crown}, {note}")

        print("4/5 Проверка нормативных отступов и сборка обоснований...")
        records = explain_items(items, utilities, zones, norms)
        violations = [r for r in records if not r.compliant]
        print(f"     соблюдено: {len(records) - len(violations)} из {len(records)}")
        if violations:
            print(f"     ВНИМАНИЕ: нарушений {len(violations)} — см. отчёт")

        print(f"5/5 Запись результата на слои {args.prefix}$*")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        base = pick_base_drawing([main_drawing, *bundle])
        if base is None:
            raise SystemExit(
                "\nОШИБКА: ни один чертёж бандла не открывается — не в копию чего писать результат."
            )
        if base != main_drawing:
            print(f"     основа: {base.name} (главный чертёж не читается)")
        write_dxf(items, args.output, base_dxf=base, records=records, prefix=args.prefix)

    report_path = args.report or args.output.with_suffix(".report.json")
    unverified = unverified_sources(records)
    report = report_payload(
        records,
        norms,
        input=str(args.input),
        output_dxf=str(args.output),
        result_layer_prefix=args.prefix,
        scoring_mode=args.scoring,
        planting_types=planting_types,
        territory_area_m2=round(float(territory.area), 2),
    )
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")

    csv_line = ""
    if args.csv is not None:
        csv_path = args.output.with_suffix(".trace.csv") if str(args.csv) == "-" else args.csv
        rows = write_trace_csv(records, csv_path)
        csv_line = f"\n  CSV:   {csv_path} ({rows} строк трассировки)"

    print(f"\nГотово.\n  DXF:   {args.output}\n  отчёт: {report_path}{csv_line}")
    if unverified:
        print("\nНесверенные ссылки на нормативы (значение применено, но с текстом акта не сверялось):")
        for citation, count in sorted(unverified.items(), key=lambda kv: -kv[1]):
            print(f"  {count:6}  {citation}")
    return 1 if violations else 0


if __name__ == "__main__":
    raise SystemExit(main())
