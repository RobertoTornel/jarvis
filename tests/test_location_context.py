"""Location is configuration only: no IP discovery, no GeoLite2, no blocking.

Replaces the old tests that exercised the removed IP/UPnP/GeoLite2/disk-cache
stack. The point of these tests is that the reply path cannot block on the
network: the old ``auto_detect=True`` lookup took ~12 seconds on a host with
no GeoLite2 database, and that path no longer exists.
"""

import socket
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

from claire.reply.engine import _live_time_location_string
from claire.utils import location
from claire.utils.location import (
    get_configured_city,
    get_location_context,
    get_location_context_with_timezone,
    is_location_available,
)

pytestmark = pytest.mark.unit


def _cfg(**over):
    base = {"location_enabled": True}
    base.update(over)
    return SimpleNamespace(**base)


class TestConfiguredCityOnly:
    """The module reports a configured city and nothing else."""

    def test_unknown_without_city(self):
        assert get_location_context(_cfg()) == "Location: Unknown"

    def test_reports_configured_city(self):
        assert get_location_context(_cfg(location_city="Lisbon")) == "Location: Lisbon"

    def test_blank_city_is_not_a_location(self):
        assert get_configured_city(_cfg(location_city="   ")) is None
        assert get_location_context(_cfg(location_city="   ")) == "Location: Unknown"

    def test_disabled_toggle_wins(self):
        cfg = _cfg(location_enabled=False, location_city="Lisbon")
        assert get_location_context(cfg) == "Location: Disabled"
        assert is_location_available(cfg) is False

    def test_timezone_is_always_none(self):
        _, tz_name = get_location_context_with_timezone(_cfg(location_city="Lisbon"))
        assert tz_name is None

    def test_legacy_discovery_kwargs_are_ignored(self):
        """Old call shapes passed the discovery knobs; they must not raise."""
        context = get_location_context(
            _cfg(),
            config_ip="203.0.113.4",
            auto_detect=True,
            resolve_cgnat_public_ip=True,
            location_cache_minutes=60,
        )
        assert context == "Location: Unknown"

    def test_mapping_config_is_accepted(self):
        assert get_location_context({"location_city": "Porto"}) == "Location: Porto"
        assert get_location_context({}) == "Location: Unknown"


class TestRemovedMachineryIsGone:
    """The IP/UPnP/GeoLite2/disk-cache stack is deleted, not just unused."""

    def test_all_removed_symbols_are_absent(self):
        removed = (
            # entry point and its public wrappers
            "get_location_info",
            "get_detailed_location_info",
            "setup_location_database",
            # GeoLite2 database handling
            "_get_database_path",
            "_download_geolite2_database",
            "_print_location_setup_instructions",
            "GEOIP2_AVAILABLE",
            # IP discovery
            "_is_private_ip",
            "_is_cgnat_ip",
            "_get_local_network_ip",
            "_get_external_ip_via_upnp",
            "_get_external_ip_via_socket",
            "_get_external_ip_automatically",
            "_resolve_public_ip_via_opendns",
            "MINIUPNPC_AVAILABLE",
            # formatting and caches
            "_format_location_context",
            "_cache_base_dir",
            "_load_disk_caches",
            "_persist_disk_caches",
            "_location_cache",
            "_cgnat_resolution_cache",
            "_upnp_attempted",
            "_upnp_cached_ip",
        )
        still_present = [name for name in removed if hasattr(location, name)]
        assert still_present == [], f"removed symbols still present: {still_present}"

    def test_no_geolocation_libraries_are_imported(self):
        assert not hasattr(location, "geoip2")
        assert not hasattr(location, "miniupnpc")


class TestReplyPathCannotBlock:
    """The live time/location line is pure config access."""

    def test_no_network_io_on_the_reply_path(self, monkeypatch):
        def _forbidden(*_args, **_kwargs):
            raise AssertionError("live context attempted network I/O")

        monkeypatch.setattr(socket, "socket", _forbidden)
        monkeypatch.setattr(socket, "create_connection", _forbidden)
        monkeypatch.setattr(socket, "getaddrinfo", _forbidden)

        out = _live_time_location_string(_cfg(location_auto_detect=True))

        assert out.startswith("Current local time:")
        assert "Location: Unknown" in out

    def test_live_context_returns_immediately(self):
        """The old auto-detect path blocked ~12s with no GeoLite2 database."""
        cfg = _cfg(
            location_auto_detect=True,
            location_ip_address=None,
            location_cgnat_resolve_public_ip=True,
            location_cache_minutes=60,
        )
        start = time.monotonic()
        out = _live_time_location_string(cfg)
        elapsed = time.monotonic() - start

        assert out
        assert elapsed < 2.0, f"live context took {elapsed:.2f}s; it must not block"

    def test_engine_uses_context_supplied_by_its_caller(self):
        """The engine attribute stays patchable, so evals can inject a city."""
        with patch(
            "claire.reply.engine.get_location_context_with_timezone",
            return_value=("Location: Berlin, Germany", None),
        ):
            out = _live_time_location_string(_cfg())

        assert "Location: Berlin, Germany" in out

    def test_disabled_location_still_reported(self):
        out = _live_time_location_string(_cfg(location_enabled=False))
        assert "Location: Disabled" in out


class TestTimeToolExplicitPlace:
    """A named place is geocoded by the tool itself, with no ambient location."""

    def test_named_place_works_with_no_ambient_location(self):
        from claire.tools.base import ToolContext
        from claire.tools.builtin.time_tool import TimeTool

        context = Mock(spec=ToolContext)
        context.user_print = Mock()
        context.cfg = _cfg()
        context.cfg.llm_tools_timeout_sec = 8.0
        context.redacted_text = ""

        geo_response = Mock()
        geo_response.status_code = 200
        geo_response.json.return_value = {
            "results": [
                {
                    "name": "Thessaloniki",
                    "country": "Greece",
                    "timezone": "Europe/Athens",
                }
            ]
        }
        geo_response.raise_for_status = Mock()

        with patch("requests.get", return_value=geo_response), patch(
            "claire.tools.builtin.time_tool.get_location_context_with_timezone",
            side_effect=AssertionError("ambient location must not be consulted"),
        ):
            result = TimeTool().run({"location": "Thessaloniki"}, context)

        assert result.success is True
        assert "Thessaloniki" in result.reply_text
