"""Live location config, retired location knobs, and old config files that still load.

``location_city`` is **live**.  Everything that used to discover a location was
removed — the UPnP probe, the socket-based external-IP lookup, the OpenDNS CGNAT
step, the GeoLite2 database and the on-disk caches — so an explicitly configured
city is the only location source the assistant has left, and
``claire.utils.location`` reads exactly this key.  It is therefore declared in
``get_default_config()`` and on ``Settings``, and a value set in the config file
must survive the load/merge/coercion path and reach the location helpers.  The
master toggle ``location_enabled`` is live too and is deliberately *not* listed
as retired.

The four location knobs that existed only to drive the removed discovery chain
are the genuinely retired ones:

* ``location_auto_detect`` — switched the IP probe on.
* ``location_ip_address`` — pinned the address to look up.
* ``location_cache_minutes`` — TTL of the removed on-disk cache.
* ``location_cgnat_resolve_public_ip`` — the removed OpenDNS CGNAT resolution.

Location is configuration-only now, so none of them has a reader; they must not
reappear in the defaults or on ``Settings``.

Unrelated keys were retired for having no reader at all:
``stop_command_fuzzy_ratio``, ``whisper_min_word_length``,
``evaluator_enabled`` / ``evaluator_nudge_max`` (the evaluator was retired),
``allowlist_bundles`` (a leftover from the removed macOS screen capture) and
``active_profiles``.

The invariant for every retired key is the same: a config file that still sets it
must load cleanly — the unknown key is simply ignored, never a crash.  These
tests pin both halves: the live keys keep working, the retired ones stay inert.
"""

import dataclasses
import json

import pytest

from claire.config import Settings, get_default_config, load_settings
from claire.utils.location import get_configured_city, get_location_context

pytestmark = pytest.mark.unit

#: The keys genuinely retired from the schema.  None may reappear in the defaults
#: or on ``Settings``, and a config file that still sets them must still load.
#: ``location_city`` is **not** here — it is live; see the module docstring.
REMOVED_KEYS = (
    # Retired with the IP/GeoLite2 discovery chain.  Location is
    # configuration-only now (``location_city``), so nothing reads these.
    "location_auto_detect",
    "location_ip_address",
    "location_cache_minutes",
    "location_cgnat_resolve_public_ip",
    # Retired for having no reader at all.
    "stop_command_fuzzy_ratio",
    "whisper_min_word_length",
    "evaluator_enabled",
    "evaluator_nudge_max",
    "allowlist_bundles",
    "active_profiles",
)

#: The four retired location knobs, called out separately because they are the
#: ones a real ``config.json`` from the discovery era would carry alongside a
#: live ``location_city``.
RETIRED_LOCATION_KEYS = (
    "location_auto_detect",
    "location_ip_address",
    "location_cache_minutes",
    "location_cgnat_resolve_public_ip",
)


def _write_config(tmp_path, monkeypatch, values):
    cfg_path = tmp_path / "config.json"
    cfg_path.write_text(json.dumps(values), encoding="utf-8")
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(cfg_path))
    return cfg_path


def test_defaults_do_not_declare_removed_keys():
    defaults = get_default_config()
    for key in REMOVED_KEYS:
        assert key not in defaults, f"{key} is back in get_default_config()"
    # Guard the other way round: the live location keys must stay declared, or
    # the assistant silently loses the only location source it has.
    assert "location_city" in defaults, "location_city went missing from get_default_config()"
    assert "location_enabled" in defaults, "location_enabled went missing from get_default_config()"


def test_settings_has_no_removed_fields():
    field_names = {f.name for f in dataclasses.fields(Settings)}
    for key in REMOVED_KEYS:
        assert key not in field_names, f"{key} is back on Settings"
    assert "location_city" in field_names, "location_city is not a Settings field"
    assert "location_enabled" in field_names, "location_enabled is not a Settings field"


