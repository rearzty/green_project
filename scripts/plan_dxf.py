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
import sys
import tempfile
from collections import Counter
from pathlib import Path

from geo_engine.compliance import explain_items, report_payload, unverified_sources, write_trace_csv
from geo_engine.io.dwg_convert import available_backend, convert_dwg_to_dxf
from geo_engine.io.dxf_reader import COMBINED_LAYER_MAP, dxf_bundle_paths, read_dxf_bundle
from geo_engine.io.dxf_writer import RESULT_LAYER_PREFIX, write_dxf
from geo_engine.norms import load_norms
from geo_engine.planner import plan_items
from geo_engine.species import load_species  # noqa: F401  (ensures the species YAML is present early)
from geo_engine.territory import MissingTerritoryError, territory_polygon
from ml_scoring.heuristic_scorer import HeuristicScorer
from ml_scoring.ml_scorer import MLScorer, ModelNotTrainedError

PLANTING_TYPES = ("tree", "shrub", "lawn")


def _convert_if_needed(path: Path, workdir: Path) -> Path:
    if path.suffix.lower() != ".dwg":
        return path
    if available_backend() is None:
        raise SystemExit(
            f"Файл {path.name} в формате DWG, но конвертер не найден на PATH.\n"
            "Установите LibreDWG (brew install libredwg, либо сборка из исходников — "
            "рецепт в geo_engine/io/dwg_convert.py) или ODA File Converter."
        )
    return convert_dwg_to_dxf(path, workdir)


def resolve_inputs(source: Path, workdir: Path) -> tuple[Path, list[Path]]:
    """(главный чертёж, все файлы бандла) — с конвертацией DWG при необходимости.

    Каталог разбирается так же, как устроены реальные поставки: чертежи в корне
    каталога — главные, всё из Xrefs/ — внешние ссылки к ним.
    """
    if source.is_dir():
        mains = sorted(p for p in source.iterdir() if p.suffix.lower() in (".dxf", ".dwg"))
        if not mains:
            raise SystemExit(f"В каталоге {source} нет ни одного .dxf/.dwg файла.")
        # Самый крупный файл в корне — почти всегда и есть главный чертёж, а не
        # вспомогательная врезка; выбор всё равно влияет только на то, в копию
        # какого документа пишется результат.
        main = max(mains, key=lambda p: p.stat().st_size)
        xref_dir = source / "Xrefs"
        xrefs = sorted(p for p in xref_dir.iterdir() if p.suffix.lower() in (".dxf", ".dwg")) if xref_dir.is_dir() else []
        converted_main = _convert_if_needed(main, workdir)
        converted = [converted_main]
        for path in xrefs:
            try:
                converted.append(_convert_if_needed(path, workdir))
            except RuntimeError as error:
                # Один нечитаемый xref не должен валить весь прогон — но и молча
                # пропасть он не должен, иначе потерянная граница участка
                # выглядит как отсутствующая.
                print(f"  ! пропущен {path.name}: {error}", file=sys.stderr)
        return converted_main, converted

    converted = _convert_if_needed(source, workdir)
    if source.suffix.lower() == ".dwg":
        # У сконвертированного файла нет соседней Xrefs/ — берём её у оригинала.
        siblings = dxf_bundle_paths(source)
        bundle = [converted]
        for path in siblings[1:]:
            try:
                bundle.append(_convert_if_needed(path, workdir))
            except RuntimeError as error:
                print(f"  ! пропущен {path.name}: {error}", file=sys.stderr)
        return converted, bundle
    return converted, dxf_bundle_paths(converted)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.plan_dxf",
        description="Генерация плана озеленения из чертежа: DXF/DWG -> DXF с результатом на отдельном слое + отчёт с обоснованиями по НПА.",
    )
    parser.add_argument("--input", required=True, type=Path, help="Чертёж (.dxf/.dwg) или каталог с чертежом и Xrefs/")
    parser.add_argument("--output", required=True, type=Path, help="Куда записать итоговый DXF")
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
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    planting_types = [t.strip() for t in args.types.split(",") if t.strip()]
    unknown = [t for t in planting_types if t not in PLANTING_TYPES]
    if unknown:
        raise SystemExit(f"Неизвестные типы посадок: {', '.join(unknown)}. Допустимы: {', '.join(PLANTING_TYPES)}")

    norms = load_norms()
    if args.tree_spacing:
        norms = norms.with_spacing_override("tree", args.tree_spacing)
    if args.shrub_spacing:
        norms = norms.with_spacing_override("shrub", args.shrub_spacing)

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

        print(f"3/5 Генерация ({args.scoring}, типы: {', '.join(planting_types)})...")
        items = plan_items(args.plan_key, utilities, zones, territory, planting_types, scorer.as_score_fn(), norms)
        counts = Counter(i.planting_type for i in items)
        print(f"     посадок: {len(items)} ({', '.join(f'{k}: {v}' for k, v in counts.most_common())})")

        print("4/5 Проверка нормативных отступов и сборка обоснований...")
        records = explain_items(items, utilities, zones, norms)
        violations = [r for r in records if not r.compliant]
        print(f"     соблюдено: {len(records) - len(violations)} из {len(records)}")
        if violations:
            print(f"     ВНИМАНИЕ: нарушений {len(violations)} — см. отчёт")

        print(f"5/5 Запись результата на слои {args.prefix}$*")
        args.output.parent.mkdir(parents=True, exist_ok=True)
        write_dxf(items, args.output, base_dxf=main_drawing, records=records, prefix=args.prefix)

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
