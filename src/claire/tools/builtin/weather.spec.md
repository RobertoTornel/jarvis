## getWeather Spec

### Purpose

Answer "what is the weather in <place>?" with a real reading from Open-Meteo (no API key), so weather questions never ride on web search — whose DuckDuckGo path can hit a bot challenge and fall back to Wikipedia, returning population figures instead of a temperature.

### Problem

Claire had no weather tool. The planner reached for `webSearch`, the scraping was blocked, the silent Wikipedia fallback produced Lisbon's population and a Europe heatwave summary, and the assistant had no temperature to report. A weather question needs a deterministic data source, not a search engine.

### Design

`getWeather` is LLM-free and keyless:

1. **Geocode** the place name via `https://geocoding-api.open-meteo.com/v1/search?name=<place>&count=1` → `results[0]` with `latitude`, `longitude`, `name`, `country`.
2. **Forecast** via `https://api.open-meteo.com/v1/forecast` with `current=temperature_2m,apparent_temperature,relative_humidity_2m,weather_code,wind_speed_10m`, `daily=temperature_2m_max,temperature_2m_min,precipitation_probability_max`, `forecast_days=3`, `timezone=auto`.
3. **Format** a readable sentence: place, temperature, feels-like, conditions, humidity, wind, then a three-day outlook.

`weather_code` is the WMO code and maps to short British-English descriptions (0 clear; 1–3 mainly clear → overcast; 45/48 fog; 51–57 drizzle; 61–67 rain; 71–77 snow; 80–82 showers; 85–86 snow showers; 95–99 thunderstorm). Timeouts use `cfg.llm_tools_timeout_sec` (default 8.0 s), exactly like `getTime`.

### Contract

- **Name**: `getWeather`
- **Input schema**: one OPTIONAL `location` string. Omitted/empty → fall back to the configured `location_city` via `get_configured_city(cfg)` (same source `getTime` uses).
- **Success**: `ToolExecutionResult(success=True, reply_text=...)` where `reply_text` contains the geocoded place (e.g. "Lisbon, Portugal") and the real reading, e.g. `Weather in Lisbon, Portugal: 24.3°C, feels like 25.6°C, mainly clear, humidity 61%, wind 14 km/h. Forecast: …`.
- **Progress**: `context.user_print` lines with emoji — `🌤️ Checking weather…`, `✅ <summary>` on success, `⚠️ …` on failure.

### Failure modes (all honest, no fabrication)

| Condition | Result |
| :--- | :--- |
| No `location` and no configured city | `success=False`, names the missing location; no network call |
| Geocoding returns no results | `success=False`, `Could not find place '<given>'` — the given place is named |
| Timeout / HTTP error / transport error | `success=False`, says the service is unavailable or slow; **never** a temperature |
| Forecast payload without `temperature_2m` | `success=False`, says no temperature was returned |
| Unknown WMO code | Reply stays `success=True` but reports `unrecognised conditions (weather code N)` — the raw code, not a guessed condition |
| Any other exception | Caught and wrapped into `success=False` with a debug log |

### No-fabrication rule

The reply text may only contain values the API returned. If the reading is missing or the request failed, the tool says exactly that and instructs the assistant not to invent a temperature. (This is the guard that stopped the old failure chain recorded in `reply/reply.spec.md` — a failed `getWeather` must stay a failure, never a plausible guess.)

### Testing

`tests/test_weather_tool.py` (`pytestmark = pytest.mark.unit`) covers the success sentence, the configured-city fallback, the unknown-place failure, the network/HTTP failure with no fabricated temperature, known-code mapping, and honest degradation of an unknown code. All HTTP is monkeypatched — unit tests never call the live API.
