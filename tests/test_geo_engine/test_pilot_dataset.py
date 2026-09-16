"""End-to-end import of the real pilot dataset — opt-in.

The dataset (`Датасет/Пилотный проект 20 улиц`, ~5 GB of DWG/PDF/photos) is not
in the repo and cannot be: it is the organisers' material. So these tests skip
unless GREENPROJECT_PILOT_DATA points at the `Пилотный проект 20 улиц`
directory, and they skip again if no DWG converter is on PATH.

They exist because every synthetic fixture in this suite is built by the same
code that reads it, and so cannot catch the two things that actually broke on
real drawings: geometry hidden in nested blocks, and Cyrillic layer names the
default map has never heard of.

Run them with:
    GREENPROJECT_PILOT_DATA="/path/to/Пилотный проект 20 улиц" pytest tests/test_geo_engine/test_pilot_dataset.py
"""

import os
from pathlib import Path

import pytest

from backend.app.services.pipeline_service import territory_polygon
from geo_engine.io.dwg_convert import available_backend, convert_dwg_to_dxf
from geo_engine.io.dxf_reader import (
    COMBINED_LAYER_MAP,
    MOSGEOTREST_LAYER_MAP,
    dxf_bundle_paths,
    read_dxf,
    read_dxf_bundle,
)
from geo_engine.io.geometry_cleanup import merge_dashed_lines

PILOT_ROOT = os.environ.get("GREENPROJECT_PILOT_DATA")

# One street, one geobase sheet. Песчаный переулок is the reference case the
# reader and the layer map were built against; the numbers asserted below were
# measured on it.
STREET = "2. Песчаный переулок"
UTILITY_SHEET = Path("Генеральный план редформат/Xrefs/output_1-3__3_ДЖКХ-25_02794up.dwg")
DRAWING_DIR = Path("Генеральный план редформат")

# The work area measured off the real outline: two polygons, 29739 + 16132 m².
EXPECTED_TERRITORY_AREA_M2 = 45870

pytestmark = [
    pytest.mark.skipif(not PILOT_ROOT, reason="GREENPROJECT_PILOT_DATA is not set"),
    pytest.mark.skipif(available_backend() is None, reason="no DWG converter on PATH"),
]


@pytest.fixture(scope="module")
def utility_sheet_dxf(tmp_path_factory):
    source = Path(PILOT_ROOT) / STREET / UTILITY_SHEET
    if not source.exists():
        pytest.skip(f"sheet not found under GREENPROJECT_PILOT_DATA: {source}")
    return convert_dwg_to_dxf(source, tmp_path_factory.mktemp("dwg"))


@pytest.fixture(scope="module")
def imported(utility_sheet_dxf):
    return read_dxf(utility_sheet_dxf, layer_map=MOSGEOTREST_LAYER_MAP)


def test_the_sheet_imports_thousands_of_utilities_not_dozens(imported):
    """The headline regression. Iterating modelspace flat finds 50 polylines on
    this sheet; following blocks finds thousands. A threshold well above the
    flat count is what distinguishes "worked" from "silently imported nothing".
    """
    utilities, _ = imported

    assert len(utilities) > 1000


def test_every_utility_type_the_norms_price_is_present(imported):
    """This sheet carries gas, heat, water, sewer and cables. If any comes back
    empty the layer map has drifted from the drawings.
    """
    utilities, _ = imported
    found = {u.object_type for u in utilities}

    assert {"gas_pipe", "heat_network", "water_pipe", "sewer", "cable_line"} <= found


def test_nothing_important_falls_through_to_unknown(imported):
    """Utilities are matched by exact layer name, so a renamed or re-spelled
    layer shows up as a spike in "unknown" rather than as an error. Assert the
    known-unmapped remainder ("Трубопроводы", "Подземные коммуникации",
    annotation layers) stays a small minority.
    """
    utilities, zones = imported
    unknown = [z for z in zones if z.zone_type == "unknown"]
    total = len(utilities) + len(zones)

    # Measured on this sheet: 24 of 6886, all on "Подземные коммуникации".
    assert len(unknown) / total < 0.05


def test_dash_stitching_reconstructs_continuous_pipe_runs(imported):
    """The point of stitching is not a smaller list, it is getting *runs* back.

    Measured on this sheet: 1265 gas segments collapse to ~868 pieces, of which
    124 are continuous runs over 10 m carrying 2682 m of the 3645 m total, the
    longest reaching 146 m. A modest drop in count with a long run appearing is
    the correct signature; a big drop in count would mean tick marks were being
    swallowed too (see the next test).
    """
    utilities, _ = imported
    gas = [u.geometry for u in utilities if u.object_type == "gas_pipe"]

    merged = merge_dashed_lines(gas)

    assert len(merged) < len(gas) * 0.8
    assert max(g.length for g in merged) > 50.0


def test_stitching_leaves_the_symbology_tick_marks_alone(imported):
    """A pipe on a Russian topographic sheet is drawn as a run plus periodic
    cross-ticks, and the ticks live on the same layer as the pipe. Measured
    here: of the pieces left under 2 m, 69% meet their nearest long run at over
    60 degrees and touch it exactly. They must stay separate — folding a tick
    into the run would bend the pipe sideways by a metre.
    """
    utilities, _ = imported
    gas = [u.geometry for u in utilities if u.object_type == "gas_pipe"]

    merged = merge_dashed_lines(gas)
    short_pieces = [g for g in merged if g.length <= 2.0]

    assert len(short_pieces) > 100


