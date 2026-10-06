"""Unit tests for :mod:`claire.senses.browser`.

No test in the UNIT section may launch a browser: the module's launcher
(``_launch_driver``) is monkeypatched, or a :class:`FakeDriver` is injected
directly into :class:`BrowserSession`. The one test that does drive real
chromium is marked ``integration`` and is therefore deselected by the unit
gate; it is run explicitly with ``-m integration``.

The gate is ``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no
unit marker -- hence ``pytestmark = pytest.mark.unit`` below. Do not remove it
and do not add this file to the guard's baseline list.
"""

import json
import threading
import time
from pathlib import Path

import pytest

from claire.senses import browser
from claire.senses.browser import (
    ActionResult,
    ActionTimeoutError,
    BrowserError,
    BrowserSession,
    BrowserUnavailableError,
    EmptyPageError,
    FindResult,
    NavigationError,
    NavigationTimeoutError,
    PageRead,
    SelectorNotFoundError,
    redact_value,
    secret_field,
    looks_like_secret,
)

pytestmark = pytest.mark.unit


# Playwright's own timeout type, so the classifier's name-based match is
# exercised the same way production will exercise it. The fallback keeps this
# file importable if playwright is ever absent (it is installed here).
try:  # pragma: no cover - the try branch is what runs on this machine
    from playwright.sync_api import TimeoutError as PlaywrightTimeout
except Exception:  # pragma: no cover
    class PlaywrightTimeout(Exception):
        """Stand-in named like playwright's TimeoutError for the classifier."""


class FakeTimeout(PlaywrightTimeout):
    """A playwright-shaped timeout raised by the fake driver."""


class FakeDriver(browser.BrowserDriver):
    """A driver that never touches a browser and records everything it is told."""

    def __init__(
        self,
        *,
        url="https://example.test/",
        title="Example",
        body_text="Hello there. This is a test page about Lisbon weather.",
        frames=(),
        links=3,
    ):
        self.url = url
        self.title = title
        self.body_text = body_text
        self.frames = list(frames)
        self.links = links
        self.calls = []
        self.closed_count = 0
        # failure injection
        self.navigate_error = None
        self.click_selector_error = None
        self.click_text_error = None
        self.fill_error = None
        self.back_error = None
        self.back_result = True
        # concurrency probe
        self._active = 0
        self.max_active = 0
        self._probe_lock = threading.Lock()
        self.read_delay = 0.0

    # -- readers ----------------------------------------------------------

    def navigate(self, url, timeout_ms):
        self.calls.append(("navigate", url, timeout_ms))
        if self.navigate_error is not None:
            raise self.navigate_error
        self.url = url

    def get_url(self):
        self.calls.append(("get_url",))
        return self.url

    def get_title(self):
        self.calls.append(("get_title",))
        return self.title

    def get_body_text(self):
        self.calls.append(("get_body_text",))
        if self.read_delay:
            with self._probe_lock:
                self._active += 1
                self.max_active = max(self.max_active, self._active)
            time.sleep(self.read_delay)
            with self._probe_lock:
                self._active -= 1
        return self.body_text

    def get_frame_texts(self):
        self.calls.append(("get_frame_texts",))
        return list(self.frames)

    def get_link_count(self):
        self.calls.append(("get_link_count",))
        return self.links

    # -- actions ----------------------------------------------------------

    def click_selector(self, selector, timeout_ms):
        self.calls.append(("click_selector", selector, timeout_ms))
        if self.click_selector_error is not None:
            raise self.click_selector_error

    def click_text(self, text, timeout_ms):
        self.calls.append(("click_text", text, timeout_ms))
        if self.click_text_error is not None:
            raise self.click_text_error

    #: The optional DOM-truth capability added for credential-safe filling.
    #: This fake models an ordinary page of text inputs (that is the fixture
    #: page above), so every selector reports 'text' unless a test overrides it
    #: with ``field_types``; ``None`` means "type cannot be determined" and a
    #: set ``field_type_error`` models a driver that fails the probe.
    field_types = {}
    field_type_error = None
    default_field_type = "text"

    def field_type(self, selector):
        self.calls.append(("field_type", selector))
        if self.field_type_error is not None:
            raise self.field_type_error
        if selector in self.field_types:
            return self.field_types[selector]
        return self.default_field_type

    def fill_field(self, selector, value, timeout_ms):
        self.calls.append(("fill_field", selector, value, timeout_ms))
        if self.fill_error is not None:
            raise self.fill_error

    def save_screenshot(self, path):
        self.calls.append(("save_screenshot", path))
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b"\x89PNG\r\n\x1a\n" + b"fake-png")

    def go_back(self, timeout_ms):
        self.calls.append(("go_back", timeout_ms))
        if self.back_error is not None:
            raise self.back_error
        return self.back_result

    def close(self):
        self.calls.append(("close",))
        self.closed_count += 1


