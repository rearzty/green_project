"""Экспорт плана из веба — тем же комплектом, что пишет CLI.

Раньше экспорт писал пустой чертёж: кружки посадок без подосновы и без
обоснований (`write_dxf(items, path)`), и эксперт открывал файл, в котором
от исходного проекта ничего не было. ТЗ требует обратного: результат
отдельным слоем поверх исходного чертежа и обоснование каждой посадки.
Теперь архив содержит:

* `plan.dxf` — главный чертёж проекта с слоями GREEN_AI$* и XDATA-обоснованием;
* файлы внешних ссылок по тем же относительным путям — AutoCAD подгрузит все
  листы (см. geo_engine/io/result_package.py);
* `plan.report.json` и `plan.trace.csv` — машиночитаемое обоснование.

Если исходника нет (проект из GeoJSON) или он не открывается, план пишется
отдельным чертежом, как раньше, — и об этом сказано в отчёте.
"""

from __future__ import annotations

import json
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path

from backend.app.db.models import Plan
from backend.app.services.geo_io import db_to_shape, layers_to_domain
from backend.app.services.pipeline_service import plan_norms
from backend.app.services.project_service import materialize_source
from geo_engine.compliance import explain_items, report_payload, write_trace_csv
from geo_engine.io.dwg_convert import convert_dwg_to_dxf
from geo_engine.io.dxf_reader import discover_bundle_members
from geo_engine.io.dxf_writer import (
    LAYER_SUFFIX_BY_TYPE,
    RESULT_LAYER_PREFIX,
    result_layer_name,
    violation_layer_name,
    violation_marks_for,
    write_dxf,
)
from geo_engine.io.result_package import copy_references, missing_references, place_by_saved_paths, xref_paths
from geo_engine.model import PlantingItem


MAX_UNVERIFIED_ITEM_IDS = 2000
PLANTING_LAYER_KEYS = tuple(LAYER_SUFFIX_BY_TYPE)  # tree, shrub, lawn
ALL_LAYER_KEYS = (*PLANTING_LAYER_KEYS, "violations")


class EmptyExportError(ValueError):
    """Выбран пустой набор слоёв: файл без единого слоя результата — не экспорт."""


@dataclass(frozen=True)
class ExportOptions:
    """Выбор пользователя в диалоге «Перед экспортом» (routes_export.py)."""

    layers: frozenset[str] = frozenset(ALL_LAYER_KEYS)
    include_base: bool = True
    include_report: bool = True


def _domain_items(plan: Plan) -> list[PlantingItem]:
    return [
        PlantingItem(
            geometry=db_to_shape(row.geometry),
            planting_type=row.planting_type,
            species=row.species,
            score=row.score,
            rationale=row.rationale,
            is_manual_edit=row.is_manual_edit,
        )
        for row in plan.items
    ]


def plan_to_dxf(plan: Plan, output_path: str | Path) -> Path:
    """Только чертёж плана без подосновы — оставлено для тестов и вызовов,
    которым комплект не нужен."""
    output_path = Path(output_path)
    write_dxf(_domain_items(plan), output_path)
    return output_path


