"""Unit tests for the Signal transport (``claire.outreach.signal``).

Every test injects a fake sending layer or passes an explicit ``env_file`` and
``environ``, so nothing here spawns ``signal-cli``, opens a socket, rings a
phone, or reads the machine's real ``channels.env``.  The phone numbers below
are invented fixtures (``+1555...``), used to prove they never leak.

The contract under test is the three-outcome one from
``claire.outreach.transport``: sent (delivered, not a dry run), rehearsed
(nothing sent, dry run, no error) and failed (nothing sent, a NAMED error).
"""
from __future__ import annotations

import logging
import os
import subprocess

import pytest

from claire.outreach import signal as sig

pytestmark = pytest.mark.unit

# Fictional, non-dialable fixtures.  If either appears in a result, a log line,
# an exception or a repr, that is a redaction failure.
ACCOUNT = "+15550000111"
RECIPIENT = "+15550000222"
FAKE_TOKEN = "should-never-appear-0123456789"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
class FakeSender:
    """An injectable sending layer that records exactly what it was asked to do.

    ``available()`` is a device probe and is counted separately from
    ``send_calls``/``call_calls``: a dry run may probe, but it must never ask
    for a message or a call to be sent.
    """

    def __init__(
        self,
        *,
        device_available: bool = True,
        can_call: bool = False,
        send_id: str = "1700000000001",
        call_id: str = "1700000000002",
        send_error: BaseException | None = None,
        call_error: BaseException | None = None,
        device_reason: str | None = None,
        call_reason: str | None = None,
    ) -> None:
        self._device_available = device_available
        self._can_call = can_call
        self.send_id = send_id
        self.call_id = call_id
        self.send_error = send_error
        self.call_error = call_error
        self._device_reason = device_reason
        self._call_reason = call_reason
        self.send_calls: list[tuple[str, str, str]] = []
        self.call_calls: list[tuple[str, str, str]] = []
        self.probe_count = 0

    # -- capability -------------------------------------------------------
    def available(self) -> bool:
        self.probe_count += 1
        return self._device_available

    def unavailable_reason(self):
        return self._device_reason

    def supports_calls(self) -> bool:
        return self._can_call

    def call_unavailable_reason(self):
        return self._call_reason

    # -- sending ----------------------------------------------------------
    def send_text(self, *, account: str, recipient: str, message: str):
        self.send_calls.append((account, recipient, message))
        if self.send_error is not None:
            raise self.send_error
        return self.send_id

    def place_call(self, *, account: str, recipient: str, message: str):
        self.call_calls.append((account, recipient, message))
        if self.call_error is not None:
            raise self.call_error
        return self.call_id

    # -- assertions -------------------------------------------------------
    @property
    def invocations(self) -> list:
        """Every request to actually send something (empty for a rehearsal)."""
        return list(self.send_calls) + list(self.call_calls)


def write_env(tmp_path, lines: list[str], *, name: str = "channels.env", mode: int = 0o600):
    path = tmp_path / name
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def make_config(
    tmp_path,
    *,
    account: str | None = ACCOUNT,
    recipient: str | None = RECIPIENT,
    live: bool = True,
    call: bool = False,
    mode: str | None = "live",
    extra: list[str] | None = None,
    file_name: str = "channels.env",
):
    lines: list[str] = ["# fictional test credentials"]
    if mode is not None:
        lines.append(f"SIGNAL_TRANSPORT={mode}")
    if account is not None:
        lines.append(f"SIGNAL_ACCOUNT={account}")
    if recipient is not None:
        lines.append(f"SIGNAL_RECIPIENT={recipient}")
    if live:
        lines.append("SIGNAL_LIVE=1")
    if call:
        lines.append("SIGNAL_VOICE_CALL=1")
    lines.extend(extra or [])
    path = write_env(tmp_path, lines, name=file_name)
    return sig.load_config(env_file=path, environ={})


def make_transport(tmp_path, sender=None, **kwargs):
    """A transport with an explicit env file, fake sender and no real device."""
    config = make_config(tmp_path, **kwargs)
    return sig.SignalTransport(config=config, sender=sender if sender is not None else FakeSender())