def have(driver, name):
    """Return the arguments of the first recorded call named ``name``."""
    for call in driver.calls:
        if call[0] == name:
            return call[1:]
    return None


@pytest.fixture(autouse=True)
def _no_leaked_session():
    """Every test starts and ends with no process-wide browser."""
    browser.close_session()
    yield
    browser.close_session()


@pytest.fixture
def driver():
    return FakeDriver()


@pytest.fixture
def session(driver):
    return BrowserSession(driver, timeout_ms=1000)


# ---------------------------------------------------------------------------
# availability probe / named errors
# ---------------------------------------------------------------------------


def test_probe_reports_no_binary_when_none_installed(monkeypatch):
    monkeypatch.setattr(browser, "_chromium_candidates", lambda: [])
    assert browser.probe_chromium_binary() is None
    assert browser.browser_available() is False


def test_missing_binary_raises_named_error_naming_the_exact_fix(monkeypatch):
    monkeypatch.setattr(browser, "_chromium_candidates", lambda: [])
    with pytest.raises(BrowserUnavailableError) as caught:
        browser.ensure_browser_installed()
    message = str(caught.value)
    assert browser.CHROMIUM_INSTALL_COMMAND == "playwright install chromium"
    assert "playwright install chromium" in message
    assert caught.value.fix == "playwright install chromium"
    # The spoken form carries the fix too, and it is not a stack trace.
    assert "playwright install chromium" in caught.value.user_message()
    assert "Traceback" not in message


def test_launching_without_a_browser_raises_the_named_error(monkeypatch):
    """get_session() must surface the install command, not an ImportError."""
    monkeypatch.setattr(browser, "_chromium_candidates", lambda: [])
    with pytest.raises(BrowserUnavailableError) as caught:
        browser.get_session()
    assert "playwright install chromium" in str(caught.value)


def test_no_binary_leaves_no_session_behind(monkeypatch):
    monkeypatch.setattr(browser, "_chromium_candidates", lambda: [])
    with pytest.raises(BrowserUnavailableError):
        browser.get_session()
    assert browser.session_is_open() is False


def test_probe_finds_the_installed_chromium_on_this_machine():
    """MEASURED FACT: chromium IS installed at ~/.cache/ms-playwright/chromium-1243."""
    found = browser.probe_chromium_binary()
    if found is None:
        pytest.skip("chromium is not installed in this environment")
    assert Path(found).exists()


# ---------------------------------------------------------------------------
# one browser, one profile, one ordering
# ---------------------------------------------------------------------------


def test_get_session_builds_exactly_one_browser_for_the_process(monkeypatch, driver):
    launches = []

    def fake_launch(**kwargs):
        launches.append(kwargs)
        return driver

    monkeypatch.setattr(browser, "_launch_driver", fake_launch)
    first = browser.get_session()
    second = browser.get_session()
    assert first is second
    assert len(launches) == 1, "a second browser would fight over the profile lock"
    assert browser.session_is_open() is True


def test_closing_the_session_allows_a_fresh_one(monkeypatch, driver):
    launches = []

    def fake_launch(**kwargs):
        launches.append(kwargs)
        return driver

    monkeypatch.setattr(browser, "_launch_driver", fake_launch)
    first = browser.get_session()
    browser.close_session()
    assert browser.session_is_open() is False
    assert first.closed is True
    second = browser.get_session()
    assert second is not first
    assert len(launches) == 2


def test_single_session_is_the_default_even_for_an_empty_base_url():
    """get_session must not decide anything from caller-supplied page state."""
    assert browser.session_is_open() is False
    assert browser._SESSION is None


