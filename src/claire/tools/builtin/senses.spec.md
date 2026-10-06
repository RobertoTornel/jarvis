# senses — screen, camera and browser as tools (R-SENSES-TOOLS-1)

`src/claire/tools/builtin/senses.py` exposes the senses modules
(`claire.senses.screen`, `claire.senses.camera`, `claire.senses.browser`) to the
reply loop as three builtins:

| tool | wraps | answers |
| --- | --- | --- |
| `readScreen` | `senses.screen` | "what is on my screen?", "read this off the screen", "where is the Save button?" |
| `lookThroughCamera` | `senses.camera` | "can you see me?", "what am I doing?" |
| `browseWeb` | `senses.browser` | open a page, read it, find a phrase, click, type, screenshot, back, close |

The senses modules are the honest reporters: named errors, capture mechanisms,
OCR verdicts, install commands. Almost none of that belongs in a sentence
spoken aloud, so this module is a deliberately thin translation layer. It adds
no capture, no OCR, no vision and no browser logic of its own — every real
operation is delegated to the module named above.

## Rules the tools guarantee

* **Speech, not a dump.** Every `reply_text` is collapsed to a single paragraph
  with no newlines, no markdown and no traceback fragments, capped at
  `MAX_SPOKEN_CHARS` (1200) for page/screen text and `MAX_REASON_CHARS` (240)
  for a reason or fix line. Truncation says so ("there is more after that")
  rather than stopping mid-sentence.
* **A missing capability is not an error to shout about.** `success=False` is
  the honest report for "this machine cannot do that", and the reply names the
  fix taken from the module's own verdict:
  * no OCR engine → the exact `fix_command` from `screen.diagnose()`
    (`sudo apt-get install -y tesseract-ocr` on this machine);
  * no camera → `CameraAvailability.spoken_summary()` plus the first item of
    its own fix list;
  * no chromium → `browser.CHROMIUM_INSTALL_COMMAND`
    (`playwright install chromium`).
  The tool short-circuits *before* capturing when the verdict is already known
  (OCR checked via `diagnose()` first), so a missing engine costs no screen
  grab.
* **Nothing raises out of `execute()`.** Expected conditions are caught and
  returned in the `ToolExecutionResult`; `_SenseTool.run` keeps a last-resort
  guard that converts even an unforeseen `Exception` into a calm reply with the
  exception *type* (never a traceback) in `error_message`.
* **A typed value is never echoed.** `browseWeb` holds the text the user typed
  in one local. It is passed to `session.fill()`, never logged, never placed in
  `reply_text` or `error_message`, and anything a lower layer hands back is
  scrubbed through `_scrub()` (the literal value becomes `[redacted]` before it
  can reach the reply or the error field). A credential test asserts the
  password appears in no reply, no error and no log line.
* **Secret fields are over-detected, not under-detected.** A field counts as a
  credential if *either* the browser module's `secret_field()` rule *or* the
  local name heuristic (`_SECRET_FIELD_RE`) says so. Over-redaction costs one
  sentence; under-redaction costs the user's password. Values are only scrubbed
  out of text when they are at least `_MIN_SCRUB_CHARS` (3) long, so a tiny
  needle cannot mangle unrelated words.

## readScreen

Parameters: `mode` (`"read"` default, `"find"`), `phrase` (required when
`mode="find"`). Nothing is required, so "what is on my screen?" needs no
arguments.

* `read` → `screen.read_screen()`; with words: "Here is what is on your
  screen: …"; with none: "I looked at your screen, but there are no readable
  words on it" (a *success*: a blank screen is an answer, not a failure).
* `find` → `screen.find_phrase_on_screen(phrase)` and `PhraseLocation.describe()`
  is reused verbatim, so the location wording lives in exactly one place.
  A miss is a success ("No, I cannot see …"), because "it is not there" is an
  answer.

The OCR verdict is checked first (`screen.diagnose()`); if OCR cannot run the
tool returns the calm capability report and never attempts a capture. A
`ScreenError` that still reaches the tool (capture unavailable, engine gone
between the check and the read) is converted to a spoken sentence carrying the
first fix the module offers.

## lookThroughCamera

Parameter: `question` (optional).

1. `camera.describe_availability()` — no camera is reported plainly with what
   to do instead (plug in a webcam, or send a photo from the phone) and the
   tool stops there: `capture_frame()` is never called.
2. Otherwise `camera.capture_frame()` then `camera.describe_image(path, question)`.
3. `seen` → the description's `spoken_summary()`.
4. Not seen → "I took a picture, but I could not describe it, so I will not make
   one up", plus the module's `remedy`. Hallucinating a picture nobody saw is
   the worst failure this tool could produce, so the not-seen case is spoken as
   a refusal, never as a scene.

## browseWeb

Parameter: `action` (required; one of `open`, `read`, `find`, `click`, `fill`,
`screenshot`, `back`, `close`), plus `url`, `phrase`, `target`, `selector`,
`value`, `path` as each action needs them.

* Availability is checked first (`browser_available()`, cheap, no launch); when
  chromium is missing the exact install command is spoken and no session is
  created.
* The session is the process-wide one from `browser.get_session()` — this tool
  never builds a second browser and never touches a driver directly.
* `fill` never reads the value back. A password field gets "I typed it into the
  … field. I will not read a password back out loud."; any other field gets
  "I typed your text into the … field." No branch includes the text itself.
* A missing `action` value, an unknown action, a missing URL/target/selector/
  phrase/value each produce a calm question back instead of doing something
  surprising; none of them touch the browser.
* `close` calls `browser.close_session()` and is the only action that works
  without a live session.

## Testability (why the seams exist)

Each tool takes its collaborators as constructor arguments — `read_screen`,
`find_phrase_on_screen`, `diagnose`, `describe_availability`, `capture_frame`,
`describe_image`, `session_factory`, `available_fn`, `close_fn`. The registry
passes none of them, so production resolves the real module attributes lazily at
call time; tests inject fakes and never need a display, a camera, a network or a
real browser. The default path is a lazy module lookup (not a captured function
reference), which keeps registration free of side effects and keeps
`monkeypatch.setattr(claire.senses.screen, "read_screen", …)` effective.