# ---------------------------------------------------------------------------
# available() by configuration
# ---------------------------------------------------------------------------
def test_available_when_account_and_recipient_are_configured(tmp_path):
    transport = make_transport(tmp_path)

    assert transport.available() is True
    assert transport.unavailable_reason is None


def test_not_available_without_a_recipient(tmp_path):
    transport = make_transport(tmp_path, recipient=None)

    assert transport.available() is False
    reason = transport.unavailable_reason
    assert reason and reason.startswith("SIGNAL_NOT_CONFIGURED")
    assert "SIGNAL_RECIPIENT" in reason, "the fix must name the key to add"


def test_not_available_without_a_sender_account(tmp_path):
    transport = make_transport(tmp_path, account=None)

    assert transport.available() is False
    assert "SIGNAL_ACCOUNT" in (transport.unavailable_reason or "")


def test_not_available_when_the_env_file_is_missing(tmp_path):
    config = sig.load_config(env_file=tmp_path / "does-not-exist.env", environ={})
    transport = sig.SignalTransport(config=config, sender=FakeSender())

    assert transport.available() is False
    reason = transport.unavailable_reason or ""
    assert reason.startswith("SIGNAL_NOT_CONFIGURED")
    assert "does-not-exist.env" in reason
    assert "0600" in reason, "the fix must say how to create the file safely"


def test_available_is_true_even_before_a_device_is_probed(tmp_path):
    """available() answers the configuration question, not the device question."""
    sender = FakeSender(device_available=False)
    transport = make_transport(tmp_path, sender=sender)

    assert transport.available() is True
    assert sender.probe_count == 0


# ---------------------------------------------------------------------------
# Rehearsal: the default, and provably inert
# ---------------------------------------------------------------------------
def test_dry_run_text_is_rehearsed_and_never_invokes_the_sender(tmp_path):
    sender = FakeSender()
    transport = make_transport(tmp_path, sender=sender)

    result = transport.send_text("hello there")

    assert result.delivered is False
    assert result.dry_run is True
    assert result.error is None
    assert result.outcome == "rehearsed"
    assert result.channel == "signal"
    assert sender.invocations == [], "a dry run must not ask for anything to be sent"


def test_dry_run_call_is_rehearsed_and_never_invokes_the_sender(tmp_path):
    sender = FakeSender(can_call=True)
    transport = make_transport(tmp_path, sender=sender, call=True)

    result = transport.place_call("call me back")

    assert result.delivered is False
    assert result.dry_run is True
    assert result.error is None
    assert result.outcome == "rehearsed"
    assert result.action == "call"
    assert sender.invocations == [], "a dry run must not ring a phone"


def test_dry_run_is_the_default_for_every_entry_point(tmp_path):
    sender = FakeSender(can_call=True)
    transport = make_transport(tmp_path, sender=sender, call=True)

    for result in (
        transport.send_text("x"),
        transport.place_call("x"),
        transport.send("x", level="medium"),
        transport.call("x"),
    ):
        assert result.dry_run is True and result.delivered is False
    assert sender.invocations == []


def test_a_loosely_typed_dry_run_is_still_a_rehearsal(tmp_path):
    """Only an explicit False may permit a live send."""
    sender = FakeSender()
    transport = make_transport(tmp_path, sender=sender)

    for value in (None, 0, "", "no"):
        result = transport.send_text("must not leave", dry_run=value)
        assert result.delivered is False, value
        assert result.dry_run is True, value
        assert result.error is None, value
    assert sender.invocations == []


