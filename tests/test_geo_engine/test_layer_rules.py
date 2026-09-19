"""Распознавание слоя по образцу имени.

Дословная карта покрывала не все чертежи пилота: геоподоснова называет слои
единообразно, а проектные бюро — каждое по-своему. Измеренный эффект правил:
улиц, читаемых как вход (нужны сети и граница участка), стало 19 из 19 вместо
16 — три улицы не читались вообще.

Тесты держат две вещи, которые ломаются молча: служебные слои не должны
становиться объектами, а частные правила не должны перекрываться общими.
"""

import pytest

from geo_engine.io.layer_rules import (
    classify_layer,
    classify_planting_layer,
    is_annotation_layer,
    is_symbol_layer,
    normalize_name,
)


class TestNormalization:
    @pytest.mark.parametrize(
        "raw, expected",
        [
            ("Посадка_Дерево", "посадка дерево"),
            ("ГП_ПБ_Озеленение_Посадка_Деревья", "гп пб озеленение посадка деревья"),
            ("ЗЕЛЕНЬ$ДЕРЕВЬЯ$КЛЕН ОСТРОЛИСТНЫЙ", "зелень деревья клен остролистный"),
            ("!!!_1. Дендра_сохранить", "!!! 1 дендра сохранить"),
        ],
    )
    def test_separators_and_case_do_not_decide(self, raw, expected):
        assert normalize_name(raw) == expected

    def test_yo_is_folded(self):
        """В одних чертежах «Дёрен», в других «Дерен» — один слой."""
        assert normalize_name("Дёрен") == normalize_name("Дерен")


class TestUtilityRecognition:
    @pytest.mark.parametrize(
        "layer, expected",
        [
            ("Газопровод", "gas_pipe"),
            ("!Project_utilites газопровод", "gas_pipe"),
            ("Теплосеть", "heat_network"),
            ("Теплотрасса существующая", "heat_network"),
            ("!Project_utilites водопровод", "water_pipe"),
            ("Канализация самотёчная", "sewer"),
            ("Водосток проектный", "sewer"),
            ("Кабель электрический", "cable_line"),
            ("55_Трасса сущ. ЛСС ПАО МГТС", "cable_line"),
            ("ЛЭП", "power_line_corridor"),
        ],
    )
    def test_networks_are_recognized_across_naming_conventions(self, layer, expected):
        assert classify_layer(layer) == ("utility", expected)


class TestZoneRecognition:
    @pytest.mark.parametrize(
        "layer, expected",
        [
            ("Здания", "building"),
            ("Части зданий", "building"),
            ("_ГП_граница работ", "territory"),
            ("!!!_1. ГРАНИЦА РАБОТ", "territory"),
            ("Бортовой камень", "road"),
            ("Леса и газоны", "existing_greenery"),
            ("Отдельно стоящее дерево", "existing_greenery"),
            ("Подпорная стенка", "retaining_wall"),
        ],
    )
    def test_zones_are_recognized(self, layer, expected):
        assert classify_layer(layer) == ("zone", expected)

    def test_a_school_is_not_swallowed_by_the_generic_building_rule(self):
        """743-ПП табл. 3.6.1 даёт школе и детскому саду 10 м против 5 у
        обычного здания. Попади они в общее правило — норма потерялась бы, и
        это тот отказ, который никто не заметит.
        """
        assert classify_layer("Детский сад")[1] == "school_kindergarten"
        assert classify_layer("Здание школы")[1] == "school_kindergarten"
        assert classify_layer("Здания")[1] == "building"

    def test_traffic_lights_share_the_mast_row(self):
        """«Мачта и опора осветительной сети, трамвая, мостовая опора и
        эстакада» — 4 м. Светофор стоит на такой же опоре.
        """
        assert classify_layer("Светофоры")[1] == "lighting_pole"
        assert classify_layer("Фонари")[1] == "lighting_pole"


