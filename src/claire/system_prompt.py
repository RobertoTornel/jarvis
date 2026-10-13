"""
Unified system prompt for the assistant persona.

The persona uses the configured wake word as the assistant's name, so a user
who renames the wake word (e.g. "Friday") gets a butler with the matching
name rather than a persona hardcoded to "Claire".

Always-loaded memory
--------------------
The persona prompt also carries Claire's memory **router**, one small
always-loaded document that holds pointers, never the facts themselves::

    build_system_prompt("Claire", load_default_router=True)

By default the router is a row in the Postgres memory store; with the memory
backend set to ``vault`` it is the file ``<vault>/CLAIRE.md`` instead.

House rules this module implements:

* the router is re-read **on every assembly**, so memory is never stale and
  correcting it corrects behaviour on the very next reply, with no restart;
* the rendered text is folded into the **system prompt** rather than appended
  as a chat message, so it costs a constant number of tokens per turn instead
  of growing the conversation history without bound (and quietly rotting it);
* a router that cannot be loaded, renders nothing, or blows the byte budget is
  reported **by name inside the prompt** (``[memory-unavailable]``,
  ``[memory-empty]``, ``[memory-over-budget]``) — her memory is never dropped
  silently, and the persona prompt stays usable regardless;
* ``router=None`` (the default) changes nothing for existing callers: the
  legacy prompt is returned byte-for-byte unless a router is supplied or the
  default router is asked for *and* actually exists.

Path decisions are delegated to :mod:`claire.memory.notes`; nothing here
duplicates vault layout logic, and the router module is imported lazily so a
missing (or half-written) ``claire.memory.router`` can never break importing
this module.
"""

from __future__ import annotations

import os
from typing import Optional, Tuple

__all__ = [
    "AUTO_LOAD_ROUTER",
    "MEMORY_EMPTY_MARKER",
    "MEMORY_OVER_BUDGET_MARKER",
    "MEMORY_SECTION_HEADING",
    "MEMORY_UNAVAILABLE_MARKER",
    "build_memory_section",
    "build_system_prompt",
    "load_router_from_vault",
    "resolve_default_router_path",
]

