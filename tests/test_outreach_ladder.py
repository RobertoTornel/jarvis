"""Tests for the outreach urgency ladder (``src/claire/outreach/``).

The ladder is exercised entirely with injected fakes: no test here opens a
socket, reads a credential, or reaches a real Signal/Discord API. The gate is
``pytest tests/ -m unit`` -- a file without a unit marker is silently deselected,
hence the ``pytestmark`` below. Do not remove it.

What is pinned here:

* classification for every level, and that an explicit override beats keywords;
* routing: ``low`` sends NOTHING and says so, ``medium`` sends one text,
  ``high`` retries a failure, ``critical`` tries a voice call and reports
  honestly when the transport cannot call;
* Discord is never chosen while a preferred transport works, and IS used when
  the preferred ones are unavailable or fail;
* a failing/unavailable/raising transport falls through instead of aborting;
* dry run is the default, makes no network call, and is distinguishable from a
  real send and from a failure.
"""

from __future__ import annotations

import dataclasses
import socket

import pytest

from claire.outreach.ladder import (
    ACTION_NOTIFY,
    ACTION_NOTIFY_AND_CALL,
    ACTION_STAY,
    LEVELS,
    Ladder,
    LadderResult,
    Route,
    can_place_call,
    classify_urgency,
    dispatch,
    is_last_resort,
    order_transports,
    route_for,
)
from claire.outreach.transport import (
    ACTION_CALL,
    ACTION_SEND,
    SendResult,
    Transport,
    failed_result,
    rehearsed_result,
    sent_result,
)

pytestmark = pytest.mark.unit

# Deliberately shaped like a vendor token so the redaction rule fires. It is not
# a credential and grants nothing; the point is that it never appears in a result.
_FAKE_TOKEN = "xoxb-0000000000-synthetic-not-a-credential"


class FakeTransport:
    """An in-memory transport that records every call and needs no network.

    ``script`` supplies the results (or exceptions) of successive *live* sends;
    when it is exhausted the fake succeeds. Dry runs always rehearse.
    """

    def __init__(
        self,
        name: str,
        level_rank: int | None = None,
        *,
        available: bool = True,
        unavailable_reason: str | None = None,
        action: str = ACTION_SEND,
        can_call: bool | None = None,
        script: list[object] | None = None,
        available_raises: BaseException | None = None,
    ) -> None:
        self.name = name
        self.level_rank = level_rank
        self.action = action
        self._available = available
        self._available_raises = available_raises
        if unavailable_reason is not None:
            self.unavailable_reason = unavailable_reason
        if can_call is not None:
            self.can_call = can_call
        self._script = list(script or [])
        self.calls: list[tuple[str, str, bool]] = []
        self.real_sends = 0
        self.dry_runs = 0

    # -- Transport contract -------------------------------------------------

    def available(self) -> bool:
        if self._available_raises is not None:
            raise self._available_raises
        return self._available

    def send(self, message: str, *, level: str, dry_run: bool = True) -> SendResult:
        self.calls.append((message, level, dry_run))
        if dry_run:
            self.dry_runs += 1
            return rehearsed_result(
                self.name, self.action, f"dry run: would send {message!r}"
            )
        self.real_sends += 1
        return self._next()

    # -- helpers ------------------------------------------------------------

    def _next(self) -> SendResult:
        if self._script:
            item = self._script.pop(0)
            if isinstance(item, BaseException):
                raise item
            assert isinstance(item, SendResult)
            return item
        return sent_result(self.name, self.action, "sent", message_id="mid-1")


class FakeCallTransport(FakeTransport):
    """A transport that can really place a voice call, via ``call()``."""

    def __init__(self, name: str = "signal_call", level_rank: int | None = 1, **kwargs):
        kwargs.setdefault("action", ACTION_CALL)
        kwargs.setdefault("can_call", True)
        super().__init__(name, level_rank, **kwargs)
        self.call_calls: list[tuple[str, str, bool]] = []
        self.real_calls = 0
        self.dry_call_rehearsals = 0

    def call(self, message: str, *, level: str, dry_run: bool = True) -> SendResult:
        self.call_calls.append((message, level, dry_run))
        if dry_run:
            self.dry_call_rehearsals += 1
            return rehearsed_result(
                self.name, ACTION_CALL, f"dry run: would call with {message!r}"
            )
        self.real_calls += 1
        return self._next()


