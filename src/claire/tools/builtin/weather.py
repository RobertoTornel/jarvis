"""Current weather for a named place, via Open-Meteo.

Geocodes the place with the free Open-Meteo geocoding endpoint (no API key),
then reads current conditions and a three-day outlook from the forecast
endpoint. WMO ``weather_code`` values are mapped to short British-English
descriptions; an unmapped code is reported as unrecognised rather than
guessed.

The tool is deliberately LLM-free: it formats exactly what the API returned.
On any failure — unknown place, timeout, HTTP error, missing temperature — it
returns an honest ``ToolExecutionResult`` failure and NEVER a fabricated
reading. See ``weather.spec.md`` next to this file.
"""

from typing import Any, Dict, Optional

import requests

from ...debug import debug_log
from ...utils.location import get_configured_city
from ..base import Tool, ToolContext
from ..types import ToolExecutionResult

# Free endpoints, no API key (same provider the getTime tool geocodes with).
_GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# Current conditions read for the reply.
_CURRENT_VARS = (
    "temperature_2m,apparent_temperature,relative_humidity_2m,"
    "weather_code,wind_speed_10m"
)
_DAILY_VARS = "temperature_2m_max,temperature_2m_min,precipitation_probability_max"

# WMO weather codes -> short British-English descriptions. Codes outside
# this table degrade honestly via ``describe_weather_code`` — we report the
# raw code as unrecognised instead of guessing a condition.
_WMO_DESCRIPTIONS: Dict[int, str] = {
    0: "clear sky",
    1: "mainly clear",
    2: "partly cloudy",
    3: "overcast",
    45: "fog",
    48: "depositing rime fog",
    51: "light drizzle",
    53: "moderate drizzle",
    55: "dense drizzle",
    56: "light freezing drizzle",
    57: "dense freezing drizzle",
    61: "light rain",
    63: "moderate rain",
    65: "heavy rain",
    66: "light freezing rain",
    67: "heavy freezing rain",
    71: "light snow fall",
    73: "moderate snow fall",
    75: "heavy snow fall",
    77: "snow grains",
    80: "light rain showers",
    81: "moderate rain showers",
    82: "heavy rain showers",
    85: "light snow showers",
    86: "heavy snow showers",
    95: "thunderstorm",
    96: "thunderstorm with light hail",
    99: "thunderstorm with heavy hail",
}


def describe_weather_code(code: Any) -> str:
    """Map a WMO ``weather_code`` to a short British-English description.

    Unknown or malformed codes degrade honestly: the raw code is reported
    as unrecognised conditions, never guessed at.
    """
    try:
        key = int(code)
    except (TypeError, ValueError):
        return "unknown conditions (weather code not reported)"
    description = _WMO_DESCRIPTIONS.get(key)
    if description is None:
        return f"unrecognised conditions (weather code {key})"
    return description


def _geocode(location: str, timeout_sec: float) -> Optional[Dict[str, Any]]:
    """Resolve a place name to its first geocoding result, or None."""
    params = {
        "name": location.strip(),
        "count": 1,
        "language": "en",
        "format": "json",
    }
    response = requests.get(_GEOCODING_URL, params=params, timeout=timeout_sec)
    response.raise_for_status()
    results = response.json().get("results") or []
    return results[0] if results else None


def _fetch_forecast(latitude: Any, longitude: Any, timeout_sec: float) -> Dict[str, Any]:
    """Fetch current conditions plus a three-day outlook for a coordinate."""
    params = {
        "latitude": latitude,
        "longitude": longitude,
        "current": _CURRENT_VARS,
        "daily": _DAILY_VARS,
        "forecast_days": 3,
        "timezone": "auto",
    }
    response = requests.get(_FORECAST_URL, params=params, timeout=timeout_sec)
    response.raise_for_status()
    data = response.json()
    return data if isinstance(data, dict) else {}


def _display_name(place: Dict[str, Any], fallback: str) -> str:
    """Geocoded display name: "Lisbon, Portugal" when the API gave a country."""
    name = str(place.get("name") or fallback).strip() or fallback
    country = str(place.get("country") or "").strip()
    if country and country.lower() != name.lower():
        return f"{name}, {country}"
    return name


def _format_daily(daily: Dict[str, Any]) -> str:
    """Three-day outlook as one compact sentence fragment ("" when absent)."""
    times = daily.get("time") or []
    if not isinstance(times, list) or not times:
        return ""
    highs = daily.get("temperature_2m_max") or []
    lows = daily.get("temperature_2m_min") or []
    rain = daily.get("precipitation_probability_max") or []

    def _num(seq, index):
        if index < len(seq) and isinstance(seq[index], (int, float)):
            return float(seq[index])
        return None

    bits = []
    for i, day in enumerate(times[:3]):
        bit = str(day)
        lo, hi = _num(lows, i), _num(highs, i)
        if lo is not None and hi is not None:
            bit += f" {lo:.1f}–{hi:.1f}°C"
        prob = _num(rain, i)
        if prob is not None:
            bit += f", rain {prob:.0f}%"
        bits.append(bit)
    return "Forecast: " + "; ".join(bits) + "."


