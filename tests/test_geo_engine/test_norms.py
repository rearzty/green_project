from geo_engine.norms import load_norms

NORMS = load_norms()


def test_with_spacing_override_derives_canopy_radius_as_half_the_interval():
    """The 2:1 ratio matches the shipped tree_default/shrub_default defaults
    (5.0/2.5, 3.0/1.5) -- a user-supplied interval should keep the same
    relationship, not just move min_distance_m and leave canopy_radius_m
    wherever it was (that mismatch is exactly what once made shrub density
    basically uncontrolled, see CLAUDE.md)."""
    overridden = NORMS.with_spacing_override("tree", 8.0)
    spacing = overridden.spacing_for("tree")
    assert spacing.min_distance_m == 8.0
    assert spacing.canopy_radius_m == 4.0


def test_with_spacing_override_does_not_mutate_the_original():
    NORMS.with_spacing_override("tree", 8.0)
    assert NORMS.spacing_for("tree").min_distance_m == 5.0
    assert NORMS.spacing_for("tree").canopy_radius_m == 2.5


def test_with_spacing_override_leaves_other_planting_types_alone():
    overridden = NORMS.with_spacing_override("tree", 8.0)
    assert overridden.spacing_for("shrub") == NORMS.spacing_for("shrub")


class TestSetbackCitations:
    """Every setback must be traceable to an act — the brief rejects a result
    that cannot be checked clause by clause, so an uncited value is a defect,
    not a gap in documentation.
    """

    def test_every_setback_value_has_a_source_binding(self):
        from geo_engine.norms import load_norms

        norms = load_norms()
        missing = [
            (object_type, planting_type)
            for object_type, rules in norms.setbacks_m.items()
            for planting_type in rules
            if norms.source_for(object_type, planting_type) is None
        ]

        assert missing == []

    def test_every_binding_points_at_a_declared_source(self):
        from geo_engine.norms import load_norms

        norms = load_norms()
        dangling = [
            (object_type, planting_type, binding.source)
            for object_type, rules in norms.setback_sources.items()
            for planting_type, binding in rules.items()
            if binding.source not in norms.sources
        ]

        assert dangling == []

    def test_the_sp42_table_values_match_the_act(self):
        """Spot-check against the text of СП 42.13330.2016, table 9.1, as
        published. These four were wrong before the table was actually read:
        gas took 2.0 from 743-ПП (kept, it is stricter), cable took 1.5 where
        the table says 2.0, cable shrub took 1.0 against 0.7, heat shrub took
        1.5 against 1.0.
        """
        from geo_engine.norms import load_norms

        norms = load_norms()

        assert norms.setback_for("building", "tree") == 5.0
        assert norms.setback_for("building", "shrub") == 1.5
        assert norms.setback_for("road", "tree") == 2.0
        assert norms.setback_for("cable_line", "tree") == 2.0
        assert norms.setback_for("cable_line", "shrub") == 0.7
        assert norms.setback_for("heat_network", "tree") == 2.0
        assert norms.setback_for("heat_network", "shrub") == 1.0
        assert norms.setback_for("sewer", "tree") == 1.5
        assert norms.setback_for("lighting_pole", "tree") == 4.0
        assert norms.setback_for("sidewalk", "tree") == 0.7

    def test_a_value_taken_from_the_brief_is_not_marked_as_verified(self):
        """743-ПП's 2 m comes from the brief's own wording, not from the act.
        Presenting it as verified would be the failure mode this whole
        mechanism exists to prevent.
        """
        from geo_engine.norms import load_norms

        norms = load_norms()
        source, _ = norms.source_for("gas_pipe", "tree")

        assert "743-ПП" in source.act
        assert source.verified is False
        assert "не сверено" in source.citation()
