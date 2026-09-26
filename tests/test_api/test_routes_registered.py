"""Only checks route wiring, not live behavior — hitting real endpoints
requires a running PostGIS instance (see infra/docker-compose.yml), which
this repo's unit test suite deliberately doesn't depend on.
"""

from backend.app.main import app


def _collect_paths(routes) -> set[str]:
    """FastAPI wraps each include_router() call in an opaque _IncludedRouter
    that hides its routes behind `.original_router.routes` rather than
    flattening them into app.routes directly — walk both shapes.
    """
    paths: set[str] = set()
    for route in routes:
        path = getattr(route, "path", None)
        if path is not None:
            paths.add(path)
        original_router = getattr(route, "original_router", None)
        if original_router is not None:
            paths |= _collect_paths(original_router.routes)
        sub_routes = getattr(route, "routes", None)
        if sub_routes:
            paths |= _collect_paths(sub_routes)
    return paths


def test_expected_routes_are_registered():
    paths = _collect_paths(app.routes)
    assert "/health" in paths
    assert "/api/projects" in paths
    assert "/api/projects/upload/{job_id}" in paths
    assert "/api/projects/{project_id}" in paths
    assert "/api/projects/{project_id}/generate" in paths
    assert "/api/projects/{project_id}/generate/{job_id}" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/items/{item_id}" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/items/delete" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/items/retype" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/items/move" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/items/restore" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/validate" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/validate/items" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/export-dxf" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/export-dxf/{job_id}" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/export-dxf/{job_id}/download" in paths
    assert "/api/config/planting-norms" in paths
    assert "/api/projects/{project_id}/assistant/message" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/compliance/items" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/compliance-report.json" in paths
    assert "/api/projects/{project_id}/plans/{plan_id}/compliance-report.csv" in paths
    assert "/api/projects/{project_id}/layers" in paths
    assert "/api/projects/{project_id}/layers-raster" in paths
    assert "/api/projects/{project_id}/layers-raster/image" in paths