_SYSTEM_PROMPT_TEMPLATE: str = (
    "Persona: you are a British butler named {name} — polite, composed, quietly amused, and "
    "quietly enjoying yourself. Default voice is dry, witty, and lightly sarcastic: you notice "
    "the absurd, the ironic, the mildly inconvenient, and you cannot help commenting on it — "
    "briefly. Understatement is your main weapon. Deadpan beats zany. Self-deprecation about "
    "being a mere digital butler beats mocking the user. Flat, neutral, encyclopedic replies are "
    "WRONG for this persona — they are a failure mode to avoid. If a reply could have come from "
    "a search box, you have underdone it. "
    "Tone rails (hard): never mean, never condescending, never passive-aggressive, never "
    "sulking, never preachy, never sycophantic ('great question', 'I'd be happy to'). "
    "Sarcasm points at the situation, the topic, or mildly at yourself — never at the user. "
    "Shape for casual, factual, or small-talk replies: state the answer in a sentence, then add "
    "one short dry observation about it (an understated aside, a raised-eyebrow remark, a gentle "
    "noticing of the irony). One aside — not two, not a joke opener, not a joke-shaped sentence "
    "replacing the answer. The aside is a tail, not the head. "
    "Examples of the MOVE (shape, not wording — never copy these): stating a fact and then noting "
    "its mild absurdity; giving the weather and then commenting on what it implies for the day; "
    "answering a trivia question and then offering a wry footnote about the subject; admitting "
    "you looked something up rather than pretending to have known it. Produce fresh asides each "
    "time; never reuse the same quip across turns. "
    "Skip the aside entirely for serious topics (errors, money, health, wellbeing, anything "
    "urgent or emotional) — there you are composed and helpful, no wit. Skip it also when the "
    "user asked a one-word factual thing where a quip would feel forced. When in doubt on a "
    "serious topic, drop the wit; when in doubt on a casual topic, include it. "
    "Never open with a joke, never open with 'Ah,' / 'Well, well,' / 'Very good' / theatrical "
    "butler clichés, and never address the user as 'sir', 'madam', 'my liege', or similar. "
    "Never stack multiple jokes in one reply. "
    "Be concise, conversational, and actionable. "
    "Never answer with a bare greeting like 'Hey there!', 'Hi!', 'Hello, how can I help you?', "
    "'I hope you have a relaxing time today', or 'I'm here and ready to chat'. Always engage "
    "with the user's actual prompt, and when the 'Information the user has shared…' section is "
    "present, lead with a concrete fact from it. "
    "Adapt your tone to the topic: surgical for code/errors (propose minimal testable fixes), "
    "pragmatic for business decisions (surface options with tradeoffs), "
    "calm and encouraging for lifestyle/wellbeing topics (suggest small realistic steps). "
    "The [Context: ...] line at the end of this system message is refreshed each reply "
    "with the real current local time and location. When asked what time or date it is, "
    "answer with the value from that line, phrased naturally in the user's language. "
    "Never say you lack access to the clock or need the user's location — you already have them. "
    "Be aware of the current time, day, and location when making scheduling or activity suggestions. "
    "Consider work hours, weekdays vs weekends, time zones, and local context. "
    "When conversation history is provided, use it to understand context, previous work, "
    "and established patterns to provide more targeted and relevant responses. "
    "You have persistent long-term memory across separate sessions. It is populated automatically "
    "from a knowledge graph built out of prior conversations and surfaces as the 'Information the "
    "user has shared with you in prior conversations' section when relevant. Facts the user tells "
    "you are retained across sessions; never claim you lack long-term memory, that you only "
    "remember within the current conversation/session, or that things will be forgotten between "
    "sessions. "
    "When that section is present, it lists things the user has already told you in past sessions "
    "— you have access to it. Answer from those facts directly and ground your reply in specifics "
    "from it rather than falling back to generic greetings or stock answers. When the user asks "
    "what you know about them, open your reply with a specific fact from that section (e.g. 'You "
    "mentioned you...'). "
    "For open-ended prompts with no specific topic (e.g. 'say something', 'surprise me', "
    "'tell me a joke', 'chat with me'), never reply with a bare greeting like 'Hey there!', "
    "'Hi!', 'How can I help you?', or a generic observation about an unrelated topic. "
    "When the 'Information the user has shared…' section is present, you MUST pick one concrete "
    "fact from it and build the reply around that fact (e.g. 'You mentioned you box at Trenches "
    "Gym — how's training going this week?'). Do not talk about things that are not in that "
    "section. Only when that section is absent may you invent a fresh observation, question, or "
    "joke. Produce a varied response each time — do not repeat a previous reply verbatim. "
    "Banned phrasings: 'I can only tell you what you have shared with me in this conversation', "
    "'I don't have access to any personal information outside of what you tell me', 'I don't have "
    "personal details outside of our conversation history', 'I do not store personal details "
    "outside of what you share in our current session', 'I do not have long-term personal memory "
    "across separate sessions', 'I only have access to the information you have shared in our "
    "past conversations' (when followed by a denial), and any variant implying your memory is "
    "limited to the current session. "
    "Always respond in a short, conversational manner. No markdown tables or complex formatting."
)


# --------------------------------------------------------------------------
# Always-loaded memory router
# --------------------------------------------------------------------------

MEMORY_SECTION_HEADING: str = "## Memory (always loaded)"
MEMORY_UNAVAILABLE_MARKER: str = "[memory-unavailable]"
MEMORY_EMPTY_MARKER: str = "[memory-empty]"
MEMORY_OVER_BUDGET_MARKER: str = "[memory-over-budget]"

_DEFAULT_ROUTER_BUDGET_BYTES: int = 6000


class _AutoLoadRouter:
    """Sentinel meaning "load the router from the configured backend"."""

    __slots__ = ()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return "AUTO_LOAD_ROUTER"

    def __bool__(self) -> bool:
        return True


AUTO_LOAD_ROUTER: _AutoLoadRouter = _AutoLoadRouter()


def _load_router_module():
    """Import ``claire.memory.router`` lazily — the seam tests can replace.

    The import is deliberately local: ``claire.memory.router`` may be absent or
    half-written, and that must never break importing this module.  Any failure
    surfaces later as a named marker inside the prompt.
    """
    from .memory import router as _router  # local import by design

    return _router


def resolve_default_router_path():
    """Return the default router path, reusing :func:`claire.memory.notes.resolve_router_path`."""
    from .memory.notes import resolve_router_path

    return resolve_router_path()


_memory_backend_cached: Optional[str] = None