class TestSurfaceFillLayers:
    """Заливки.dwg — реальный xref с HATCH-геометрией поверхностей (асфальт/
    газон/плитка), найденный на «1. Олимпийская деревня». Слои называются
    парой кодов через дефис (normalize_name разбивает его на пробел), и
    реальное имя пишет то одну сторону шва первой, то другую.
    """

    @pytest.mark.parametrize(
        "layer, expected",
        [
            ("_ГЗН-ГЗН", "existing_greenery"),
            ("_ГЗН-АБ ПЧ", "existing_greenery"),  # частное правило гзн идёт раньше составного
            ("_АБ ПЧ-АБ ПЧ", "road"),
            ("_АБ ТР-АБ ПЧ (2 и более метров)", "road"),  # ПЧ выигрывает у ТР при обоих в имени
            ("_АБ ПЧ-АБ ТР", "road"),  # тот же шов, порядок токенов обратный
            ("_ЩМА ПЧ-АБ ТР", "road"),
            ("_АБ ТР-АБ ТР", "sidewalk"),
            ("_АБ ТР-ПЛ ТР", "sidewalk"),
            ("_ПЛ ТР-ПЛ ТР", "sidewalk"),
            # Шов с гравийной стороной ("Щ") всё равно распознаётся по
            # СВОЕЙ, опознанной стороне — неопределённость только там, где
            # опознанного кода нет вовсе (см. тест ниже).
            ("_АБ ТР-Щ", "sidewalk"),
            ("_ГЗН-Щ", "existing_greenery"),
        ],
    )
    def test_fill_seam_layers_map_to_the_dominant_surface(self, layer, expected):
        assert classify_layer(layer) == ("zone", expected)

    def test_bare_gravel_code_is_deliberately_left_unmapped(self):
        """«Щ» без уточнения (щебень?) — соответствие неочевидно и площадь
        пренебрежимо мала в замере; придумывать тип значило бы гадать.
        """
        assert classify_layer("_Щ -Щ") is None


class TestWhatIsDeliberatelyNotMapped:
    @pytest.mark.parametrize("layer", ["Ограды", "Береговая линия", "Красные линии", "Горизонтали"])
    def test_objects_no_act_sets_a_distance_to_stay_unmapped(self, layer):
        """Придумать им отступ означало бы выдать догадку за норму. Они
        остаются нераспознанными и попадают в "unknown", а не тихо получают
        чужое значение.
        """
        assert classify_layer(layer) is None


class TestAnnotationLayers:
    @pytest.mark.parametrize(
        "layer",
        [
            "!!!_1. Дендра_выноски_сохранить",
            "!!!_1. Дендра_номера вырубка",
            "Общая ведомость проектируемых растений_1",
            "_ПП_газон_экспликация",
            "!Project_road dimension 500",
            "50 Дендроплан (привязки)",
        ],
    )
    def test_annotation_never_becomes_an_object(self, layer):
        """В названиях выносок и номеров те же слова, что и в содержательных
        слоях: без явного исключения «Дендра_выноски_сохранить» стала бы
        существующей зеленью.
        """
        assert is_annotation_layer(layer)
        assert classify_layer(layer) is None
        assert classify_planting_layer(layer) is None


class TestDesignedPlantings:
    @pytest.mark.parametrize(
        "layer, expected",
        [
            ("52 Проектируемые деревья", "tree"),
            ("52 Проектируемые кустарники", "shrub"),
            ("!Посадка_Дерево", "tree"),
            ("!Посадка_Куст", "shrub"),
            ("ГП_ПБ_Озеленение_Посадка_Деревья", "tree"),
            ("ГП_ПБ_Озеленение_Посадка_Кустарники_Сирень", "shrub"),
            ("оникс_ГП_посадки_Деревья", "tree"),
            ("ПРОЕКТИРУЕМЫЕ ДЕРЕВЬЯ", "tree"),
        ],
    )
    def test_all_six_conventions_found_in_the_pilot_are_recognized(self, layer, expected):
        assert classify_planting_layer(layer) == expected

    def test_designed_plantings_are_not_constraints(self):
        """Проектная посадка — чужой результат, с которым можно сравнивать
        свой, а не препятствие, которое надо обойти. Попади она в
        classify_layer — buildable_area вычитала бы чужой проект.
        """
        for layer in ("52 Проектируемые деревья", "!Посадка_Дерево"):
            assert classify_layer(layer) is None


class TestSymbolLayers:
    @pytest.mark.parametrize("layer", ["Отдельно стоящее дерево", "Колодцы", "Люки", "Фонари"])
    def test_symbol_layers_are_flagged(self, layer):
        """У значка точка вставки и есть объект: разворачивание блока
        превратило бы одно дерево в кружки и чёрточки, из которых он нарисован.
        """
        assert is_symbol_layer(layer)

    def test_a_pipe_is_not_a_symbol(self):
        assert not is_symbol_layer("Газопровод")
