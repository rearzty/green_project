"""The CLI pipeline: drawing in, DXF with a result layer plus a report out.

This is the delivery the brief centres on — «пайплайн DXF -> обработка -> DXF
(результат на отдельном слое)», with a terminal run explicitly accepted as the
demo. So the CLI is covered like a product surface, not like a helper script.
"""

import csv
import json

import ezdxf
import pytest
from shapely.geometry import Point

from geo_engine.io.dxf_writer import RESULT_LAYER_PREFIX
from scripts.plan_dxf import main


def _drawing(tmp_path, name="site.dxf"):
    """A minimal but realistic input: a work boundary, a gas main, a building."""
    doc = ezdxf.new(setup=True)
    for layer in ("!Граница работ", "Газопровод", "Здания"):
        doc.layers.add(name=layer)
    msp = doc.modelspace()
    msp.add_lwpolyline([(0, 0), (120, 0), (120, 90), (0, 90)], close=True, dxfattribs={"layer": "!Граница работ"})
    msp.add_lwpolyline([(0, 45), (120, 45)], dxfattribs={"layer": "Газопровод"})
    msp.add_lwpolyline(
        [(10, 70), (40, 70), (40, 85), (10, 85)], close=True, dxfattribs={"layer": "Здания"}
    )
    path = tmp_path / name
    doc.saveas(str(path))
    return path


def test_cli_produces_a_dxf_and_a_report(tmp_path):
    source = _drawing(tmp_path)
    out = tmp_path / "out" / "plan.dxf"

    exit_code = main(["--input", str(source), "--output", str(out), "--types", "tree"])

    assert exit_code == 0, "нарушений быть не должно — план строится по тем же отступам"
    assert out.exists()
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["total_items"] > 0
    assert report["violations"] == 0
    assert report["compliant_items"] == report["total_items"]


def test_every_item_in_the_report_carries_a_citation(tmp_path):
    source = _drawing(tmp_path)
    out = tmp_path / "plan.dxf"

    main(["--input", str(source), "--output", str(out), "--types", "tree"])
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))

    for item in report["items"]:
        assert item["summary"]
        assert item["checks"], "посадка без единой проверки — это и есть чёрный ящик"
        for check in item["checks"]:
            source_entry = report["sources"][check["source"]]
            assert source_entry["act"]
            assert source_entry["clause"]


def test_cli_result_lands_on_its_own_layers_over_the_source(tmp_path):
    source = _drawing(tmp_path)
    out = tmp_path / "plan.dxf"
    before = {layer.dxf.name for layer in ezdxf.readfile(str(source)).layers}
    source_entities = [(e.dxftype(), e.dxf.layer) for e in ezdxf.readfile(str(source)).modelspace()]

    main(["--input", str(source), "--output", str(out), "--types", "tree"])

    doc = ezdxf.readfile(str(out))
    after_entities = [(e.dxftype(), e.dxf.layer) for e in doc.modelspace()]
    for entity in source_entities:
        assert entity in after_entities
    assert before <= {layer.dxf.name for layer in doc.layers}
    assert any(e.dxf.layer.startswith(f"{RESULT_LAYER_PREFIX}$") for e in doc.modelspace())


def test_csv_trace_is_written_on_request(tmp_path):
    source = _drawing(tmp_path)
    out = tmp_path / "plan.dxf"

    main(["--input", str(source), "--output", str(out), "--types", "tree", "--csv"])

    trace = out.with_suffix(".trace.csv")
    assert trace.exists()
    with open(trace, encoding="utf-8-sig", newline="") as handle:
        rows = list(csv.DictReader(handle, delimiter=";"))
    assert rows
    assert {"act", "clause", "required_m", "actual_m"} <= set(rows[0])


def test_a_drawing_without_a_boundary_fails_with_a_readable_message(tmp_path):
    """Not a traceback: this is the single most likely thing to go wrong on a
    real drawing, because the outline often lives in an xref.
    """
    doc = ezdxf.new(setup=True)
    doc.layers.add(name="Газопровод")
    doc.modelspace().add_lwpolyline([(0, 0), (100, 0)], dxfattribs={"layer": "Газопровод"})
    source = tmp_path / "no_boundary.dxf"
    doc.saveas(str(source))

    with pytest.raises(SystemExit) as excinfo:
        main(["--input", str(source), "--output", str(tmp_path / "out.dxf")])

    assert "границы участка" in str(excinfo.value)


def test_unknown_planting_type_is_rejected_before_any_work(tmp_path):
    source = _drawing(tmp_path)

    with pytest.raises(SystemExit) as excinfo:
        main(["--input", str(source), "--output", str(tmp_path / "out.dxf"), "--types", "tree,кактус"])

    assert "кактус" in str(excinfo.value)