def _signal(**kwargs) -> FakeTransport:
    kwargs.setdefault("level_rank", 1)
    return FakeTransport("signal", **kwargs)


def _discord(**kwargs) -> FakeTransport:
    kwargs.setdefault("level_rank", None)
    return FakeTransport("discord", **kwargs)


# ---------------------------------------------------------------------------
# Classification
# ---------------------------------------------------------------------------


class TestClassifyUrgency:
    def test_emergency_wording_is_critical(self):
        assert classify_urgency("there is a fire in the kitchen") == "critical"
        assert classify_urgency("I think I'm having a heart attack") == "critical"

    def test_pressing_wording_is_high(self):
        assert classify_urgency("this is urgent, please respond") == "high"
        assert classify_urgency("can you do this asap") == "high"

    def test_acknowledgement_wording_is_medium(self):
        assert classify_urgency("let me know when the parcel arrives") == "medium"
        assert classify_urgency("heads up, the meeting moved") == "medium"

    def test_nothing_notable_is_low(self):
        assert classify_urgency("the kettle is where you left it") == "low"
        assert classify_urgency("just so you know, no rush") == "low"

    def test_empty_and_none_are_low(self):
        assert classify_urgency("") == "low"
        assert classify_urgency(None) == "low"

    def test_negated_urgency_does_not_escalate(self):
        assert classify_urgency("not urgent at all") == "low"
        assert classify_urgency("no hurry, whenever") == "low"

    def test_explicit_override_beats_keywords(self):
        """The owner's own choice always wins over the wording."""
        assert classify_urgency("the house is on fire", override="low") == "low"
        assert classify_urgency("just an fyi", override="critical") == "critical"
        assert classify_urgency("let me know", override="high") == "high"

    def test_override_is_case_insensitive(self):
        assert classify_urgency("anything", override="CRITICAL") == "critical"
        assert classify_urgency("anything", override=" Medium ") == "medium"

    def test_unknown_override_is_a_named_error(self):
        with pytest.raises(ValueError) as excinfo:
            classify_urgency("hello", override="extremely-urgent")
        message = str(excinfo.value)
        assert "extremely-urgent" in message
        assert "low, medium, high, critical" in message

    def test_non_string_text_is_a_named_error(self):
        with pytest.raises(TypeError):
            classify_urgency(42)  # type: ignore[arg-type]

    def test_ladder_classify_uses_the_same_rules(self):
        ladder = Ladder([])
        assert ladder.classify("heads up") == "medium"
        assert ladder.classify("heads up", level="low") == "low"


class TestRoutingTable:
    def test_low_sends_nothing(self):
        route = route_for("low")
        assert route.action == ACTION_STAY
        assert route.send is False
        assert route.max_attempts == 0
        assert route.call is False

    def test_medium_sends_once(self):
        route = route_for("medium")
        assert route.action == ACTION_NOTIFY
        assert route.max_attempts == 1
        assert route.call is False

    def test_high_retries(self):
        route = route_for("high")
        assert route.action == ACTION_NOTIFY
        assert route.max_attempts == 2
        assert route.call is False

    def test_critical_texts_and_calls(self):
        route = route_for("critical")
        assert route.action == ACTION_NOTIFY_AND_CALL
        assert route.max_attempts == 2
        assert route.call is True

    def test_unknown_level_is_a_named_error(self):
        with pytest.raises(ValueError) as excinfo:
            route_for("panic")
        assert "panic" in str(excinfo.value)
        assert "low, medium, high, critical" in str(excinfo.value)

    def test_every_level_is_routed(self):
        assert [route_for(level).level for level in LEVELS] == list(LEVELS)


# ---------------------------------------------------------------------------
# low: nothing is sent, and that is reported
# ---------------------------------------------------------------------------


