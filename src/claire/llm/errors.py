"""Classify transport failures without exposing URLs or credentials."""

import json
import re
from typing import Any, Optional

import requests
from urllib3.exceptions import ReadTimeoutError


def is_timeout_error(error: BaseException) -> bool:
    """Recognise typed timeouts wrapped by Requests while reading a body.

    Requests can raise ConnectionError(ReadTimeoutError(...)) after receiving
    response headers. Inspect explicit causes and exception arguments, not
    message text or unrelated exceptions in the handling context.
    """
    pending = [error]
    seen = set()
    while pending:
        current = pending.pop()
        if not isinstance(current, BaseException) or id(current) in seen:
            continue
        seen.add(id(current))
        if isinstance(current, (requests.exceptions.Timeout, ReadTimeoutError)):
            return True
        pending.extend(current.args)
        pending.append(current.__cause__)
    return False


# ── "the server rejected the tools parameter" detection ─────────────────
#
# HTTP 400 is NOT by itself evidence that a model cannot do native tool
# calling. The same status covers a malformed message, an unknown sampling
# field, a bad model name, an over-long context and a rate-limit proxy's
# "invalid request error". Treating every 400 as "tools unsupported"
# permanently downgrades the whole session to text-based tools, which
# rebuilds a *larger* system prompt on every later turn (measured
# 15001 -> 17822 chars, ~+700 tokens) and burns a retry that fails the
# same way. So we require the error body to actually blame the tools
# field before taking that irreversible step.
#
# These names are OpenAI Chat Completions wire constants, not natural
# language: matching them is protocol decoding, which is why they live
# here and not in a language-specific word list.
_TOOL_FIELD = r"(?:tools?|tool_choice|tool_calls?|function[\s_]?call(?:ing)?|functions?)"
_UNSUPPORTED = (
    r"(?:not\s+support(?:ed)?|unsupported|does\s*n[o']?t\s+support|"
    r"not\s+allowed|not\s+permitted|unknown|unrecognized|unrecognised|"
    r"unexpected|not\s+recognized|invalid)"
)
# The tool token and the unsupported signal must sit in the same clause
# (no sentence terminator between them), so a 400 that merely *mentions*
# tools elsewhere is not enough.
_TOOLS_UNSUPPORTED_RE = re.compile(
    rf"(?:{_TOOL_FIELD}\b[^.;]{{0,48}}{_UNSUPPORTED}"
    rf"|{_UNSUPPORTED}\b[^.;]{{0,48}}{_TOOL_FIELD})",
    re.IGNORECASE,
)


def _error_body_text(response: Any) -> str:
    """Best-effort error body as text. Never raises, never returns much."""
    if response is None:
        return ""
    candidates = []
    try:
        candidates.append(response.text)
    except Exception:
        pass
    try:
        candidates.append(response.content)
    except Exception:
        pass
    for candidate in candidates:
        if isinstance(candidate, bytes):
            try:
                return candidate.decode("utf-8", "replace")[:8192]
            except Exception:
                continue
        if isinstance(candidate, str) and candidate:
            return candidate[:8192]
    return ""


def _structured_tool_blame(payload: Any) -> bool:
    """True when a JSON error object names tools as the offending field."""
    if not isinstance(payload, dict):
        return False
    err = payload.get("error")
    if isinstance(err, dict):
        for key in ("param", "code", "type"):
            value = err.get(key)
            if isinstance(value, str) and "tool" in value.lower():
                return True
    elif isinstance(err, str):
        return bool(_TOOLS_UNSUPPORTED_RE.search(err))
    # vLLM / OpenAI-style double wrapping: {"detail": {...}}
    detail = payload.get("detail")
    if isinstance(detail, dict):
        return _structured_tool_blame({"error": detail})
    return False


def indicates_tools_unsupported(response: Any) -> bool:
    """True only when an HTTP 400 response blames the tools parameter.

    Conservative by design: a false negative costs one failed turn (the
    response is treated as empty, exactly as a 400 without tools always
    was), while a false positive costs the rest of the session — a
    permanent text-tools downgrade, a bigger system prompt on every
    subsequent turn, and a wasted retry.
    """
    if response is None:
        return False
    text = _error_body_text(response)
    if text:
        try:
            payload = json.loads(text)
        except Exception:
            payload = None
        if _structured_tool_blame(payload):
            return True
    if _TOOLS_UNSUPPORTED_RE.search(text):
        return True
    return _TOOLS_UNSUPPORTED_RE.search(str(getattr(response, "reason", "") or "")) is not None