def test_profile_directory_is_one_place_and_can_be_overridden(monkeypatch, tmp_path):
    monkeypatch.setenv("CLAIRE_BROWSER_PROFILE_DIR", str(tmp_path / "profile"))
    resolved = browser._profile_dir()
    assert resolved == tmp_path / "profile"
    assert resolved.is_dir()


def test_operations_are_serialised_behind_one_lock(driver):
    driver.read_delay = 0.02
    session = BrowserSession(driver)
    errors = []

    def worker():
        try:
            session.read()
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    assert errors == []
    assert driver.max_active == 1, "one browser means one ordering"


# ---------------------------------------------------------------------------
# open / read
# ---------------------------------------------------------------------------


def test_open_returns_title_url_and_text(session, driver):
    page = session.open("https://example.test/weather")
    assert isinstance(page, PageRead)
    assert page.url == "https://example.test/weather"
    assert page.title == "Example"
    assert "Lisbon weather" in page.text
    assert ("navigate", "https://example.test/weather", 1000) in driver.calls


def test_open_adds_a_scheme_only_when_one_is_missing(session, driver):
    session.open("example.test/page")
    assert have(driver, "navigate")[0] == "https://example.test/page"
    driver.calls.clear()
    session.open("http://example.test/page")
    assert have(driver, "navigate")[0] == "http://example.test/page"
    driver.calls.clear()
    session.open("example.test:8080/page")
    assert have(driver, "navigate")[0] == "https://example.test:8080/page"


def test_read_returns_counts(session, driver):
    driver.body_text = "one two three four"
    driver.frames = ["frame one two", "frame three"]
    driver.links = 7
    page = session.read()
    assert page.char_count == len("one two three four")
    assert page.word_count == 4
    assert page.frame_count == 2
    assert page.link_count == 7
    assert page.frames == ("frame one two", "frame three")
    assert page.total_char_count == len("one two three four") + len("frame one two") + len("frame three")
    assert page.is_empty is False
    assert page.as_dict()["frame_count"] == 2


def test_read_on_a_page_with_no_text_is_its_own_named_outcome(session, driver):
    driver.body_text = "   \n  "
    with pytest.raises(EmptyPageError) as caught:
        session.read()
    assert "no visible text" in str(caught.value)
    # Distinct from a timeout and from a missing selector.
    assert not isinstance(caught.value, (NavigationTimeoutError, SelectorNotFoundError))
    # The raw answer stays reachable when a caller asks for it.
    page = session.read(allow_empty=True)
    assert page.is_empty is True
    assert page.text.strip() == ""


def test_open_reports_an_empty_page_the_same_way(session, driver):
    driver.body_text = ""
    with pytest.raises(EmptyPageError):
        session.open("https://example.test/blank")


def test_frame_text_is_included_when_cheaply_available(session, driver):
    driver.frames = ["embedded frame text"]
    page = session.read()
    assert page.frames == ("embedded frame text",)


# ---------------------------------------------------------------------------
# find: a hit and a miss are different answers, not an error
# ---------------------------------------------------------------------------


def test_find_reports_a_hit_with_a_rough_location(session, driver):
    driver.body_text = "x" * 10 + "Lisbon" + "y" * 200
    result = session.find("Lisbon")
    assert isinstance(result, FindResult)
    assert result.found is True
    assert result.count == 1
    assert result.region == "near the top of the page"
    assert result.in_frame is None
    assert "Lisbon" in result.snippet
    assert "Yes" in result.detail


def test_find_reports_a_miss_distinctly_and_without_raising(session, driver):
    hit = session.find("Lisbon")
    miss = session.find("Reykjavik")
    assert hit.found is True
    assert miss.found is False
    assert miss.count == 0
    assert miss.region == ""
    assert miss.in_frame is None
    assert "not on the current page" in miss.detail
    assert miss.as_dict()["found"] is False


def test_find_counts_repeats_and_reports_the_bottom_of_the_page(session, driver):
    driver.body_text = "z" * 400 + "Lisbon and Lisbon"
    result = session.find("lisbon")
    assert result.count == 2
    assert result.region == "towards the bottom of the page"


