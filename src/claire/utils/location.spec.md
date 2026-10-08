## Location Spec — configured city only

This specification documents `src/claire/utils/location.py`.

The module no longer detects a location. It reports the city the user
configured, and nothing else. There is no IP lookup, no UPnP probe, no
geolocation database, no network call and no disk cache.

### Why IP detection was removed

The old implementation resolved the machine's location from an IP address:
it probed the LAN, asked the router over UPnP, fell back to a socket-based
external-IP lookup, optionally resolved CGNAT addresses through an OpenDNS
query, read a city out of a local MaxMind GeoLite2 database, and cached the
result in memory and on disk.

With no GeoLite2 database installed the chain still ran every network probe
first, so a single call could block for roughly **12 seconds** — on the reply
path, once per agentic turn — to produce a value that was usually
`"Location: Unknown"` anyway. The `get_location_info()` entry point and the
whole discovery/caching/database stack were removed for that reason.

### What the module does now

`get_location_context(cfg)` returns exactly one of three strings:

| Condition | Result |
|-----------|--------|
| `location_enabled` is false | `"Location: Disabled"` |
| `location_city` is a non-empty string | `"Location: <city>"` |
| otherwise | `"Location: Unknown"` |

Nothing is probed, fetched, cached or written to disk. The call is pure
attribute access and returns immediately.

### Public API

| Function | Returns | Description |
|----------|---------|-------------|
| `get_configured_city(cfg)` | `str \| None` | The stripped `location_city` value, or `None`. |
| `get_location_context(cfg)` | `str` | The context line described above. Extra keyword arguments are accepted and ignored for backwards compatibility. |
| `get_location_context_with_timezone(cfg)` | `(str, str \| None)` | The context line plus a timezone name, which is **always `None`** — a city name is not an IANA zone and there is no database to derive one from. Callers pass the `None` to `format_time_context`, which uses the OS local timezone. |
| `is_location_available(cfg)` | `bool` | True only when a city is configured and location is not disabled. |

`cfg` may be the settings dataclass or any mapping. All lookups use
`getattr`/`get`, so a missing key is simply `None`.

### Configuration

| Key | Status | Notes |
|-----|--------|-------|
| `location_city` | **retired, read defensively** | The only key that can still produce a location. It is currently not exposed by `claire.config`; re-exposing it (a plain string) is all that is needed to give the assistant a city again. |
| `location_enabled` | still read | Master toggle. When false the context is `"Location: Disabled"`. |
| `location_auto_detect` | dead | Nothing is detected any more. |
| `location_ip_address` | dead | No IP is geolocated any more. |
| `location_cgnat_resolve_public_ip` | dead | No CGNAT resolution any more. |
| `location_cache_minutes` | dead | No cache exists any more. |

### Integration Points

- **Daemon** (`src/claire/daemon.py`): prints the context line once at
  startup (`📍 <context>`). It never probes the network.
- **Reply Engine** (`src/claire/reply/engine.py`): `_live_time_location_string`
  refreshes the time/location line each turn. With no city configured the tz
  is `None` and the OS local timezone is reported.
- **Time tool** (`src/claire/tools/builtin/time_tool.py`): for an explicitly
  named place it geocodes that place itself (unchanged, still network-backed
  and still works); with no place it reports the user's own local time in the
  OS zone.
- **Setup Wizard** (`src/desktop_app/setup_wizard.py`): the Location page and
  its IP-validation/"Detect My IP" UI were removed. The wizard tail runs
  models → MCP → search providers → complete.
- **Listener** (`src/claire/listening/listener.py`): uses
  `is_location_available()` only to choose the weather example in the startup
  banner; with no city configured it shows the `[your city]` placeholder form.

### Honest consequence

There is currently **no way for the assistant to know her own city**, because
`location_city` is retired from the config schema. The capability returns
without any code change here if that key is re-exposed; see the module
docstring.