class TestLowStaysInTheConversation:
    def test_low_sends_nothing_even_in_live_mode(self):
        signal, discord = _signal(), _discord()

        result = Ladder([signal, discord], dry_run=False).run(
            "just so you know, the parcel is in the porch"
        )

        assert result.level == "low"
        assert result.action == ACTION_STAY
        assert result.attempts == []
        assert result.sent_anything is False
        assert result.delivered is False
        assert signal.calls == []
        assert discord.calls == []
        assert discord.real_sends == 0

    def test_sending_nothing_is_a_success_and_is_explicit(self):
        result = Ladder([_signal()]).run("no rush at all")

        assert result.ok is True
        assert result.error is None
        assert result.nothing_sent_reason == (
            "urgency is low: this stays in the conversation"
        )
        assert "nothing was sent" in result.detail
        assert "stays in the conversation" in result.detail

    def test_low_does_not_touch_a_transport_that_would_have_failed(self):
        exploding = FakeTransport(
            "signal", 1, script=[RuntimeError("should never be called")]
        )

        result = Ladder([exploding], dry_run=False).run("fyi, no rush")

        assert result.ok is True
        assert exploding.calls == []


# ---------------------------------------------------------------------------
# medium: exactly one text
# ---------------------------------------------------------------------------


class TestMediumSendsOneText:
    def test_dry_run_rehearses_exactly_one_text(self):
        signal, discord = _signal(), _discord()
        text = "heads up: the boiler is being serviced tomorrow"

        result = Ladder([signal, discord]).run(text)

        assert result.level == "medium"
        assert result.dry_run is True
        assert len(result.attempts) == 1
        assert result.attempts[0].outcome == "rehearsed"
        assert result.attempts[0].delivered is False
        assert result.attempts[0].dry_run is True
        assert result.attempts[0].channel == "signal"
        assert signal.dry_runs == 1
        assert signal.calls == [(text, "medium", True)]
        assert result.rehearsal == text

    def test_live_sends_exactly_one_text_through_signal(self):
        signal, discord = _signal(), _discord()
        text = "please let me know when you are home"

        result = Ladder([signal, discord], dry_run=False).run(text)

        assert result.level == "medium"
        assert signal.real_sends == 1
        assert signal.calls == [(text, "medium", False)]
        assert len(result.attempts) == 1
        assert result.attempts[0].outcome == "sent"
        assert result.attempts[0].delivered is True
        assert result.delivered is True
        assert result.channel == "signal"
        assert discord.calls == []
        assert result.rehearsal is None

    def test_message_is_sent_verbatim(self):
        signal = _signal()
        text = "heads up -- line one.\nLine two with quotes 'x' and emoji \U0001f600"

        Ladder([signal], dry_run=False).run(text)

        assert signal.calls[0][0] == text
        assert signal.calls[0][1] == "medium"


# ---------------------------------------------------------------------------
# high: retry after a failure
# ---------------------------------------------------------------------------


class TestHighRetries:
    def test_high_retries_the_same_transport_once(self):
        signal = _signal(
            script=[
                failed_result("signal", "signal: HTTP 502 from the Signal bridge"),
            ]
        )
        discord = _discord()

        result = Ladder([signal, discord], dry_run=False).run(
            "urgent: the contract expires in an hour"
        )

        assert result.level == "high"
        assert signal.real_sends == 2
        assert [a.outcome for a in result.attempts] == ["failed", "sent"]
        assert result.delivered is True
        assert result.channel == "signal"
        assert discord.calls == []

    def test_high_falls_through_after_exhausting_the_retry(self):
        signal = _signal(
            script=[
                failed_result("signal", "signal: HTTP 502 from the Signal bridge"),
                failed_result("signal", "signal: HTTP 502 from the Signal bridge"),
            ]
        )
        backup = FakeTransport("signal_backup", 2)
        discord = _discord()

        result = Ladder([signal, backup, discord], dry_run=False).run(
            "important: deadline today"
        )

        assert signal.real_sends == 2
        assert [a.channel for a in result.attempts] == [
            "signal",
            "signal",
            "signal_backup",
        ]
        assert [a.outcome for a in result.attempts] == ["failed", "failed", "sent"]
        assert result.delivered is True
        assert result.channel == "signal_backup"
        assert discord.calls == []

    def test_medium_does_not_retry_a_failing_transport(self):
        signal = _signal(
            script=[failed_result("signal", "signal: bridge refused the message")]
        )
        backup = FakeTransport("signal_backup", 2)

        result = Ladder([signal, backup], dry_run=False).run("let me know soon")

        assert signal.real_sends == 1
        assert [a.channel for a in result.attempts] == ["signal", "signal_backup"]
        assert result.delivered is True