def test_load_tolerates_a_config_that_still_sets_removed_keys(tmp_path, monkeypatch):
    """An old config file setting retired keys loads; live keys keep their values."""
    cfg_path = _write_config(
        tmp_path,
        monkeypatch,
        {
            # Live keys, including the only location source there is.
            "location_city": "Lisbon",
            "agentic_max_turns": 3,
            "use_stdin": True,
            # Retired keys from the discovery era, still present on disk.
            "location_auto_detect": True,
            "location_ip_address": "203.0.113.7",
            "location_cache_minutes": 60,
            "location_cgnat_resolve_public_ip": True,
            "stop_command_fuzzy_ratio": 0.8,
            "whisper_min_word_length": 2,
            "evaluator_enabled": True,
            "evaluator_nudge_max": 3,
            "allowlist_bundles": ["com.apple.Safari"],
            "active_profiles": ["default"],
        },
    )

    cfg = load_settings()

    assert isinstance(cfg, Settings)
    # Live values survive the merge.
    assert cfg.location_city == "Lisbon"
    assert cfg.agentic_max_turns == 3
    assert cfg.use_stdin is True
    # Retired keys are dropped by the coercion step, not carried onto Settings.
    for key in REMOVED_KEYS:
        assert not hasattr(cfg, key), f"{key} was reintroduced as a Settings attribute"
    # A plain load never has to rewrite the file, and it must not have been
    # damaged when the unknown keys were ignored.
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["location_city"] == "Lisbon"


def test_migration_leaves_removed_keys_inert(tmp_path, monkeypatch):
    """The migration path must not resurrect a retired key, nor crash on it."""
    cfg_path = _write_config(
        tmp_path,
        monkeypatch,
        {
            "_config_version": 0,
            "location_city": "Lisbon",
            "location_auto_detect": True,
            "location_ip_address": "203.0.113.7",
            "location_cache_minutes": 60,
            "location_cgnat_resolve_public_ip": True,
        },
    )

    cfg = load_settings()

    assert isinstance(cfg, Settings)
    # The live city comes through the migration/merge path too.
    assert cfg.location_city == "Lisbon"
    # Migrations may rewrite the file (the version stamp is added).  The live key
    # survives that rewrite untouched; the retired ones are left on disk as
    # unknown keys rather than promoted onto Settings.
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    assert on_disk.get("location_city") == "Lisbon"
    assert all(key not in vars(cfg) for key in REMOVED_KEYS)
    assert not any(hasattr(cfg, key) for key in RETIRED_LOCATION_KEYS)


def test_location_city_is_live_end_to_end(tmp_path, monkeypatch):
    """A configured city reaches both ``Settings`` and the location helpers.

    This is the regression guard the retired-key list cannot provide: it fails if
    ``location_city`` is dropped from the defaults, the coercion block or the
    ``Settings(...)`` constructor — while the four retired discovery knobs stay
    inert even when the file still sets them.
    """
    cfg_path = _write_config(
        tmp_path,
        monkeypatch,
        {
            "location_city": "Lisbon",
            "location_auto_detect": True,
            "location_ip_address": "203.0.113.7",
            "location_cache_minutes": 60,
            "location_cgnat_resolve_public_ip": True,
        },
    )

    cfg = load_settings()

    # End to end: config file -> Settings -> location helper -> prompt context.
    assert cfg.location_city == "Lisbon"
    assert get_configured_city(cfg) == "Lisbon"
    assert get_location_context(cfg) == "Location: Lisbon"
    # The retired discovery knobs never become attributes, even when on disk.
    for key in RETIRED_LOCATION_KEYS:
        assert not hasattr(cfg, key), f"{key} was reintroduced as a Settings attribute"
        assert key not in vars(cfg), f"{key} was reintroduced as a Settings attribute"
        assert key not in get_default_config(), f"{key} is back in get_default_config()"
    # ``location_enabled`` is still live and controls the master switch.
    assert cfg.location_enabled is True
    assert json.loads(cfg_path.read_text(encoding="utf-8"))["location_city"] == "Lisbon"
