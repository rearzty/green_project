"""Правила, зависящие от породы: отступы, интервал, перечень инвазивных.

До сверки с московскими актами «дерево» было безликим объектом с одним
отступом на тип. Оказалось, что это неверно сразу по трём пунктам:

  * МГСН 1.02-02, п. 4.2.8 задаёт отступ от теплотрассы поимённым списком;
  * 743-ПП, табл. 3.6.1, прим. 3 отодвигает широкую крону от жилого дома
    на 10 м вместо табличных 5;
  * 743-ПП прим. 1 / СП 42.13330.2016 прим. 1 требуют увеличивать табличные
    значения для кроны крупнее 5 м.

Плюс 369-ПП (2026) прямо запрещает предлагать к посадке инвазивные виды, а
два из них были в нашем ассортименте.
"""

import pytest

from geo_engine.norms import load_norms
from geo_engine.planner import choose_species, norms_for_species
from geo_engine.species import load_catalogue


@pytest.fixture
def catalogue():
    return load_catalogue()


@pytest.fixture
def norms():
    return load_norms()


class TestInvasiveSpecies:
    def test_nothing_in_the_assortment_is_on_the_369_pp_list(self, catalogue):
        """Регрессия на реальный дефект: сервис предлагал «Дёрен белый» и
        «Пузыреплодник», оба внесены в перечень 369-ПП (группа III).
        """
        assert catalogue.invasive_in_assortment() == []

    def test_the_list_is_matched_by_full_name_not_by_genus(self, catalogue):
        """Сопоставление по первому слову запретило бы половину нормального
        ассортимента: инвазивен «Клен ясенелистный», а не клён вообще, и
        «Дуб красный», а не дуб вообще.
        """
        assert catalogue.is_invasive("Клен ясенелистный") is not None
        assert catalogue.is_invasive("Клён остролистный") is None
        assert catalogue.is_invasive("Дуб красный") is not None
        assert catalogue.is_invasive("Дуб черешчатый") is None

    def test_spelling_of_yo_does_not_decide(self, catalogue):
        """В 369-ПП «Дерен белый», у нас было «Дёрен белый» — один вид."""
        assert catalogue.is_invasive("Дёрен белый") is not None
        assert catalogue.is_invasive("Дерен белый") is not None

    def test_latin_names_are_recognized(self, catalogue):
        """В чертежах и ведомостях вид иногда подписан латынью."""
        assert catalogue.is_invasive("Cornus albus") is not None
        assert catalogue.is_invasive("Acer negundo") is not None

    def test_rosa_rugosa_is_excluded_despite_being_in_the_515_pp_assortment(self, catalogue):
        """Прямой конфликт двух московских актов: 515-ПП (2017) рекомендует
        «Розу ругозу» для палисадников, 369-ПП (2026) вносит её же как «Роза
        морщинистая, Rosa rugosa». Побеждает более поздний.
        """
        assert catalogue.is_invasive("Роза морщинистая (шиповник морщинистый)") is not None
        assert all("роза ругоза" not in s.name.lower() for s in catalogue.shrub)