def _format_weather(display: str, data: Dict[str, Any]) -> Optional[str]:
    """Readable sentence from the forecast payload; None when no temperature.

    None means the API answered but carried no ``temperature_2m`` — the
    caller turns that into an honest failure rather than inventing a value.
    """
    current = data.get("current") or {}
    temp = current.get("temperature_2m")
    if not isinstance(temp, (int, float)):
        return None

    parts = [f"{float(temp):.1f}°C"]
    apparent = current.get("apparent_temperature")
    if isinstance(apparent, (int, float)):
        parts.append(f"feels like {float(apparent):.1f}°C")
    parts.append(describe_weather_code(current.get("weather_code")))
    humidity = current.get("relative_humidity_2m")
    if isinstance(humidity, (int, float)):
        parts.append(f"humidity {humidity:.0f}%")
    wind = current.get("wind_speed_10m")
    if isinstance(wind, (int, float)):
        parts.append(f"wind {wind:.0f} km/h")

    reply = f"Weather in {display}: " + ", ".join(parts) + "."
    outlook = _format_daily(data.get("daily") or {})
    if outlook:
        reply += " " + outlook
    return reply


class WeatherTool(Tool):
    """Tool for getting the current weather and short outlook for a place."""

    @property
    def name(self) -> str:
        return "getWeather"

    @property
    def description(self) -> str:
        return (
            "Current weather for a city: temperature, feels-like, conditions, "
            "humidity, wind and a three-day outlook (e.g. 'what is the weather "
            "in Lisbon?'). Set location when the user names a place; if "
            "omitted it falls back to the configured home city. Reports only "
            "what Open-Meteo returns — never a guessed reading."
        )

    @property
    def inputSchema(self) -> Dict[str, Any]:
        return {
            "type": "object",
            "properties": {
                "location": {
                    "type": "string",
                    "description": (
                        "OPTIONAL. City or place to check, e.g. 'Lisbon', "
                        "'Tokyo', 'Reykjavik'. If omitted, uses the user's "
                        "configured city."
                    ),
                }
            },
            "required": [],
        }

    def run(self, args: Optional[Dict[str, Any]], context: ToolContext) -> ToolExecutionResult:
        """Get the current weather for the requested place (or the configured city)."""
        location_str = ""
        if args and isinstance(args, dict):
            raw_location = args.get("location")
            location_str = str(raw_location).strip() if raw_location else ""

        if not location_str:
            city = get_configured_city(cfg=getattr(context, "cfg", None))
            if city:
                location_str = city
            else:
                context.user_print("⚠️ No location configured.")
                return ToolExecutionResult(
                    success=False,
                    reply_text=(
                        "No location to check: none was given and no city is "
                        "configured. Ask which city the user means, or set "
                        "'location_city' in the config."
                    ),
                    error_message="getWeather: no location and no configured city",
                )

        context.user_print("🌤️ Checking weather...")

        timeout_sec = 8.0
        if context.cfg is not None:
            timeout_sec = float(getattr(context.cfg, "llm_tools_timeout_sec", 8.0))

        try:
            place = _geocode(location_str, timeout_sec)
            if place is None:
                context.user_print(f"⚠️ Place not found: {location_str}")
                return ToolExecutionResult(
                    success=False,
                    reply_text=(
                        f"Could not find place '{location_str}'. "
                        "Try a different city name or spelling."
                    ),
                )
            display = _display_name(place, location_str)
            data = _fetch_forecast(place.get("latitude"), place.get("longitude"), timeout_sec)
            reply_text = _format_weather(display, data)
            if reply_text is None:
                context.user_print(f"⚠️ No temperature for {display}.")
                return ToolExecutionResult(
                    success=False,
                    reply_text=(
                        f"The weather service returned no temperature for "
                        f"'{display}'. Report that exactly — do not guess a "
                        "reading."
                    ),
                )
            context.user_print(f"✅ {reply_text.split(' Forecast:')[0]}")
            return ToolExecutionResult(success=True, reply_text=reply_text)
        except requests.exceptions.Timeout:
            context.user_print("⚠️ Weather service timeout.")
            debug_log(f"getWeather: timeout for '{location_str}'", category="tools")
            return ToolExecutionResult(
                success=False,
                reply_text=(
                    "The weather service is taking too long to respond for "
                    f"'{location_str}'. Say so and offer to retry."
                ),
                error_message="getWeather: timeout",
            )
        except requests.exceptions.RequestException as exc:
            context.user_print("⚠️ Weather service unavailable.")
            debug_log(
                f"getWeather: request failed for '{location_str}': "
                f"{exc.__class__.__name__}: {exc}",
                category="tools",
            )
            return ToolExecutionResult(
                success=False,
                reply_text=(
                    "The weather service is temporarily unavailable, so no "
                    f"reading could be fetched for '{location_str}'. Say "
                    "exactly that — do not invent a temperature."
                ),
                error_message=f"getWeather: request failed ({exc.__class__.__name__})",
            )
        except Exception as exc:  # noqa: BLE001 - honest failure beats a crash
            context.user_print("⚠️ Error getting weather.")
            debug_log(
                f"getWeather: unexpected error for '{location_str}': "
                f"{exc.__class__.__name__}: {exc}",
                category="tools",
            )
            return ToolExecutionResult(
                success=False,
                reply_text=(
                    "Something went wrong fetching the weather for "
                    f"'{location_str}'; no reading is available."
                ),
                error_message=f"getWeather: {exc.__class__.__name__}",
            )
