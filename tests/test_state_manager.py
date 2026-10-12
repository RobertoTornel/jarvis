"""
Tests for voice listening state manager.

These tests verify the state transitions, timer-based hot window management,
and query collection behavior.
"""

import time
import threading
import pytest
from unittest.mock import patch, MagicMock

from claire.listening.state_manager import StateManager, ListeningState

# The unit gate deselects any test that is not marked `unit`; a module-level
# pytestmark makes this whole file visible to it.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`


class TestStateTransitions:
    """Tests for basic state transitions."""

    def test_initial_state_is_wake_word(self):
        """State manager starts in WAKE_WORD state."""
        sm = StateManager()
        assert sm.get_state() == ListeningState.WAKE_WORD

    def test_start_collection_changes_state(self):
        """Starting collection changes state to COLLECTING."""
        sm = StateManager()
        sm.start_collection("hello")
        assert sm.get_state() == ListeningState.COLLECTING

    def test_clear_collection_returns_to_wake_word(self):
        """Clearing collection returns to WAKE_WORD state."""
        sm = StateManager()
        sm.start_collection("hello")
        sm.clear_collection()
        assert sm.get_state() == ListeningState.WAKE_WORD

    def test_is_collecting_helper(self):
        """is_collecting() accurately reflects state."""
        sm = StateManager()
        assert sm.is_collecting() is False
        sm.start_collection("test")
        assert sm.is_collecting() is True
        sm.clear_collection()
        assert sm.is_collecting() is False

    def test_is_hot_window_active_helper(self):
        """is_hot_window_active() accurately reflects state."""
        sm = StateManager()
        assert sm.is_hot_window_active() is False
        # Force hot window state for testing
        sm._state = ListeningState.HOT_WINDOW
        assert sm.is_hot_window_active() is True


class TestQueryCollection:
    """Tests for query collection functionality."""

    def test_start_collection_stores_initial_text(self):
        """Starting collection stores initial text."""
        sm = StateManager()
        sm.start_collection("hello world")
        assert sm.get_pending_query() == "hello world"

    def test_add_to_collection_appends_text(self):
        """Adding to collection appends text."""
        sm = StateManager()
        sm.start_collection("hello")
        sm.add_to_collection("world")
        assert sm.get_pending_query() == "hello world"

    def test_add_to_collection_only_works_when_collecting(self):
        """Adding to collection only works in COLLECTING state."""
        sm = StateManager()
        sm.add_to_collection("ignored")
        assert sm.get_pending_query() == ""

    def test_clear_collection_returns_query(self):
        """Clearing collection returns the accumulated query."""
        sm = StateManager()
        sm.start_collection("hello")
        sm.add_to_collection("world")
        query = sm.clear_collection()
        assert query == "hello world"
        assert sm.get_pending_query() == ""

    def test_silence_timeout_triggers_collection_complete(self):
        """Collection times out after silence period."""
        sm = StateManager(voice_collect_seconds=0.05)  # 50ms timeout
        sm.start_collection("test")

        # Initially no timeout
        assert sm.check_collection_timeout() is False

        # Wait for timeout
        time.sleep(0.06)
        assert sm.check_collection_timeout() is True

    def test_max_duration_timeout(self):
        """Collection times out after max duration."""
        sm = StateManager(max_collect_seconds=0.05)  # 50ms max
        sm.start_collection("test")

        # Keep adding to prevent silence timeout
        for _ in range(3):
            time.sleep(0.02)
            sm.add_to_collection("more")

        assert sm.check_collection_timeout() is True


class TestHotWindowActivation:
    """Tests for hot window activation timer."""

    def test_schedule_hot_window_activation(self):
        """Hot window activates after echo tolerance delay."""
        sm = StateManager(echo_tolerance=0.05, hot_window_seconds=1.0)

        # Patch print to avoid test output
        with patch('builtins.print'):
            sm.schedule_hot_window_activation()

            # Not active immediately
            assert sm.is_hot_window_active() is False

            # Wait for activation
            time.sleep(0.1)
            assert sm.is_hot_window_active() is True

        sm.stop()

    def test_cancel_hot_window_activation(self):
        """Can cancel pending hot window activation."""
        sm = StateManager(echo_tolerance=0.1, hot_window_seconds=1.0)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()

            # Cancel before activation
            time.sleep(0.02)
            sm.cancel_hot_window_activation()

            # Wait past activation time
            time.sleep(0.15)
            assert sm.is_hot_window_active() is False

        sm.stop()

    def test_hot_window_not_activated_during_collection(self):
        """Hot window doesn't activate if already collecting."""
        sm = StateManager(echo_tolerance=0.05, hot_window_seconds=1.0)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()

            # Start collection before activation
            time.sleep(0.02)
            sm.start_collection("new query")

            # Wait past activation time
            time.sleep(0.1)

            # Should still be in COLLECTING, not HOT_WINDOW
            assert sm.get_state() == ListeningState.COLLECTING

        sm.stop()


