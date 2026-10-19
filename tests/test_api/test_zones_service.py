"""Слой «где можно сажать»: доли допустимой площади и зон по ограничениям на
сетке всей рамки участка — и что картинка на карте совпадает с числами."""

from __future__ import annotations

import asyncio
import io
import zipfile

import ezdxf
import numpy as np
from PIL import Image
from shapely.geometry import Point

from backend.app.db.models import Plan, PlantingItemRow
from backend.app.services import zones_service
from backend.app.services.geo_io import shape_to_db
from backend.app.services.project_service import create_project_from_file
from geo_engine.area_budget import area_zone_masks


def _site_zip(tmp_path):
    """Участок 100×80; газопровод по y=40, здание 20×20 в углу."""
    site = tmp_path / "site"
    site.mkdir()
    doc = ezdxf.new(setup=True)
    for name in ("!Граница работ", "Газопровод", "Здания"):
        doc.layers.add(name=name)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (100, 0), (100, 80), (0, 80)], close=True, dxfattribs={"layer": "!Граница работ"})
    msp.add_lwpolyline([(0, 40), (100, 40)], dxfattribs={"layer": "Газопровод"})
    msp.add_lwpolyline([(60, 50), (80, 50), (80, 70), (60, 70)], close=True, dxfattribs={"layer": "Здания"})
    doc.saveas(str(site / "main.dxf"))
    path = tmp_path / "site.zip"
    with zipfile.ZipFile(path, "w") as archive:
        archive.write(site / "main.dxf", "main.dxf")
    return path


def _plan(tmp_path, project_id_hint="zones"):
    project = asyncio.run(create_project_from_file(project_id_hint, _site_zip(tmp_path)))
    plan = Plan(id=f"plan-{project.id}", project_id=project.id, project=project)
    plan.items = [
        PlantingItemRow(plan_id=plan.id, geometry=shape_to_db(Point(10, 10)), planting_type="tree", species="Липа мелколистная", score=0.5, rationale="тест")
    ]
    return plan


def test_summary_lists_allowed_area_and_every_blocker_with_its_norm(tmp_path):
    plan = _plan(tmp_path)

    summary = zones_service.zones_summary(plan, "tree")

    assert summary is not None
    keys = [g["key"] for g in summary["groups"]]
    assert keys[0] == "allowed" and "gas_pipe" in keys and "building" in keys
    gas = next(g for g in summary["groups"] if g["key"] == "gas_pipe")
    assert gas["required_m"] and gas["label"].startswith("Газопровод"), "подпись несёт норматив: «Газопровод 2 м»"
    assert 0.0 < summary["usable_share"] < 1.0
    assert summary["groups"][0]["share"] == summary["usable_share"]
    shares = [g["share"] for g in summary["groups"][1:]]
    assert shares == sorted(shares, reverse=True), "самые «дорогие» ограничения — первыми"


def test_the_picture_agrees_with_the_numbers(tmp_path):
    """Доля закрашенных ячеек «можно сажать» на картинке — та же, что в сводке."""
    plan = _plan(tmp_path)
    summary = zones_service.zones_summary(plan, "tree")

    png = zones_service.zone_png(plan, "tree", "allowed")
    alpha = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3]
    inside = zones_service._entry(plan, "tree").masks
    drawn = (alpha > 0).sum()
    expected = summary["usable_share"] * (summary["territory_m2"] / inside.cell_m**2)

    assert abs(drawn - expected) / expected < 0.03


def test_a_blocker_picture_is_transparent_outside_its_own_zone(tmp_path):
    plan = _plan(tmp_path)

    png = zones_service.zone_png(plan, "tree", "gas_pipe")
    alpha = np.asarray(Image.open(io.BytesIO(png)).convert("RGBA"))[..., 3]

    height, width = alpha.shape
    row_of_gas = int((1 - 40 / 80) * height)  # y=40 — середина по высоте (север сверху)
    assert alpha[row_of_gas, width // 2] > 0, "на самой трубе зона закрашена"
    assert alpha[2, width // 2] == 0, "у северной границы, далеко от газопровода, — прозрачно"


def test_removing_the_biggest_blocker_is_advised_with_an_area_estimate(tmp_path):
    plan = _plan(tmp_path)

    advice = zones_service.zones_summary(plan, "tree")["advice"]

    assert advice is not None and advice["exclusive_m2"] > 0
    assert advice["extra_items"] >= 0


def test_unknown_group_has_no_picture(tmp_path):
    plan = _plan(tmp_path)

    assert zones_service.zone_png(plan, "tree", "no-such-group") is None


def test_masks_cover_the_territory_bounds_and_share_adds_up():
    from shapely.geometry import box

    from geo_engine.model import Utility
    from geo_engine.norms import load_norms

    norms = load_norms("geo_engine/config/planting_norms.yaml")
    territory = box(0, 0, 100, 80)
    from shapely.geometry import LineString

    masks = area_zone_masks(territory, [Utility(geometry=LineString([(0, 40), (100, 40)]), object_type="gas_pipe")], [], "tree", norms)

    assert masks is not None
    assert (masks.minx, masks.miny, masks.maxx, masks.maxy) == (0, 0, 100, 80)
    blocked = np.zeros_like(masks.allowed)
    for blocker in masks.blockers:
        blocked |= blocker.mask
    assert not (masks.allowed & blocked).any(), "допустимая и закрытая площадь не пересекаются"