# ---------------------------------------------------------------------------
# critical: text then a voice call, reported honestly
# ---------------------------------------------------------------------------


class TestCriticalCall:
    def test_critical_texts_then_places_a_real_call(self):
        signal = _signal()
        caller = FakeCallTransport("signal_call", 1)

        result = Ladder([signal, caller], dry_run=False).run(
            "emergency: call an ambulance"
        )

        assert result.level == "critical"
        assert [a.action for a in result.attempts] == [ACTION_SEND, ACTION_CALL]
        assert [a.outcome for a in result.attempts] == ["sent", "sent"]
        assert caller.real_calls == 1
        assert result.called is True
        assert result.call_placed is True
        assert result.call_reason is None
        assert "voice call placed" in result.detail

    def test_critical_reports_honestly_when_no_transport_can_call(self):
        signal = _signal()

        result = Ladder([signal], dry_run=False).run("emergency: call 911")

        assert result.level == "critical"
        assert result.called is False
        assert result.call_attempted is False
        assert result.call_reason is not None
        assert "no injected transport can place a voice call" in result.call_reason
        assert "Nothing was called" in result.call_reason
        assert "no voice call placed" in result.detail
        # The text still went out; the ladder reports both facts.
        assert result.attempts[0].outcome == "sent"

    def test_critical_catches_a_transport_that_answers_a_call_with_a_text(self):
        """A transport that claims call capability but only sends must not be
        reported as having placed a call."""
        liar = _signal(can_call=True)  # declares calling, answers with a text

        result = Ladder([liar], dry_run=False).run("emergency: call 911")

        assert result.called is False
        assert result.call_attempted is True
        assert result.call_reason is not None
        assert "cannot place a call" in result.call_reason
        # The text step and the call step both went out as ordinary sends, and
        # neither is reported as a voice call.
        assert [a.action for a in result.attempts] == [ACTION_SEND, ACTION_SEND]
        assert liar.real_sends == 2

    def test_critical_dry_run_rehearses_the_call_but_never_claims_one(self):
        signal = _signal()
        caller = FakeCallTransport("signal_call", 1)

        result = Ladder([signal, caller]).run("emergency: call 911")

        assert result.called is False
        assert result.call_attempted is True
        assert caller.real_calls == 0
        assert caller.dry_call_rehearsals == 1
        assert result.call_reason is not None
        assert "rehearsed, NOT placed" in result.call_reason
        assert "no voice call placed" in result.detail
        assert [a.outcome for a in result.attempts] == ["rehearsed", "rehearsed"]

    def test_critical_call_uses_call_entry_point_not_send(self):
        signal = _signal()
        caller = FakeCallTransport("signal_call", 1)

        Ladder([signal, caller], dry_run=False).run("emergency: call 911")

        assert caller.calls == []  # send() was never used for the call
        assert len(caller.call_calls) == 1
        assert caller.call_calls[0][1] == "critical"

    def test_critical_call_failure_is_named_and_not_dressed_up(self):
        signal = _signal()
        caller = FakeCallTransport(
            "signal_call",
            1,
            script=[failed_result("signal_call", "signal_call: no SIP trunk configured", ACTION_CALL)],
        )

        result = Ladder([signal, caller], dry_run=False).run("emergency: call 911")

        assert result.called is False
        assert result.call_attempted is True
        assert result.call_reason is not None
        assert "no SIP trunk configured" in result.call_reason
        assert "no voice call placed" in result.detail

    def test_discord_can_never_be_call_capable(self):
        assert can_place_call(_discord(can_call=True)) is False

    def test_explicit_level_override_drives_critical_routing(self):
        signal = _signal()

        result = Ladder([signal], dry_run=False).run("just an fyi", level="critical")

        assert result.level == "critical"
        assert signal.calls == [("just an fyi", "critical", False)]


