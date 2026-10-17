"""Экспорт из веба — план поверх исходного чертежа со ссылками, а не пустой
чертёж (замечание экспертов «криво формируется итоговый DXF»)."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import ezdxf
from shapely.geometry import Point

from backend.app.db.models import Plan, PlantingItemRow
from backend.app.services.export_service import plan_to_package
from backend.app.services.geo_io import shape_to_db
from backend.app.services.project_service import create_project_from_file


def _site_zip(tmp_path):
    site = tmp_path / "site"
    (site / "Xrefs").mkdir(parents=True)
    boundary = ezdxf.new(setup=True)
    boundary.layers.add(name="!Граница работ")
    boundary.layers.add(name="Газопровод")
    msp = boundary.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True, dxfattribs={"layer": "!Граница работ"})
    msp.add_lwpolyline([(0, 40), (100, 40)], dxfattribs={"layer": "Газопровод"})
    boundary.saveas(str(site / "Xrefs" / "boundary.dxf"))

    main = ezdxf.new(setup=True)
    main.add_xref_def("Xrefs\boundary.dxf", "boundary")
    main.modelspace().add_blockref("boundary", (0, 0))
    main.layers.add(name="Рамка")
    main.modelspace().add_line((0, 0), (1, 1), dxfattribs={"layer": "Рамка"})
    main.saveas(str(site / "main.dxf"))

    zip_path = tmp_path / "site.zip"
    with zipfile.ZipFile(zip_path, "w") as archive:
        for path in site.rglob("*"):
            if path.is_file():
                archive.write(path, path.relative_to(site))
    return zip_path


def test_export_puts_the_plan_over_the_source_drawing_with_its_references(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    assert project.source_file is not None

    plan = Plan(id="plan-x", project_id=project.id, project=project)
    plan.items = [
        PlantingItemRow(plan_id=plan.id, geometry=shape_to_db(Point(20, 20)), planting_type="tree",
                        species="Берёза повислая", score=0.5, rationale="тест")
    ]
    out = plan_to_package(plan, tmp_path / "export.zip")

    with zipfile.ZipFile(out) as archive:
        names = set(archive.namelist())
        assert {"plan.dxf", "Xrefs/boundary.dxf", "plan.report.json", "plan.trace.csv"} <= names
        doc = ezdxf.read(io.StringIO(archive.read("plan.dxf").decode("utf-8", errors="replace")))
        report = json.loads(archive.read("plan.report.json"))

    assert any(block.block_record.is_xref for block in doc.blocks)
    assert "Рамка" in doc.layers
    tree = next(e for e in doc.modelspace() if e.dxf.layer.startswith("GREEN_AI"))
    assert tree.has_xdata("GREEN_AI")
    assert "поверх исходного чертежа main" in report["export_note"]