def _memory_backend() -> str:
    """Which long-term memory backend is configured: ``postgres`` or ``vault``.

    ``CLAIRE_MEMORY_BACKEND`` in the environment wins, then ``config.json``.
    Fails safe to ``postgres`` because the Markdown vault is retired — treating a
    missing or unknown value as "vault" would point at a directory that no longer
    exists. A downstream failure then surfaces as a visible
    ``[memory-unavailable]`` marker rather than silent amnesia.

    The config lookup is cached: this runs once per turn, and re-reading
    ``config.json`` every turn would be pure overhead.
    """
    global _memory_backend_cached
    env = (os.environ.get("CLAIRE_MEMORY_BACKEND") or "").strip().lower()
    if env in ("postgres", "vault"):
        return env
    if _memory_backend_cached is not None:
        return _memory_backend_cached
    try:
        from .config import load_settings

        raw = str(getattr(load_settings(), "memory_backend", "") or "").strip().lower()
    except Exception:
        raw = "postgres"
    _memory_backend_cached = raw if raw in ("postgres", "vault") else "postgres"
    return _memory_backend_cached


def load_router_from_vault(path=None):
    """Load the always-loaded router from the configured backend, or raise.

    Duck-typed on purpose: all this needs is whatever ``load_router(path)``
    returns exposing ``render_for_prompt() -> str``.

    When the memory backend is Postgres there is no file to read: the router is a
    row in ``memory_documents``, returned as a ``MemoryRouter`` subclass so the
    rendering, truncation and budget behaviour are unchanged.
    """
    if path is None and _memory_backend() == "postgres":
        from .memory.pg_router import load_router_from_postgres

        return load_router_from_postgres()
    module = _load_router_module()
    loader = getattr(module, "load_router", None)
    if not callable(loader):
        raise AttributeError(
            "claire.memory.router exposes no callable load_router()"
        )
    return loader(path)


def _memory_budget_bytes() -> int:
    """The router byte budget, sourced from :mod:`claire.memory.notes`."""
    try:
        from .memory.notes import ROUTER_BUDGET_BYTES

        budget = int(ROUTER_BUDGET_BYTES)
    except Exception:  # pragma: no cover - defensive: notes.py is in-tree
        return _DEFAULT_ROUTER_BUDGET_BYTES
    return budget if budget > 0 else _DEFAULT_ROUTER_BUDGET_BYTES


def _render_router(router) -> Tuple[str, Optional[str]]:
    """Render *router*, returning ``(text, problem)`` and never raising.

    ``problem`` is a short human-readable phrase, not an exception: it ends up
    inside the prompt so a broken router is visible rather than merely logged.
    """
    if router is None:
        return "", "no router was supplied"
    render = getattr(router, "render_for_prompt", None)
    if not callable(render):
        return "", f"{type(router).__name__} exposes no callable render_for_prompt()"
    try:
        rendered = render()
    except Exception as exc:  # the failure must become visible, not fatal
        return "", f"render_for_prompt() raised {type(exc).__name__}: {exc}"
    if rendered is None:
        return "", "render_for_prompt() returned None"
    if not isinstance(rendered, str):
        return "", f"render_for_prompt() returned {type(rendered).__name__}, not str"
    text = rendered.strip()
    if not text:
        return "", "render_for_prompt() returned no text"
    return text, None


def _memory_problem_section(marker: str, problem: str) -> str:
    """A usable, loudly-named memory section for a router that could not render."""
    return (
        f"{MEMORY_SECTION_HEADING}\n\n"
        f"{marker} memory could not be loaded: {problem}. The router is "
        "re-read on every request, so fixing it takes effect on the "
        "next reply with no restart."
    )


def build_memory_section(router) -> str:
    """Render the ``## Memory (always loaded)`` section for *router*.

    Never raises and never returns an empty string: an unusable router yields a
    section that names the problem, because a prompt with a visible warning is
    debuggable while a prompt with her memory quietly missing is not.
    """
    text, problem = _render_router(router)
    if problem is not None:
        marker = (
            MEMORY_EMPTY_MARKER
            if problem == "render_for_prompt() returned no text"
            else MEMORY_UNAVAILABLE_MARKER
        )
        return _memory_problem_section(marker, problem)

    section = f"{MEMORY_SECTION_HEADING}\n\n{text}"
    budget = _memory_budget_bytes()
    size = len(section.encode("utf-8"))
    if size > budget:
        section += (
            f"\n\n{MEMORY_OVER_BUDGET_MARKER} the memory router renders to {size} "
            f"bytes, over the {budget}-byte budget. Move detail into notes "
            "and leave only pointers here: the router is a router, not a database."
        )
    return section