# ---------------------------------------------------------------------------
# Discord is the last resort
# ---------------------------------------------------------------------------


class TestDiscordIsLastResort:
    def test_discord_is_not_chosen_while_signal_works(self):
        discord, signal = _discord(), _signal()

        result = Ladder([discord, signal], dry_run=False).run("let me know soon")

        assert result.channel == "signal"
        assert [a.channel for a in result.attempts] == ["signal"]
        assert discord.calls == []

    def test_discord_stays_last_even_with_a_better_rank(self):
        greedy_discord = _discord(level_rank=1)
        signal = _signal(level_rank=5)

        assert is_last_resort(greedy_discord) is True
        assert [t.name for t in order_transports([greedy_discord, signal])] == [
            "signal",
            "discord",
        ]

        result = Ladder([greedy_discord, signal], dry_run=False).run("heads up")
        assert result.channel == "signal"
        assert greedy_discord.calls == []

    def test_discord_is_used_when_preferred_transports_are_unavailable(self):
        signal = _signal(
            available=False,
            unavailable_reason=(
                "Signal is not configured: set signal_cli_path or register a "
                "number with signal-cli"
            ),
        )
        discord = _discord()

        result = Ladder([signal, discord], dry_run=False).run("please text me today")

        assert [a.channel for a in result.attempts] == ["discord"]
        assert result.attempts[0].outcome == "sent"
        assert result.delivered is True
        assert result.channel == "discord"
        assert any("Signal is not configured" in line for line in result.skipped)

    def test_discord_is_used_when_preferred_transports_fail(self):
        signal = _signal(
            script=[failed_result("signal", "signal: bridge refused the message")]
        )
        discord = _discord()

        result = Ladder([signal, discord], dry_run=False).run("let me know soon")

        assert [a.channel for a in result.attempts] == ["signal", "discord"]
        assert [a.outcome for a in result.attempts] == ["failed", "sent"]
        assert result.delivered is True
        assert discord.real_sends == 1

    def test_discord_is_not_touched_in_a_dry_run_when_signal_rehearses(self):
        signal, discord = _signal(), _discord()

        result = Ladder([signal, discord]).run("let me know soon")

        assert result.channel == "signal"
        assert discord.dry_runs == 0
        assert discord.calls == []

    def test_two_preferred_rungs_are_ordered_by_rank(self):
        secondary = FakeTransport("signal_secondary", 2)
        primary = _signal(level_rank=1)

        assert [t.name for t in order_transports([secondary, primary])] == [
            "signal",
            "signal_secondary",
        ]

    def test_last_resort_flag_is_honoured_for_a_non_discord_transport(self):
        pager = FakeTransport("pager", 9)
        pager.last_resort = True
        signal = _signal(level_rank=1)

        assert is_last_resort(pager) is True
        assert [t.name for t in order_transports([pager, signal])] == ["signal", "pager"]


# ---------------------------------------------------------------------------
# Fall-through and honest bookkeeping
# ---------------------------------------------------------------------------