class TestHotWindowExpiry:
    """Tests for hot window expiry timer."""

    def test_hot_window_expires_after_duration(self):
        """Hot window expires after configured duration."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=0.05)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()

            # Wait for activation
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Wait for expiry
            time.sleep(0.1)
            assert sm.is_hot_window_active() is False
            assert sm.get_state() == ListeningState.WAKE_WORD

        sm.stop()

    def test_manual_expire_hot_window(self):
        """Can manually expire hot window."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=10.0)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            sm.expire_hot_window()
            assert sm.is_hot_window_active() is False

        sm.stop()

    def test_reset_hot_window_expiry_extends_timer(self):
        """reset_hot_window_expiry restarts the timer so echo time doesn't eat the window."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=0.10)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Wait until most of the window has elapsed
            time.sleep(0.07)
            assert sm.is_hot_window_active() is True  # still within 0.10s

            # Reset the timer (simulating echo rejection)
            sm.reset_hot_window_expiry()

            # After the original window would have expired, it should still be active
            time.sleep(0.05)
            assert sm.is_hot_window_active() is True

            # Wait for the full reset window to expire
            time.sleep(0.07)
            assert sm.is_hot_window_active() is False

        sm.stop()

    def test_reset_hot_window_expiry_reactivates_expired_window(self):
        """reset_hot_window_expiry reactivates a hot window that expired during echo processing."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=0.08)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Let the hot window fully expire
            time.sleep(0.12)
            assert sm.get_state() == ListeningState.WAKE_WORD

            # Simulate echo rejection arriving after expiry — should reactivate
            sm.reset_hot_window_expiry()
            assert sm.is_hot_window_active() is True

            # New timer should keep it alive for another full window
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Then expire normally
            time.sleep(0.06)
            assert sm.is_hot_window_active() is False

        sm.stop()

    def test_reset_hot_window_expiry_noop_when_collecting(self):
        """reset_hot_window_expiry does not interfere with COLLECTING state."""
        sm = StateManager()
        sm.start_collection("test query")
        assert sm.get_state() == ListeningState.COLLECTING

        sm.reset_hot_window_expiry()
        assert sm.get_state() == ListeningState.COLLECTING
        sm.stop()

    def test_check_hot_window_expiry_fallback(self):
        """check_hot_window_expiry provides synchronous expiry check."""
        sm = StateManager(echo_tolerance=0.0, hot_window_seconds=0.05)

        with patch('builtins.print'):
            # Manually set hot window state
            sm._state = ListeningState.HOT_WINDOW
            sm._hot_window_start_time = time.time()

            # Not expired yet
            assert sm.check_hot_window_expiry() is False

            # Wait for expiry
            time.sleep(0.06)
            assert sm.check_hot_window_expiry() is True
            assert sm.get_state() == ListeningState.WAKE_WORD


