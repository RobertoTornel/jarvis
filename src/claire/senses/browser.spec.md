# Senses / Browser Specification (R-BROWSER-1)

`src/claire/senses/browser.py` gives Claire a real browser: real rendering, real
clicks, real form filling, real screenshots. It is the only sense on this machine
that can *look at a web page* rather than at a fetched string, so it is the one
that has to be honest about what it cannot do.

## Why Playwright, and not WebDriver BiDi

**Recorded deliberately, because the reference project chose the other way.**

The reference implementation drove Firefox over WebDriver BiDi instead of using
Playwright, and its stated reason was that Node 24 ships a native `WebSocket`, so
no extra dependency was needed.

That reason does not transfer to this Python port:

* Python's standard library has **no** built-in WebSocket client, so the BiDi
  route would *add* a dependency (`websockets`) rather than avoid one;
* it would require hand-rolling the protocol — session handshake, browsing-context
  bookkeeping, script evaluation, event demultiplexing — on top of that;
* Playwright is already installed here, and its chromium binary is already on
  disk (`~/.cache/ms-playwright/chromium-1243`).

So Playwright is the right call for this port. A future reader who knows the
reference project should not "fix" this back.

## One browser, one profile, one ordering

The browser is **process-wide state**:

* exactly one chromium process, one user-data (profile) directory, one
  operation ordering, for the whole Claire process;
* chromium takes an exclusive `SingletonLock` on the profile directory. A second
  browser pointed at that locked profile **can exit immediately having done
  nothing at all**, which is indistinguishable from success if nobody checks;
* therefore the session is created **lazily and once**, behind a module lock
  (`_SESSION`, `_SESSION_LOCK`, `get_session()`), and the module never offers a
  way to build a second browser;
* every session method serialises on one `threading.RLock`, so "one ordering" is
  a property of the module rather than of luck. A unit test drives four threads
  through `read()` and asserts the maximum observed concurrency is 1;
* a launch failure caused by a locked profile raises
  `BrowserProfileLockedError`, whose fix names the profile path and
  `CLAIRE_BROWSER_PROFILE_DIR` — not "something went wrong".

The profile directory is `$CLAIRE_BROWSER_PROFILE_DIR`, else
`$XDG_DATA_HOME/claire/browser-profile`, else
`~/.local/share/claire/browser-profile`. It is resolved in exactly one place
(`_profile_dir()`), and a test pins that two calls agree.

## API

| Action | Signature | Returns |
| --- | --- | --- |
| open | `open(url, *, timeout_ms=None, allow_empty=False)` | `PageRead` |
| read | `read(*, allow_empty=False)` | `PageRead` |
| find | `find(phrase, *, context_chars=80)` | `FindResult` |
| click | `click(target, *, by="auto"\|"selector"\|"text", timeout_ms=None)` | `ActionResult` |
| fill | `fill(selector, value, *, timeout_ms=None)` | `ActionResult` |
| screenshot | `screenshot(path)` | `ActionResult` (`.path`) |
| back | `back(*, timeout_ms=None)` | `PageRead` |
| close | `close()` | `None`, idempotent |

* `PageRead` carries `url`, `title`, `text`, embedded-frame `frames`, and the
  counts `char_count`, `word_count`, `frame_count`, `link_count`.
* `read()`/`open()` include the text of embedded frames **when it is cheap to
  get** (per-frame `inner_text`, each frame failure tolerated); a frame that
  cannot be read degrades that one field, it does not lose the whole reading.
* `find()` answers "does it exist and roughly where". "Where" is the phrase's
  position in the document text — top third / middle / bottom — plus the frame
  index when the phrase only appears inside a frame. It is approximate and says
  so.
* A bare host gets `https://` prepended; a URL that already states its scheme
  (`http://`, `data:`, `file:`, `about:`) is used as given. `example.com:8080/x`
  is a host:port, not a scheme, and still gets `https://`.
* `_launch_driver()` is the single launcher and the single test seam.

## Outcomes are distinguishable, and each one is named

Nothing collapses to "something went wrong".

| Situation | Outcome |
| --- | --- |
| page did not finish loading in time | `NavigationTimeoutError` (carries the timeout and a fix) |
| DNS/refused/other hard navigation failure | `NavigationError` |
| `back()` with no earlier page | `NavigationError`, message says "no earlier page" |
| selector / visible text is not there | `SelectorNotFoundError` |
| element is there but never became clickable/fillable | `ActionTimeoutError` |
| page rendered with no visible text | `EmptyPageError` (`allow_empty=True` returns the raw `PageRead`) |
| chromium binary missing | `BrowserUnavailableError` with `fix="playwright install chromium"` |
| profile locked by another chromium | `BrowserProfileLockedError` naming the profile and `CLAIRE_BROWSER_PROFILE_DIR` |
| phrase simply is not on the page | **not an error** — `FindResult(found=False)` plus a sentence |