class TestFallThroughAndHonesty:
    def test_failing_transport_falls_through_to_the_next(self):
        signal = _signal(
            script=[failed_result("signal", "signal: bridge refused the message")]
        )
        backup = FakeTransport("signal_backup", 2)

        result = Ladder([signal, backup], dry_run=False).run("let me know soon")

        assert [a.channel for a in result.attempts] == ["signal", "signal_backup"]
        assert result.delivered is True
        assert result.channel == "signal_backup"
        assert result.ok is True

    def test_raising_transport_is_recorded_and_does_not_abort_the_ladder(self):
        signal = _signal(script=[RuntimeError("connection reset")])
        backup = FakeTransport("signal_backup", 2)

        result = Ladder([signal, backup], dry_run=False).run("let me know soon")

        assert result.attempts[0].outcome == "failed"
        assert "RuntimeError" in (result.attempts[0].error or "")
        assert "connection reset" in (result.attempts[0].error or "")
        assert result.delivered is True
        assert result.channel == "signal_backup"

    def test_availability_check_that_raises_falls_through(self):
        broken = _signal(available_raises=RuntimeError("keyring unavailable"))
        backup = FakeTransport("signal_backup", 2)

        result = Ladder([broken, backup], dry_run=False).run("let me know soon")

        assert result.attempts[0].channel == "signal_backup"
        assert result.delivered is True
        assert any("keyring unavailable" in line for line in result.skipped)

    def test_non_transport_object_is_reported_not_crashed(self):
        unavailable_signal = _signal(available=False)

        result = Ladder([object(), unavailable_signal], dry_run=False).run(
            "let me know soon"
        )

        assert result.delivered is False
        assert any("no available() method" in line for line in result.skipped)
        assert result.error is not None
        assert "no available() method" in result.error

    def test_every_transport_unavailable_names_the_total_failure(self):
        signal = _signal(
            available=False, unavailable_reason="Signal is not configured"
        )
        discord = _discord(
            available=False, unavailable_reason="Discord bot token is absent"
        )

        result = Ladder([signal, discord], dry_run=False).run("let me know soon")

        assert result.delivered is False
        assert result.sent_anything is False
        assert result.ok is False
        assert result.error is not None
        assert "nothing could be sent" in result.error
        assert "Signal is not configured" in result.error
        assert "Discord bot token is absent" in result.error

    def test_no_transports_injected_is_a_named_error(self):
        result = Ladder([], dry_run=False).run("let me know soon")

        assert result.ok is False
        assert result.error is not None
        assert "no transports were injected" in result.error

    def test_unavailable_transport_without_a_reason_is_still_named(self):
        silent = _signal(available=False)

        result = Ladder([silent], dry_run=False).run("let me know soon")

        assert result.error is not None
        assert "no reason reported" in result.error

    def test_result_reports_which_transports_were_tried_and_what_each_did(self):
        signal = _signal(
            script=[failed_result("signal", "signal: bridge refused the message")]
        )
        discord = _discord()

        result = Ladder([signal, discord], dry_run=False).run("let me know soon")

        assert result.tried == ["signal", "discord"]
        assert result.outcomes == ["failed", "sent"]
        assert isinstance(result, LadderResult)


# ---------------------------------------------------------------------------
# Dry run is the default
# ---------------------------------------------------------------------------


class TestDryRunIsTheDefault:
    def test_ladder_defaults_to_dry_run(self):
        signal = _signal()

        result = Ladder([signal]).run("let me know soon")

        assert result.dry_run is True
        assert signal.real_sends == 0
        assert signal.dry_runs == 1
        assert all(attempt.outcome == "rehearsed" for attempt in result.attempts)

    def test_dispatch_defaults_to_dry_run(self):
        signal = _signal()

        result = dispatch("urgent: please respond", [signal])

        assert result.dry_run is True
        assert signal.real_sends == 0
        assert result.attempts[0].outcome == "rehearsed"

    def test_a_dry_run_opens_no_socket(self, monkeypatch):
        class _NoNetwork:
            def __init__(self, *args, **kwargs):
                raise AssertionError("a dry run tried to open a socket")

        monkeypatch.setattr(socket, "socket", _NoNetwork)
        signal, caller = _signal(), FakeCallTransport("signal_call", 1)

        result = Ladder([signal, caller]).run("emergency: call 911")

        assert result.dry_run is True
        assert signal.real_sends == 0
        assert caller.real_calls == 0

    def test_live_requires_an_explicit_flag(self):
        dry_signal, live_signal = _signal(), _signal()

        dry = Ladder([dry_signal]).run("let me know soon")
        assert dry.dry_run is True
        assert dry_signal.real_sends == 0
        assert dry_signal.dry_runs == 1

        live = Ladder([live_signal]).run("let me know soon", dry_run=False)
        assert live.dry_run is False
        assert live_signal.real_sends == 1
        assert live_signal.dry_runs == 0

    def test_constructor_default_can_be_overridden_per_run(self):
        signal = _signal()
        ladder = Ladder([signal], dry_run=False)

        live = ladder.run("let me know soon")
        assert live.dry_run is False
        assert signal.real_sends == 1

        rehearsed = ladder.run("let me know soon", dry_run=True)
        assert rehearsed.dry_run is True
        assert signal.real_sends == 1  # the second run rehearsed only
        assert signal.dry_runs == 1

    def test_non_boolean_dry_run_is_a_named_error(self):
        with pytest.raises(TypeError):
            Ladder([_signal()]).run("hi", dry_run="yes")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Three outcomes, never collapsed
