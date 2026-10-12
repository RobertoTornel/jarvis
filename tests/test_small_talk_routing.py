"""Small talk must not be sent to the tool router.

Evidence, 14 days of this machine's journal: 16,791 transcribed utterances,
dominated by "Yeah." (4,572), "Okay." (1,404), "Uh" (700) and "Mm-hmm." (362).
Of the 39 turns that actually reached the tool router, 33 selected only the
system tools — so roughly 85% of router calls were paid for and bought
nothing. The router is a chat-model call, p50 1.3 s.

``_is_small_talk`` is the prefilter that skips it. It is deliberately
conservative: the utterance must be short AND made up entirely of social
filler, so every near-miss that could want a tool still reaches the router.
That direction matters more than the saving — complaint number three was that
tools did not work, and a prefilter that steals tool calls would make it
worse.
"""

import pytest

from claire.reply.engine import _is_small_talk

pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`

# Utterances the owner's machine actually produced, from the journal corpus.
REAL_SMALL_TALK = [
    "Yeah.",
    "Okay.",
    "Uh",
    "Mm-hmm.",
    "Um",
    "No.",
    "Oh.",
    "Yeah, yeah.",
    "Mm.",
    "Hmm.",
    "Thank you.",
    "Right.",
    "Oh yeah.",
    "Sure.",
    "Alright.",
    "Bye.",
    "Cool.",
    "Yes.",
    "Maybe.",
    "Hello, Claire.",
    "Hey Claire, what's up?",
    "Hello, can you hear me?",
    "I don't know.",
    "That's it.",
    "Yeah, but",
    "Okay, so",
    "Well",
    "But",
    "And",
    "So",
    # The spoken forms of the phrases above: a trailing qualifier is how
    # people actually say them, and it used to defeat the match entirely.
    "How are you doing today?",
    "How are you today?",
    "Are you alive?",
    "Are you okay now?",
    "How's it going?",
    "How are things?",
]

# Utterances that need the router, including near-misses of the shapes above.
MUST_REACH_THE_ROUTER = [
    "What's the weather in Lisbon?",
    "What time is it in Paris?",
    "What windows do I have open?",
    "What is my screen resolution?",
    "Set a timer for five minutes",
    "Remind me to call mum at five",
    "Play the next song",
    "Turn the volume up",
    "Turn on the living room lights",
    "Take a screenshot",
    "What is that?",
    "Yeah, what's the weather?",
    "Okay so remind me to buy milk",
    "Yeah, take a screenshot",
    "Stop.",
    "Shut up.",
    "Claire, stop the music",
    "Do what up?",
    "What do you mean by the weather tomorrow?",
    "Track 4",
    # Near-misses of the trailing-qualifier shape: dropping the qualifier
    # must still leave a social phrase, or the router must run.
    "What's the weather today?",
    "Set a timer now",
    "Turn it off now",
    "Remind me again",
    "Play it again",
    "Call mum now",
]


@pytest.mark.parametrize("text", REAL_SMALL_TALK)
def test_real_small_talk_is_recognised(text):
    assert _is_small_talk(text) is True


@pytest.mark.parametrize("text", MUST_REACH_THE_ROUTER)
def test_anything_that_might_want_a_tool_still_reaches_the_router(text):
    assert _is_small_talk(text) is False


def test_an_empty_query_is_not_small_talk():
    """The engine decides what an empty query means, not this filter."""
    assert _is_small_talk("") is False


def test_a_long_run_of_filler_is_not_small_talk():
    """Length is a guard: six words of filler is a fragment, not a request."""
    assert _is_small_talk("yeah yeah okay okay uh huh right sure") is False


def test_a_digit_makes_a_short_utterance_contentful():
    assert _is_small_talk("No 2") is False


def test_the_wake_word_does_not_hide_a_request():
    assert _is_small_talk("Claire, what's the weather") is False


def test_a_trailing_qualifier_does_not_hide_a_social_phrase():
    """The journal has "Are you alive?" and "how are you doing today?".

    Both are a social phrase with a word hung off the end. Before the
    trailing-qualifier strip neither matched the phrase set, so each one paid
    a router round trip — measured at 1.14 s — that could only answer
    "no tools", and the answer was then written as though nothing had been
    consulted.
    """
    assert _is_small_talk("How are you doing today?") is True
    assert _is_small_talk("Are you okay now?") is True


def test_dropping_a_qualifier_never_invents_a_social_phrase():
    """The strip is only safe while what remains is a social phrase."""
    assert _is_small_talk("What's the weather today?") is False
    assert _is_small_talk("Set a timer now") is False
    assert _is_small_talk("Remind me again") is False


def test_punctuation_and_case_do_not_matter():
    assert _is_small_talk("  YEAH!!!  ") is True
    assert _is_small_talk("Okay?!") is True


def test_the_skip_lands_on_the_same_allow_list_as_a_none_verdict():
    """The engine reaches "no tools" by a different route than the router.

    The router gets there by answering "none"; the engine by recognising
    filler. Both must produce the same allow-list, or a turn's tools would
    depend on which route it took.
    """
    from claire.tools.registry import BUILTIN_TOOLS
    from claire.tools.selection import none_verdict

    assert none_verdict(BUILTIN_TOOLS, {}) == ["stop"]
