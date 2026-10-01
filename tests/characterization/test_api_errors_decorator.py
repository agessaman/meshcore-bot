"""Routes using BotDataViewer._api_errors still log and answer exactly as before."""

from unittest.mock import MagicMock

from tests.characterization.test_web_endpoint_inventory import _viewer


def test_failing_route_logs_prefix_and_returns_500(tmp_path):
    viewer = _viewer(tmp_path, "")
    viewer.logger = MagicMock()
    viewer.db_manager.get_metadata = MagicMock(side_effect=RuntimeError("disk on fire"))
    resp = viewer.app.test_client().get("/api/maintenance/list_backups")
    assert resp.status_code == 500
    assert resp.get_json() == {"error": "An internal error occurred"}  # rewritten by set_security_headers
    viewer.logger.error.assert_any_call("Error listing backups: disk on fire")


def test_decorated_routes_keep_their_endpoint_names(tmp_path):
    viewer = _viewer(tmp_path, "")
    endpoints = {rule.endpoint for rule in viewer.app.url_map.iter_rules()}
    assert "api_maintenance_list_backups" in endpoints