# ---------------------------------------------------------------------------


class TestThreeOutcomesAreDistinguishable:
    def test_the_three_outcomes_have_distinct_flags(self):
        sent = sent_result("signal", ACTION_SEND, "sent")
        rehearsed = rehearsed_result("signal", ACTION_SEND, "rehearsed")
        failed = failed_result("signal", "signal: bridge refused the message")

        triples = {
            (sent.delivered, sent.dry_run, sent.error),
            (rehearsed.delivered, rehearsed.dry_run, rehearsed.error),
            (failed.delivered, failed.dry_run, failed.error),
        }
        assert len(triples) == 3
        assert (sent.outcome, rehearsed.outcome, failed.outcome) == (
            "sent",
            "rehearsed",
            "failed",
        )
        assert (sent.ok, rehearsed.ok, failed.ok) == (True, True, False)

    def test_a_silent_no_op_result_is_rejected(self):
        with pytest.raises(ValueError) as excinfo:
            SendResult(
                action=ACTION_SEND,
                channel="signal",
                delivered=False,
                dry_run=False,
                detail="nothing happened",
            )
        assert "silent no-op" in str(excinfo.value)

    def test_a_rehearsal_cannot_claim_delivery(self):
        with pytest.raises(ValueError):
            SendResult(
                action=ACTION_SEND,
                channel="signal",
                delivered=True,
                dry_run=True,
                detail="contradictory",
            )

    def test_a_delivered_result_cannot_carry_an_error(self):
        with pytest.raises(ValueError):
            SendResult(
                action=ACTION_SEND,
                channel="signal",
                delivered=True,
                dry_run=False,
                detail="contradictory",
                error="boom",
            )

    def test_failed_result_requires_a_named_reason(self):
        with pytest.raises(ValueError) as excinfo:
            failed_result("signal", "   ")
        assert "named reason" in str(excinfo.value)

    def test_shared_interface_field_order_is_pinned(self):
        """Sibling transports construct this positionally -- keep the order."""
        assert [f.name for f in dataclasses.fields(SendResult)] == [
            "action",
            "channel",
            "delivered",
            "dry_run",
            "detail",
            "message_id",
            "error",
        ]
        positional = SendResult("send", "signal", False, True, "rehearsed")
        assert positional.message_id is None
        assert positional.error is None

    def test_all_three_outcomes_surface_in_one_ladder_result(self):
        signal = _signal(
            script=[
                failed_result("signal", "signal: bridge refused the message"),
                failed_result("signal", "signal: bridge refused the message"),
            ]
        )
        backup = FakeTransport("signal_backup", 2)

        live = Ladder([signal, backup], dry_run=False).run("important: deadline today")
        dry = Ladder([backup]).run("important: deadline today")

        assert "failed" in live.outcomes and "sent" in live.outcomes
        assert dry.outcomes == ["rehearsed"]
        assert live.dry_run is False and dry.dry_run is True


class TestTransportContract:
    def test_fakes_satisfy_the_transport_protocol(self):
        assert isinstance(_signal(), Transport)
        assert isinstance(FakeCallTransport(), Transport)
        assert not isinstance(object(), Transport)

    def test_can_place_call_reads_the_declarations(self):
        assert can_place_call(FakeCallTransport("signal_call", 1)) is True
        assert can_place_call(_signal(can_call=True)) is True
        assert can_place_call(FakeTransport("signal_voice_call", 1)) is True
        assert can_place_call(_signal(can_call=False)) is False
        assert can_place_call(_signal()) is False
        assert can_place_call(_discord(can_call=True)) is False

    def test_constructor_rejects_a_non_iterable_transport_list(self):
        with pytest.raises(TypeError):
            Ladder("signal")  # type: ignore[arg-type]

    def test_run_rejects_a_non_string_message(self):
        with pytest.raises(TypeError):
            Ladder([_signal()]).run(123)  # type: ignore[arg-type]

    def test_run_rejects_a_non_string_level(self):
        with pytest.raises(TypeError):
            Ladder([_signal()]).run("hi", level=3)  # type: ignore[arg-type]

    def test_ladder_never_touches_a_rail_in_a_dry_run(self):
        """Nothing leaves the machine: every injected fake records dry runs only."""
        signal, caller = _signal(), FakeCallTransport("signal_call", 1)

        Ladder([signal, caller]).run("emergency: call 911")

        assert signal.real_sends == 0
        assert caller.real_calls == 0
        assert all(call[2] is True for call in signal.calls)
        assert all(call[2] is True for call in caller.call_calls)