class TestTimestampBasedHotWindowDetection:
    """Tests for timestamp-based hot window detection.

    Instead of capturing a mutable boolean at VAD onset (which gets cleared
    by timer-based expiry before Whisper finishes), we compare the utterance
    start time against the hot window's time span. This eliminates race
    conditions between the expiry timer and Whisper transcription."""

    def test_speech_during_active_window_detected(self):
        """Speech starting while hot window is active returns True."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=3.0)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Speech starts now, during active window
            speech_start = time.time()
            assert sm.was_speech_during_hot_window(speech_start) is True

        sm.stop()

    def test_speech_before_window_not_detected(self):
        """Speech starting before the hot window span returns False."""
        sm = StateManager(echo_tolerance=0.5, hot_window_seconds=3.0)

        # Speech started before any window was scheduled
        old_time = time.time() - 10.0
        assert sm.was_speech_during_hot_window(old_time) is False
        sm.stop()

    def test_speech_during_pending_activation_detected(self):
        """Speech starting during echo_tolerance delay (pending) returns True."""
        sm = StateManager(echo_tolerance=1.0, hot_window_seconds=3.0)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            # State is still WAKE_WORD, but activation timer is pending
            assert sm.get_state() == ListeningState.WAKE_WORD

            speech_start = time.time()
            assert sm.was_speech_during_hot_window(speech_start) is True

        sm.stop()

    def test_speech_after_expiry_not_detected(self):
        """Speech starting after hot window expired returns False."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=0.05)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Wait for expiry
            time.sleep(0.08)
            assert sm.is_hot_window_active() is False

            # Speech starts AFTER expiry
            speech_start = time.time()
            assert sm.was_speech_during_hot_window(speech_start) is False

        sm.stop()

    def test_speech_during_window_detected_after_expiry(self):
        """Speech that STARTED during window is detected even after expiry.

        This is the core fix: Whisper takes time to transcribe, so the
        transcript arrives after the window expired. But the speech started
        during the window, so it should be treated as hot window input.
        """
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=0.08)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Speech starts during active window
            speech_start = time.time()

            # Window expires while "Whisper is transcribing"
            time.sleep(0.10)
            assert sm.is_hot_window_active() is False

            # Transcript arrives — but speech_start was during the window
            assert sm.was_speech_during_hot_window(speech_start) is True

        sm.stop()

    def test_no_timestamp_falls_back_to_current_state(self):
        """When utterance_start_time is 0, falls back to current state."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=3.0)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.was_speech_during_hot_window(0.0) is True

        sm.stop()

    def test_no_timestamp_after_expiry_returns_false(self):
        """When utterance_start_time is 0 and window expired, returns False."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=0.05)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            time.sleep(0.08)
            assert sm.was_speech_during_hot_window(0.0) is False

        sm.stop()

    def test_new_window_resets_old_span(self):
        """A new hot window span doesn't match speech from before it."""
        sm = StateManager(echo_tolerance=0.02, hot_window_seconds=0.05)

        with patch('builtins.print'):
            # First window
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            time.sleep(0.08)
            assert sm.is_hot_window_active() is False

            # Speech between windows
            between_speech = time.time()

            # Second window
            time.sleep(0.05)
            sm.schedule_hot_window_activation()
            time.sleep(0.04)
            assert sm.is_hot_window_active() is True

            # Wait for second window to expire
            time.sleep(0.08)
            assert sm.is_hot_window_active() is False

            # Speech from between windows should NOT match the second window's span
            assert sm.was_speech_during_hot_window(between_speech) is False

        sm.stop()


class TestStopBehavior:
    """Tests for state manager stop behavior."""

    def test_stop_cancels_all_timers(self):
        """Stopping state manager cancels all pending timers."""
        sm = StateManager(echo_tolerance=1.0, hot_window_seconds=1.0)

        with patch('builtins.print'):
            sm.schedule_hot_window_activation()

            # Verify timer is scheduled
            assert sm._hot_window_activation_timer is not None

            sm.stop()

            # Timer should be cancelled
            assert sm._hot_window_activation_timer is None
            assert sm._should_stop is True

    def test_stop_resets_state(self):
        """Stopping state manager resets to WAKE_WORD."""
        sm = StateManager()
        sm._state = ListeningState.HOT_WINDOW

        sm.stop()
        assert sm.get_state() == ListeningState.WAKE_WORD


class TestThreadSafety:
    """Tests for thread safety of state operations."""

    def test_concurrent_state_access(self):
        """State operations are thread-safe."""
        sm = StateManager(voice_collect_seconds=10.0)
        errors = []

        def reader():
            for _ in range(100):
                try:
                    _ = sm.get_state()
                    _ = sm.is_collecting()
                    _ = sm.get_pending_query()
                except Exception as e:
                    errors.append(e)

        def writer():
            for i in range(100):
                try:
                    if i % 2 == 0:
                        sm.start_collection(f"test {i}")
                    else:
                        sm.clear_collection()
                except Exception as e:
                    errors.append(e)

        threads = [
            threading.Thread(target=reader),
            threading.Thread(target=reader),
            threading.Thread(target=writer),
        ]

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert len(errors) == 0, f"Thread safety errors: {errors}"
        sm.stop()

# -- silence window anchored on the voice, not on the transcript ----------