class TestSpeciesAwareSetbacks:
    def test_a_named_species_gets_its_own_heat_network_distance(self, norms, catalogue):
        """МГСН 4.2.8: «тополь, боярышник, кизильник, дерен, лиственницу,
        березу — ближе 3-4 м». Табличные 2,0 м для берёзы недостаточны.
        """
        birch = catalogue.get("Берёза повислая")

        resolution = norms.resolve_setback("heat_network", "tree", birch, catalogue.crown_reference_diameter_m)

        assert resolution.table_m == 2.0
        assert resolution.required_m == 4.0
        assert "МГСН" in norms.sources[resolution.source_id].act
        assert "Берёза" in resolution.species_rule

    def test_a_wide_crown_is_pushed_ten_metres_off_a_building(self, norms, catalogue):
        """743-ПП, табл. 3.6.1, прим. 3 — вместо табличных 5 м."""
        linden = catalogue.get("Липа мелколистная")

        resolution = norms.resolve_setback("building", "tree", linden, catalogue.crown_reference_diameter_m)

        assert resolution.table_m == 5.0
        assert resolution.required_m == 10.0
        assert "прим" in norms.sources[resolution.source_id].clause.lower()

    def test_table_values_scale_with_crown_diameter(self, norms, catalogue):
        """Примечание 1 к обеим таблицам: нормативы даны для кроны не более
        5 м и «должны быть соответственно увеличены» для большей.
        """
        linden = catalogue.get("Липа мелколистная")  # крона 8 м

        resolution = norms.resolve_setback("gas_pipe", "tree", linden, catalogue.crown_reference_diameter_m)

        assert resolution.table_m == 1.5
        assert resolution.crown_multiplier == pytest.approx(1.6)
        assert resolution.required_m == pytest.approx(2.4)
        assert resolution.adjusted_for_crown

    def test_a_narrow_crown_never_gets_closer_than_the_table(self, norms, catalogue):
        """Множитель не опускается ниже 1.0: узкая крона не даёт права
        придвинуться ближе таблицы.
        """
        rowan = catalogue.get("Рябина обыкновенная")  # крона 4 м, меньше эталонных 5

        resolution = norms.resolve_setback("gas_pipe", "tree", rowan, catalogue.crown_reference_diameter_m)

        assert resolution.crown_multiplier == 1.0
        assert resolution.required_m == norms.setback_for("gas_pipe", "tree")

    def test_without_a_species_the_table_value_stands(self, norms, catalogue):
        resolution = norms.resolve_setback("building", "tree", None, catalogue.crown_reference_diameter_m)

        assert resolution.required_m == resolution.table_m == 5.0
        assert resolution.species_rule == ""

    def test_the_crown_multiplier_is_not_applied_to_values_no_act_sets(self, norms, catalogue):
        """Примечание 1 относится к конкретным таблицам. Масштабировать
        пропорцией то, чего норматив не задавал (ПУЭ, практика), значило бы
        выдавать нашу оценку за требование акта.
        """
        linden = catalogue.get("Липа мелколистная")

        resolution = norms.resolve_setback(
            "power_line_corridor", "tree", linden, catalogue.crown_reference_diameter_m
        )

        assert resolution.crown_multiplier == 1.0
        assert resolution.required_m == resolution.table_m


class TestCrownDrivenSpacing:
    def test_spacing_follows_the_crown_class(self, norms, catalogue):
        """МГСН 4.2.9.2: 8-10 м широкая, 5-6 средняя, 3-4 узкая — берётся
        нижняя граница, это минимум.
        """
        linden = catalogue.get("Липа мелколистная")  # wide
        rowan = catalogue.get("Рябина обыкновенная")  # narrow

        wide = norms_for_species(norms, "tree", linden, catalogue)
        narrow = norms_for_species(norms, "tree", rowan, catalogue)

        assert wide.spacing_for("tree").min_distance_m == 8.0
        assert narrow.spacing_for("tree").min_distance_m == 3.0

    def test_the_two_to_one_ratio_between_step_and_radius_is_preserved(self, norms, catalogue):
        """min_distance_m служит ещё и шагом сетки кандидатов; пара «шаг /
        радиус отбора» обязана держать 2:1, иначе отбор перестаёт отсеивать
        соседей — именно так когда-то сломали плотность кустарника.
        """
        for species in catalogue.tree:
            adjusted = norms_for_species(norms, "tree", species, catalogue)
            spacing = adjusted.spacing_for("tree")
            assert spacing.min_distance_m == pytest.approx(spacing.canopy_radius_m * 2)

    def test_shrub_spacing_is_left_alone(self, norms, catalogue):
        """743-ПП, табл. 3.6.2 даёт кустарнику 0,3-1,0 м, но это расстояния
        внутри рядовой или групповой посадки, а генератор разбрасывает кусты
        независимыми точками. Подставить сюда 0,3 м значило бы применить
        нормативное число к чужому паттерну и вернуть сплошной ковёр.
        """
        lilac = catalogue.get("Сирень обыкновенная")

        adjusted = norms_for_species(norms, "shrub", lilac, catalogue)

        assert adjusted.spacing_for("shrub").min_distance_m == norms.spacing_for("shrub").min_distance_m


