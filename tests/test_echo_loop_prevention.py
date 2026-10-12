"""Regression tests for the self-talk (TTS echo) runaway loop.

Observed live: Claire's own speech was picked up by the always-open microphone,
transcribed by Whisper into unrelated garble ("Yeah." / "There"), accepted as a
genuine hot-window follow-up, answered — and her answer's TTS re-armed the hot
window, forever.

The failure is that text similarity cannot recognise a *garbled* echo:

    partial_ratio("yeah.", "It is a lovely day today, isn't it?") < 70

so the text-only ``is_pure_echo`` guard in the follow-up accept path says "not
echo". These tests pin the acoustic/temporal defence instead:

  (a) audio captured while Claire is speaking is rejected outright unless the
      utterance carries an explicit wake word;
  (b) audio that begins just after she finishes is rejected until the room's
      acoustic tail guard has elapsed;
  (c) the hot window cannot re-arm itself off Claire's own replies forever.

Genuine human follow-ups (not during TTS, after the tail guard) must still be
accepted — see ``TestGenuineFollowUpStillWorks``.

Tests drive ``VoiceListener._process_transcript`` with real ``StateManager`` and
``EchoDetector`` instances; only audio, the recogniser, and the intent judge are
mocked.
"""

import time
from unittest.mock import patch, MagicMock

import pytest

from claire.listening.state_manager import StateManager, ListeningState
from claire.listening.intent_judge import IntentJudgment
from claire.listening.listener import (
    POST_TTS_TAIL_GUARD_SECONDS,
    MAX_CONSECUTIVE_HOT_WINDOW_TURNS,
)

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _create_listener(**kwargs):
    """Create a VoiceListener with mocked heavy subsystems.

    Uses a real StateManager and EchoDetector so the accept decision is
    exercised end to end. Returns (listener, mock_tts).
    """
    mock_cfg = MagicMock()
    mock_cfg.whisper_model = "small"
    mock_cfg.whisper_device = "auto"
    mock_cfg.whisper_compute_type = "int8"
    mock_cfg.whisper_backend = "faster-whisper"
    mock_cfg.sample_rate = 16000
    mock_cfg.vad_enabled = False
    mock_cfg.vad_aggressiveness = 2
    mock_cfg.echo_tolerance = kwargs.get("echo_tolerance", 0.05)
    mock_cfg.echo_energy_threshold = 2.0
    mock_cfg.hot_window_seconds = kwargs.get("hot_window_seconds", 3.0)
    mock_cfg.hot_window_enabled = True
    # Defaults to the module constant so existing loop-breaker tests keep the
    # legacy one-follow-up chain; new tests override it to exercise the config
    # key. `cfg_omits` leaves the attribute off entirely, exercising the
    # listener's getattr fallback.
    mock_cfg.hot_window_max_follow_ups = kwargs.get(
        "hot_window_max_follow_ups", MAX_CONSECUTIVE_HOT_WINDOW_TURNS
    )
    mock_cfg.voice_collect_seconds = 2.0
    mock_cfg.voice_max_collect_seconds = 60.0
    mock_cfg.voice_device = None
    mock_cfg.voice_debug = False
    mock_cfg.voice_min_energy = 0.0045
    mock_cfg.tune_enabled = False
    mock_cfg.wake_word = "claire"
    mock_cfg.wake_aliases = []
    mock_cfg.wake_fuzzy_ratio = 0.78
    mock_cfg.stop_commands = ["stop", "quiet", "shush", "silence", "enough", "shut up"]
    mock_cfg.tts_rate = 200
    mock_cfg.transcript_buffer_duration_sec = 120.0
    mock_cfg.fast_model = "gemma4:e2b"
    mock_cfg.ollama_base_url = "http://127.0.0.1:11434"
    mock_cfg.intent_judge_timeout_sec = 3.0
    mock_db = MagicMock()
    mock_tts = MagicMock()
    mock_tts.enabled = True
    mock_tts.is_speaking.return_value = kwargs.get("tts_speaking", False)
    mock_dialogue_memory = MagicMock()

    if kwargs.get("cfg_omits"):
        omitted = set(kwargs["cfg_omits"])
        wrapped = mock_cfg

        class _CfgOmitting:
            """Config proxy that raises AttributeError for selected names.

            Lets a test exercise a getattr(...) fallback on a key the loaded
            Settings would always carry.
            """

            def __getattr__(self, name):
                if name in omitted:
                    raise AttributeError(name)
                return getattr(wrapped, name)

        mock_cfg = _CfgOmitting()

    with patch("claire.listening.listener.webrtcvad", None), \
         patch("claire.listening.listener.sd", None), \
         patch("claire.listening.listener.np", None), \
         patch("claire.listening.listener.create_intent_judge", return_value=None):
        from claire.listening.listener import VoiceListener
        listener = VoiceListener(mock_db, mock_cfg, mock_tts, mock_dialogue_memory)

    return listener, mock_tts