class TestCollectionWindowRunsDuringTranscription:
    """The follow-up window must run while the transcript is decoding.

    Voice turns used to stamp the silence window when the transcript arrived,
    so the decode (p50 0.96 s against a 2.0 s window) was *added* to the
    follow-up window instead of running inside it.
    """

    def test_voice_stopped_before_transcript_still_collecting_one_second_later(self):
        """Win: voice stopped 0.65 s before start_collection still collects.

        The window is anchored on the voice moment T, so at T+1.65 there is
        only 1.65 s of silence (1.65 < 2.0, still collecting) and it expires
        at T+2.0 -- two seconds of real silence after the voice stopped, not
        two seconds after the transcript arrived (T+2.65).
        """
        sm = StateManager(voice_collect_seconds=2.0)
        voice_stopped = time.time()
        sm.note_voice_heard(voice_stopped)
        time.sleep(0.65)  # transcription latency
        sm.start_collection("remind me to", last_voice_time=voice_stopped)
        assert sm.is_collecting()

        one_second_later = voice_stopped + 1.65
        while time.time() < one_second_later:
            time.sleep(0.02)
        # Guard: a poll past the deadline would make the next assert vacuous.
        assert time.time() - voice_stopped < 1.9
        assert sm.check_collection_timeout() is False

        expiry = voice_stopped + 2.0
        while time.time() < expiry:
            time.sleep(0.02)
        assert sm.check_collection_timeout() is True
        sm.stop()

    def test_transcription_slower_than_the_window_keeps_half_the_window(self):
        """A decode longer than the window still leaves at least half of it.

        The voice stopped 2.32 s ago (the slowest observed decode) and the
        transcript has just arrived: credited silence is capped at half a
        window (floor = now - w/2 = now - 1.0), so the first poll is not a
        timeout (1.0 < 2.0) and the turn keeps a further 1.0 s of follow-up
        window -- but never a window longer than one window in total.
        """
        sm = StateManager(voice_collect_seconds=2.0)
        voice_stopped = time.time() - 2.32
        sm.note_voice_heard(voice_stopped)
        sm.start_collection("remind me to", last_voice_time=voice_stopped)
        opened = time.time()

        assert sm.check_collection_timeout() is False  # half the window remains
        assert time.time() - opened < 0.5  # the poll really happened at open

        expiry = opened + 1.0  # floor(open) + w = open + w/2
        while time.time() < expiry:
            time.sleep(0.02)
        assert sm.check_collection_timeout() is True
        # Capped: measured from the transcript's arrival the turn never got
        # a window longer than one window.
        assert time.time() - opened < 1.5
        sm.stop()

    def test_transcription_shorter_than_half_the_window_is_credited_in_full(self):
        """A quick decode is credited in full, not truncated by the floor.

        Voice stopped 0.4 s before the transcript arrived (0.4 < w/2):
        the anchor stays at the voice moment, so the window ends at
        voice + 2.0 = arrival + 1.6 -- after half a window of follow-up
        (1.6 > 1.0) and short of a full window from arrival (1.6 < 2.0).
        """
        sm = StateManager(voice_collect_seconds=2.0)
        voice_stopped = time.time()
        sm.note_voice_heard(voice_stopped)
        time.sleep(0.4)  # short transcription
        sm.start_collection("remind me to", last_voice_time=voice_stopped)
        opened = time.time()

        assert sm.check_collection_timeout() is False

        halfway = voice_stopped + 1.0
        while time.time() < halfway:
            time.sleep(0.02)
        assert sm.check_collection_timeout() is False  # more than half left

        expiry = voice_stopped + 2.0
        while time.time() < expiry:
            time.sleep(0.02)
        assert sm.check_collection_timeout() is True
        credit = time.time() - opened
        assert 1.4 < credit < 1.8  # arrival + 1.6, not arrival + 2.0
        sm.stop()

    def test_start_collection_without_a_timestamp_starts_the_window_now(self):
        """The old call behaves exactly as it does today.

        start_collection(text) with no last_voice_time and no noted voice
        anchors the window at *now*: half a window later it is still open,
        a full window later it has expired.
        """
        sm = StateManager(voice_collect_seconds=0.5)
        sm.start_collection("hello")
        time.sleep(0.3)  # between half (0.25 s) and full (0.5 s)
        assert sm.check_collection_timeout() is False
        time.sleep(0.25)  # 0.55 s of silence in total
        assert sm.check_collection_timeout() is True
        sm.stop()

    def test_a_stale_voice_note_does_not_rewind_the_anchor(self):
        """note_voice_heard only ever moves forward.

        An out-of-order capture (a late queued transcript) must not push the
        window back to an older moment, or a fresh continuation would inherit
        a window that has already run down.
        """
        sm = StateManager(voice_collect_seconds=2.0)
        recent = time.time()
        sm.note_voice_heard(recent)
        sm.note_voice_heard(recent - 5.0)  # stale, must not win
        assert sm._last_voice_heard_at == recent
        sm.stop()