class TestSpeciesChoice:
    def test_the_same_plan_key_picks_the_same_species(self):
        """Пересчёт схлопнутого плана обязан вернуть ту же породу — иначе
        план перестаёт быть чистой функцией от рецепта.
        """
        first = choose_species("plan-1", "tree")
        again = choose_species("plan-1", "tree")

        assert first is not None
        assert first.name == again.name

    def test_different_plans_can_get_different_species(self):
        picks = {choose_species(f"plan-{i}", "tree").name for i in range(25)}

        assert len(picks) > 1

    def test_lawn_has_no_species(self):
        assert choose_species("plan-1", "lawn") is None


class TestGeneratorAndCheckerAgree:
    """Самая опасная форма ошибки в этой связке: план, который отвергает
    собственная проверка того же сервиса.

    Поймано живьём при интеграции породных правил. `compliance.explain_items`
    уже применял поправки по породе, а `buffers.build_exclusion_zone` ещё
    считал по табличным значениям — на реальной улице с широкой кроной 80 из
    121 посадок вышли «нарушениями», хотя генератор поставил их ровно туда,
    куда сам же и разрешил.
    """

    @pytest.mark.parametrize("species_name", ["Липа мелколистная", "Берёза повислая", "Рябина обыкновенная"])
    def test_every_generated_planting_passes_its_own_compliance_check(self, species_name, norms, catalogue):
        from shapely.geometry import LineString, Polygon

        from geo_engine.compliance import explain_items
        from geo_engine.model import Utility, Zone
        from geo_engine.planner import plan_items

        territory = Polygon([(0, 0), (200, 0), (200, 160), (0, 160)])
        utilities = [
            Utility(geometry=LineString([(0, 40), (200, 40)]), object_type="heat_network"),
            Utility(geometry=LineString([(0, 90), (200, 90)]), object_type="gas_pipe"),
        ]
        zones = [
            Zone(geometry=territory, zone_type="territory"),
            Zone(geometry=Polygon([(10, 130), (60, 130), (60, 155), (10, 155)]), zone_type="building"),
        ]

        def score_fn(candidates):
            return [(1.0, "test") for _ in candidates]

        items = plan_items(
            "agreement",
            utilities,
            zones,
            territory,
            ["tree"],
            score_fn,
            norms,
            species_overrides={"tree": species_name},
            catalogue=catalogue,
        )

        assert items, "фикстура должна давать хоть одну посадку, иначе тест ничего не проверяет"
        assert all(i.species == species_name for i in items)

        records = explain_items(items, utilities, zones, norms, catalogue)
        violations = [r for r in records if not r.compliant]

        assert violations == [], f"генератор поставил {len(violations)} посадок, которые сам же считает нарушением"

    def test_a_wider_crown_yields_fewer_plantings(self, norms, catalogue):
        """Проверка, что порода действительно влияет на результат, а не просто
        подписывается в атрибутах: широкая крона — и больший интервал, и
        большие отступы.
        """
        from shapely.geometry import LineString, Polygon

        from geo_engine.model import Utility, Zone
        from geo_engine.planner import plan_items

        territory = Polygon([(0, 0), (200, 0), (200, 160), (0, 160)])
        utilities = [Utility(geometry=LineString([(0, 40), (200, 40)]), object_type="heat_network")]
        zones = [Zone(geometry=territory, zone_type="territory")]

        def score_fn(candidates):
            return [(1.0, "test") for _ in candidates]

        def count(species_name):
            return len(
                plan_items(
                    "crown-effect",
                    utilities,
                    zones,
                    territory,
                    ["tree"],
                    score_fn,
                    norms,
                    species_overrides={"tree": species_name},
                    catalogue=catalogue,
                    # DEFAULT_DENSITY_PER_HA (planner.py) would otherwise cap
                    # both species down to the same allowance on this
                    # 3.2ha/narrow-crown-heavy scene, hiding exactly the
                    # count difference this test exists to catch.
                    density_per_ha={"tree": 0},
                )
            )

        assert count("Липа мелколистная") < count("Рябина обыкновенная")
