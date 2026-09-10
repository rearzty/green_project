import pytest

from scripts.generate_synthetic_data import generate_synthetic_territory


@pytest.fixture
def synthetic_scene():
    return generate_synthetic_territory(seed=1)