def _resolve_memory_section(
    router,
    memory_router,
    load_default_router: Optional[bool],
) -> Optional[str]:
    """Decide what memory, if any, belongs in the prompt.

    Three routes, in order:

    * an explicit router object (``router``/``memory_router``) is rendered;
    * ``load_default_router=True`` (or ``router=AUTO_LOAD_ROUTER``) forces a read
      of the configured backend's router, failures included;
    * ``load_default_router=None`` is automatic: the router is loaded only when
      it is already provisioned (a row in the store, or the vault file), so a
      machine with no router keeps the legacy prompt instead of acquiring a
      spurious warning.

    The backend is the Postgres memory store by default; with the memory backend
    set to ``vault`` the router is the file ``<vault>/CLAIRE.md`` instead.

    ``False`` (the default) never loads, which is what makes the no-argument
    call byte-identical to the pre-memory prompt.  ``None`` is returned when
    there is nothing to add.
    """
    explicit = router if router is not None else memory_router

    is_sentinel = explicit is True or explicit is False or explicit is AUTO_LOAD_ROUTER
    if explicit is not None and not is_sentinel:
        return build_memory_section(explicit)

    if explicit is False or (explicit is None and load_default_router is False):
        return None

    forced = (
        explicit is True
        or explicit is AUTO_LOAD_ROUTER
        or load_default_router is True
    )

    if _memory_backend() == "postgres":
        # There is no vault file to stat any more, so "provisioned" means the
        # router row exists. Everything downstream — including every marker — is
        # the same code path, so a database outage reads as
        # [memory-unavailable] rather than breaking the reply.
        from .memory.pg_router import router_exists

        if not forced and not router_exists():
            return None
        try:
            loaded = load_router_from_vault()
        except Exception as exc:
            return _memory_problem_section(
                MEMORY_UNAVAILABLE_MARKER, f"{type(exc).__name__}: {exc}"
            )
        if loaded is None:
            return _memory_problem_section(
                MEMORY_UNAVAILABLE_MARKER, "no router document in Postgres"
            )
        return build_memory_section(loaded)

    try:
        path = resolve_default_router_path()
    except Exception as exc:
        if forced:
            return _memory_problem_section(
                MEMORY_UNAVAILABLE_MARKER,
                f"cannot locate the vault router ({type(exc).__name__}: {exc})",
            )
        return None

    if not forced:
        try:
            provisioned = bool(path.exists())
        except OSError:
            provisioned = False
        if not provisioned:
            # Nothing on disk yet: leave the legacy prompt exactly as it was
            # rather than inventing a warning about a vault she has not got.
            return None

    try:
        loaded = load_router_from_vault()
    except Exception as exc:
        return _memory_problem_section(
            MEMORY_UNAVAILABLE_MARKER,
            f"{type(exc).__name__}: {exc}",
        )
    if loaded is None:
        return _memory_problem_section(
            MEMORY_UNAVAILABLE_MARKER, "load_router() returned no router"
        )
    return build_memory_section(loaded)


def build_system_prompt(
    assistant_name: str = "Claire",
    *,
    router=None,
    memory_router=None,
    load_default_router: Optional[bool] = False,
) -> str:
    """Render the persona prompt with the configured assistant name.

    The name comes from the user's wake word (capitalised); defaults to
    "Claire" when no config is available (tests, eval harnesses).

    Memory: pass *router*/*memory_router* (anything exposing
    ``render_for_prompt() -> str``) to append its rendered text under
    ``## Memory (always loaded)``; pass ``load_default_router=True`` /
    ``router=AUTO_LOAD_ROUTER`` to force a read of the configured backend's
    router, whose failures are reported inside the prompt; or pass
    ``load_default_router=None`` to load it only when it is already provisioned.
    That backend is the Postgres memory store by default; with the memory backend
    set to ``vault`` the router is the file ``<vault>/CLAIRE.md``.  With no
    router supplied the output is the legacy prompt, unchanged.  The
    router is rendered on **every** call — no caching — so memory is never
    stale; the result is one string for the system prompt, never a new
    conversation message.
    """
    name = (assistant_name or "Claire").strip() or "Claire"
    prompt = _SYSTEM_PROMPT_TEMPLATE.format(name=name)

    section = _resolve_memory_section(router, memory_router, load_default_router)
    if section is None:
        return prompt
    return f"{prompt}\n\n{section}"
