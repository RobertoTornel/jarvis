"""Fail-proven tests for credential-safe filling in :mod:`claire.senses.browser`.

WHY THIS FILE EXISTS (the leak it was written to catch)
-------------------------------------------------------
An independent verification reproduced this against the shipping code::

    s.open('data:text/html,...<input id="pw" type="password">...')
    r = s.fill('#pw', 'Sup3rSecretValue!42')
    r.value_redacted  ->  'Sup3rSecretValue!42'     # the secret, in plaintext

and the same string went into the debug log. Two independent holes made that
possible: the field-name heuristic matched ``pwd``/``pass...`` but not ``pw``
(one of the most common password-field ids in the world), and the value
backstop missed a strong password because it required a pure alnum run while
``Sup3rSecretValue!42`` contains punctuation.

The fix is not "add one more regex". Correctness now rests on what the DOM
says the field IS, and on failing closed when that cannot be established:

* DOM truth -- :meth:`BrowserDriver.field_type` reports the element's ``type``;
  ``password`` means secret regardless of the selector and regardless of what
  the value looks like;
* fail closed -- if the type cannot be determined (no capability, no element,
  any error) the value is a secret unless the caller positively identified a
  harmless field (a reported/declared non-secret input type);
* :func:`looks_like_secret` remains only a BACKSTOP, and the field-name pattern
  is a hint, not the guarantee.

MARKERS
-------
``pytest tests/ -m unit`` SILENTLY DESELECTS a test file that carries no unit
marker, so every unit test below is marked ``@pytest.mark.unit`` explicitly
(the split is deliberate: it lets the one real-chromium test stay out of the
unit gate under ``@pytest.mark.integration`` only, instead of being dragged
into it by a module-level marker). No unit test here launches a browser or
touches the network.
"""

import json

import pytest

from claire.senses import browser
from claire.senses.browser import (
    BrowserSession,
    looks_like_secret,
    redact_value,
    secret_field,
)

#: The value from the reproduced leak. Mixed case, digits, a symbol, no spaces:
#: it defeats the old heuristic completely.
SECRET = "Sup3rSecretValue!42"

#: A value that is unmistakably harmless: no whitespace-free mix of character
#: classes, no credential keyword -- so anything that redacts it can only be
#: doing so because it FAILED CLOSED, not because of a value heuristic.
HARMLESS = "weather in Lisbon"


# ---------------------------------------------------------------------------
# driver doubles (never a browser)
# ---------------------------------------------------------------------------


class RecordingDriver:
    """A driver that records every call and never touches a browser.

    Deliberately a plain object rather than a :class:`browser.BrowserDriver`
    subclass: the driver seam is duck-typed, and being plain is what lets a
    double genuinely LACK the optional ``field_type`` capability (subclassing
    the base would hand it a default that reports unknown).
    """

    def __init__(self):
        self.calls = []
        self.url = "about:blank"
        self.title = ""
        self.body_text = "Hello there."
        self.frames = []
        self.links = 0

    # -- readers ---------------------------------------------------------
    def navigate(self, url, timeout_ms):
        self.calls.append(("navigate", url, timeout_ms))
        self.url = url

    def get_url(self):
        return self.url

    def get_title(self):
        return self.title

    def get_body_text(self):
        return self.body_text

    def get_frame_texts(self):
        return list(self.frames)

    def get_link_count(self):
        return self.links

    # -- actions ---------------------------------------------------------
    def click_selector(self, selector, timeout_ms):
        self.calls.append(("click_selector", selector, timeout_ms))

    def click_text(self, text, timeout_ms):
        self.calls.append(("click_text", text, timeout_ms))

    def fill_field(self, selector, value, timeout_ms):
        # The raw value MUST reach the driver: redaction is about reporting,
        # never about refusing to type what the caller asked for.
        self.calls.append(("fill_field", selector, value, timeout_ms))

    def save_screenshot(self, path):
        self.calls.append(("save_screenshot", path))

    def go_back(self, timeout_ms):
        return True

    def close(self):
        self.calls.append(("close",))

    # -- test helpers ----------------------------------------------------
    def filled(self):
        for call in self.calls:
            if call[0] == "fill_field":
                return call[2]
        return None


class DomTypeDriver(RecordingDriver):
    """A driver whose DOM reports a ``type`` per selector.

    ``types`` maps selector -> reported type; a selector that is absent from
    the mapping reports ``None``, which is exactly what the contract says an
    undeterminable type looks like.
    """

    def __init__(self, types=None, error=None):
        super().__init__()
        self.types = dict(types or {})
        self.error = error

    def field_type(self, selector):
        self.calls.append(("field_type", selector))
        if self.error is not None:
            raise self.error
        return self.types.get(selector)


