"""Ordinary English must never wake Claire, and real mishearings must.

The bug this suite exists to catch: the fuzzy fallback in
``claire.listening.wake_detection.is_wake_word_detected`` compared the primary
name against *any* short token with ``difflib.SequenceMatcher``.  At the old
default ratio of 0.78 that accepted ``care`` and ``lair`` (both 0.8000 against
``claire``), so "I do not care" woke her.  A looser ratio would also have
dragged in ``clear``, ``chair``, ``declare``, ``clue`` and friends.

Two defences are asserted here, and both are checked *behaviourally* (the
function is called) as well as numerically (the ratios are asserted):

1. ``DEFAULT_WAKE_FUZZY_RATIO`` must sit above every ordinary word's ratio;
2. the structural onset guard must reject ordinary words *even if* someone
   passes the old 0.78 ratio explicitly.

The threshold is read back out of the live function signature rather than
hard-coded, so deleting or loosening the constant cannot make this suite pass
silently.  Deliberately no ``getattr(..., default)`` fallbacks anywhere: a
missing constant must be an error, not a quietly-passing test.
"""

from __future__ import annotations

import difflib
import inspect
from typing import Dict, List, Tuple

import pytest

from claire.config import get_default_config
from claire.listening.wake_detection import extract_query_after_wake
from claire.listening.wake_detection import is_stop_command_echo
from claire.listening.wake_detection import is_wake_word_detected

import claire.listening.wake_detection as wake_detection

pytestmark = pytest.mark.unit

PRIMARY = "claire"
LEGACY_RATIO = 0.78
CONFIG = get_default_config()
CONFIG_ALIASES: List[str] = list(CONFIG["wake_aliases"])

#: The curated mishearings, written out here independently of the module under
#: test so the two have to agree (test_curated_alias_constant_... below).
EXPECTED_ALIASES: Tuple[str, ...] = (
    "clair",
    "clare",
    "claira",
    "claya",
    "klair",
    "klare",
    "claires",
)

#: The ordinary words named in the work order.  None of these may ever wake her.
ORDINARY_WORDS: Tuple[str, ...] = (
    "clear",
    "chair",
    "declare",
    "clue",
    "clip",
    "care",
    "layer",
    "player",
    "clay",
    "glare",
    "flare",
    "cheer",
    "share",
    "rare",
    "core",
    "score",
    "card",
    "car",
    "air",
    "lair",
    "pair",
    "hair",
    "bear",
    "pear",
)

ORDINARY_SENTENCES: Tuple[str, ...] = (
    "the coast is clear",
    "pull up a chair",
    "I do not care",
    "that is my clue",
    "clip that",
    "layer it",
)

#: Mishearings that must wake her through the fuzzy path *without* any alias
#: list, because their shape is close enough to the primary name.  ``claya`` is
#: deliberately absent: its ratio is only 0.5455, far below any safe fuzzy
#: threshold, so it is carried by the curated alias list instead.
FUZZY_ONLY_MISHEARINGS: Tuple[str, ...] = (
    "clair",
    "clare",
    "claira",
    "klair",
    "klare",
    "claires",
)

ACCEPTED_PHRASES: Tuple[str, ...] = (
    "hey claire",
    "claire, what time is it",
    "Claire's calendar",
    "clair, what time is it?",
    "claires, are you there",
    "claya, what time is it",
    "hey klair",
)


def _effective_default_ratio() -> float:
    """The ratio the live function actually uses when no ratio is passed."""
    return inspect.signature(is_wake_word_detected).parameters["fuzzy_ratio"].default


def _ratio(word: str) -> float:
    return difflib.SequenceMatcher(a=PRIMARY, b=word).ratio()


def _fold_k_to_c(word: str) -> str:
    """'k' and 'c' are both /k/ before 'l': 'klair' sounds like 'clair'."""
    return word.replace("k", "c")


# ---------------------------------------------------------------------------
# Measured numbers: the threshold cannot silently regress.
# ---------------------------------------------------------------------------