`find()` deliberately returns a miss instead of raising: "it is not there" is an
answer, and a caller should not have to catch an exception to hear it.

## Missing binaries: named, with the exact fix

`probe_chromium_binary()` is a **cheap probe** — it only stats candidate paths
under `~/.cache/ms-playwright` (both the `chromium-*/chrome-linux64/chrome` and
`chromium-*/chrome-linux/chrome` layouts, plus the headless shell), honouring
`CLAIRE_CHROMIUM_EXECUTABLE` and `PLAYWRIGHT_BROWSERS_PATH`. It never starts
playwright's node driver and never launches a browser, so it is safe to call on
every turn.

When it finds nothing, `ensure_browser_installed()` raises
`BrowserUnavailableError` whose message contains, verbatim:

```
playwright install chromium
```

and whose `.fix` is that same string, plus the paths it looked in and
`sys.executable -m playwright install chromium`. Detection happens **before**
playwright is imported for the launch, so the user sees the install command
rather than a traceback from inside the node handshake. If the `playwright`
package itself is missing, the same named error names the pip install and the
browser install, in that order.

The honest limit: the probe checks the standard install locations plus the two
documented environment overrides. A chromium installed somewhere exotic must be
pointed at with `CLAIRE_CHROMIUM_EXECUTABLE` rather than silently assumed.

## A secret is never typed into a log

Hard project rule. **The rule does not rest on guessing what a credential looks
like** — heuristics are only backstops. Order of authority in `fill()`:

1. **DOM truth first.** `BrowserDriver.field_type(selector) -> Optional[str]` is
   an *optional* driver capability that asks the browser what the element **is**.
   `PlaywrightDriver` answers with the element's `type` **property** (so an
   `<input>` with no `type=` attribute reports `"text"`), lower-cased, and
   returns `None` if it cannot be determined. If the reported type is
   `password`, the value is a secret **regardless of the selector and regardless
   of what the value looks like**.
2. **Fail closed.** If the type cannot be determined — no capability on the
   driver, a base implementation that reports unknown, a missing element, or any
   error while asking — the value is a secret unless the field was *positively*
   identified as harmless: the DOM reported a type in the harmless set
   (`text`, `search`, `email`, `url`, `tel`, `number`, `textarea`, `date`, …) or
   the selector itself declares one (`input[type=text]`). Anything unproven is
   redacted. A redacted debug line is a small cost; a password in a log is not.
3. **Backstops.** `secret_field()` and `looks_like_secret()` may only **add**
   redaction, never remove it:
   * `secret_field()` matches credential-ish selector/field names
     (`password`, `passwd`, `pwd`, `secret`, `token`, `api_key`, `otp`, `pin`,
     `passcode`, `credential`, `cvv`, …), and `pw` with a word-ish boundary so
     that `#pw`, `pw`, `user_pw` and `pw-field` match while `pwned` and `power`
     do not;
   * `looks_like_secret()` returns True when the project scrubber
     (`claire.utils.redact.scrub_secrets`) changes the value, when the value
     carries a credential keyword, when it is an opaque no-spaces blob mixing
     letters and digits (a bare key), **or** when it has no whitespace, is at
     least 8 characters long and mixes at least two of
     lower/upper/digit/symbol — the rule that catches a strong password such as
     `Sup3rSecretValue!42`, which the alnum-blob rule missed because of its
     punctuation.

This ordering exists because heuristics alone were **proven wrong**: `#pw` (one
of the most common password ids in the world) was not matched, a strong password
was not recognised, and the value reached both the `ActionResult` and the log in
plaintext. `tests/test_senses_browser_secrets.py` is the fail-proven regression
suite; the live reproduction runs as an `integration` test so it never launches
a browser inside the unit gate.

Then:

* the logged line is built **only** from `redact_value(...)`, which returns the
  fixed marker `[REDACTED]` for a secret and a scrubbed + length-capped string
  otherwise — the raw value is never interpolated into a log line; the line
  carries safe labels only (`redacted=yes|no`, `why=…`, `field_type=…`,
  `chars=N`);
* `ActionResult` has **no field holding the raw value**: it carries
  `value_redacted`, `value_len` and `secret` instead. A test asserts the secret
  appears in neither `repr(result)` nor `json.dumps(result.as_dict())`;
