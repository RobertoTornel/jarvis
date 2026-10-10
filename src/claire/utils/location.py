"""Ambient location for the assistant — configured city only, no discovery.

This module used to resolve the machine's geographic location from an IP
address. The chain was: probe the LAN for a private address, ask the router
over UPnP for its external IP, fall back to a socket-based external-IP
lookup, optionally resolve a CGNAT address through an OpenDNS query, read a
city out of a local MaxMind GeoLite2 database, and cache the result on disk.

All of that is gone. With no GeoLite2 database installed the chain still ran
every network probe first, so one call could block for ~12 seconds on the
reply path — to produce a value that was usually unknown anyway. Geolocation
by IP is not worth that cost, and the machinery (UPnP client, GeoLite2
downloader, disk caches) existed only to serve it.

What remains is the one source that needs no network, no router, no database
and no cache: an explicitly configured city. When ``cfg.location_city`` is
set the context is ``"Location: <city>"``; when the master toggle
``location_enabled`` is off it is ``"Location: Disabled"``; otherwise it is
``"Location: Unknown"``. There is no longer a timezone to derive — a city
name is not an IANA zone and there is no database to look one up — so
:func:`get_location_context_with_timezone` always reports ``None`` and
callers fall back to the OS local timezone.

``location_city`` is a plain string on ``claire.config.Settings`` (default
``""``) and is the only location source there is: the city the owner
configures, e.g. ``"Lisbon"``. An empty string means no city is set. See
``location.spec.md``.
"""

from __future__ import annotations

from typing import Any, Mapping, Optional

__all__ = [
    "get_configured_city",
    "get_location_context",
    "get_location_context_with_timezone",
    "is_location_available",
]


def _get(cfg: Any, key: str, default: Any = None) -> Any:
    """Read ``key`` from a settings object or a plain mapping.

    Tolerant by design: callers pass the live settings dataclass, but tests
    and older call sites pass ``SimpleNamespace`` or dicts.
    """
    if cfg is None:
        return default
    if isinstance(cfg, Mapping):
        return cfg.get(key, default)
    return getattr(cfg, key, default)


def get_configured_city(cfg: Any = None) -> Optional[str]:
    """Return the explicitly configured city, or ``None``.

    Configuration only — nothing is probed, fetched or cached.
    """
    raw = _get(cfg, "location_city")
    if raw is None:
        return None
    city = str(raw).strip()
    return city or None


def get_location_context(cfg: Any = None, **_: Any) -> str:
    """Return the one-line location context injected into the prompt.

    ``"Location: Disabled"`` when ``location_enabled`` is off,
    ``"Location: <city>"`` when a city is configured, else
    ``"Location: Unknown"``.

    Extra keyword arguments are accepted and ignored so the older call
    shape (``config_ip=``, ``auto_detect=``, ...) keeps working; those
    inputs no longer have any effect.
    """
    if not bool(_get(cfg, "location_enabled", True)):
        return "Location: Disabled"
    city = get_configured_city(cfg)
    if city:
        return f"Location: {city}"
    return "Location: Unknown"


def get_location_context_with_timezone(
    cfg: Any = None, **_: Any
) -> tuple[str, Optional[str]]:
    """Return ``(location context, timezone name)``.

    The timezone is always ``None``. It used to come from the GeoLite2
    record; with the database reader removed there is nothing to name a
    zone from, and :func:`~claire.utils.time_context.format_time_context`
    treats ``None`` as "use the OS local timezone", which is the truth on
    the machine anyway.
    """
    return get_location_context(cfg), None


def is_location_available(cfg: Any = None) -> bool:
    """Whether a location is known at all.

    ``True`` only when a city is explicitly configured (and location is not
    disabled). There is no detection left that could be unavailable.
    """
    if not bool(_get(cfg, "location_enabled", True)):
        return False
    return get_configured_city(cfg) is not None