def plan_to_package(plan: Plan, output_zip: str | Path, options: ExportOptions | None = None) -> Path:
    options = options or ExportOptions()
    output_zip = Path(output_zip)
    project = plan.project
    items = _domain_items(plan)
    utilities, zones = layers_to_domain(project.layers)
    norms = plan_norms(plan)
    records = explain_items(items, utilities, zones, norms)

    # Какие слои пишет чертёж. Отчёт — всегда про весь план: это обоснование
    # версии, а не выгрузка видимого. Записи обоснования идут в ту же позицию,
    # что и посадки, поэтому отбираются вместе.
    chosen_types = [t for t in PLANTING_LAYER_KEYS if t in options.layers]
    if not chosen_types and "violations" not in options.layers:
        raise EmptyExportError("Не выбран ни один слой — в файле нечего будет показать.")
    keep = [index for index, item in enumerate(items) if item.planting_type in chosen_types]
    drawn_items = [items[index] for index in keep]
    drawn_records = [records[index] for index in keep]
    marks = violation_marks_for(items, records) if "violations" in options.layers else []

    with tempfile.TemporaryDirectory(prefix="greenproject-export-") as tmp:
        work = Path(tmp)
        package = work / "package"
        package.mkdir()
        base = main = None
        members: list[Path] = []
        note = "план записан отдельным чертежом: исходник проекта недоступен"
        source_file = Path(project.source_file) if project.source_file else None
        if not options.include_base:
            note = "план записан отдельным чертежом, без подосновы: так выбрано при экспорте"
        elif source_file is not None and source_file.exists():
            try:
                main, members = discover_bundle_members(materialize_source(source_file, work))
                base = convert_dwg_to_dxf(main, work / "converted") if main.suffix.lower() == ".dwg" else main
                note = f"план записан поверх исходного чертежа {main.name}"
            except Exception as error:  # noqa: BLE001 — экспорт без подосновы лучше, чем никакого
                base = None
                note = f"исходный чертёж не открылся ({type(error).__name__}), план записан отдельным чертежом"

        plan_path = package / "plan.dxf"
        try:
            write_dxf(drawn_items, plan_path, base_dxf=base, records=drawn_records, violation_marks=marks)
        except Exception as error:  # noqa: BLE001
            if base is None:
                raise
            note = f"запись поверх исходного чертежа не удалась ({type(error).__name__}), план записан отдельным чертежом"
            base = None
            write_dxf(drawn_items, plan_path, records=drawn_records, violation_marks=marks)

        references = 0
        missing: list[str] = []
        if base is not None and main is not None:
            attached = xref_paths(base)
            references = len(copy_references(main, members, package)) if attached else 0
            if attached:
                place_by_saved_paths(attached, members, package)
            if references:
                note += f"; внешних ссылок рядом: {references}"
            missing = missing_references(attached, package)
            if missing:
                note += (
                    f"; в исходной поставке нет {len(missing)} файл(ов), на которые ссылается чертёж "
                    f"(AutoCAD покажет их как ненайденные и у исходника): {', '.join(missing[:10])}"
                    + (" …" if len(missing) > 10 else "")
                )

        if options.include_report:
            report = report_payload(
                records,
                norms,
                output_dxf="plan.dxf",
                planting_types=sorted({i.planting_type for i in items}),
                area_budget=plan.area_budget,
                export_note=note,
            )
            (package / "plan.report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
            write_trace_csv(records, package / "plan.trace.csv")

        with zipfile.ZipFile(output_zip, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in sorted(package.rglob("*")):
                if path.is_file():
                    archive.write(path, path.relative_to(package).as_posix())
    return output_zip


def export_summary(plan: Plan) -> dict:
    """Сводка для диалога «Перед экспортом» — считается тем же `explain_items`,
    что пишет отчёт и слой нарушений, так что числа в диалоге совпадают с
    содержимым скачанного файла."""
    # Тот же мост «запись плана → домен → обоснование», что у отчёта по HTTP
    # (compliance_service), чтобы id посадок в сводке были id строк плана.
    from backend.app.services import compliance_service  # noqa: PLC0415 — цикл импортов

    project = plan.project
    norms = plan_norms(plan)
    pairs = compliance_service.explain_rows(project, list(plan.items), norms)

    by_citation: dict[str, list[str]] = {}
    violations = 0
    for item_id, record in pairs:
        if not record.compliant:
            violations += 1
        binding = record.binding_constraint
        if binding is None:
            continue
        for check in record.checks:
            if check.object_type == binding and not check.verified:
                by_citation.setdefault(check.citation, []).append(item_id)

    counts: dict[str, int] = {t: 0 for t in PLANTING_LAYER_KEYS}
    lawn_area = 0.0
    for row in plan.items:
        counts[row.planting_type] = counts.get(row.planting_type, 0) + 1
        if row.planting_type == "lawn":
            lawn_area += float(db_to_shape(row.geometry).area)

    layers = [
        {
            "key": t,
            "name": result_layer_name(t, RESULT_LAYER_PREFIX),
            "count": counts[t],
            "area_m2": round(lawn_area, 1) if t == "lawn" else None,
        }
        for t in PLANTING_LAYER_KEYS
        if counts[t] > 0
    ]
    if violations:
        layers.append({"key": "violations", "name": violation_layer_name(RESULT_LAYER_PREFIX), "count": violations, "area_m2": None})

    source_file = Path(project.source_file) if project.source_file else None
    source_layers = {str((layer.attrs or {}).get("layer")) for layer in project.layers if (layer.attrs or {}).get("layer")}
    return {
        "total_items": len(pairs),
        "violations": violations,
        "unverified_citations": [
            {"citation": citation, "items": len(ids), "item_ids": ids[:MAX_UNVERIFIED_ITEM_IDS]}
            for citation, ids in sorted(by_citation.items(), key=lambda kv: -len(kv[1]))
        ],
        "layers": layers,
        "base_available": bool(source_file is not None and source_file.exists()),
        "base_layer_count": len(source_layers) or None,
    }
