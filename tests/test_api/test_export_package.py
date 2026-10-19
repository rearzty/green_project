"""Экспорт из веба — план поверх исходного чертежа со ссылками, а не пустой
чертёж (замечание экспертов «криво формируется итоговый DXF»)."""

from __future__ import annotations

import asyncio
import io
import json
import zipfile

import ezdxf
import pytest
from shapely.geometry import Point

from backend.app.db.models import Plan, PlantingItemRow
from backend.app.services.export_service import EmptyExportError, ExportOptions, export_summary, plan_to_package
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


def _plan_with(project, *rows):
    plan = Plan(id="plan-x", project_id=project.id, project=project)
    plan.items = [
        PlantingItemRow(plan_id=plan.id, geometry=shape_to_db(geometry), planting_type=planting_type, species=species, score=0.5, rationale="тест")
        for geometry, planting_type, species in rows
    ]
    return plan


def _plan_layers(zip_path):
    with zipfile.ZipFile(zip_path) as archive:
        doc = ezdxf.read(io.StringIO(archive.read("plan.dxf").decode("utf-8", errors="replace")))
        names = set(archive.namelist())
    used = {e.dxf.layer for e in doc.modelspace() if e.dxf.layer.startswith("GREEN_AI")}
    return used, names


# Газопровод в этой площадке — прямая на y = 40: дерево в 0,2 м от неё стоит
# внутри охранной зоны, дерево в 20 м — нет.
VIOLATING_TREE = (Point(50, 40.2), "tree", "Берёза повислая")
FINE_TREE = (Point(20, 20), "tree", "Берёза повислая")
FINE_SHRUB = (Point(30, 10), "shrub", "Сирень обыкновенная")


def test_violating_plantings_get_their_own_ring_layer(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    plan = _plan_with(project, VIOLATING_TREE, FINE_TREE)

    used, _ = _plan_layers(plan_to_package(plan, tmp_path / "export.zip"))

    assert "GREEN_AI$НАРУШЕНИЯ" in used
    assert "GREEN_AI$ДЕРЕВЬЯ" in used


def test_no_violations_means_no_violation_layer(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    plan = _plan_with(project, FINE_TREE)

    used, _ = _plan_layers(plan_to_package(plan, tmp_path / "export.zip"))

    assert "GREEN_AI$НАРУШЕНИЯ" not in used


def test_unchecked_layers_are_really_left_out_of_the_file(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    plan = _plan_with(project, VIOLATING_TREE, FINE_TREE, FINE_SHRUB)

    only_shrubs = ExportOptions(layers=frozenset({"shrub"}))
    used, _ = _plan_layers(plan_to_package(plan, tmp_path / "shrubs.zip", only_shrubs))

    assert used == {"GREEN_AI$КУСТАРНИКИ"}
    with zipfile.ZipFile(tmp_path / "shrubs.zip") as archive:
        doc = ezdxf.read(io.StringIO(archive.read("plan.dxf").decode("utf-8", errors="replace")))
    in_table = {layer.dxf.name for layer in doc.layers if layer.dxf.name.startswith("GREEN_AI")}
    assert in_table == {"GREEN_AI$КУСТАРНИКИ"}, "снятые слои не остаются пустыми строками в таблице слоёв"


def test_report_and_base_can_be_left_out(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    plan = _plan_with(project, FINE_TREE)

    out = plan_to_package(plan, tmp_path / "bare.zip", ExportOptions(include_base=False, include_report=False))

    with zipfile.ZipFile(out) as archive:
        assert archive.namelist() == ["plan.dxf"]
        doc = ezdxf.read(io.StringIO(archive.read("plan.dxf").decode("utf-8", errors="replace")))
    assert "Рамка" not in doc.layers, "без подосновы исходные слои в файл не попадают"


def test_an_empty_selection_is_refused_not_exported_empty(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    plan = _plan_with(project, FINE_TREE)

    with pytest.raises(EmptyExportError):
        plan_to_package(plan, tmp_path / "none.zip", ExportOptions(layers=frozenset()))


def test_export_summary_matches_what_the_file_contains(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    plan = _plan_with(project, VIOLATING_TREE, FINE_TREE, FINE_SHRUB)

    summary = export_summary(plan)
    by_key = {layer["key"]: layer for layer in summary["layers"]}

    assert summary["total_items"] == 3
    assert summary["violations"] == by_key["violations"]["count"] == 1
    assert by_key["tree"]["count"] == 2 and by_key["shrub"]["count"] == 1
    assert "lawn" not in by_key, "слоя без объектов в файле не будет — и в списке его нет"
    assert by_key["tree"]["name"] == "GREEN_AI$ДЕРЕВЬЯ"
    assert summary["base_available"] is True

    used, _ = _plan_layers(plan_to_package(plan, tmp_path / "export.zip"))
    assert {layer["name"] for layer in summary["layers"]} == used


def test_export_summary_names_citations_nobody_checked_against_the_act(tmp_path):
    project = asyncio.run(create_project_from_file("site", _site_zip(tmp_path)))
    plan = _plan_with(project, VIOLATING_TREE, FINE_TREE)

    summary = export_summary(plan)

    for citation in summary["unverified_citations"]:
        assert citation["citation"] and citation["items"] == len(citation["item_ids"])