def test_measured_ratios_are_printed_and_the_threshold_has_real_margin():
    """Print the measurement table and pin the separation it justifies."""
    threshold = _effective_default_ratio()
    ordinary = sorted(((_ratio(w), w) for w in ORDINARY_WORDS), reverse=True)
    aliases = sorted(((_ratio(a), a) for a in EXPECTED_ALIASES), reverse=True)
    worst_ordinary, worst_ordinary_word = ordinary[0]
    best_alias, best_alias_word = aliases[0]

    print("\nmeasured difflib ratios against 'claire' (threshold = %.4f)" % threshold)
    for ratio, word in ordinary:
        print("  ordinary %-9s %.4f" % (word, ratio))
    for ratio, word in aliases:
        print("  alias    %-9s %.4f" % (word, ratio))
    print(
        "  worst ordinary: %s (%.4f); best alias: %s (%.4f); threshold: %.4f"
        % (worst_ordinary_word, worst_ordinary, best_alias_word, best_alias, threshold)
    )

    # The whole point of the fix: 0.78 is too loose, because 'care' scores 0.800.
    assert worst_ordinary >= LEGACY_RATIO, (
        "the guard is only meaningful while an ordinary word actually scores at "
        "or above the legacy 0.78 threshold"
    )
    assert _ratio("care") == pytest.approx(0.800, abs=1e-3)
    assert _ratio("lair") == pytest.approx(0.800, abs=1e-3)

    # Every ordinary word must sit strictly below the chosen threshold.
    assert threshold > worst_ordinary, (
        f"threshold {threshold:.4f} does not clear the worst ordinary word "
        f"{worst_ordinary_word!r} at {worst_ordinary:.4f}"
    )
    assert threshold - worst_ordinary >= 0.01, "separation margin is too thin"

    # ...and must still admit the genuine fuzzy mishearings.
    assert threshold <= _ratio("claira"), (
        f"threshold {threshold:.4f} would reject the genuine mishearing 'claira' "
        f"at {_ratio('claira'):.4f}"
    )


def test_default_ratio_constant_is_exposed_and_stricter_than_the_legacy_value():
    assert isinstance(wake_detection.DEFAULT_WAKE_FUZZY_RATIO, float)
    assert wake_detection.DEFAULT_WAKE_FUZZY_RATIO > LEGACY_RATIO
    assert wake_detection.DEFAULT_WAKE_FUZZY_RATIO == _effective_default_ratio(), (
        "the exposed constant and the function default must be the same value, "
        "or config cannot adopt it safely"
    )


def test_curated_alias_constant_keeps_ordinary_words_out():
    assert wake_detection.CLAIRE_WAKE_ALIASES is not None
    assert tuple(wake_detection.CLAIRE_WAKE_ALIASES) == EXPECTED_ALIASES
    assert set(wake_detection.CLAIRE_WAKE_ALIASES) == set(CONFIG_ALIASES)
    for word in ORDINARY_WORDS:
        assert word not in wake_detection.CLAIRE_WAKE_ALIASES
    # Every curated alias is a non-empty single token near the primary name.
    for alias in wake_detection.CLAIRE_WAKE_ALIASES:
        assert alias and not any(char.isspace() for char in alias)
        assert abs(len(alias) - len(PRIMARY)) <= 1


def test_every_ordinary_word_ratio_is_below_the_threshold():
    threshold = _effective_default_ratio()
    offenders = {w: _ratio(w) for w in ORDINARY_WORDS if _ratio(w) >= threshold}
    assert offenders == {}, f"ordinary words at or above {threshold:.4f}: {offenders}"


# ---------------------------------------------------------------------------
# Behaviour: ordinary words and sentences must not wake her.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("word", ORDINARY_WORDS)
def test_ordinary_word_does_not_wake_her(word):
    assert is_wake_word_detected(word, PRIMARY, CONFIG_ALIASES) is False
    assert is_wake_word_detected(word.upper(), PRIMARY, CONFIG_ALIASES) is False
    assert is_wake_word_detected(f"hey {word}", PRIMARY, CONFIG_ALIASES) is False


@pytest.mark.parametrize("sentence", ORDINARY_SENTENCES)
def test_ordinary_sentence_does_not_wake_her(sentence):
    assert is_wake_word_detected(sentence, PRIMARY, CONFIG_ALIASES) is False
    assert is_wake_word_detected(sentence.lower(), PRIMARY, CONFIG_ALIASES) is False


@pytest.mark.parametrize("word", ORDINARY_WORDS)
def test_ordinary_word_is_still_safe_at_the_legacy_ratio(word):
    """Even if someone re-passes 0.78, the structural guard must hold."""
    assert is_wake_word_detected(word, PRIMARY, CONFIG_ALIASES, LEGACY_RATIO) is False


@pytest.mark.parametrize("sentence", ORDINARY_SENTENCES)
def test_ordinary_sentence_is_still_safe_at_the_legacy_ratio(sentence):
    assert is_wake_word_detected(sentence, PRIMARY, CONFIG_ALIASES, LEGACY_RATIO) is False


@pytest.mark.parametrize("word", ORDINARY_WORDS)
def test_ordinary_word_is_safe_even_with_no_alias_list(word):
    assert is_wake_word_detected(word, PRIMARY, []) is False


