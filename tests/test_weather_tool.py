"""Tests for the getWeather tool (Open-Meteo, no API key).

Contract: ``src/claire/tools/builtin/weather.spec.md``. No network in here —
every HTTP call goes through a fake ``requests.get`` so the tests are
deterministic and can never fabricate or fetch live weather.
"""

from unittest.mock import Mock

import pytest
import requests

from src.claire.tools.base import ToolContext
from src.claire.tools.builtin.weather import WeatherTool, describe_weather_code
from src.claire.tools.registry import BUILTIN_TOOLS

# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`

GEO_URL_PREFIX = "https://geocoding-api.open-meteo.com"
FORECAST_URL_PREFIX = "https://api.open-meteo.com"

GEO_LISBON = {
    "results": [
        {
            "latitude": 38.7223,
            "longitude": -9.1393,
            "name": "Lisbon",
            "country": "Portugal",
            "timezone": "Europe/Lisbon",
        }
    ]
}

FORECAST_LISBON = {
    "current": {
        "temperature_2m": 24.3,
        "apparent_temperature": 25.6,
        "relative_humidity_2m": 61,
        "weather_code": 1,
        "wind_speed_10m": 14.2,
    },
    "daily": {
        "time": ["2026-07-01", "2026-07-02", "2026-07-03"],
        "temperature_2m_max": [28.4, 27.1, 26.0],
        "temperature_2m_min": [18.2, 17.5, 16.9],
        "precipitation_probability_max": [5, 10, 20],
    },
}


class _FakeResponse:
    def __init__(self, payload):
        self._payload = payload

    def raise_for_status(self):
        return None

    def json(self):
        return self._payload


def make_fake_get(geocode=GEO_LISBON, forecast=FORECAST_LISBON, error=None):
    """A deterministic stand-in for ``requests.get`` — no network here."""

    def fake_get(url, params=None, timeout=None, **kwargs):
        if error is not None:
            raise error
        if url.startswith(GEO_URL_PREFIX):
            return _FakeResponse(geocode)
        if url.startswith(FORECAST_URL_PREFIX):
            return _FakeResponse(forecast)
        raise AssertionError(f"unexpected URL in unit test: {url}")

    return fake_get


def make_context(city=""):
    ctx = Mock(spec=ToolContext)
    ctx.user_print = Mock()
    ctx.cfg = Mock()
    ctx.cfg.llm_tools_timeout_sec = 8.0
    ctx.cfg.location_city = city
    ctx.redacted_text = ""
    return ctx


class TestWeatherToolProperties:
    def test_tool_properties(self):
        """camelCase name, weather-focused description, optional location."""
        tool = WeatherTool()
        assert tool.name == "getWeather"
        assert "weather" in tool.description.lower()
        assert tool.inputSchema["type"] == "object"
        assert "location" in tool.inputSchema["properties"]
        assert tool.inputSchema["required"] == []

    def test_registered_in_builtin_tools(self):
        """The router must be able to select getWeather."""
        assert "getWeather" in BUILTIN_TOOLS
        assert isinstance(BUILTIN_TOOLS["getWeather"], WeatherTool)


class TestWeatherSuccess:
    def test_success_formats_readable_sentence(self, monkeypatch):
        """A successful lookup reports the temperature and the place."""
        monkeypatch.setattr(requests, "get", make_fake_get())
        tool = WeatherTool()
        context = make_context()
        result = tool.run({"location": "Lisbon"}, context)
        assert result.success is True
        assert result.error_message is None
        assert "Lisbon" in result.reply_text
        assert "24.3°C" in result.reply_text
        # known code 1 maps to a real description
        assert "mainly clear" in result.reply_text
        printed = [str(c.args[0]) for c in context.user_print.call_args_list]
        assert any(p.startswith("🌤️") for p in printed)
        assert any(p.startswith("✅") for p in printed)

    def test_no_location_falls_back_to_configured_city(self, monkeypatch):
        """No argument -> the configured ``location_city`` is used."""
        seen = {}

        def fake_get(url, params=None, timeout=None, **kwargs):
            if url.startswith(GEO_URL_PREFIX):
                seen["name"] = (params or {}).get("name")
                return _FakeResponse(GEO_LISBON)
            return _FakeResponse(FORECAST_LISBON)

        monkeypatch.setattr(requests, "get", fake_get)
        tool = WeatherTool()
        context = make_context(city="Lisbon")
        result = tool.run({}, context)
        assert result.success is True
        assert seen["name"] == "Lisbon"
        assert "Lisbon" in result.reply_text