def test_dry_run_makes_no_process_and_no_network_call(tmp_path, monkeypatch):
    """Even the real backend path must stay inert in a dry run."""

    def explode(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("a dry run must not spawn a process")

    monkeypatch.setattr(sig.subprocess, "run", explode)
    sender = FakeSender(can_call=True)
    transport = make_transport(tmp_path, sender=sender, call=True)

    assert transport.send_text("x", dry_run=True).dry_run is True
    assert transport.place_call("x", dry_run=True).dry_run is True


# ---------------------------------------------------------------------------
# Real sends
# ---------------------------------------------------------------------------
def test_live_text_reports_delivered_with_the_backend_message_id(tmp_path):
    sender = FakeSender(send_id="1700000000777")
    transport = make_transport(tmp_path, sender=sender)

    result = transport.send_text("real message", dry_run=False)

    assert result.delivered is True
    assert result.dry_run is False
    assert result.error is None
    assert result.outcome == "sent"
    assert result.message_id == "1700000000777"
    assert sender.send_calls == [(ACCOUNT, RECIPIENT, "real message")]


def test_live_call_reports_delivered_and_is_distinguishable_from_a_text(tmp_path):
    sender = FakeSender(can_call=True, call_id="1700000000888")
    transport = make_transport(tmp_path, sender=sender, call=True)

    result = transport.place_call("voice message", dry_run=False)

    assert result.delivered is True
    assert result.dry_run is False
    assert result.action == "call"
    assert result.message_id == "1700000000888"
    assert "call" in result.detail.lower()
    assert sender.call_calls == [(ACCOUNT, RECIPIENT, "voice message")]


def test_send_uses_the_shared_action_vocabulary(tmp_path):
    transport = make_transport(tmp_path, sender=FakeSender(can_call=True), call=True)

    assert transport.send_text("x").action == "send"
    assert transport.place_call("x").action == "call"
    assert sig.ACTION_SEND == "send"
    assert sig.ACTION_CALL == "call"


# ---------------------------------------------------------------------------
# Live opt-in gating
# ---------------------------------------------------------------------------
def test_live_without_the_opt_in_flag_is_rehearsed_not_sent(tmp_path):
    sender = FakeSender()
    transport = make_transport(tmp_path, sender=sender, live=False)

    result = transport.send_text("must not leave", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is True
    assert result.error is None
    assert result.outcome == "rehearsed"
    assert "SIGNAL_LIVE" in result.detail, "the detail must name the missing opt-in"
    assert sender.invocations == []


def test_live_with_an_unrecognised_mode_token_is_rehearsed(tmp_path):
    sender = FakeSender()
    transport = make_transport(tmp_path, sender=sender, mode="dryrun")

    result = transport.send_text("must not leave", dry_run=False)

    assert result.delivered is False and result.dry_run is True
    assert "SIGNAL_TRANSPORT" in result.detail
    assert sender.invocations == []


@pytest.mark.parametrize("token", ["live", "LIVE", " real ", "production"])
def test_live_mode_tokens_that_are_recognised_allow_a_real_send(tmp_path, token):
    sender = FakeSender()
    transport = make_transport(tmp_path, sender=sender, mode=token.strip())

    result = transport.send_text("go", dry_run=False)

    assert result.delivered is True
    assert len(sender.send_calls) == 1


def test_absent_mode_token_still_allows_a_real_send_when_opted_in(tmp_path):
    """The other gates remain in force; only the mode heuristic is skipped."""
    sender = FakeSender()
    transport = make_transport(tmp_path, sender=sender, mode=None)

    result = transport.send_text("go", dry_run=False)

    assert result.delivered is True
    assert len(sender.send_calls) == 1


def test_live_call_is_rehearsed_when_dry_run_is_asked_for(tmp_path):
    sender = FakeSender(can_call=True)
    transport = make_transport(tmp_path, sender=sender, call=True)

    result = transport.place_call("x", dry_run=True)

    assert result.delivered is False and result.dry_run is True and result.error is None
    assert sender.call_calls == []


# ---------------------------------------------------------------------------
# Failures, always named
# ---------------------------------------------------------------------------
def test_backend_exception_is_a_named_failure(tmp_path):
    sender = FakeSender(send_error=RuntimeError("the device went away"))
    transport = make_transport(tmp_path, sender=sender)

    result = transport.send_text("x", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is False
    assert result.outcome == "failed"
    assert result.error and result.error.startswith("SIGNAL_SEND_FAILED")
    assert "the device went away" in result.error


def test_named_transport_error_passes_through_unchanged(tmp_path):
    sender = FakeSender(send_error=sig.SignalSendFailed("signal-cli exited 1"))
    transport = make_transport(tmp_path, sender=sender)

    result = transport.send_text("x", dry_run=False)

    assert result.delivered is False
    assert result.error == "SIGNAL_SEND_FAILED: signal-cli exited 1"


def test_call_failure_is_a_named_failure_and_never_delivery(tmp_path):
    sender = FakeSender(
        can_call=True, call_error=sig.SignalSendFailed("the call was rejected")
    )
    transport = make_transport(tmp_path, sender=sender, call=True)

    result = transport.place_call("x", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is False
    assert result.action == "call"
    assert result.error and result.error.startswith("SIGNAL_SEND_FAILED")
    assert len(sender.call_calls) == 1


def test_missing_credential_is_a_named_actionable_failure(tmp_path):
    transport = make_transport(tmp_path, sender=FakeSender(), recipient=None)

    result = transport.send_text("x", dry_run=True)

    assert result.delivered is False
    assert result.error and result.error.startswith("SIGNAL_NOT_CONFIGURED")
    assert "SIGNAL_RECIPIENT" in result.error
    assert "0600" in result.error


def test_missing_device_is_a_named_actionable_failure(tmp_path):
    sender = FakeSender(
        device_available=False,
        device_reason=(
            "SIGNAL_DEVICE_UNAVAILABLE: signal-cli was not found on PATH; install "
            "signal-cli or set SIGNAL_CLI to its absolute path"
        ),
    )
    transport = make_transport(tmp_path, sender=sender)

    result = transport.send_text("x", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is False
    assert result.outcome == "failed"
    assert result.error and result.error.startswith("SIGNAL_DEVICE_UNAVAILABLE")
    assert "signal-cli" in result.error and "SIGNAL_CLI" in result.error


def test_device_probe_exception_is_a_named_failure(tmp_path):
    class Exploding(FakeSender):
        def available(self):
            raise OSError("dbus is not running")

    transport = make_transport(tmp_path, sender=Exploding())

    result = transport.send_text("x", dry_run=False)

    assert result.delivered is False
    assert result.error and result.error.startswith("SIGNAL_DEVICE_UNAVAILABLE")
    assert "dbus is not running" in result.error


# ---------------------------------------------------------------------------
# place_call when calling is impossible
# ---------------------------------------------------------------------------
def test_call_when_calling_is_disabled_reports_unsupported_not_delivery(tmp_path):
    sender = FakeSender(can_call=True)  # the backend could call; the flag is off
    transport = make_transport(tmp_path, sender=sender, call=False)

    result = transport.place_call("x", dry_run=False)

    assert result.delivered is False, "nothing may be reported as delivered"
    assert result.action == "call"
    assert result.error and result.error.startswith("SIGNAL_VOICE_CALL_UNSUPPORTED")
    assert "SIGNAL_VOICE_CALL" in result.error
    assert result.outcome == "failed", "an impossible call is not a rehearsal"
    assert sender.call_calls == []


def test_call_when_the_backend_cannot_call_reports_unsupported(tmp_path):
    sender = FakeSender(
        can_call=False,
        call_reason=(
            "SIGNAL_VOICE_CALL_UNSUPPORTED: signal-cli cannot place voice calls; "
            "configure SIGNAL_CALL_COMMAND"
        ),
    )
    transport = make_transport(tmp_path, sender=sender, call=True)

    result = transport.place_call("x", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is False
    assert result.error and result.error.startswith("SIGNAL_VOICE_CALL_UNSUPPORTED")
    assert sender.call_calls == []


def test_unsupported_call_is_not_dressed_up_as_a_rehearsal(tmp_path):
    """A dry run of a call that can never happen is still impossible."""
    sender = FakeSender(can_call=False)
    transport = make_transport(tmp_path, sender=sender, call=False)

    result = transport.place_call("x", dry_run=True)

    assert result.delivered is False
    assert result.dry_run is False, "a capability failure is not a rehearsal"
    assert result.error is not None
    assert "no voice call was placed" in result.detail
    assert sender.call_calls == []


def test_can_call_reflects_both_the_flag_and_the_backend(tmp_path):
    assert make_transport(tmp_path, sender=FakeSender(can_call=True), call=True).can_call
    assert not make_transport(tmp_path, sender=FakeSender(can_call=True), call=False).can_call
    assert not make_transport(tmp_path, sender=FakeSender(can_call=False), call=True).can_call
    # A transport that cannot call must not be advertised as call-capable.
    assert make_transport(tmp_path, recipient=None, sender=FakeSender(can_call=True), call=True).can_call is False


# ---------------------------------------------------------------------------
# send() semantics and ladder composition
# ---------------------------------------------------------------------------
def test_send_is_the_text_rung_and_does_not_call_on_critical(tmp_path):
    sender = FakeSender(can_call=True)
    transport = make_transport(tmp_path, sender=sender, call=True)

    result = transport.send("critical thing", level="critical", dry_run=False)

    assert result.action == "send"
    assert result.delivered is True
    assert len(sender.send_calls) == 1
    assert sender.call_calls == [], "the ladder owns the call step; send() must not call"


def test_send_with_call_on_critical_falls_back_to_text_and_says_so(tmp_path):
    sender = FakeSender(can_call=False)
    config = make_config(tmp_path, call=False)
    transport = sig.SignalTransport(config=config, sender=sender, call_on_critical=True)

    result = transport.send("critical thing", level="critical", dry_run=False)

    assert result.delivered is True, "the text fallback did go out"
    assert result.action == "send", "and it is reported as a text, not a call"
    assert "voice call was impossible" in result.detail
    assert "text" in result.detail.lower()
    assert sender.call_calls == [] and len(sender.send_calls) == 1


def test_transport_declares_the_shared_capabilities(tmp_path):
    from claire.outreach.ladder import can_place_call, order_transports

    transport = make_transport(tmp_path, sender=FakeSender(can_call=True), call=True)

    assert transport.name == "signal"
    assert transport.level_rank == sig.DEFAULT_LEVEL_RANK == 1
    assert transport.last_resort is False
    assert can_place_call(transport) is True, "the call() method proves call capability"
    assert order_transports([transport]) == [transport]


def test_ladder_dry_run_leaves_the_sender_untouched(tmp_path):
    """Cross-module composition: the ladder rehearsing must not send."""
    from claire.outreach.ladder import Ladder

    sender = FakeSender(can_call=True)
    transport = make_transport(tmp_path, sender=sender, call=True)

    result = Ladder([transport]).run("chest pain", level="critical", dry_run=True)

    assert sender.invocations == []
    assert result.called is False
    assert all(attempt.delivered is False for attempt in result.attempts)


# ---------------------------------------------------------------------------
# SendResult contract
# ---------------------------------------------------------------------------
def test_results_are_instances_of_the_shared_send_result(tmp_path):
    from claire.outreach.transport import SendResult

    transport = make_transport(tmp_path, sender=FakeSender(can_call=True), call=True)

    assert isinstance(transport.send_text("x"), SendResult)
    assert isinstance(transport.place_call("x"), SendResult)
    assert isinstance(transport.send_text("x", dry_run=False), SendResult)


def test_resolved_result_class_covers_the_shared_fields():
    resolved = sig.resolve_send_result_class()

    for name in sig.REQUIRED_SEND_RESULT_FIELDS:
        assert name in resolved.__dataclass_fields__, name


def test_local_fallback_result_matches_the_shared_contract(tmp_path):
    """The defensive copy used when transport.py is unavailable stays in step."""
    shared = sig.resolve_send_result_class()
    fallback = sig._FallbackSendResult

    if shared is sig._FallbackSendResult:
        pytest.skip("transport.py is unavailable in this environment")
    assert tuple(shared.__dataclass_fields__) == tuple(fallback.__dataclass_fields__)


def test_the_three_outcomes_are_never_collapsed(tmp_path):
    transport = make_transport(tmp_path, sender=FakeSender())  # succeeds when live
    failing = make_transport(
        tmp_path, sender=FakeSender(send_error=RuntimeError("nope")), file_name="env2.env"
    )

    rehearsed = transport.send_text("x", dry_run=True)
    sent = transport.send_text("x", dry_run=False)
    failed = failing.place_call("x", dry_run=False)

    assert (rehearsed.outcome, rehearsed.delivered, rehearsed.dry_run) == ("rehearsed", False, True)
    assert (sent.outcome, sent.delivered, sent.dry_run) == ("sent", True, False)
    assert failed.outcome == "failed"
    assert len({rehearsed.outcome, sent.outcome, failed.outcome}) == 3


# ---------------------------------------------------------------------------
# Redaction: no secret value anywhere
# ---------------------------------------------------------------------------
def test_result_repr_and_fields_never_contain_a_secret_value(tmp_path):
    leaky = RuntimeError(
        f"connection refused while dialling {RECIPIENT} for {ACCOUNT} with {FAKE_TOKEN}"
    )
    sender = FakeSender(send_error=leaky, call_error=leaky, can_call=True)
    transport = make_transport(
        tmp_path, sender=sender, call=True, extra=[f"SIGNAL_SIGNATURE={FAKE_TOKEN}"]
    )

    results = [
        transport.send_text("x", dry_run=False),
        transport.place_call("x", dry_run=False),
        transport.send_text("x", dry_run=True),
    ]

    for result in results:
        blob = repr(result) + repr(result.detail) + repr(result.error) + repr(result.message_id)
        assert ACCOUNT not in blob
        assert RECIPIENT not in blob
        assert FAKE_TOKEN not in blob, "a credential-shaped value reached a result"
    # The failure is still named and explained.
    assert results[0].error.startswith("SIGNAL_SEND_FAILED")


def test_credential_shaped_values_are_redacted_from_free_text(tmp_path):
    """A token in the env file is masked even though it is not the phone number."""
    config = make_config(tmp_path, extra=[f"SIGNAL_SIGNATURE={FAKE_TOKEN}"])

    assert FAKE_TOKEN in config.secret_values()
    assert "on" not in config.secret_values(), "a short flag must not join the redaction set"

    transport = sig.SignalTransport(config=config, sender=FakeSender())
    result = transport.send_text("x", dry_run=False)
    assert result.error is None  # nothing failed here; the redaction set is the point
    assert FAKE_TOKEN not in repr(config.masked())


def test_log_lines_never_contain_a_secret_value(tmp_path, caplog):
    leaky = RuntimeError(f"cannot reach {RECIPIENT} as {ACCOUNT}")
    sender = FakeSender(send_error=leaky, device_reason=f"device {ACCOUNT} is offline")
    transport = make_transport(tmp_path, sender=sender, mode="dryrun")

    with caplog.at_level(logging.DEBUG, logger="claire.outreach.signal"):
        transport.send_text("secret-ish body", dry_run=False)
        transport.send_text("secret-ish body", dry_run=True)

    text = caplog.text
    assert ACCOUNT not in text, "a phone number reached a log record"
    assert RECIPIENT not in text
    assert "secret-ish body" not in text, "the message body is never logged"


def test_config_repr_and_describe_never_contain_a_secret_value(tmp_path):
    transport = make_transport(tmp_path)

    blob = repr(transport.config) + repr(transport.describe()) + repr(transport.config.masked())
    assert ACCOUNT not in blob
    assert RECIPIENT not in blob
    assert "present" in blob or "absent" in blob
    # Key NAMES are safe to report; values are not.
    assert "SIGNAL_ACCOUNT" in blob


def test_mask_secret_reports_last4_and_fingerprint_only():
    masked = sig.mask_secret(RECIPIENT)

    assert RECIPIENT not in masked
    assert masked.endswith("222 (fp " + __import__("hashlib").sha256(RECIPIENT.encode()).hexdigest()[:8] + ")")
    assert sig.mask_secret(None) == "absent"
    assert sig.mask_secret("") == "absent"
    short = sig.mask_secret("abc")
    assert "abc" not in short and "present" in short


def test_redact_text_masks_known_secrets_and_phone_shaped_numbers():
    text = f"failed for {ACCOUNT} and +15550000999 while token={FAKE_TOKEN}"
    cleaned = sig.redact_text(text, [ACCOUNT, FAKE_TOKEN])

    assert ACCOUNT not in cleaned
    assert FAKE_TOKEN not in cleaned
    assert "+15550000999" not in cleaned
    assert "redacted" in cleaned


def test_permission_warning_names_the_path_and_never_the_content(tmp_path):
    path = write_env(tmp_path, [f"SIGNAL_ACCOUNT={ACCOUNT}"], mode=0o644)

    warning = sig.file_permission_warning(path)

    assert warning is not None
    assert str(path) in warning and "0600" in warning
    assert ACCOUNT not in warning
    assert sig.file_permission_warning(write_env(tmp_path, ["X=1"], name="tight.env", mode=0o600)) is None


# ---------------------------------------------------------------------------
# Configuration parsing
# ---------------------------------------------------------------------------
def test_read_env_file_ignores_comments_and_unquotes_values(tmp_path):
    path = write_env(
        tmp_path,
        ["# a comment", "", "SIGNAL_ACCOUNT=" + ACCOUNT, 'SIGNAL_RECIPIENT="' + RECIPIENT + '"'],
    )

    values = sig.read_env_file(path)

    assert values["SIGNAL_ACCOUNT"] == ACCOUNT
    assert values["SIGNAL_RECIPIENT"] == RECIPIENT


def test_read_env_file_on_a_missing_file_is_empty_not_an_error(tmp_path):
    assert sig.read_env_file(tmp_path / "nope.env") == {}


def test_environment_variables_override_the_file(tmp_path):
    path = write_env(tmp_path, ["SIGNAL_ACCOUNT=" + ACCOUNT, "SIGNAL_RECIPIENT=" + RECIPIENT])

    config = sig.load_config(env_file=path, environ={"SIGNAL_ACCOUNT": "+15550000333"})

    assert config.account == "+15550000333"
    assert config.recipient == RECIPIENT


def test_truthy_aliases_are_accepted_for_the_opt_in(tmp_path):
    for token in ("1", "true", "YES", "on", "allowed"):
        path = write_env(
            tmp_path,
            ["SIGNAL_ACCOUNT=x", "SIGNAL_RECIPIENT=y", f"SIGNAL_LIVE={token}"],
            name=f"env-{token}.env",
        )
        assert sig.load_config(env_file=path, environ={}).live_flag is True, token
    path = write_env(tmp_path, ["SIGNAL_LIVE=0"], name="off.env")
    assert sig.load_config(env_file=path, environ={}).live_flag is False


def test_config_missing_lists_key_names_not_values(tmp_path):
    config = make_config(tmp_path, account=None, recipient=None)

    missing = config.missing()

    assert any("SIGNAL_ACCOUNT" in entry for entry in missing)
    assert any("SIGNAL_RECIPIENT" in entry for entry in missing)
    assert ACCOUNT not in " ".join(missing)
    assert config.config_error().startswith("SIGNAL_NOT_CONFIGURED")


def test_constructing_a_transport_touches_no_device(tmp_path, monkeypatch):
    """Even without an injected sender, construction and available() stay inert."""

    def explode(*args, **kwargs):  # pragma: no cover - only on failure
        raise AssertionError("construction must not run a process")

    monkeypatch.setattr(sig.subprocess, "run", explode)
    config = make_config(tmp_path)

    transport = sig.SignalTransport(config=config)

    assert transport.available() is True
    assert transport.sender() is not None


# ---------------------------------------------------------------------------
# The real backend (SignalCliSender) with an injected runner
# ---------------------------------------------------------------------------
def _fake_binary(tmp_path, name="signal-cli"):
    path = tmp_path / name
    path.write_text("#!/bin/sh\n", encoding="utf-8")
    os.chmod(path, 0o755)
    return path


def _runner(returncode=0, stdout="", stderr="", recorder=None):
    def run(cmd, timeout):
        if recorder is not None:
            recorder.append(list(cmd))
        return returncode, stdout, stderr

    return run


def test_real_sender_builds_the_expected_command_and_returns_the_message_id(tmp_path):
    calls: list[list[str]] = []
    sender = sig.SignalCliSender(
        cli_path=str(_fake_binary(tmp_path)),
        runner=_runner(0, "Sent message with timestamp: 1700000000123", recorder=calls),
    )

    message_id = sender.send_text(account=ACCOUNT, recipient=RECIPIENT, message="hello")

    assert message_id == "1700000000123"
    assert calls[0][1:3] == ["-a", ACCOUNT]
    assert calls[0][3] == "send"
    assert "hello" in calls[0]


def test_real_sender_missing_binary_is_a_named_failure(tmp_path):
    sender = sig.SignalCliSender(cli_path=str(tmp_path / "not-here"))

    assert sender.available() is False
    assert "SIGNAL_DEVICE_UNAVAILABLE" in (sender.unavailable_reason() or "")
    with pytest.raises(sig.SignalDeviceUnavailable) as excinfo:
        sender.send_text(account=ACCOUNT, recipient=RECIPIENT, message="x")
    assert "SIGNAL_DEVICE_UNAVAILABLE" in str(excinfo.value)


def test_real_sender_refuses_to_claim_it_can_call(tmp_path):
    sender = sig.SignalCliSender(cli_path=str(_fake_binary(tmp_path)))

    assert sender.supports_calls() is False
    assert "SIGNAL_VOICE_CALL_UNSUPPORTED" in sender.call_unavailable_reason()
    assert "SIGNAL_CALL_COMMAND" in sender.call_unavailable_reason()
    with pytest.raises(sig.SignalVoiceCallUnsupported):
        sender.place_call(account=ACCOUNT, recipient=RECIPIENT, message="x")


def test_real_sender_can_call_through_a_configured_wrapper(tmp_path):
    calls: list[list[str]] = []
    sender = sig.SignalCliSender(
        cli_path=str(_fake_binary(tmp_path)),
        call_command=("my-caller", "{recipient}", "{message}"),
        runner=_runner(0, "call id 1700000000999", recorder=calls),
    )

    assert sender.supports_calls() is True
    call_id = sender.place_call(account=ACCOUNT, recipient=RECIPIENT, message="please call")

    assert call_id == "1700000000999"
    assert calls[0] == ["my-caller", RECIPIENT, "please call"], "the template must substitute"


def test_real_sender_nonzero_exit_is_named_and_redacted(tmp_path):
    sender = sig.SignalCliSender(
        cli_path=str(_fake_binary(tmp_path)),
        runner=_runner(1, "", f"Failed to send to {RECIPIENT} as {ACCOUNT}"),
    )

    with pytest.raises(sig.SignalSendFailed) as excinfo:
        sender.send_text(account=ACCOUNT, recipient=RECIPIENT, message="x")

    text = str(excinfo.value)
    assert "SIGNAL_SEND_FAILED" in text
    assert ACCOUNT not in text and RECIPIENT not in text, "stderr must be redacted"
    assert "exited 1" in text


def test_real_sender_timeout_is_a_named_failure(tmp_path):
    def timing_out(cmd, timeout):
        raise subprocess.TimeoutExpired(cmd, timeout)

    sender = sig.SignalCliSender(cli_path=str(_fake_binary(tmp_path)), runner=timing_out, timeout=5.0)

    with pytest.raises(sig.SignalSendFailed) as excinfo:
        sender.send_text(account=ACCOUNT, recipient=RECIPIENT, message="x")

    assert "timed out" in str(excinfo.value)
    assert "SIGNAL_SEND_FAILED" in str(excinfo.value)


def test_real_sender_file_not_found_is_a_named_failure(tmp_path):
    def missing(cmd, timeout):
        raise FileNotFoundError(2, "No such file or directory")

    sender = sig.SignalCliSender(cli_path=str(_fake_binary(tmp_path)), runner=missing)

    with pytest.raises(sig.SignalDeviceUnavailable) as excinfo:
        sender.send_text(account=ACCOUNT, recipient=RECIPIENT, message="x")

    assert "SIGNAL_DEVICE_UNAVAILABLE" in str(excinfo.value)
    assert "SIGNAL_CLI" in str(excinfo.value)


def test_error_codes_are_all_named():
    for code, description in sig.ERROR_CODES.items():
        assert code.startswith("SIGNAL_")
        assert isinstance(description, str) and description.strip()
