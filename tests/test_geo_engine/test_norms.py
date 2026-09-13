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