def test_the_same_plan_key_reproduces_the_same_plan(tmp_path):
    """Placement is randomized but seeded from the plan key, so a rerun with the
    same arguments has to give the same drawing — the brief grades
    reproducibility of the pipeline directly.
    """
    source = _drawing(tmp_path)

    positions = []
    for name in ("a", "b"):
        out = tmp_path / f"{name}.dxf"
        main(["--input", str(source), "--output", str(out), "--types", "tree", "--plan-key", "fixed"])
        report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
        positions.append([(i["x"], i["y"]) for i in report["items"]])

    assert positions[0] == positions[1]


def _layouts_for_keys(source, tmp_path, planting_type):
    layouts = []
    for key in ("one", "two"):
        out = tmp_path / f"{key}-{planting_type}.dxf"
        main(["--input", str(source), "--output", str(out), "--types", planting_type, "--plan-key", key])
        report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
        layouts.append([(i["x"], i["y"]) for i in report["items"]])
    return layouts


def test_tree_layout_is_decided_by_the_site_not_by_the_plan_key(tmp_path):
    """Раскладка деревьев больше НЕ должна зависеть от ключа плана.

    Раньше здесь проверялось обратное — и это было верно, пока деревья
    сыпались россыпью: у случайной раскладки сид обязан был что-то менять.
    Россыпь деревьев в схеме auto убрана, а ряд и солитеры выводятся из
    геометрии площадки: у улицы есть одна ближайшая допустимая эквидистанта и
    один набор самых просторных карманов. Менять это жребием означало бы
    вернуть ровно ту случайность, из-за которой план читался как «понатыкали
    деревьев».

    Воспроизводимость при этом никуда не делась, её держит соседний тест
    (тот же ключ — та же раскладка); здесь закреплено более сильное свойство.
    """
    source = _drawing(tmp_path)

    layouts = _layouts_for_keys(source, tmp_path, "tree")

    assert layouts[0], "на этой площадке деревья должны появиться"
    assert layouts[0] == layouts[1]


def test_a_different_plan_key_still_moves_the_shrub_clumps(tmp_path):
    """А вот у кустарника сид по-прежнему решает.

    Куртины ставятся по сетке со случайным смещением (`group_positions`), и
    это осознанно: нормативного правила «где именно стоит группа» нет, в
    отличие от ряда вдоль улицы и солитера в самом просторном месте.
    """
    source = _drawing(tmp_path)

    layouts = _layouts_for_keys(source, tmp_path, "shrub")

    assert layouts[0], "на этой площадке кустарник должен появиться"
    assert layouts[0] != layouts[1]


def test_bundle_directory_input_picks_up_the_xrefs(tmp_path):
    """Real input is a folder: the drawing plus Xrefs/ holding the boundary."""
    site = tmp_path / "site"
    (site / "Xrefs").mkdir(parents=True)

    main_doc = ezdxf.new(setup=True)
    main_doc.layers.add(name="Газопровод")
    main_doc.modelspace().add_lwpolyline([(0, 45), (120, 45)], dxfattribs={"layer": "Газопровод"})
    main_doc.saveas(str(site / "plan.dxf"))

    xref = ezdxf.new(setup=True)
    xref.layers.add(name="!Граница работ")
    xref.modelspace().add_lwpolyline(
        [(0, 0), (120, 0), (120, 90), (0, 90)], close=True, dxfattribs={"layer": "!Граница работ"}
    )
    xref.saveas(str(site / "Xrefs" / "boundary.dxf"))

    out = tmp_path / "plan.dxf"
    exit_code = main(["--input", str(site), "--output", str(out), "--types", "tree"])

    assert exit_code == 0
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["total_items"] > 0


def test_bundle_directory_input_also_recognises_ссылки_as_the_xref_folder(tmp_path):
    """Same shape as the Xrefs/ case above, but named the way the pilot
    dataset actually names it on some streets ("1. Олимпийская деревня":
    <id>_Генплан... - Standard/ссылки/) instead of others ("2. Песчаный
    переулок": Xrefs/) -- both are real folder names in the delivery, neither
    is a stand-in for testing."""
    site = tmp_path / "site"
    (site / "ссылки").mkdir(parents=True)

    main_doc = ezdxf.new(setup=True)
    main_doc.layers.add(name="Газопровод")
    main_doc.modelspace().add_lwpolyline([(0, 45), (120, 45)], dxfattribs={"layer": "Газопровод"})
    main_doc.saveas(str(site / "plan.dxf"))

    xref = ezdxf.new(setup=True)
    xref.layers.add(name="!Граница работ")
    xref.modelspace().add_lwpolyline(
        [(0, 0), (120, 0), (120, 90), (0, 90)], close=True, dxfattribs={"layer": "!Граница работ"}
    )
    xref.saveas(str(site / "ссылки" / "boundary.dxf"))

    out = tmp_path / "plan.dxf"
    exit_code = main(["--input", str(site), "--output", str(out), "--types", "tree"])

    assert exit_code == 0
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
    assert report["total_items"] > 0