* `value_len` is kept deliberately: the length alone is not a credential, and it
  is what makes a redacted line still useful for debugging;
* **every** log line in the module goes through `_log()`, which runs
  `scrub_secrets` structurally. This is what stops a credential in a page URL
  (`/reset?token=…`) or in an exception message from reaching the log, rather
  than relying on each call site to remember.

The value still reaches the driver — redaction is about reporting, not about
lying to the browser — and a test asserts that too.

Redaction is not a mute button: a field the DOM positively identifies as
harmless (for example `#q` reported as `text`) still logs `weather in Lisbon` in
the clear, so debug output stays useful.

## Lifecycle: never leave an orphan chromium holding the profile lock

* `BrowserSession` is a context manager; `__exit__` closes even when the body
  raises.
* `close()` is idempotent and safe to call twice (tested), at both the session
  and the driver level.
* `close_session()` / `reset_session()` close and forget the process-wide
  session, so the next `get_session()` builds a fresh browser.
* Teardown closes the browser context **and** stops playwright. This is not
  cosmetic: `PlaywrightContextManager` has no `close()`, and skipping its
  `__exit__` leaves the sync dispatcher greenlet with a *running* event loop, so
  the next `sync_playwright().start()` in the process dies with
  `It looks like you are using Playwright Sync API inside the asyncio loop`.
  This was found by actually running the real smoke test; a unit test now pins
  both halves of the teardown without launching a browser.
* `go_back()` treats an unchanged URL and no response as "did not move" rather
  than claiming a navigation that may not have happened: playwright returns
  `None` both for "no history" and for a history entry that produced no resource
  response (`data:`/`about:` URLs).

## Measured facts about this machine

Recorded because the honest path is the tested default:

* the `tesseract` **binary** is not installed (pytesseract is, but it needs the
  binary), and `scrot` is not on `PATH` either — so screenshot-OCR senses fail
  with a named install command, not an empty result;
* playwright's **chromium IS installed** (`~/.cache/ms-playwright/chromium-1243`);
  `probe_chromium_binary()` resolves it on this machine, and the integration
  smoke test below drives it for real;
* there is **no camera** (`/dev/video*` does not exist).

## Tests

`tests/test_senses_browser.py` (`pytestmark = pytest.mark.unit`, 54 unit tests).
**No unit test launches a browser**: the module's launcher `_launch_driver` is
monkeypatched, or a `FakeDriver` is injected straight into `BrowserSession`.
Covered: open returns title/url/text; read returns counts; a hit and a miss from
`find` are distinct; click/fill dispatch to the fake; screenshot writes to the
given path; a missing binary raises the named error carrying
`playwright install chromium`; timeout vs missing selector vs empty page vs
locked profile are distinguishable; the redaction helper masks a secret and the
secret never appears in logs or in the returned structure; `close()` is
idempotent; one process gets one browser.

The two real smoke tests in `tests/test_senses_browser.py` carry
`@pytest.mark.integration`, but that file also sets `pytestmark =
pytest.mark.unit` at module level, so those two tests are *also* marked unit and
the `-m unit` gate does select them (verified: `-m unit -k real_chromium` runs
both). They serve a page from a `127.0.0.1` HTTP server on an ephemeral port
and drive real chromium through open → read → find → click → fill → screenshot →
open → back, plus a real missing-selector and real empty-page case. Both were
run on this machine and pass. The secrets suite deliberately does **not** repeat
that mistake: its live test carries `@pytest.mark.integration` only, so
`-m unit` deselects it rather than launching chromium inside the gate.

`tests/test_senses_browser_secrets.py` is the **fail-proven** suite for the
credential rule — it was written against the leaking code first, and the
headline test failed on the reproduced leak (`fill('#pw', 'Sup3rSecretValue!42')`
on a `type=password` element returned and logged the plaintext) before the fix.
It covers: a password DOM type with a `#pw` selector never leaks the value into
`repr(result)` or any log line; an undeterminable type fails closed even for a
benign selector and prose value; a driver with no `field_type` at all, one that
raises, and one whose element vanished all fail closed; a field the DOM reports
as `text` (or a selector declaring `type=text`) may still be logged in the
clear; `pw` matches `#pw`/`pw`/`user_pw`/`pw-field` but not `pwned`/`power`; the
mixed-character-class value backstop; `value_len` is reported while the detail
and the log line are built only from the redacted rendering. 25 unit tests are
marked `@pytest.mark.unit`; the live chromium reproduction is marked
`@pytest.mark.integration` **only**, so the unit gate deselects it instead of
launching a browser. Observed live after the fix (real chromium, headless):
`value_redacted='[REDACTED]'`, `secret=True`.
