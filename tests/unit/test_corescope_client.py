"""Unit tests for modules.clients.corescope_client.

No network: every test drives the client through a fake ``requests.Session``. The
fixture in ``tests/fixtures/corescope/`` is a real response from
https://analyzer.comchan.net, trimmed to the six observations that cover the cases
worth pinning -- a duplicate route across two observers, three nested prefixes, and
the 12-hop outlier.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import requests

from modules.clients.corescope_client import (
    MAX_RESPONSE_BYTES,
    CoreScopeClient,
    ScopePacket,
    normalize_base_url,
)

FIXTURE = Path(__file__).parent.parent / "fixtures" / "corescope" / "packet-7ba8bf11.json"
HASH = "7ba8bf1119ca1fe1"
BASE = "https://analyzer.example.net"


def _fixture_text() -> str:
    return FIXTURE.read_text(encoding="utf-8")


def _response(*, status=200, body="", headers=None):
    """A stand-in for a streamed ``requests`` response."""
    raw = body.encode("utf-8") if isinstance(body, str) else body
    response = MagicMock(spec=requests.Response)
    response.status_code = status
    response.headers = headers or {}
    response.iter_content = lambda chunk_size=65536: [
        raw[i:i + chunk_size] for i in range(0, len(raw), chunk_size)
    ]
    response.close = MagicMock()
    return response


def _client(*, body=None, status=200, headers=None, raises=None, base=BASE, **kwargs):
    session = MagicMock(spec=requests.Session)
    if raises is not None:
        session.get.side_effect = raises
    else:
        session.get.return_value = _response(
            status=status,
            body=_fixture_text() if body is None else body,
            headers=headers,
        )
    return CoreScopeClient(base, session=session, **kwargs), session


class TestNormalizeBaseUrl:
    def test_adds_https_when_no_scheme(self):
        assert normalize_base_url("analyzer.example.net") == "https://analyzer.example.net"

    def test_strips_trailing_slash_and_whitespace(self):
        assert normalize_base_url("  https://a.example/  ") == "https://a.example"

    def test_keeps_an_explicit_http_scheme(self):
        assert normalize_base_url("http://localhost:8080") == "http://localhost:8080"

    @pytest.mark.parametrize("value", ["", "   ", None, "ftp://a.example", "https://"])
    def test_unusable_values_are_not_configured(self, value):
        assert normalize_base_url(value) == ""

    def test_unconfigured_client_never_calls_out(self):
        client, session = _client(base="")
        assert client.configured is False
        assert client.fetch(HASH) is None
        session.get.assert_not_called()


class TestMapUrl:
    def test_ends_in_paths_all_and_lowercases_the_hash(self):
        client, _ = _client()
        assert client.map_url(HASH.upper()) == (
            f"{BASE}/#/map?packet={HASH}&paths=all"
        )

    def test_empty_for_a_bad_hash_rather_than_a_broken_link(self):
        client, _ = _client()
        assert client.map_url("notahash") == ""
        assert client.map_url("") == ""

    def test_empty_when_unconfigured(self):
        client, _ = _client(base="")
        assert client.map_url(HASH) == ""


class TestFetchCounting:
    def test_counts_the_fixture(self):
        client, _ = _client()
        packet = client.fetch(HASH)
        assert packet == ScopePacket(
            unique_paths=5,
            observations=6,
            observers=4,
            max_hops=12,
            paths=(
                "db82",
                "db82,e34c",
                "db82,e34c,3d73",
                "db82,e34c,7e7f,5757,4216,2ad2,1ee8,349d,5790,90e2,42e7,a84b",
                "db82,e34c,a937",
            ),
        )

    def test_a_route_that_prefixes_another_counts_separately(self):
        """Two observers hearing the same packet 2 and 3 hops out saw two routes."""
        client, _ = _client()
        paths = client.fetch(HASH).paths
        assert "db82,e34c" in paths
        assert "db82,e34c,a937" in paths

    def test_one_route_heard_by_two_observers_counts_once(self):
        client, _ = _client()
        packet = client.fetch(HASH)
        # Six observations, five routes: RUST-CORE and Forum Observer both heard
        # db82,e34c,3d73.
        assert packet.observations == 6
        assert packet.unique_paths == 5
        assert packet.paths.count("db82,e34c,3d73") == 1

    def test_hops_are_lowercased_to_match_the_bots_own_paths(self):
        body = json.dumps({"packet": {"observations": [
            {"observer_id": "a", "path_json": '["db82","e34c"]'},
            {"observer_id": "b", "path_json": '["DB82","E34C"]'},
        ]}})
        client, _ = _client(body=body)
        packet = client.fetch(HASH)
        assert packet.unique_paths == 1
        assert packet.paths == ("db82,e34c",)

    def test_empty_paths_are_excluded_from_the_count(self):
        """A direct reception has no hops, as it does in the bot's own count."""
        body = json.dumps({"packet": {"observations": [
            {"observer_id": "a", "path_json": "[]"},
            {"observer_id": "b", "path_json": '["DB82"]'},
        ]}})
        client, _ = _client(body=body)
        packet = client.fetch(HASH)
        assert packet.unique_paths == 1
        assert packet.observations == 2
        assert packet.paths == ("db82",)

    def test_a_malformed_path_skips_only_that_observation(self):
        body = json.dumps({"packet": {"observations": [
            {"observer_id": "a", "path_json": "not json at all"},
            {"observer_id": "b", "path_json": '["DB82","E34C"]'},
            {"observer_id": "c", "path_json": '{"not": "a list"}'},
            {"observer_id": "d"},
        ]}})
        client, _ = _client(body=body)
        packet = client.fetch(HASH)
        assert packet.unique_paths == 1
        assert packet.observations == 4
        assert packet.observers == 4

    def test_observation_count_from_the_server_is_not_trusted(self):
        """The reception count must describe the same list the paths came from."""
        body = json.dumps({"packet": {
            "observation_count": 46,
            "observations": [{"observer_id": "a", "path_json": '["DB82"]'}],
        }})
        client, _ = _client(body=body)
        assert client.fetch(HASH).observations == 1

    def test_a_flat_payload_without_the_packet_wrapper(self):
        body = json.dumps({"observations": [{"observer_id": "a", "path_json": '["DB82"]'}]})
        client, _ = _client(body=body)
        assert client.fetch(HASH).unique_paths == 1

    def test_paths_are_shaped_for_the_reply_layout(self):
        """_condense_path_lines takes comma-joined hops, as collected_paths holds."""
        from modules.commands.multitest_command import _condense_path_lines

        client, _ = _client()
        paths = sorted(client.fetch(HASH).paths)
        assert _condense_path_lines(paths, "flat")