class BaseContractDriver(RecordingDriver, browser.BrowserDriver):
    """Relies on the base ``field_type``: it is present and reports unknown."""


class NoCapabilityDriver(RecordingDriver):
    """A driver built before DOM truth existed: it has no ``field_type`` at all."""


@pytest.fixture(autouse=True)
def _no_leaked_session():
    """Every test starts and ends with no process-wide browser."""
    browser.close_session()
    yield
    browser.close_session()


@pytest.fixture
def logs(monkeypatch):
    """Capture the module's debug log lines instead of emitting them."""
    captured = []
    monkeypatch.setattr(
        browser, "debug_log", lambda message, category="debug": captured.append(message)
    )
    return captured


def session_for(driver):
    return BrowserSession(driver, timeout_ms=1000)


def assert_no_secret_anywhere(result, logs):
    """The whole point: the string is nowhere in the result or the log."""
    assert SECRET not in repr(result)
    assert SECRET not in str(result.as_dict())
    assert SECRET not in json.dumps(result.as_dict())
    assert all(SECRET not in line for line in logs), logs


# ---------------------------------------------------------------------------
# 1. HEADLINE: a password field whose selector is '#pw' never leaks
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_password_dom_type_with_pw_selector_never_leaks_the_value(logs):
    """THE regression test for the reproduced leak.

    The DOM says ``type=password``; the selector is ``#pw``; the value is a
    strong password that no value heuristic needs to recognise. Neither the
    returned structure nor the log may contain it.
    """
    driver = DomTypeDriver(types={"#pw": "password"})
    result = session_for(driver).fill("#pw", SECRET)

    assert_no_secret_anywhere(result, logs)
    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    # ... and the caller's value really was typed.
    assert driver.filled() == SECRET


@pytest.mark.unit
def test_password_dom_type_wins_over_an_innocent_looking_selector(logs):
    """DOM truth, not the field name: an oddly named password input is secret."""
    driver = DomTypeDriver(types={"#newsletter_email": "password"})
    result = session_for(driver).fill("#newsletter_email", SECRET)

    assert_no_secret_anywhere(result, logs)
    assert result.secret is True


@pytest.mark.unit
def test_password_dom_type_redacts_even_a_value_no_heuristic_would_flag(logs):
    """A short lowercase password is a secret because the DOM says so."""
    driver = DomTypeDriver(types={"#pw": "password"})
    result = session_for(driver).fill("#pw", "letmein")

    assert all("letmein" not in line for line in logs), logs
    assert "letmein" not in repr(result)
    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"


# ---------------------------------------------------------------------------
# 2. FAIL CLOSED: an undeterminable type is a secret
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pw_selector_with_unknown_dom_type_is_still_redacted(logs):
    """``#pw`` + unknown type: two independent reasons to redact."""
    driver = DomTypeDriver(types={})  # -> field_type('#pw') is None
    result = session_for(driver).fill("#pw", SECRET)

    assert_no_secret_anywhere(result, logs)
    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"


@pytest.mark.unit
def test_unknown_dom_type_fails_closed_for_a_benign_selector_and_value(logs):
    """Fail closed must not depend on the selector OR on the value.

    ``#username`` is not a credential-shaped name and ``HARMLESS`` is prose, so
    the only reason this is redacted is that the field type could not be
    established. Redaction you cannot prove unnecessary is the acceptable cost.
    """
    driver = DomTypeDriver(types={})
    result = session_for(driver).fill("#username", HARMLESS)

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    assert all(HARMLESS not in line for line in logs), logs


@pytest.mark.unit
def test_a_driver_without_the_capability_fails_closed(logs):
    """No ``field_type`` attribute at all: unknown, therefore secret."""
    driver = NoCapabilityDriver()
    assert not hasattr(driver, "field_type")
    result = session_for(driver).fill("#username", HARMLESS)

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    assert all(HARMLESS not in line for line in logs), logs


@pytest.mark.unit
def test_the_base_capability_reports_unknown_and_fails_closed(logs):
    """The base implementation knows nothing, so it must not imply safety."""
    driver = BaseContractDriver()
    assert driver.field_type("#username") is None
    result = session_for(driver).fill("#username", HARMLESS)

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    assert all(HARMLESS not in line for line in logs), logs