def test_nearly_misspelled_ordinary_words_are_safe():
    # Words that merely *contain* the letters of 'claire'.  The whole-word
    # pattern and the length window keep the long ones out; 'eclair' is kept out
    # by the onset guard, since it does not start with /kl/.
    for text in (
        "claireian is a made-up word",
        "the claireville address",
        "clairvoyant",
        "eclair",
        "the declaration of independence",
    ):
        assert is_wake_word_detected(text, PRIMARY, CONFIG_ALIASES) is False, text


def test_residual_overlap_of_the_plain_string_metric_is_documented():
    """'claret' ties with the genuine mishearing 'claira' at 0.8333.

    The threshold therefore cannot drop below 0.8333 to exclude it — and the
    existing alias-boundary suite pins the default at or below that, because
    ``claira`` must still fuzzy-match when only the ``clare`` alias is
    configured.  This test records the residue rather than hiding it.
    """
    assert _ratio("claret") == pytest.approx(_ratio("claira"), abs=1e-4)
    assert _ratio("claret") >= _effective_default_ratio()


# ---------------------------------------------------------------------------
# Behaviour: genuine mishearings and accepted phrases must still wake her.
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("alias", EXPECTED_ALIASES)
def test_every_curated_alias_wakes_her(alias):
    assert is_wake_word_detected(alias, PRIMARY, CONFIG_ALIASES) is True
    assert is_wake_word_detected(f"{alias}, what time is it?", PRIMARY, CONFIG_ALIASES) is True


@pytest.mark.parametrize("mishearing", FUZZY_ONLY_MISHEARINGS)
def test_genuine_mishearing_wakes_her_without_an_alias_list(mishearing):
    assert is_wake_word_detected(f"hey {mishearing}", PRIMARY, []) is True


def test_claya_is_carried_by_the_curated_alias_list_not_by_fuzzy_matching():
    """Its ratio (0.5455) is far below any threshold that rejects 'clay' (0.6)."""
    assert _ratio("claya") < _effective_default_ratio()
    assert "claya" in wake_detection.CLAIRE_WAKE_ALIASES
    assert is_wake_word_detected("claya, hello", PRIMARY, CONFIG_ALIASES) is True


@pytest.mark.parametrize("phrase", ACCEPTED_PHRASES)
def test_accepted_phrase_wakes_her(phrase):
    assert is_wake_word_detected(phrase, PRIMARY, CONFIG_ALIASES) is True


def test_possessive_and_punctuated_forms_wake_her():
    for text in (
        "Claire's calendar",
        "claire's, what's next",
        "claires",
        "CLAIRES!",
        "  clair  ",
    ):
        assert is_wake_word_detected(text, PRIMARY, CONFIG_ALIASES) is True, text


# ---------------------------------------------------------------------------
# No regression in the neighbouring contracts of the same module.
# ---------------------------------------------------------------------------


def test_extract_query_after_wake_still_works():
    assert extract_query_after_wake("claire what time is it", PRIMARY, []) == "what time is it"
    assert (
        extract_query_after_wake("hey computer what time is it", PRIMARY, ["hey computer"])
        == "what time is it"
    )
    assert extract_query_after_wake("claire", PRIMARY, []) == ""
    assert extract_query_after_wake("", PRIMARY, []) == ""


def test_extract_query_does_not_strip_an_ordinary_word():
    assert extract_query_after_wake("the coast is clear", PRIMARY, CONFIG_ALIASES) == (
        "the coast is clear"
    )


def test_echo_and_self_speech_rejection_still_works():
    stop_commands = ["stop", "be quiet"]
    # A literal echo of her own speech is recognised.
    assert is_stop_command_echo("i will be there soon", "i will be there soon", stop_commands) is True
    # A standalone control word keeps priority over echo rejection.
    assert is_stop_command_echo("stop", "stop talking now please", stop_commands) is False
    assert is_stop_command_echo("", "stop talking now please", stop_commands) is False


def test_ratio_table_matches_the_words_the_bug_report_names():
    """The words in the report, split into 'actually woke her' and 'did not'."""
    reported: Dict[str, float] = {
        word: _ratio(word)
        for word in (
            "clear",
            "chair",
            "declare",
            "clue",
            "clip",
            "care",
            "layer",
            "player",
        )
    }
    print("\nreported false positives: " + ", ".join(
        f"{w}={r:.4f}" for w, r in sorted(reported.items(), key=lambda i: -i[1])
    ))
    # care and lair are the two that actually crossed 0.78; the rest are the
    # words a looser threshold would have dragged in.
    assert reported["care"] >= LEGACY_RATIO
    assert reported["declare"] < LEGACY_RATIO
    assert max(reported.values()) < _effective_default_ratio()
