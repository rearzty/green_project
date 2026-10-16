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
from pathlib import Path

from backend.app.core.config import settings
from backend.app.db.models import Plan
from backend.app.services.geo_io import db_to_shape, layers_to_domain
from backend.app.services.project_service import materialize_source
from geo_engine.compliance import explain_items, report_payload, write_trace_csv
from geo_engine.io.dwg_convert import convert_dwg_to_dxf
from geo_engine.io.dxf_reader import discover_bundle_members
from geo_engine.io.dxf_writer import write_dxf
from geo_engine.io.result_package import copy_references, xref_attachment_count
from geo_engine.model import PlantingItem
from geo_engine.norms import load_norms


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


def plan_to_package(plan: Plan, output_zip: str | Path) -> Path:
    output_zip = Path(output_zip)
    project = plan.project
    items = _domain_items(plan)
    utilities, zones = layers_to_domain(project.layers)
    norms = load_norms(settings.planting_norms_path)
    records = explain_items(items, utilities, zones, norms)

    with tempfile.TemporaryDirectory(prefix="greenproject-export-") as tmp:
        work = Path(tmp)
        package = work / "package"
        package.mkdir()
        base = main = None
        members: list[Path] = []
        note = "план записан отдельным чертежом: исходник проекта недоступен"
        source_file = Path(project.source_file) if project.source_file else None
        if source_file is not None and source_file.exists():
            try:
                main, members = discover_bundle_members(materialize_source(source_file, work))
                base = convert_dwg_to_dxf(main, work / "converted") if main.suffix.lower() == ".dwg" else main
                note = f"план записан поверх исходного чертежа {main.name}"
            except Exception as error:  # noqa: BLE001 — экспорт без подосновы лучше, чем никакого
                base = None
                note = f"исходный чертёж не открылся ({type(error).__name__}), план записан отдельным чертежом"

        plan_path = package / "plan.dxf"
        try:
            write_dxf(items, plan_path, base_dxf=base, records=records)
        except Exception as error:  # noqa: BLE001
            if base is None:
                raise
            note = f"запись поверх исходного чертежа не удалась ({type(error).__name__}), план записан отдельным чертежом"
            base = None
            write_dxf(items, plan_path, records=records)

        references = 0
        if base is not None and main is not None:
            references = len(copy_references(main, members, package)) if xref_attachment_count(base) else 0
            if references:
                note += f"; внешних ссылок рядом: {references}"

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