@pytest.mark.unit
def test_a_driver_raising_on_field_type_does_not_crash_and_still_redacts(logs):
    """An error while asking the DOM is "cannot be determined", not "safe"."""
    driver = DomTypeDriver(error=RuntimeError("target closed"))
    result = session_for(driver).fill("#username", HARMLESS)

    assert result.ok is True
    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    assert all(HARMLESS not in line for line in logs), logs


@pytest.mark.unit
def test_an_element_that_disappeared_is_unknown_and_fails_closed(logs):
    """Playwright raises when the element is gone; the type is then unknown."""
    driver = DomTypeDriver(error=browser._ElementNotFound("no element matches '#username'"))
    result = session_for(driver).fill("#username", HARMLESS)

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"


# ---------------------------------------------------------------------------
# 3. a genuinely harmless field with a known type may still show
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_a_known_text_field_still_logs_the_value_in_the_clear(logs):
    """Redaction must not become a mute button: DOM truth also permits speech."""
    driver = DomTypeDriver(types={"#q": "text"})
    result = session_for(driver).fill("#q", HARMLESS)

    assert result.secret is False
    assert result.value_redacted == HARMLESS
    assert any(HARMLESS in line for line in logs), logs


@pytest.mark.unit
def test_every_harmless_input_type_is_permitted_but_a_password_is_not(logs):
    """A known non-secret type is a positive identification of safety."""
    harmless_types = ("text", "search", "email", "url", "tel", "number", "textarea")
    for index, kind in enumerate(harmless_types):
        selector = f"#field{index}"
        driver = DomTypeDriver(types={selector: kind})
        result = session_for(driver).fill(selector, HARMLESS)
        assert result.secret is False, kind
        assert result.value_redacted == HARMLESS, kind

    driver = DomTypeDriver(types={"#field0": "password"})
    result = session_for(driver).fill("#field0", HARMLESS)
    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"


@pytest.mark.unit
def test_a_selector_declaring_a_harmless_type_is_positively_identified():
    """When the driver cannot say, an explicit declared type still proves safety."""
    driver = DomTypeDriver(types={})
    result = session_for(driver).fill("input[type=text]", HARMLESS)

    assert result.secret is False
    assert result.value_redacted == HARMLESS


@pytest.mark.unit
def test_another_attribute_named_type_is_not_a_declaration_about_the_input(logs):
    """``[data-type=text]`` says nothing about the element's own ``type``."""
    driver = DomTypeDriver(types={})
    result = session_for(driver).fill("input[data-type=text]", HARMLESS)

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"


# ---------------------------------------------------------------------------
# 4. the field-name pattern: 'pw' yes, 'pwned'/'power' no
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_pw_is_recognised_as_a_credential_field_name():
    for selector in ("#pw", "pw", "input#pw", "input[name=user_pw]", "#pw-field",
                     "input[name='pw']", ".pw", "#user-pw", "input[type=password]",
                     "#password", "input[name=api_key]"):
        assert secret_field(selector) is True, selector


@pytest.mark.unit
def test_pwned_and_power_are_not_credential_field_names():
    """A word-ish boundary, not a bare substring: 'pw' inside a word is not it."""
    for selector in ("#pwned", "pwned", "#power", "power", "input[name=power]",
                     ".powerful", "#q", "#search", "#username", "#note"):
        assert secret_field(selector) is False, selector


@pytest.mark.unit
def test_pwned_and_power_fields_can_still_be_filled_in_the_clear(logs):
    """The boundary is enforced in the fill path, not only in the regex."""
    for selector in ("#pwned", "#power"):
        driver = DomTypeDriver(types={selector: "text"})
        result = session_for(driver).fill(selector, "power level 3")
        assert result.secret is False, selector
        assert result.value_redacted == "power level 3", selector
        assert any("power level 3" in line for line in logs), selector


@pytest.mark.unit
def test_pw_selector_is_secret_even_when_the_dom_claims_text(logs):
    """The field name is a hint that can only ever ADD redaction, never remove it."""
    driver = DomTypeDriver(types={"#pw": "text"})
    result = session_for(driver).fill("#pw", SECRET)

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    assert_no_secret_anywhere(result, logs)


# ---------------------------------------------------------------------------
# 5. the value backstop (documented as a backstop, not the guarantee)
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_strong_password_backstop_catches_a_mixed_class_value():
    assert looks_like_secret(SECRET) is True
    assert looks_like_secret("Tr0ub4dor&3") is True
    assert redact_value(SECRET) == "[REDACTED]"