def test_find_reports_a_frame_hit_with_the_frame_index(session, driver):
    driver.body_text = "nothing here"
    driver.frames = ["a plain frame", "the Rivoli cinema is inside this frame"]
    result = session.find("Rivoli")
    assert result.found is True
    assert result.in_frame == 2
    assert "embedded frame 2" in result.detail


def test_find_needs_a_phrase(session):
    with pytest.raises(BrowserError) as caught:
        session.find("   ")
    assert "non-empty phrase" in str(caught.value)


# ---------------------------------------------------------------------------
# click / fill are dispatched, and the outcomes stay distinguishable
# ---------------------------------------------------------------------------


def test_click_dispatches_a_selector_and_a_text_separately(session, driver):
    by_selector = session.click("#submit")
    assert by_selector.action == "click"
    assert have(driver, "click_selector")[0] == "#submit"

    by_text = session.click("Sign in")
    assert have(driver, "click_text")[0] == "Sign in"
    assert have(driver, "click_text")[1] == 1000
    assert by_text.ok is True


def test_click_honours_an_explicit_by(session, driver):
    session.click("#submit", by="text")
    assert have(driver, "click_text")[0] == "#submit"
    with pytest.raises(BrowserError):
        session.click("#submit", by="magic")


def test_click_rejects_an_empty_target(session):
    with pytest.raises(BrowserError):
        session.click("  ")


def test_fill_dispatches_to_the_driver(session, driver):
    result = session.fill("#q", "weather in Lisbon")
    assert result.action == "fill"
    assert have(driver, "fill_field")[0] == "#q"
    assert have(driver, "fill_field")[1] == "weather in Lisbon"
    assert result.value_redacted == "weather in Lisbon"
    assert result.value_len == len("weather in Lisbon")
    assert result.secret is False


def test_a_timeout_and_a_missing_selector_are_distinguishable(session, driver):
    # 1. navigation timeout
    driver.navigate_error = FakeTimeout("Timeout 1000ms exceeded")
    with pytest.raises(NavigationTimeoutError) as timeout:
        session.open("https://example.test/slow")
    # 2. hard navigation failure (not a timeout)
    driver.navigate_error = RuntimeError("net::ERR_NAME_NOT_RESOLVED")
    with pytest.raises(NavigationError) as hard:
        session.open("https://nope.invalid/")
    # 3. selector genuinely absent
    driver.click_text_error = browser._ElementNotFound("no element shows 'Buy now'")
    with pytest.raises(SelectorNotFoundError) as missing:
        session.click("Buy now")
    # 4. element present but never became clickable
    driver.click_text_error = FakeTimeout("Timeout 1000ms exceeded")
    with pytest.raises(ActionTimeoutError) as stuck:
        session.click("Buy now")

    kinds = [type(e.value) for e in (timeout, hard, missing, stuck)]
    assert kinds == [NavigationTimeoutError, NavigationError,
                     SelectorNotFoundError, ActionTimeoutError]
    assert not isinstance(timeout.value, SelectorNotFoundError)
    assert not isinstance(missing.value, ActionTimeoutError)
    assert not isinstance(stuck.value, SelectorNotFoundError)
    assert "did not finish loading" in str(timeout.value)
    assert "no element" in str(missing.value)


def test_fill_maps_not_found_and_timeout_too(session, driver):
    driver.fill_error = browser._ElementNotFound("no input matches '#pw'")
    with pytest.raises(SelectorNotFoundError):
        session.fill("#pw", "x")
    driver.fill_error = FakeTimeout("Timeout")
    with pytest.raises(ActionTimeoutError):
        session.fill("#pw", "x")


def test_every_named_error_can_be_caught_as_a_browser_error(session, driver):
    driver.navigate_error = FakeTimeout("Timeout")
    with pytest.raises(BrowserError):
        session.open("https://example.test/")


# ---------------------------------------------------------------------------
# screenshot
# ---------------------------------------------------------------------------


def test_screenshot_writes_to_the_given_path(session, driver, tmp_path):
    target = tmp_path / "shots" / "page.png"
    result = session.screenshot(str(target))
    assert result.path == str(target)
    assert target.is_file()
    assert target.read_bytes().startswith(b"\x89PNG")
    assert have(driver, "save_screenshot")[0] == str(target)