def test_merging_preserves_total_length_within_the_bridged_gaps(imported):
    """Stitching adds the gaps it bridges and must not otherwise invent or drop
    length. Measured: 3645 m of dashes become 3773 m of runs, +3%.
    """
    utilities, _ = imported
    gas = [u.geometry for u in utilities if u.object_type == "gas_pipe"]
    dashed_length = sum(g.length for g in gas)

    merged_length = sum(g.length for g in merge_dashed_lines(gas))

    assert dashed_length <= merged_length <= dashed_length * 1.3


@pytest.fixture(scope="module")
def converted_bundle(tmp_path_factory):
    """The main drawing plus every xref beside it, all converted to DXF.

    This is the shape a real project arrives in and the reason single-file
    import is not enough: the utilities, the topography and — the part that
    blocks everything else — the site outline each live in a different file.
    """
    drawing_dir = Path(PILOT_ROOT) / STREET / DRAWING_DIR
    # Picked by size rather than by name, the way scripts.plan_dxf does. Not
    # cosmetic: macOS stores these filenames decomposed (NFD), so "Генеральный"
    # on disk has "и" plus a combining breve where a Python literal has the
    # precomposed "й", and an exact-name lookup silently finds nothing.
    drawings = sorted(p for p in drawing_dir.glob("*.dwg")) if drawing_dir.is_dir() else []
    if not drawings:
        pytest.skip(f"main drawing not found under GREENPROJECT_PILOT_DATA: {drawing_dir}")
    main = max(drawings, key=lambda p: p.stat().st_size)

    out = tmp_path_factory.mktemp("bundle")
    (out / "Xrefs").mkdir()
    convert_dwg_to_dxf(main, out)
    converted_main = next(out.glob("*.dxf"))
    for dwg in sorted((drawing_dir / "Xrefs").glob("*.dwg")):
        try:
            convert_dwg_to_dxf(dwg, out / "Xrefs")
        except RuntimeError:
            # One unconvertible xref must not take the bundle down -- the same
            # tolerance read_dxf_bundle needs in production.
            continue
    return converted_main


def test_the_site_outline_is_in_the_xrefs_not_the_main_drawing(converted_bundle):
    """Regression for a wrong conclusion, kept as a test so it cannot be drawn
    again: the work-area outline was first reported as missing from this
    dataset. It is not missing — it is in the xref files shipped alongside, and
    reading only the main drawing is what made it look absent.
    """
    main_only_zones = read_dxf(converted_bundle, layer_map=COMBINED_LAYER_MAP)[1]
    main_only_areas = [
        z for z in main_only_zones if z.zone_type == "territory" and z.geometry.geom_type in ("Polygon", "MultiPolygon")
    ]

    bundle_zones = read_dxf_bundle(dxf_bundle_paths(converted_bundle), layer_map=COMBINED_LAYER_MAP)[1]
    bundle_areas = [
        z for z in bundle_zones if z.zone_type == "territory" and z.geometry.geom_type in ("Polygon", "MultiPolygon")
    ]

    assert main_only_areas == []
    assert len(bundle_areas) >= 2


def test_the_bundle_yields_a_territory_the_pipeline_accepts(converted_bundle):
    """territory_polygon() is the gate every plan goes through. Area is asserted
    against the measured work area rather than "greater than zero", because the
    obvious wrong answer is right there in the same bundle: "Граница заказа",
    the survey order extent, is 119011 m² and covers a neighbouring block.
    """
    _, zones = read_dxf_bundle(
        dxf_bundle_paths(converted_bundle), layer_map=COMBINED_LAYER_MAP, stitch_dashes=True, drop_origin=True
    )

    territory = territory_polygon(zones)

    assert territory.geom_type in ("Polygon", "MultiPolygon")
    assert abs(territory.area - EXPECTED_TERRITORY_AREA_M2) < EXPECTED_TERRITORY_AREA_M2 * 0.02


def test_the_bundle_carries_the_utilities_too(converted_bundle):
    """The outline alone is not enough — a plan needs both, from one read."""
    utilities, _ = read_dxf_bundle(
        dxf_bundle_paths(converted_bundle), layer_map=COMBINED_LAYER_MAP, stitch_dashes=True, drop_origin=True
    )
    found = {u.object_type for u in utilities}

    assert {"gas_pipe", "heat_network", "water_pipe", "sewer", "cable_line"} <= found
    assert len(utilities) > 5000


def test_the_cli_runs_end_to_end_on_the_real_drawing(tmp_path):
    """The delivery the brief grades: drawing in, DXF with a result layer plus a
    report out, from a terminal.

    Also the regression for a failure that only real data produced: the general
    plan converted from DWG loads fine and then dies on save, because its
    MATERIAL table comes back with a string where an entity belongs. It killed
    the run at the last step, after the whole plan was computed, and no
    synthetic fixture in this suite reproduces it.
    """
    import json

    from scripts.plan_dxf import main

    drawing_dir = Path(PILOT_ROOT) / STREET / DRAWING_DIR
    if not drawing_dir.is_dir():
        pytest.skip(f"drawing directory not found: {drawing_dir}")

    out = tmp_path / "plan.dxf"
    exit_code = main(["--input", str(drawing_dir), "--output", str(out), "--types", "tree", "--csv"])

    assert exit_code == 0
    assert out.exists()
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["total_items"] > 0
    assert report["violations"] == 0
    assert out.with_suffix(".trace.csv").exists()

    # Source layers survive: the criterion an expert checks by hand in nanoCAD.
    import ezdxf
    import ezdxf.recover

    result_doc, _ = ezdxf.recover.readfile(str(out))
    layers = {layer.dxf.name for layer in result_doc.layers}
    assert any(name.startswith("GREEN_AI$") for name in layers)
    assert "Газопровод" in " ".join(layers) or any("Газопровод" in name for name in layers)