class TestFetchFailures:
    def test_404_is_none(self):
        client, _ = _client(status=404, body='{"error":"Not found"}')
        assert client.fetch(HASH) is None

    def test_server_error_is_none(self):
        client, _ = _client(status=500, body="nope")
        assert client.fetch(HASH) is None

    def test_timeout_is_none(self):
        client, _ = _client(raises=requests.exceptions.Timeout())
        assert client.fetch(HASH) is None

    def test_connection_error_is_none(self):
        client, _ = _client(raises=requests.exceptions.ConnectionError())
        assert client.fetch(HASH) is None

    def test_other_request_exceptions_are_none(self):
        client, _ = _client(raises=requests.exceptions.TooManyRedirects())
        assert client.fetch(HASH) is None

    def test_non_json_body_is_none(self):
        client, _ = _client(body="<html>maintenance</html>")
        assert client.fetch(HASH) is None

    def test_no_observations_is_none(self):
        client, _ = _client(body=json.dumps({"packet": {"observations": []}}))
        assert client.fetch(HASH) is None

    def test_missing_observations_key_is_none(self):
        client, _ = _client(body=json.dumps({"packet": {"hash": HASH}}))
        assert client.fetch(HASH) is None

    @pytest.mark.parametrize("bad", ["", "notahash", "7ba8bf1119ca1fe", "7ba8bf1119ca1fe1a", "zzzzzzzzzzzzzzzz"])
    def test_a_bad_hash_is_rejected_before_any_request(self, bad):
        client, session = _client()
        assert client.fetch(bad) is None
        session.get.assert_not_called()

    def test_an_uppercase_hash_is_accepted(self):
        client, session = _client()
        assert client.fetch(HASH.upper()) is not None
        assert session.get.call_args[0][0].endswith(f"/api/packets/{HASH}")


class TestResponseSizeCeiling:
    def test_a_declared_oversize_length_is_refused_without_parsing(self):
        client, _ = _client(headers={"content-length": str(MAX_RESPONSE_BYTES + 1)})
        assert client.fetch(HASH) is None

    def test_an_undeclared_oversize_body_is_abandoned_mid_stream(self):
        body = '{"packet":{"observations":[' + ("x" * (MAX_RESPONSE_BYTES + 10))
        client, _ = _client(body=body)
        assert client.fetch(HASH) is None

    def test_a_declared_length_within_the_ceiling_is_read(self):
        client, _ = _client(headers={"content-length": str(len(_fixture_text()))})
        assert client.fetch(HASH) is not None


class TestCaching:
    def test_a_second_lookup_inside_the_ttl_makes_one_request(self):
        client, session = _client(cache_seconds=300)
        first = client.fetch(HASH)
        second = client.fetch(HASH)
        assert first == second
        assert session.get.call_count == 1

    def test_the_cache_is_case_insensitive_on_the_hash(self):
        client, session = _client(cache_seconds=300)
        client.fetch(HASH)
        client.fetch(HASH.upper())
        assert session.get.call_count == 1

    def test_an_expired_entry_is_refetched(self):
        client, session = _client(cache_seconds=0)
        client.fetch(HASH)
        client.fetch(HASH)
        assert session.get.call_count == 2

    def test_failures_are_not_cached(self):
        client, session = _client(status=404, body="{}", cache_seconds=300)
        client.fetch(HASH)
        client.fetch(HASH)
        assert session.get.call_count == 2


class TestRequestShape:
    def test_calls_the_packets_endpoint_with_the_configured_timeout(self):
        client, session = _client(timeout=2.5)
        client.fetch(HASH)
        args, kwargs = session.get.call_args
        assert args[0] == f"{BASE}/api/packets/{HASH}"
        assert kwargs["timeout"] == 2.5
        assert kwargs["stream"] is True

    def test_the_response_is_closed(self):
        client, session = _client()
        client.fetch(HASH)
        session.get.return_value.close.assert_called_once()
