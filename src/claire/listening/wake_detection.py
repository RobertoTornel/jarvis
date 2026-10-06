"""Wake word and stop command detection logic.

Why the fuzzy fallback is fenced in (measured, not guessed)
----------------------------------------------------------
The primary name is matched *exactly* at word boundaries against the wake word
and its aliases; only then does a cheap approximate match run, because speech
recognisers emit near-misses such as "clair" or "klair".  Comparing ``claire``
against every short token with ``difflib.SequenceMatcher`` at the previous
default ratio of 0.78 was too loose — it woke her on ordinary English:

  ========= ========  ====================================================
  word      ratio     why it was a false positive
  ========= ========  ====================================================
  care      0.8000    >= 0.78, so "I do not care" woke her
  lair      0.8000    >= 0.78
  declare   0.7692    below 0.78, but any slight loosening admits it
  clear     0.7273    same
  chair     0.7273    same
  glare     0.7273    same
  flare     0.7273    same
  car       0.6667    same
  air       0.6667    same
  clue      0.6000    same
  clip      0.6000    same
  clay      0.6000    same
  rare      0.6000    same
  core      0.6000    same
  card      0.6000    same
  pair      0.6000    same
  hair      0.6000    same
  layer     0.5455    same
  share     0.5455    same
  score     0.5455    same
  player    0.5000    same
  bear      0.4000    same
  pear      0.4000    same
  cheer     0.3636    same
  ========= ========  ====================================================

The genuine mishearings measure:

  ========= ========  ====================================================
  variant   ratio     note
  ========= ========  ====================================================
  claires   0.9231    fuzzy-accepted
  clair     0.9091    fuzzy-accepted
  clare     0.9091    fuzzy-accepted
  klair     0.9091    after folding k -> c (it is spelled with the same /k/)
  klare     0.9091    after folding k -> c
  claira    0.8333    fuzzy-accepted
  claya     0.5455    ONLY via :data:`CLAIRE_WAKE_ALIASES`; see below
  ========= ========  ====================================================

So the threshold is set to :data:`DEFAULT_WAKE_FUZZY_RATIO` = 0.82: above the
worst ordinary word (0.8000, a 0.02 margin) and at or below the weakest genuine
mishearing that must still fuzzy-match (0.8333).  It cannot be pushed higher,
because ``claira`` has to keep matching when only another alias is configured.
``claya`` (0.5455) is below any safe threshold — a threshold low enough to admit
it also admits ``clay`` (0.6000) — so it is carried by the curated alias list,
which is what an alias list is for.

Two independent defences are therefore in place, so re-loosening the ratio alone
cannot reintroduce the bug:

1. the ratio must clear 0.82, and
2. the token must pass :func:`_wake_onset_allows`, which requires it to start
   with the primary name's onset consonant cluster (``k`` folded to ``c``,
   because both are /k/ before /l/).  That is what rejects ``care``, ``lair``
   and ``declare`` even if 0.78 is passed explicitly again.

Residual, honestly recorded: ``claret`` (0.8333) ties with ``claira`` (0.8333),
so no threshold that keeps ``claira`` can exclude ``claret``.  It is an obscure
wine/colour word, it already matched before this change, and ``claira`` is the
required mishearing, so the overlap is kept.  See
``tests/test_wake_false_positives.py::test_residual_overlap_of_the_plain_string_metric_is_documented``.
"""

from typing import List, Optional, Tuple
import difflib
import re
import unicodedata

from ..debug import debug_log

#: Curated mishearings of the name "Claire" ONLY.  Every entry is a plausible
#: pronunciation/spelling of the name; no ordinary English word belongs here.
#: This is the same list as ``config.DEFAULT_CONFIG["wake_aliases"]``, exposed so
#: the curated set has one obvious home next to the matcher that uses it.
CLAIRE_WAKE_ALIASES: Tuple[str, ...] = (
    "clair",
    "clare",
    "claira",
    "claya",
    "klair",
    "klare",
    "claires",
)

#: Ratio for the fuzzy fallback on the *primary* name.  Measured separation:
#: worst ordinary word 0.8000 ("care", "lair") vs weakest required mishearing
#: 0.8333 ("claira").  0.78 is too loose — see the module docstring.
#: ``src/claire/config.py`` should import this and use it as the
#: ``wake_fuzzy_ratio`` default (currently 0.78); the structural guard below
#: keeps even the 0.78 config value safe in the meantime.
DEFAULT_WAKE_FUZZY_RATIO: float = 0.82

#: 'k' and 'c' are both /k/ before 'l', so "klair" sounds like "clair".
_K_TO_C = str.maketrans({"k": "c"})


def _fold_wake_phonetics(value: str) -> str:
    """Fold the spellings that stand for the same sound before comparing."""
    return value.translate(_K_TO_C)