def test_screenshot_refuses_a_directory_and_an_empty_path(session, tmp_path):
    with pytest.raises(BrowserError):
        session.screenshot(str(tmp_path))
    with pytest.raises(BrowserError):
        session.screenshot("")


# ---------------------------------------------------------------------------
# back
# ---------------------------------------------------------------------------


def test_back_reports_the_previous_page(session, driver):
    driver.title = "Previous"
    page = session.back()
    assert page.title == "Previous"
    assert have(driver, "go_back")[0] == 1000


def test_back_with_no_history_is_not_a_timeout(session, driver):
    driver.back_result = False
    with pytest.raises(NavigationError) as caught:
        session.back()
    assert not isinstance(caught.value, NavigationTimeoutError)
    assert "no earlier page" in str(caught.value)


def test_back_timeout_is_named(session, driver):
    driver.back_error = FakeTimeout("Timeout")
    with pytest.raises(NavigationTimeoutError):
        session.back()


# ---------------------------------------------------------------------------
# secrets: the hard rule
# ---------------------------------------------------------------------------


def test_redaction_helper_masks_a_secret():
    assert redact_value("hunter2", secret=True) == "[REDACTED]"
    assert redact_value("sk-abcdefghijklmnopqrstuvwxyz0123456789") == "[REDACTED]"
    assert redact_value("correct horse battery staple", secret=False) == \
        "correct horse battery staple"


def test_looks_like_secret_recognises_credentials_but_not_prose():
    assert looks_like_secret("sk-abcdefghijklmnopqrstuvwxyz0123456789") is True
    assert looks_like_secret("user@example.com") is True
    assert looks_like_secret("my password is hunter2") is True
    assert looks_like_secret("a8f3k2m9x1q7z4b6") is True
    assert looks_like_secret("") is False
    assert looks_like_secret(None) is False
    assert looks_like_secret("weather in Lisbon") is False


def test_secret_field_recognises_credential_inputs():
    assert secret_field("input[type=password]") is True
    assert secret_field("#password") is True
    assert secret_field("input[name=api_key]") is True
    assert secret_field("#search") is False
    assert secret_field("") is False


def test_a_secret_never_reaches_the_logs_or_the_returned_structure(session, driver, monkeypatch):
    logged = []
    monkeypatch.setattr(browser, "debug_log", lambda message, category="debug": logged.append(message))
    secret = "hunter2-the-real-password"

    result = session.fill("#password", secret)

    # 1. it DID reach the driver (redaction is about reporting, not about lying)
    assert have(driver, "fill_field")[1] == secret
    # 2. it is nowhere in the logs
    assert logged, "fill must log something"
    assert all(secret not in line for line in logged)
    assert any("[REDACTED]" in line for line in logged)
    # 3. it is nowhere in the returned structure
    assert secret not in repr(result)
    assert secret not in json.dumps(result.as_dict())
    assert secret not in str(result.as_dict())
    assert result.value_redacted == "[REDACTED]"
    assert result.secret is True
    assert result.value_len == len(secret)


def test_a_secret_shaped_value_is_masked_even_in_an_innocent_field(session, driver, monkeypatch):
    logged = []
    monkeypatch.setattr(browser, "debug_log", lambda message, category="debug": logged.append(message))
    token = "sk-live-abcdefghijklmnopqrstuvwxyz012345"
    result = session.fill("#search", token)
    assert all(token not in line for line in logged)
    assert token not in repr(result)
    assert result.secret is True


def test_a_password_word_in_prose_is_masked_too(session, driver, monkeypatch):
    logged = []
    monkeypatch.setattr(browser, "debug_log", lambda message, category="debug": logged.append(message))
    text = "my password is swordfish"
    result = session.fill("#note", text)
    assert all(text not in line for line in logged)
    assert result.secret is True


def test_non_secret_fill_is_still_logged_in_the_clear(session, driver, monkeypatch):
    """Redaction must not become a mute button: debug logging stays useful."""
    logged = []
    monkeypatch.setattr(browser, "debug_log", lambda message, category="debug": logged.append(message))
    session.fill("#q", "weather in Lisbon")
    assert any("weather in Lisbon" in line for line in logged)