def test_bundle_members_scattered_across_several_subfolders_are_all_picked_up(tmp_path):
    """Live case: "1. Олимпийская деревня"'s project folder keeps utility
    sheets under per-survey-order subfolders (3ДЖКХ-24_02565/,
    3ДЖКХ-25_03117/, ...) next to ссылки/, not merged into either -- a fixed
    Xrefs/ссылки-only check missed all of them and left the whole drawing
    "unknown" (no boundary, no utilities). Bundle discovery has to walk the
    whole project folder, not a fixed set of named subfolders."""
    site = tmp_path / "site"
    (site / "ссылки").mkdir(parents=True)
    (site / "3ДЖКХ-25_00103").mkdir(parents=True)
    (site / "PaxHeader").mkdir(parents=True)  # tar-extraction litter, must be skipped

    main_doc = ezdxf.new(setup=True)
    main_doc.layers.add(name="ДВ_ГП_П_МАФ")
    main_doc.modelspace().add_lwpolyline([(0, 10), (10, 10)], dxfattribs={"layer": "ДВ_ГП_П_МАФ"})
    main_doc.saveas(str(site / "plan.dxf"))

    boundary = ezdxf.new(setup=True)
    boundary.layers.add(name="!Граница работ")
    boundary.modelspace().add_lwpolyline(
        [(0, 0), (120, 0), (120, 90), (0, 90)], close=True, dxfattribs={"layer": "!Граница работ"}
    )
    boundary.saveas(str(site / "ссылки" / "boundary.dxf"))

    utility = ezdxf.new(setup=True)
    utility.layers.add(name="Газопровод")
    utility.modelspace().add_lwpolyline([(0, 45), (120, 45)], dxfattribs={"layer": "Газопровод"})
    utility.saveas(str(site / "3ДЖКХ-25_00103" / "output_1_up.dxf"))

    litter = ezdxf.new(setup=True)
    litter.saveas(str(site / "PaxHeader" / "stray.dxf"))

    out = tmp_path / "plan.dxf"
    exit_code = main(["--input", str(site), "--output", str(out), "--types", "tree"])

    assert exit_code == 0
    report = json.loads(out.with_suffix(".report.json").read_text(encoding="utf-8"))
    # A tree needs a territory (from ссылки/) and gets a real gas_pipe
    # citation (from 3ДЖКХ-25_00103/) only if both were actually read.
    assert report["total_items"] > 0
    assert any(check["object_type"] == "gas_pipe" for item in report["items"] for check in item["checks"])


def test_an_umbrella_folder_with_no_drawing_at_its_own_level_names_its_subfolders(tmp_path):
    """Live case: a street's top "Исходные данные" folder holds only
    subfolders (permits, a dendrology survey, and the actual drawing set,
    each one level down) -- zero .dxf/.dwg at that level is a real, common
    shape, not a malformed upload. The error should point at the subfolders
    instead of just saying nothing was found."""
    umbrella = tmp_path / "Исходные данные"
    (umbrella / "01 ирд").mkdir(parents=True)
    (umbrella / "10000176_Генплан_Олимп - Standard").mkdir(parents=True)

    with pytest.raises(SystemExit) as excinfo:
        main(["--input", str(umbrella), "--output", str(tmp_path / "out.dxf")])

    message = str(excinfo.value)
    assert "01 ирд" in message
    assert "10000176_Генплан_Олимп - Standard" in message


class TestBaseDrawingChoice:
    """Какой файл бандла становится холстом для слоя результата."""

    def _drawing(self, path, entity_count, layer="Газопровод"):
        doc = ezdxf.new(setup=True)
        doc.layers.add(name=layer)
        msp = doc.modelspace()
        for i in range(entity_count):
            msp.add_lwpolyline([(i, 0), (i + 1, 0)], dxfattribs={"layer": layer})
        doc.saveas(str(path))
        return path

    def test_the_most_substantial_readable_drawing_wins(self, tmp_path):
        """Не первый открывшийся и не самый большой по байтам: у бандла бывают
        файлы-заглушки в пару объектов, и копия такой заглушки со слоем
        результата формально проходит, но эксперт открывает её и не видит своей
        подосновы — ровно то, ради чего результат и пишется поверх исходника.
        """
        from scripts.plan_dxf import pick_base_drawing

        stub = self._drawing(tmp_path / "stub.dxf", 2)
        real = self._drawing(tmp_path / "real.dxf", 50)

        assert pick_base_drawing([stub, real]) == real

    def test_an_unreadable_drawing_is_skipped(self, tmp_path):
        """Живой случай: главный чертёж улицы (76 МБ) не открывается ни
        readfile, ни recover ни в одном режиме — внутри испорченная
        юникод-последовательность. Терять из-за неё весь прогон незачем:
        исходник нужен только как холст.
        """
        from scripts.plan_dxf import pick_base_drawing

        broken = tmp_path / "broken.dxf"
        broken.write_text("это не DXF", encoding="utf-8")
        real = self._drawing(tmp_path / "real.dxf", 20)

        assert pick_base_drawing([broken, real]) == real

    def test_nothing_readable_yields_none(self, tmp_path):
        from scripts.plan_dxf import pick_base_drawing

        broken = tmp_path / "broken.dxf"
        broken.write_text("мусор", encoding="utf-8")

        assert pick_base_drawing([broken]) is None