def _wake_onset_allows(token: str, primary: str) -> bool:
    """Cheap structural guard: a variant must share the name's onset cluster.

    "care", "lair", "declare" and friends score surprisingly high on a plain
    character ratio, but none of them *starts* like the name.  Folding k -> c
    first keeps "klair"/"klare" (same /kl/ onset) working.  Applied to the
    possessive/plural token too, which is why "claires" still passes.
    """
    folded_primary = _fold_wake_phonetics(primary)
    onset = folded_primary[:2] if len(folded_primary) >= 2 else folded_primary
    if not onset:
        return False
    return _fold_wake_phonetics(token).startswith(onset)


def _wake_name_pattern(wake_word: str, aliases: List[str]) -> Optional[re.Pattern[str]]:
    """Match literal configured names at Unicode word boundaries."""
    names = {name.strip() for name in [wake_word, *aliases] if name.strip()}
    if not names:
        return None
    alternatives = "|".join(re.escape(name) for name in sorted(names, key=len, reverse=True))
    return re.compile(r"(?<!\w)(?:" + alternatives + r")(?!\w)", re.IGNORECASE)


def is_wake_word_detected(text_lower: str, wake_word: str, aliases: List[str],
                          fuzzy_ratio: float = DEFAULT_WAKE_FUZZY_RATIO) -> bool:
    """Accept whole configured names, or a structurally plausible mishearing.

    The exact, word-bounded match against ``wake_word`` plus ``aliases`` runs
    first and is the primary path.  The fuzzy fallback exists only for
    transcriptions that are *shaped* like the name: the token must pass
    :func:`_wake_onset_allows` (same onset consonant cluster) and reach
    ``fuzzy_ratio``, which is why ordinary English such as "care", "clear" or
    "declare" can no longer wake her.
    """
    if not text_lower or not text_lower.strip():
        return False

    pattern = _wake_name_pattern(wake_word, aliases)
    if pattern and pattern.search(text_lower):
        return True

    # Aliases already represent known mishearings; do not approximate them again.
    primary = wake_word.strip().casefold()
    if not primary or any(char.isspace() for char in primary):
        return False
    folded_primary = _fold_wake_phonetics(primary)
    for token in re.findall(r"\w+", text_lower.casefold(), re.UNICODE):
        # Longer tokens stay near the name; shorter pronunciations use the threshold.
        if len(token) > len(primary) + 1:
            continue
        if not _wake_onset_allows(token, primary):
            continue
        ratio = difflib.SequenceMatcher(a=folded_primary, b=_fold_wake_phonetics(token)).ratio()
        if ratio >= fuzzy_ratio:
            debug_log(f"primary wake name fuzzy match (ratio={ratio:.3f})", "wake")
            return True
    return False


def extract_query_after_wake(text_lower: str, wake_word: str, aliases: List[str]) -> str:
    """
    Extract the query portion after removing wake word.
    
    Args:
        text_lower: Lowercase text containing wake word
        wake_word: Primary wake word
        aliases: List of wake word aliases
    
    Returns:
        Query text with wake word removed
    """
    if not text_lower:
        return ""
    
    pattern = _wake_name_pattern(wake_word, aliases)
    fragment = pattern.sub(" ", text_lower) if pattern else text_lower
    
    # Clean up punctuation that might be left after wake word removal
    fragment = fragment.strip().lstrip(",.!?;:")
    fragment = fragment.strip()
    
    return fragment if fragment else ""


def is_stop_command(text_lower: str, stop_commands: List[str], fuzzy_ratio: float = 0.8) -> bool:
    """
    Check if text contains a stop command.
    
    Args:
        text_lower: Lowercase text to check
        stop_commands: List of stop command phrases
        fuzzy_ratio: Threshold for fuzzy matching short inputs
    
    Returns:
        True if stop command detected
    """
    if not text_lower or not text_lower.strip():
        return False
    
    # Check for exact matches
    detected_commands = []
    for cmd in stop_commands:
        if cmd in text_lower:
            detected_commands.append(cmd)
    
    # Check fuzzy matches for short inputs (2 words or less)
    if len(text_lower.split()) <= 2:
        try:
            for word in text_lower.split():
                for cmd in stop_commands:
                    ratio = difflib.SequenceMatcher(a=cmd, b=word).ratio()
                    if ratio >= fuzzy_ratio:
                        detected_commands.append(f"{cmd}~{word}")
        except Exception:
            pass
    
    if detected_commands:
        debug_log(f"stop command detected: {detected_commands[0]} in '{text_lower}'", "voice")
        return True
    
    return False


def is_stop_command_echo(text: str, tts_text: str, stop_commands: List[str], fuzzy_ratio: float = 0.8) -> bool:
    """Recognise literal TTS echo while preserving standalone control priority."""
    def normalise(value: str) -> str:
        value = unicodedata.normalize("NFC", value).casefold()
        value = "".join(" " if unicodedata.category(char).startswith("P") else char
                        for char in value)
        return " ".join(value.split())

    heard = normalise(text)
    spoken = normalise(tts_text)
    if not heard or not spoken:
        return False
    for command in stop_commands:
        control = normalise(command)
        if control and difflib.SequenceMatcher(a=heard, b=control).ratio() >= fuzzy_ratio:
            return False
    return heard in spoken