def test_open_and_read_never_log_a_credential_from_the_page(session, driver, monkeypatch):
    logged = []
    monkeypatch.setattr(browser, "debug_log", lambda message, category="debug": logged.append(message))
    driver.url = "https://example.test/reset?token=abcd1234abcd1234"
    session.read()
    assert all("abcd1234abcd1234" not in line for line in logged)


# ---------------------------------------------------------------------------
# lifecycle: never leave an orphan chromium holding the profile lock
# ---------------------------------------------------------------------------


def test_close_is_idempotent(session, driver):
    session.close()
    session.close()
    session.close()
    assert driver.closed_count == 1
    assert session.closed is True


def test_context_manager_closes_even_when_the_body_raises(driver):
    with pytest.raises(ValueError):
        with BrowserSession(driver) as live:
            assert live.closed is False
            raise ValueError("boom")
    assert driver.closed_count == 1


def test_actions_after_close_raise_a_named_error(session):
    session.close()
    for call in (
        lambda: session.open("https://example.test/"),
        session.read,
        lambda: session.find("x"),
        lambda: session.click("#a"),
        lambda: session.fill("#a", "x"),
        lambda: session.screenshot("/tmp/never.png"),
        session.back,
    ):
        with pytest.raises(BrowserError) as caught:
            call()
        assert "already closed" in str(caught.value)


def test_a_reader_that_explodes_does_not_lose_the_whole_reading(session, driver):
    def broken():
        raise RuntimeError("detached frame")

    driver.get_link_count = broken  # type: ignore[assignment]
    page = session.read()
    assert "Lisbon" in page.text
    assert page.link_count == 0


def test_close_of_the_driver_is_reached_through_the_module_helper(monkeypatch, driver):
    monkeypatch.setattr(browser, "_launch_driver", lambda **kwargs: driver)
    browser.get_session()
    assert driver.closed_count == 0
    browser.reset_session()
    assert driver.closed_count == 1
    browser.reset_session()
    assert driver.closed_count == 1


def test_as_dicts_are_json_serialisable(session):
    page = session.read()
    assert json.dumps(page.as_dict())
    assert json.dumps(session.find("Lisbon").as_dict())
    assert json.dumps(ActionResult(action="click", target="#a").as_dict())


# ---------------------------------------------------------------------------
# real-driver teardown: PlaywrightContextManager has no close(), and skipping
# its __exit__ leaves a running event loop behind (found by running the real
# smoke test -- the second browser launch died with "Sync API inside the
# asyncio loop"). This unit test pins the fix without launching a browser.
# ---------------------------------------------------------------------------


def _bare_driver():
    """A PlaywrightDriver with its playwright objects faked out."""
    return browser.PlaywrightDriver.__new__(browser.PlaywrightDriver)


def test_driver_close_stops_the_context_and_playwright(monkeypatch):
    calls = []

    class FakeContext:
        def close(self):
            calls.append("context")

    class FakePlaywright:
        def stop(self):
            calls.append("playwright")

    class FakeContextManager:
        def __exit__(self, *args):
            calls.append("__exit__")

    driver = _bare_driver()
    driver._context = FakeContext()
    driver._playwright = FakePlaywright()
    driver._pw_cm = FakeContextManager()
    driver._closed = False

    driver.close()
    driver.close()  # idempotent at the driver level too

    assert calls == ["context", "playwright"], (
        "missing the playwright stop leaves a running event loop behind"
    )


def test_driver_close_falls_back_to_the_context_manager_exit():
    calls = []

    class FakeContext:
        def close(self):
            calls.append("context")

    class FakeContextManager:
        def __exit__(self, *args):
            calls.append("__exit__")

    driver = _bare_driver()
    driver._context = FakeContext()
    driver._playwright = None
    driver._pw_cm = FakeContextManager()
    driver._closed = False

    driver.close()
    assert calls == ["context", "__exit__"]


def test_driver_close_survives_a_context_that_explodes():
    class ExplodingContext:
        def close(self):
            raise RuntimeError("already gone")

    driver = _bare_driver()
    driver._context = ExplodingContext()
    driver._playwright = None
    driver._pw_cm = None
    driver._closed = False
    driver.close()  # must not raise
    assert driver._context is None