# ---------------------------------------------------------------------------
# Secrets never reach a result
# ---------------------------------------------------------------------------


class TestSecretBoundary:
    def test_a_token_in_a_failure_message_is_masked_everywhere(self):
        signal = _signal(
            script=[
                failed_result(
                    "signal",
                    f"signal: auth failed for token {_FAKE_TOKEN} (regenerate it)",
                )
            ]
        )

        result = Ladder([signal], dry_run=False).run("let me know soon")

        assert _FAKE_TOKEN not in result.detail
        assert _FAKE_TOKEN not in (result.error or "")
        assert _FAKE_TOKEN not in (result.attempts[0].error or "")
        assert _FAKE_TOKEN not in result.attempts[0].detail
        assert "[REDACTED_TOKEN]" in (result.error or "")
        assert result.attempts[0].outcome == "failed"

    def test_a_token_in_an_unavailable_reason_is_masked(self):
        signal = _signal(
            available=False,
            unavailable_reason=f"Signal is not configured: bad token {_FAKE_TOKEN}",
        )

        result = Ladder([signal], dry_run=False).run("let me know soon")

        assert _FAKE_TOKEN not in result.detail
        assert _FAKE_TOKEN not in (result.error or "")
        assert all(_FAKE_TOKEN not in line for line in result.skipped)


class TestCallCapabilityDeclarationIsNotMissed:
    """A transport that declares calling the documented way must be dialled.

    Regression: ``can_place_call`` used to recognise ``call``/``can_call``/
    ``supports_call``/a ``*_call`` name but NOT ``place_call`` - the method name
    this project's own shared interface documents. The real Signal transport
    happened to also define ``call``, so nothing broke; a transport implementing
    only ``place_call`` would have been silently skipped at ``critical``, which is
    the one level where the call is the whole point. Found by integration
    verification rather than by the unit tests, because the unit tests' fakes all
    happened to define one of the recognised spellings.
    """

    class _PlaceCallOnly:
        name = "signal"
        level_rank = 1

        def available(self) -> bool:
            return True

        def send(self, message, *, level, dry_run=True):
            raise AssertionError("a critical alert must not need a text first")

        def place_call(self, message, *, dry_run=True):  # noqa: D102
            raise AssertionError("not called directly in this test")

    def test_place_call_only_transport_is_call_capable(self):
        assert can_place_call(self._PlaceCallOnly()) is True

    def test_supports_calls_method_is_call_capable(self):
        class T:
            name = "signal"
            level_rank = 1
            supports_calls = staticmethod(lambda: True)

        assert can_place_call(T()) is True

    def test_supports_calls_false_is_not_call_capable(self):
        class T:
            name = "signal"
            level_rank = 1
            supports_calls = staticmethod(lambda: False)

        assert can_place_call(T()) is False

    def test_supports_calls_that_raises_is_not_call_capable(self):
        class T:
            name = "signal"
            level_rank = 1

            def supports_calls(self):
                raise RuntimeError("device vanished")

        assert can_place_call(T()) is False

    def test_discord_is_never_call_capable_however_it_declares_itself(self):
        class Discord:
            name = "discord"
            level_rank = 9

            def place_call(self, message, *, dry_run=True):  # pragma: no cover
                return None

            def call(self):  # pragma: no cover
                return None

            can_call = True
            supports_calls = staticmethod(lambda: True)

        assert can_place_call(Discord()) is False