@pytest.mark.unit
def test_the_backstop_does_not_flag_prose_or_empty_values():
    assert looks_like_secret("weather in Lisbon") is False
    assert looks_like_secret("correct horse battery staple") is False
    assert looks_like_secret("") is False
    assert looks_like_secret(None) is False
    assert redact_value("weather in Lisbon", secret=False) == "weather in Lisbon"


@pytest.mark.unit
def test_the_backstop_masks_a_strong_password_in_an_innocent_field(logs):
    """The backstop is a second line of defence behind DOM truth."""
    driver = DomTypeDriver(types={"#search": "text"})
    result = session_for(driver).fill("#search", SECRET)

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    assert_no_secret_anywhere(result, logs)


@pytest.mark.unit
def test_a_short_password_is_caught_by_dom_truth_not_by_the_backstop():
    """Length alone must not decide: the backstop may miss, the DOM may not."""
    assert looks_like_secret("hunter2") is False  # backstop alone would leak this
    driver = DomTypeDriver(types={"#pw": "password"})
    result = session_for(driver).fill("#pw", "hunter2")

    assert result.secret is True
    assert result.value_redacted == "[REDACTED]"
    assert "hunter2" not in repr(result)


# ---------------------------------------------------------------------------
# 6. the result and the log are built only from the redacted rendering
# ---------------------------------------------------------------------------


@pytest.mark.unit
def test_the_result_is_built_only_from_the_redacted_rendering(logs):
    driver = DomTypeDriver(types={"#pw": "password"})
    result = session_for(driver).fill("#pw", SECRET)

    assert result.action == "fill"
    assert result.target == "#pw"
    assert result.detail == "filled '#pw' with '[REDACTED]'"
    assert result.value_redacted == "[REDACTED]"
    # Length alone is not a credential and stays useful for debugging.
    assert result.value_len == len(SECRET)
    assert SECRET not in result.detail


@pytest.mark.unit
def test_the_log_line_is_built_only_from_the_redacted_rendering(logs):
    driver = DomTypeDriver(types={"#pw": "password"})
    session_for(driver).fill("#pw", SECRET)

    assert logs, "fill must still log something useful"
    assert any("fill" in line and "#pw" in line for line in logs), logs
    assert all(SECRET not in line for line in logs), logs
    # No fragment of the value either: no partial leak through a truncation.
    assert all(SECRET[:8] not in line for line in logs), logs
    assert all(SECRET[-4:] not in line for line in logs), logs


@pytest.mark.unit
def test_an_empty_secret_is_still_reported_as_a_secret_field(logs):
    """Redaction must not depend on the value being non-empty."""
    driver = DomTypeDriver(types={"#pw": "password"})
    result = session_for(driver).fill("#pw", "")

    assert result.secret is True
    assert result.value_len == 0


@pytest.mark.unit
def test_redaction_is_identical_for_redacted_and_unredacted_paths(logs):
    """One rendering function, one marker: no path can invent its own output."""
    driver = DomTypeDriver(types={"#pw": "password", "#q": "text"})
    session = session_for(driver)
    masked = session.fill("#pw", SECRET)
    shown = session.fill("#q", HARMLESS)

    assert masked.value_redacted == redact_value(SECRET, secret=True) == "[REDACTED]"
    assert shown.value_redacted == redact_value(HARMLESS)


# ---------------------------------------------------------------------------
# live reproduction -- REAL chromium, so NOT the unit gate
# ---------------------------------------------------------------------------

_PASSWORD_PAGE = (
    "data:text/html,"
    "<html><body><h1>Login page</h1>"
    "<input id='pw' type='password'>"
    "<input id='q' type='text'>"
    "</body></html>"
)


@pytest.mark.integration
def test_live_chromium_password_input_returns_a_redacted_value():
    """The original reproduction, against real chromium, verbatim.

    Marked ``integration`` only, so ``pytest -m unit`` deselects it and no unit
    run ever launches a browser.
    """
    with browser.get_session(headless=True, timeout_ms=20_000) as live:
        live.open(_PASSWORD_PAGE)
        result = live.fill("#pw", SECRET)
        # Report the observed value in the test output as well as asserting it.
        print(f"\nlive value_redacted={result.value_redacted!r} secret={result.secret}")
        assert result.value_redacted == "[REDACTED]"
        assert result.secret is True
        assert SECRET not in repr(result)
        # A real text input on the same page is still usable.
        shown = live.fill("#q", HARMLESS)
        assert shown.secret is False
        assert shown.value_redacted == HARMLESS