def test_launch_failure_is_translated_to_a_named_actionable_error():
    locked = browser._translate_launch_failure(
        RuntimeError("Failed to launch: ProcessSingleton lock is held"), "/tmp/p"
    )
    assert isinstance(locked, browser.BrowserProfileLockedError)
    assert "/tmp/p" in str(locked)
    assert locked.fix and "CLAIRE_BROWSER_PROFILE_DIR" in locked.fix

    missing = browser._translate_launch_failure(
        RuntimeError("Executable doesn't exist at /nope/chrome"), "/tmp/p"
    )
    assert isinstance(missing, BrowserUnavailableError)
    assert "playwright install chromium" in str(missing)

    other = browser._translate_launch_failure(RuntimeError("something else"), "/tmp/p")
    assert type(other) is BrowserError
    assert "something else" in str(other)


def test_the_profile_directory_is_one_shared_place(monkeypatch, tmp_path):
    """One browser means one profile: a second one would exit doing nothing."""
    monkeypatch.delenv("CLAIRE_BROWSER_PROFILE_DIR", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path / "data"))
    first = browser._profile_dir()
    second = browser._profile_dir()
    assert first == second == tmp_path / "data" / "claire" / "browser-profile"
    assert first.is_dir()


# ---------------------------------------------------------------------------
# REAL smoke test: chromium IS installed on this machine. Deselected by the
# unit gate on purpose; run it with `-m integration`.
# ---------------------------------------------------------------------------


@pytest.fixture
def local_site(tmp_path):
    """A real HTTP server on 127.0.0.1 so navigation has real responses.

    Only the integration tests request this fixture, so the unit gate never
    opens a socket.
    """
    import functools
    import http.server
    import socketserver

    (tmp_path / "index.html").write_text(
        "<html><head><title>Claire Smoke</title></head><body>"
        "<h1>Claire smoke page</h1>"
        "<p>The Rivoli theatre is mentioned here.</p>"
        "<a href='second.html'>second page</a>"
        "<form><input id='q' name='q' type='text'>"
        "<button id='go' type='button' onclick=\"document.title='Clicked OK'\">Go</button>"
        "</form></body></html>"
    )
    (tmp_path / "second.html").write_text(
        "<html><head><title>Second Page</title></head><body><p>second</p></body></html>"
    )

    class QuietHandler(http.server.SimpleHTTPRequestHandler):
        def log_message(self, *args):  # keep the test output clean
            pass

    handler = functools.partial(QuietHandler, directory=str(tmp_path))
    server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.mark.integration
def test_real_chromium_renders_clicks_fills_and_screenshots(local_site, tmp_path):
    """Drive a real browser over a real local HTTP server (no network needed)."""
    base = local_site
    with browser.get_session(headless=True, timeout_ms=20_000) as live:
        page = live.open(base + "index.html")
        assert page.title == "Claire Smoke"
        assert "Rivoli theatre" in page.text
        assert page.char_count > 0
        assert page.link_count >= 1

        hit = live.find("Rivoli")
        assert hit.found is True and hit.region
        miss = live.find("Reykjavik")
        assert miss.found is False

        click = live.click("#go")
        assert click.ok is True
        assert live.read().title == "Clicked OK"

        filled = live.fill("#q", "hello from claire")
        assert filled.ok is True
        assert filled.value_redacted == "hello from claire"

        shot = tmp_path / "smoke.png"
        live.screenshot(str(shot))
        assert shot.read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"

        second = live.open(base + "second.html")
        assert second.title == "Second Page"
        back = live.back()
        assert back.title == "Claire Smoke"
    assert browser.session_is_open() is False


@pytest.mark.integration
def test_real_chromium_missing_selector_is_reported_as_not_found():
    with browser.get_session(headless=True, timeout_ms=10_000) as live:
        live.open("data:text/html,<html><body><p>nothing to click</p></body></html>")
        with pytest.raises(SelectorNotFoundError):
            live.click("#does-not-exist")
        with pytest.raises(EmptyPageError):
            live.open("data:text/html,<html><body></body></html>")