class TestWeatherFailures:
    def test_no_geocoding_result_names_the_place(self, monkeypatch):
        """A place Open-Meteo does not know fails honestly, naming it."""
        monkeypatch.setattr(
            requests, "get", make_fake_get(geocode={"results": []})
        )
        tool = WeatherTool()
        result = tool.run({"location": "Atlantis"}, make_context())
        assert result.success is False
        assert "Atlantis" in result.reply_text
        assert "°C" not in (result.reply_text or "")

    def test_no_location_and_no_configured_city_is_honest(self):
        """No argument and no configured city: fail, do not guess a place."""
        tool = WeatherTool()
        context = make_context(city="")
        result = tool.run({}, context)
        assert result.success is False
        assert result.reply_text
        context.user_print.assert_called_once()

    def test_network_error_returns_failure_never_a_temperature(self, monkeypatch):
        """A transport failure must not produce a fabricated reading."""
        monkeypatch.setattr(
            requests,
            "get",
            make_fake_get(error=requests.exceptions.ConnectionError("boom")),
        )
        tool = WeatherTool()
        result = tool.run({"location": "Lisbon"}, make_context())
        assert result.success is False
        reply = result.reply_text or ""
        assert "Lisbon" in reply
        assert "24.3" not in reply
        assert "°C" not in reply

    def test_http_error_status_returns_failure(self, monkeypatch):
        """An HTTP 500 from the forecast endpoint is an honest failure."""

        class _Boom(_FakeResponse):
            def raise_for_status(self):
                raise requests.exceptions.HTTPError("500 Server Error")

        def fake_get(url, params=None, timeout=None, **kwargs):
            if url.startswith(GEO_URL_PREFIX):
                return _FakeResponse(GEO_LISBON)
            return _Boom(FORECAST_LISBON)

        monkeypatch.setattr(requests, "get", fake_get)
        tool = WeatherTool()
        result = tool.run({"location": "Lisbon"}, make_context())
        assert result.success is False
        assert "°C" not in (result.reply_text or "")


class TestWeatherCodeMapping:
    def test_known_codes(self):
        """A couple of WMO codes map to short British-English descriptions."""
        assert "clear" in describe_weather_code(0)
        assert "fog" in describe_weather_code(45)
        assert "rain" in describe_weather_code(63)
        assert "snow" in describe_weather_code(75)
        assert "thunderstorm" in describe_weather_code(95)

    def test_unknown_code_degrades_honestly(self, monkeypatch):
        """An unmapped code reports itself as unrecognised — never a guess."""
        assert "unrecognised" in describe_weather_code(42)
        assert "42" in describe_weather_code(42)
        for guess in ("clear", "rain", "snow", "fog", "thunderstorm"):
            assert guess not in describe_weather_code(42)

        # ...and through the full tool path with a weird live code.
        weird = dict(FORECAST_LISBON)
        weird["current"] = dict(FORECAST_LISBON["current"], weather_code=4242)
        monkeypatch.setattr(
            requests, "get", make_fake_get(forecast=weird)
        )
        result = WeatherTool().run({"location": "Lisbon"}, make_context())
        assert result.success is True
        assert "unrecognised" in result.reply_text
        assert "4242" in result.reply_text