def _make_judgment(directed=True, query="", stop=False, confidence="high", reasoning="test"):
    return IntentJudgment(
        directed=directed, query=query, stop=stop,
        confidence=confidence, reasoning=reasoning,
    )


def _install_intent_judge(listener, judgment):
    """Replace the listener's intent judge with a mock returning *judgment*."""
    mock_judge = MagicMock()
    mock_judge.available = True
    mock_judge.judge.return_value = judgment
    listener._intent_judge = mock_judge
    return mock_judge


def _accepted_query(listener) -> str:
    """The query the listener actually took, or "" when input was rejected."""
    if listener.state_manager.get_pending_query():
        return listener.state_manager.get_pending_query()
    return ""


def _start_tts(listener, text):
    """Track a TTS utterance and return its start timestamp."""
    listener.echo_detector.track_tts_start(text)
    return listener.echo_detector._tts_start_time


def _finish_tts(listener):
    """Finish TTS and arm the hot window the way the TTS callback does."""
    listener.activate_hot_window()
    return listener.echo_detector._last_tts_finish_time


def _wait_for_hot_window_active(listener, timeout=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if listener.state_manager.is_hot_window_active():
            return True
        time.sleep(0.01)
    return False


def _wait_manager_active(manager, timeout=0.5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if manager.is_hot_window_active():
            return True
        time.sleep(0.01)
    return False


# ---------------------------------------------------------------------------
# (a) + the reproduction: a garble of Claire's own voice is rejected
# ---------------------------------------------------------------------------

class TestGarbledEchoIsRejected:
    """The runaway's exact shape: TTS text, but a short unrelated transcript."""

    @patch("builtins.print")
    def test_garbled_echo_during_tts_is_rejected(self, _print):
        """REPRODUCTION: 'Yeah.' transcribed from Claire's own voice is rejected.

        Before the acoustic gate this was accepted: the fuzzy echo score of
        "yeah." against the long TTS sentence is far below 70, so ``is_pure_echo``
        was False and the utterance became a follow-up — which re-armed the
        window and looped forever.
        """
        listener, _ = _create_listener()
        tts_start = _start_tts(listener, "It is a lovely day today, isn't it?")

        # A mock judge would happily accept the garble as a real follow-up.
        judge = _install_intent_judge(
            listener, _make_judgment(directed=True, query="tell me more"))

        # TTS has finished by the time the worker processes the transcript, but
        # the audio was captured while she was still speaking.
        tts_finish = _finish_tts(listener)
        assert _wait_for_hot_window_active(listener), "hot window should be open"

        listener._process_transcript(
            "Yeah.",
            utterance_energy=0.01,
            utterance_start_time=tts_start + 0.02,
            utterance_end_time=tts_finish + 0.02,
            generation=listener._capture_generation,
            captured_during_tts=True,
            captured_tts_start_time=tts_start,
        )

        assert _accepted_query(listener) == "", (
            "a garble of Claire's own voice must not be accepted as a follow-up "
            "(this is the self-talk runaway)"
        )
        assert judge.judge.call_count == 0, (
            "the acoustic gate must reject before the intent judge can be fooled "
            "by a plausible-looking transcript"
        )
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_garbled_echo_of_different_sentence_is_rejected(self, _print):
        """Same reproduction with the other garble seen in the journal ("There")."""
        listener, _ = _create_listener()
        tts_start = _start_tts(listener, "The current temperature is nine degrees.")
        judge = _install_intent_judge(
            listener, _make_judgment(directed=True, query="what about tomorrow"))
        tts_finish = _finish_tts(listener)
        _wait_for_hot_window_active(listener)

        listener._process_transcript(
            "There",
            utterance_energy=0.01,
            utterance_start_time=tts_start + 0.05,
            utterance_end_time=tts_finish,
            generation=listener._capture_generation,
            captured_during_tts=True,
            captured_tts_start_time=tts_start,
        )

        assert _accepted_query(listener) == ""
        assert judge.judge.call_count == 0
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_echo_with_no_hot_window_still_rejected(self, _print):
        """The gate is independent of hot-window state (defence in depth)."""
        listener, _ = _create_listener(echo_tolerance=0.05)
        tts_start = _start_tts(listener, "A fairly long spoken answer, spoken aloud.")
        judge = _install_intent_judge(
            listener, _make_judgment(directed=True, query="tell me more"))
        tts_finish = _finish_tts(listener)
        # Cancel the window so only the acoustic gate can reject this.
        listener.state_manager.cancel_hot_window_activation()

        listener._process_transcript(
            "Yeah.",
            utterance_energy=0.01,
            utterance_start_time=tts_start + 0.02,
            utterance_end_time=tts_finish,
            generation=listener._capture_generation,
            captured_during_tts=True,
            captured_tts_start_time=tts_start,
        )

        assert _accepted_query(listener) == ""
        assert judge.judge.call_count == 0
        listener.state_manager.stop()


# ---------------------------------------------------------------------------
# (b) the post-TTS acoustic tail guard
# ---------------------------------------------------------------------------

class TestPostTTSTailGuard:
    """0.3 s echo_tolerance was too short; the tail guard extends rejection."""

    @patch("builtins.print")
    def test_audio_just_after_tts_finish_is_rejected(self, _print):
        """Audio beginning 0.1 s after TTS finish is still the room's echo."""
        listener, _ = _create_listener()
        tts_start = _start_tts(listener, "It is a lovely day today.")
        judge = _install_intent_judge(
            listener, _make_judgment(directed=True, query="tell me more"))
        tts_finish = _finish_tts(listener)
        _wait_for_hot_window_active(listener)

        # Captured *after* TTS finished (so captured_during_tts is False when
        # echo_tolerance is short) but inside the acoustic tail guard.
        listener._process_transcript(
            "tell me more",
            utterance_energy=0.01,
            utterance_start_time=tts_finish + 0.1,
            utterance_end_time=tts_finish + 0.5,
            generation=listener._capture_generation,
            captured_during_tts=False,
            captured_tts_start_time=tts_start,
        )

        assert POST_TTS_TAIL_GUARD_SECONDS > 0.3, "0.3 s is demonstrably too short"
        assert _accepted_query(listener) == ""
        assert judge.judge.call_count == 0
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_audio_before_tts_finish_is_rejected(self, _print):
        """An utterance that began mid-TTS is rejected even without the flag."""
        listener, _ = _create_listener()
        tts_start = _start_tts(listener, "It is a lovely day today.")
        _install_intent_judge(
            listener, _make_judgment(directed=True, query="tell me more"))
        _finish_tts(listener)
        _wait_for_hot_window_active(listener)

        listener._process_transcript(
            "Yeah.",
            utterance_energy=0.01,
            utterance_start_time=tts_start + 0.01,
            utterance_end_time=tts_start + 0.3,
            generation=listener._capture_generation,
            captured_during_tts=False,
            captured_tts_start_time=tts_start,
        )

        assert _accepted_query(listener) == ""
        listener.state_manager.stop()


# ---------------------------------------------------------------------------
# (d) + (e) genuine behaviour preserved
# ---------------------------------------------------------------------------

class TestGenuineFollowUpStillWorks:
    """A real human follow-up after the tail guard is still accepted."""

    @patch("builtins.print")
    def test_follow_up_after_tail_guard_accepted(self, _print):
        listener, _ = _create_listener()
        tts_start = _start_tts(listener, "The weather is sunny today.")
        _finish_tts(listener)
        assert _wait_for_hot_window_active(listener)

        # A human cannot answer before Claire has finished and the room settled.
        time.sleep(POST_TTS_TAIL_GUARD_SECONDS + 0.05)
        speech_start = time.time()
        _install_intent_judge(listener, _make_judgment(directed=True, query="thanks"))

        listener._process_transcript(
            "thanks",
            utterance_energy=0.01,
            utterance_start_time=speech_start,
            utterance_end_time=speech_start + 0.4,
            generation=listener._capture_generation,
            captured_during_tts=False,
            captured_tts_start_time=tts_start,
        )

        assert _accepted_query(listener) == "thanks"
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_wake_word_over_tts_is_still_honoured(self, _print):
        """A deliberate "Claire ..." over her voice is not mistaken for echo."""
        listener, _ = _create_listener()
        tts_start = _start_tts(listener, "A long answer that keeps going for a while.")
        _install_intent_judge(
            listener, _make_judgment(directed=True, query="what about tomorrow"))
        tts_finish = _finish_tts(listener)
        _wait_for_hot_window_active(listener)

        listener._process_transcript(
            "claire what about tomorrow",
            utterance_energy=0.05,
            utterance_start_time=tts_start + 0.1,
            utterance_end_time=tts_finish + 0.1,
            generation=listener._capture_generation,
            captured_during_tts=True,
            captured_tts_start_time=tts_start,
        )

        assert _accepted_query(listener) != "", (
            "an explicit wake word must keep barging in during TTS working"
        )
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_stop_command_over_tts_is_still_honoured(self, _print):
        """The fast stop-command interrupt path runs before the acoustic gate."""
        listener, tts = _create_listener(tts_speaking=True)
        tts_start = _start_tts(listener, "A long answer that keeps going.")
        listener.state_manager.cancel_hot_window_activation()

        listener._process_transcript(
            "stop",
            utterance_energy=0.05,
            utterance_start_time=tts_start + 0.1,
            utterance_end_time=tts_start + 0.4,
            generation=listener._capture_generation,
            captured_during_tts=True,
            captured_tts_start_time=tts_start,
        )

        assert tts.interrupt.called, "a human saying 'stop' must still interrupt"
        listener.state_manager.stop()


# ---------------------------------------------------------------------------
# (c) the loop-breaker: no unbounded renewal
# ---------------------------------------------------------------------------

class TestHotWindowCannotRenewForever:
    """Even an echo that slips past the gate cannot loop indefinitely."""

    def test_state_manager_refuses_second_window_without_wake_word(self):
        manager = StateManager(
            hot_window_seconds=0.5, echo_tolerance=0.01,
            hot_window_turn_limit=1,
        )
        try:
            manager.schedule_hot_window_activation()
            assert _wait_manager_active(manager)
            assert manager.hot_window_chain_length() == 1
            manager.expire_hot_window()

            # No wake word in between: the reply must not re-open the window.
            manager.schedule_hot_window_activation()
            time.sleep(0.05)
            assert not manager.is_hot_window_active()
            assert not manager.was_speech_during_hot_window(0.0)

            # An explicit wake word restarts the chain.
            manager.note_wake_word_engagement()
            assert manager.hot_window_chain_length() == 0
            manager.schedule_hot_window_activation()
            assert _wait_manager_active(manager)
        finally:
            manager.stop()

    def test_listener_wires_the_chain_limit(self):
        """The configured follow-up count reaches the StateManager.

        The module constant survives only as the getattr fallback for a cfg
        that lacks the key — it is the loop-breaker, not the default policy.
        """
        listener, _ = _create_listener(hot_window_max_follow_ups=7)
        assert listener.state_manager.hot_window_turn_limit == 7
        listener.state_manager.stop()

        listener, _ = _create_listener(cfg_omits=("hot_window_max_follow_ups",))
        assert listener.state_manager.hot_window_turn_limit == MAX_CONSECUTIVE_HOT_WINDOW_TURNS
        listener.state_manager.stop()

    def test_multi_turn_follow_ups_are_admitted_up_to_the_limit(self):
        """A configured cap of 3 admits three turns, then demands the wake word."""
        listener, _ = _create_listener(
            echo_tolerance=0.01, hot_window_seconds=0.5, hot_window_max_follow_ups=3,
        )
        listener.state_manager.note_wake_word_engagement()

        for turn in range(1, 4):
            _start_tts(listener, f"Answer number {turn}.")
            _finish_tts(listener)
            assert _wait_for_hot_window_active(listener), (
                f"follow-up turn {turn} must be admitted within the cap"
            )
            assert listener.state_manager.hot_window_chain_length() == turn
            listener.state_manager.expire_hot_window()

        # Fourth turn: the cap is reached, so the window must not re-arm.
        _start_tts(listener, "Answer number four.")
        _finish_tts(listener)
        time.sleep(0.05)
        assert not listener.state_manager.is_hot_window_active()
        listener.state_manager.stop()

    def test_zero_limit_means_unlimited_renewal(self):
        """0 is the documented 'unlimited' value: the chain never refuses."""
        listener, _ = _create_listener(
            echo_tolerance=0.01, hot_window_seconds=0.5, hot_window_max_follow_ups=0,
        )
        listener.state_manager.note_wake_word_engagement()

        for turn in range(1, 5):
            _start_tts(listener, f"Reply {turn}.")
            _finish_tts(listener)
            assert _wait_for_hot_window_active(listener), (
                f"turn {turn} must renew with no cap configured"
            )
            listener.state_manager.expire_hot_window()

        assert listener.state_manager.hot_window_chain_length() == 4
        listener.state_manager.stop()

    def test_renewal_refused_at_the_configured_cap(self):
        """An arbitrary configured cap (not the legacy constant) ends the chain."""
        manager = StateManager(
            hot_window_seconds=0.5, echo_tolerance=0.01, hot_window_turn_limit=3,
        )
        try:
            for turn in range(1, 4):
                manager.schedule_hot_window_activation()
                assert _wait_manager_active(manager), (
                    f"turn {turn} must be admitted below the cap"
                )
                manager.expire_hot_window()

            manager.schedule_hot_window_activation()
            time.sleep(0.05)
            assert not manager.is_hot_window_active()
            assert not manager.was_speech_during_hot_window(0.0)
        finally:
            manager.stop()

    @patch("builtins.print")
    def test_reply_cannot_rearm_the_hot_window(self, _print):
        """Wake word opens one window; the follow-up's reply must not open another."""
        listener, _ = _create_listener(echo_tolerance=0.01, hot_window_seconds=0.5)

        # A wake-worded turn: "Claire, ..." resets the chain.
        listener.state_manager.note_wake_word_engagement()
        _start_tts(listener, "Here is the answer.")
        _finish_tts(listener)
        assert _wait_for_hot_window_active(listener)
        assert listener.state_manager.hot_window_chain_length() == 1
        listener.state_manager.expire_hot_window()

        # The follow-up was accepted (hot window used), and its reply finishes.
        _start_tts(listener, "And here is the follow-up answer.")
        _finish_tts(listener)
        time.sleep(0.05)

        assert not listener.state_manager.is_hot_window_active(), (
            "the window must not re-arm off Claire's own reply — that is the "
            "unbounded self-talk loop"
        )
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_wake_word_reopens_the_window(self, _print):
        listener, _ = _create_listener(echo_tolerance=0.01, hot_window_seconds=0.5)
        listener.state_manager.note_wake_word_engagement()
        _start_tts(listener, "One.")
        _finish_tts(listener)
        assert _wait_for_hot_window_active(listener)
        listener.state_manager.expire_hot_window()

        # Chain saturated: no window.
        _start_tts(listener, "Two.")
        _finish_tts(listener)
        time.sleep(0.05)
        assert not listener.state_manager.is_hot_window_active()

        # Saying "Claire" again restarts the chain and re-opens the window.
        listener.state_manager.note_wake_word_engagement()
        _start_tts(listener, "Three.")
        _finish_tts(listener)
        assert _wait_for_hot_window_active(listener)
        listener.state_manager.stop()

    @patch("builtins.print")
    def test_wake_word_utterance_resets_chain_through_process_transcript(self, _print):
        """The listener resets the chain when it hears the wake word (reuse of
        is_wake_word_detected, not a second implementation)."""
        listener, _ = _create_listener(echo_tolerance=0.01, hot_window_seconds=0.5)
        listener.state_manager.note_wake_word_engagement()
        _start_tts(listener, "One.")
        _finish_tts(listener)
        assert _wait_for_hot_window_active(listener)
        listener.state_manager.expire_hot_window()
        _start_tts(listener, "Two.")
        _finish_tts(listener)
        time.sleep(0.05)
        assert listener.state_manager.hot_window_chain_length() >= 1

        listener._process_transcript(
            "claire what is the weather",
            utterance_energy=0.01,
            utterance_start_time=time.time(),
            utterance_end_time=time.time() + 0.5,
            generation=listener._capture_generation,
            captured_during_tts=False,
            captured_tts_start_time=listener.echo_detector._tts_start_time,
        )

        assert listener.state_manager.hot_window_chain_length() == 0, (
            "an explicit wake word is a fresh human turn and must reset the chain"
        )
        listener.state_manager.stop()
