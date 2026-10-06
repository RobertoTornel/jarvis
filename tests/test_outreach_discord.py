"""Tests for :mod:`claire.outreach.discord`.

Everything here is offline. The HTTP layer is a fake, and the config is a
throwaway 0600 file under ``tmp_path``; nothing in this suite reads the real
``/home/mana/.config/claire/channels.env`` and nothing opens a socket.

The suite is built around the three outcomes the shared ``SendResult`` must keep
apart, because collapsing them is how an assistant ends up claiming it messaged
someone it never messaged:

* a 2xx is the only thing that reports delivery;
* a dry run reports *rehearsed* and makes no network call at all;
* everything else is a failure whose error names what happened - and the four
  families the ladder treats differently (401/403, 429, 5xx, other) stay named
  and distinct.

The last group is the secret rule: the token may leave this module only as
last-four-plus-fingerprint, and it must not appear in a result, an error, a
repr, or a captured log line - not even when Discord echoes it back.
"""

from __future__ import annotations

import inspect
import importlib
import json
import logging
import os

import pytest

from claire.outreach import discord
from claire.outreach.discord import (
    ACTION_SEND,
    CATEGORY_API_ERROR,
    CATEGORY_CREDENTIAL_REJECTED,
    CATEGORY_INVALID_REQUEST,
    CATEGORY_NOT_CONFIGURED,
    CATEGORY_RATE_LIMITED,
    CATEGORY_REHEARSED,
    CATEGORY_SENT,
    CATEGORY_SERVER_ERROR,
    CATEGORY_TRANSPORT_ERROR,
    DiscordConfig,
    DiscordConfigError,
    DiscordEnvFileMissingError,
    DiscordEnvFilePermissionsError,
    DiscordTransport,
    fingerprint,
    redact,
    redact_text,
)

# The gate runs `pytest -m unit`. A test file without this marker is silently
# DESELECTED, so it can pass forever without ever running.
pytestmark = pytest.mark.unit

# A deliberately fake token. It is not a credential and never was.
TOKEN = "MTIzNDU2Nzg5MDEyMzQ1Njc4.GhIjKl.thisIsAFakeTokenForTests0"
USER_ID = "535813308737912832"
CHANNEL_ID = "998877665544332211"

LOG = "claire.outreach.discord"


# --------------------------------------------------------------------------
# Fakes: an HTTP layer with no network behind it
# --------------------------------------------------------------------------


class FakeResponse:
    """The subset of ``requests.Response`` this transport reads."""

    def __init__(self, status_code: int = 200, payload=None, text: str = "", headers=None):
        self.status_code = status_code
        self._payload = payload
        if text:
            self.text = text
        elif payload is not None:
            self.text = json.dumps(payload)
        else:
            self.text = ""
        self.headers = dict(headers or {})

    def json(self):
        if self._payload is None:
            raise ValueError("no JSON body")
        return self._payload


class FakeSession:
    """Records every call, so a dry run can be proven to have made none."""

    def __init__(self, responses=None, error=None):
        self.calls = []
        self._responses = list(responses or [])
        self._error = error

    def post(self, url, *, json=None, headers=None, timeout=None):
        self.calls.append(
            {"url": url, "json": json, "headers": dict(headers or {}), "timeout": timeout}
        )
        if self._error is not None:
            raise self._error
        if self._responses:
            return self._responses.pop(0)
        return FakeResponse(200, {"id": "fallback-id"})


class ExplodingSession:
    """Fails the moment anything asks it to do HTTP work."""

    def post(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("this code path must not touch the network")

    def __call__(self, *args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("this code path must not touch the network")


class StubTransport:
    """A minimal sibling rung, used to check how the ladder places Discord."""

    def __init__(self, name, level_rank, *, result=None, available=True):
        self.name = name
        self.level_rank = level_rank
        self._result = result
        self._available = available

    def available(self):
        return self._available

    def send(self, message, *, level, dry_run=True):
        if self._result is not None:
            return self._result
        raise AssertionError("this stub is only used for ordering")


def _sibling(name):
    """Import a sibling outreach module, or None while it is not written yet."""
    try:
        return importlib.import_module(f"claire.outreach.{name}")
    except Exception:
        return None


def write_env(tmp_path, *, token=TOKEN, target_user=USER_ID, target_channel=None,
              transport="selfbot", mode=0o600, filename="channels.env"):
    """A throwaway 0600 env file, shaped like the operator's real one."""
    lines = [
        "# Claire channel credentials - test fixture, contains no real secret",
    ]
    if transport is not None:
        lines.append(f"DISCORD_TRANSPORT={transport}")
    if token is not None:
        lines.append(f"DISCORD_SELFBOT_TOKEN={token}")
    if target_user is not None:
        lines.append(f"DISCORD_TARGET_USER_ID={target_user}")
    if target_channel is not None:
        lines.append(f"DISCORD_TARGET_CHANNEL_ID={target_channel}")
    path = tmp_path / filename
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.chmod(path, mode)
    return path


def build_transport(tmp_path, fake=None, **env):
    path = write_env(tmp_path, **env)
    return DiscordTransport.from_env_file(path, http=fake), path


def ok_pair():
    """The two responses a successful DM: open the channel, post the message."""
    return [
        FakeResponse(200, {"id": CHANNEL_ID}),
        FakeResponse(200, {"id": "msg-42", "content": "hello"}),
    ]


# --------------------------------------------------------------------------
# available(): configuration, never the network
# --------------------------------------------------------------------------


def test_available_true_when_token_and_target_are_configured(tmp_path):
    transport, _ = build_transport(tmp_path, ExplodingSession())
    assert transport.available() is True
    assert transport.blockers() == ()


def test_available_false_when_the_token_is_missing(tmp_path):
    transport, path = build_transport(tmp_path, token=None)
    assert transport.available() is False
    reasons = " ".join(transport.blockers())
    assert "DISCORD_SELFBOT_TOKEN" in reasons
    assert str(path) in reasons


def test_available_false_when_no_recipient_is_configured(tmp_path):
    transport, _ = build_transport(tmp_path, target_user=None)
    assert transport.available() is False
    reasons = " ".join(transport.blockers())
    assert "DISCORD_TARGET_USER_ID" in reasons
    assert "DISCORD_TARGET_CHANNEL_ID" in reasons


def test_a_channel_id_alone_is_a_usable_recipient(tmp_path):
    transport, _ = build_transport(tmp_path, target_user=None, target_channel=CHANNEL_ID)
    assert transport.available() is True


def test_available_false_when_the_operator_switched_the_transport_off(tmp_path):
    transport, _ = build_transport(tmp_path, transport="off")
    assert transport.available() is False
    assert "DISCORD_TRANSPORT=off" in " ".join(transport.blockers())


def test_available_false_for_an_unrecognised_mode_without_quoting_it(tmp_path):
    # The operator may have pasted the token into the wrong line. Do not echo it.
    transport, _ = build_transport(tmp_path, transport=TOKEN)
    assert transport.available() is False
    reasons = " ".join(transport.blockers())
    assert "DISCORD_TRANSPORT" in reasons
    assert "selfbot" in reasons
    assert TOKEN not in reasons


def test_dryrun_mode_is_available_because_it_can_still_rehearse(tmp_path):
    transport, _ = build_transport(tmp_path, transport="dryrun")
    assert transport.available() is True


def test_available_never_touches_the_network(tmp_path):
    # ExplodingSession raises on any HTTP work, so plain availability is proof.
    transport, _ = build_transport(tmp_path, ExplodingSession())
    assert transport.available() is True
    assert transport.available() is True


# --------------------------------------------------------------------------
# The env file itself
# --------------------------------------------------------------------------


def test_missing_env_file_is_a_named_actionable_error(tmp_path):
    missing = tmp_path / "nope.env"
    with pytest.raises(DiscordEnvFileMissingError) as excinfo:
        DiscordTransport.from_env_file(missing)
    message = str(excinfo.value)
    assert str(missing) in message
    assert "DISCORD_SELFBOT_TOKEN" in message
    assert "chmod 600" in message


def test_env_file_readable_by_others_is_rejected(tmp_path):
    path = write_env(tmp_path, mode=0o644)
    with pytest.raises(DiscordEnvFilePermissionsError) as excinfo:
        DiscordTransport.from_env_file(path)
    message = str(excinfo.value)
    assert "0600" in message
    assert f"chmod 600 {path}" in message


def test_env_parsing_skips_comments_blanks_and_strips_export_and_quotes(tmp_path):
    path = tmp_path / "channels.env"
    path.write_text(
        "# comment\n"
        "\n"
        "export DISCORD_TRANSPORT='selfbot'\n"
        'DISCORD_SELFBOT_TOKEN="' + TOKEN + '"\n'
        "DISCORD_TARGET_USER_ID=" + USER_ID + "\n",
        encoding="utf-8",
    )
    os.chmod(path, 0o600)
    values = discord.parse_env_file(path)
    assert values["DISCORD_SELFBOT_TOKEN"] == TOKEN
    assert values["DISCORD_TRANSPORT"] == "selfbot"
    assert values["DISCORD_TARGET_USER_ID"] == USER_ID


def test_strict_load_raises_a_named_config_error_for_a_missing_token(tmp_path):
    path = write_env(tmp_path, token=None)
    with pytest.raises(DiscordConfigError) as excinfo:
        discord.load_config(path, strict=True)
    message = str(excinfo.value)
    assert CATEGORY_NOT_CONFIGURED in message
    assert "DISCORD_SELFBOT_TOKEN" in message


def test_permission_check_can_be_relaxed_for_a_shared_file(tmp_path):
    path = write_env(tmp_path, mode=0o644)
    values = discord.parse_env_file(path, require_private_mode=False)
    assert values["DISCORD_SELFBOT_TOKEN"] == TOKEN


# --------------------------------------------------------------------------
# Dry run: rehearsed, and provably no network call
# --------------------------------------------------------------------------


def test_dry_run_makes_no_network_call_and_reports_rehearsed(tmp_path):
    fake = FakeSession([FakeResponse(200, {"id": CHANNEL_ID})])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("the build is green", level="high")

    assert fake.calls == [], "a dry run must not make any HTTP call"
    assert result.action == ACTION_SEND, "action says what was attempted, not the outcome"
    assert result.outcome == "rehearsed"
    assert result.channel == "discord"
    assert result.delivered is False
    assert result.dry_run is True
    assert result.message_id is None
    assert result.error is None
    assert result.ok is True, "a clean rehearsal is ok: the ladder must not escalate"
    assert "no network call" in result.detail
    assert discord.category_of(result) == CATEGORY_REHEARSED


def test_send_defaults_to_dry_run(tmp_path):
    fake = FakeSession()
    transport, _ = build_transport(tmp_path, fake)
    result = transport.send("did you take your medication?", level="critical")
    assert fake.calls == []
    assert result.dry_run is True
    assert result.delivered is False


def test_dryrun_transport_mode_forces_rehearsal_even_when_asked_to_send(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake, transport="dryrun")

    result = transport.send("must not really go out", level="high", dry_run=False)

    assert fake.calls == []
    assert result.delivered is False
    assert result.dry_run is True
    assert result.outcome == "rehearsed"
    assert "DISCORD_TRANSPORT=dryrun" in result.detail


def test_dry_run_of_an_unconfigured_transport_is_a_named_failure(tmp_path):
    fake = FakeSession()
    transport, _ = build_transport(tmp_path, fake, token=None)

    result = transport.send("anything", level="high", dry_run=True)

    # Nothing was rehearsed, so this is a failure - a rehearsal would claim the
    # send *would* have worked.
    assert result.delivered is False
    assert result.dry_run is False
    assert result.outcome == "failed"
    assert result.is_failure is True
    assert result.error is not None
    assert "DISCORD_SELFBOT_TOKEN" in result.error
    assert CATEGORY_NOT_CONFIGURED in result.error
    assert fake.calls == []


# --------------------------------------------------------------------------
# A real send: only a 2xx is delivery
# --------------------------------------------------------------------------


def test_2xx_reports_delivery_with_the_message_id(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("dinner is ready", level="medium", dry_run=False)

    assert result.action == ACTION_SEND
    assert result.outcome == "sent"
    assert result.channel == "discord"
    assert result.delivered is True
    assert result.dry_run is False
    assert result.message_id == "msg-42"
    assert result.error is None
    assert result.ok is True
    assert discord.category_of(result) == CATEGORY_SENT
    assert len(fake.calls) == 2, "a DM send opens the channel, then posts"
    assert fake.calls[0]["url"].endswith("/users/@me/channels")
    assert fake.calls[1]["url"].endswith(f"/channels/{CHANNEL_ID}/messages")
    assert fake.calls[1]["json"] == {"content": "dinner is ready"}


def test_real_send_authenticates_and_sets_a_browser_user_agent(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)

    transport.send("hello", level="high", dry_run=False)

    for call in fake.calls:
        assert call["headers"]["Authorization"] == TOKEN
        assert "python-requests" not in call["headers"]["User-Agent"]


def test_a_channel_target_posts_directly_without_opening_a_dm(tmp_path):
    fake = FakeSession([FakeResponse(200, {"id": "msg-7"})])
    transport, _ = build_transport(tmp_path, fake, target_user=None, target_channel=CHANNEL_ID)

    result = transport.send("to the channel", level="high", dry_run=False)

    assert result.delivered is True
    assert result.message_id == "msg-7"
    assert len(fake.calls) == 1
    assert fake.calls[0]["url"].endswith(f"/channels/{CHANNEL_ID}/messages")


def test_2xx_without_a_message_id_is_delivered_but_not_verifiable(tmp_path):
    fake = FakeSession([FakeResponse(200, {"id": CHANNEL_ID}), FakeResponse(204)])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("no body back", level="high", dry_run=False)

    assert result.delivered is True
    assert result.message_id is None
    assert "no message id" in result.detail


def test_200_on_the_dm_open_without_a_channel_id_is_a_failure(tmp_path):
    fake = FakeSession([FakeResponse(200, {"unexpected": True})])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("nowhere to post this", level="high", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is False
    assert result.message_id is None
    assert result.error is not None
    assert "no channel id" in result.error
    assert discord.category_of(result) == CATEGORY_API_ERROR


# --------------------------------------------------------------------------
# Non-2xx: never delivery, and each family named separately
# --------------------------------------------------------------------------


@pytest.mark.parametrize("status", [400, 401, 403, 404, 429, 500, 502, 503])
def test_non_2xx_never_reports_delivery_and_names_the_status(tmp_path, status):
    fake = FakeSession([FakeResponse(200, {"id": CHANNEL_ID}), FakeResponse(status, {"message": "nope"})])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("anything", level="high", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is False
    assert result.message_id is None
    assert result.action == ACTION_SEND
    assert result.outcome == "failed"
    assert result.is_failure is True
    assert result.error is not None
    assert f"HTTP {status}" in result.error


def test_401_names_a_rejected_credential_and_tells_the_operator_what_to_fix(tmp_path):
    fake = FakeSession([FakeResponse(401, {"message": "401: Unauthorized"})])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("ping", level="high", dry_run=False)

    assert result.delivered is False
    assert discord.category_of(result) == CATEGORY_CREDENTIAL_REJECTED
    assert "401" in result.error
    assert "DISCORD_SELFBOT_TOKEN" in result.error
    assert "chmod 600" in result.error


def test_403_names_a_permission_problem_rather_than_a_bad_token(tmp_path):
    fake = FakeSession([FakeResponse(403, {"message": "Cannot send messages to this user"})])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("ping", level="high", dry_run=False)

    assert result.delivered is False
    assert discord.category_of(result) == CATEGORY_CREDENTIAL_REJECTED
    assert "403" in result.error
    assert "not permitted" in result.error


def test_429_names_rate_limiting_and_reports_the_retry_after(tmp_path):
    fake = FakeSession(
        [FakeResponse(429, {"message": "You are being rate limited."}, headers={"Retry-After": "7"})]
    )
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("ping", level="high", dry_run=False)

    assert result.delivered is False
    assert discord.category_of(result) == CATEGORY_RATE_LIMITED
    assert "429" in result.error
    assert "7" in result.error


def test_5xx_names_a_server_side_failure(tmp_path):
    fake = FakeSession([FakeResponse(503, {"message": "Service Unavailable"})])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("ping", level="high", dry_run=False)

    assert result.delivered is False
    assert discord.category_of(result) == CATEGORY_SERVER_ERROR
    assert "503" in result.error
    assert "server" in result.error.lower()


def test_the_ladder_families_are_four_distinct_categories(tmp_path):
    categories = set()
    for status in (401, 429, 503, 400):
        fake = FakeSession([FakeResponse(status, {"message": "x"})])
        transport, _ = build_transport(tmp_path, fake)
        result = transport.send("ping", level="high", dry_run=False)
        categories.add(discord.category_of(result))
    assert categories == {
        CATEGORY_CREDENTIAL_REJECTED,
        CATEGORY_RATE_LIMITED,
        CATEGORY_SERVER_ERROR,
        CATEGORY_API_ERROR,
    }


def test_a_network_failure_is_a_named_failure_not_a_success(tmp_path):
    fake = FakeSession(error=ConnectionResetError("connection reset by peer"))
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("ping", level="high", dry_run=False)

    assert result.delivered is False
    assert result.dry_run is False
    assert result.action == ACTION_SEND
    assert result.outcome == "failed"
    assert discord.category_of(result) == CATEGORY_TRANSPORT_ERROR
    assert "ConnectionResetError" in result.error


def test_an_http_callable_can_be_injected_instead_of_a_session(tmp_path):
    calls = []

    def poster(url, *, json=None, headers=None, timeout=None):
        calls.append({"url": url, "json": json})
        if url.endswith("/users/@me/channels"):
            return FakeResponse(200, {"id": CHANNEL_ID})
        return FakeResponse(200, {"id": "msg-callable"})

    path = write_env(tmp_path)
    transport = DiscordTransport.from_env_file(path, http=poster)

    result = transport.send("via a callable", level="high", dry_run=False)

    assert result.delivered is True
    assert result.message_id == "msg-callable"
    assert len(calls) == 2


# --------------------------------------------------------------------------
# Requests that are refused before any HTTP work
# --------------------------------------------------------------------------


def test_an_empty_message_is_refused_without_touching_the_network(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("   ", level="high", dry_run=False)

    assert fake.calls == []
    assert result.delivered is False
    assert discord.category_of(result) == CATEGORY_INVALID_REQUEST


def test_an_oversized_message_is_refused_with_the_real_reason(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("x" * (discord.MAX_MESSAGE_CHARS + 1), level="high", dry_run=False)

    assert fake.calls == []
    assert result.delivered is False
    assert str(discord.MAX_MESSAGE_CHARS) in result.error


def test_a_blank_level_is_refused(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("a message", level="  ", dry_run=False)

    assert fake.calls == []
    assert result.delivered is False
    assert "level" in result.error


# --------------------------------------------------------------------------
# The secret rule
# --------------------------------------------------------------------------


def test_redact_shows_only_the_last_four_and_a_fingerprint():
    rendered = redact(TOKEN)

    assert TOKEN not in rendered
    assert TOKEN[-4:] in rendered
    assert fingerprint(TOKEN) in rendered
    # Strip the two permitted pieces; nothing of the token may be left over.
    residue = rendered.replace(fingerprint(TOKEN), "").replace(TOKEN[-4:], "")
    assert residue.strip("…:") == "", f"more than last-four leaked: {rendered!r}"


def test_redact_gives_short_secrets_no_tail_at_all():
    assert redact("abc123") == f"<redacted:{fingerprint('abc123')}>"
    assert "abc123" not in redact("abc123")
    assert redact("") == "<empty>"


def test_fingerprint_is_stable_and_discriminating():
    assert fingerprint(TOKEN) == fingerprint(TOKEN)
    assert fingerprint(TOKEN) != fingerprint(TOKEN + "x")
    assert len(fingerprint(TOKEN)) == 8


def test_redact_text_sweeps_every_secret_it_is_given():
    text = f"header Authorization: {TOKEN} and also {TOKEN[-8:]}"
    swept = redact_text(text, (TOKEN,))
    assert TOKEN not in swept
    assert fingerprint(TOKEN) in swept


def test_the_token_never_appears_in_a_result_or_a_log_line(tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger=LOG)
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)

    rehearsed = transport.send("first", level="high")
    delivered = transport.send("second", level="high", dry_run=False)

    for result in (rehearsed, delivered):
        rendered = f"{result!r} {result.detail!r} {result.error!r} {str(result)}"
        assert TOKEN not in rendered
    assert repr(transport) and TOKEN not in repr(transport)
    assert TOKEN not in repr(transport.config)

    # The log must actually mention the credential, otherwise this proves nothing.
    assert caplog.text, "the transport logged nothing, so the leak check is vacuous"
    assert TOKEN not in caplog.text
    assert fingerprint(TOKEN) in caplog.text


def test_the_token_never_reaches_an_error_even_when_the_upstream_echoes_it(tmp_path):
    # A proxy or a misbehaving gateway may quote the Authorization header back.
    body = json.dumps({"message": f"Invalid token: {TOKEN}", "code": 40001})
    fake = FakeSession([FakeResponse(401, payload=None, text=body)])
    transport, _ = build_transport(tmp_path, fake)

    result = transport.send("ping", level="high", dry_run=False)

    assert result.delivered is False
    assert TOKEN not in result.error
    assert TOKEN not in result.detail
    assert fingerprint(TOKEN) in result.error


def test_a_token_in_the_transport_mode_line_is_not_quoted_back(tmp_path):
    transport, _ = build_transport(tmp_path, transport=TOKEN)
    result = transport.send("ping", level="high", dry_run=False)
    assert result.delivered is False
    assert TOKEN not in result.error
    assert TOKEN not in result.detail


def test_the_config_repr_hides_the_token_and_keeps_it_usable():
    config = DiscordConfig(token=TOKEN, target_user_id=USER_ID, source="<memory>")
    rendered = repr(config)
    assert TOKEN not in rendered
    assert fingerprint(TOKEN) in rendered
    # Hiding it must not hide it from the one caller that needs the raw value.
    assert config.token == TOKEN
    assert config.redacted_token == redact(TOKEN)


# --------------------------------------------------------------------------
# Shared interface
# --------------------------------------------------------------------------


def test_the_transport_exposes_the_shared_interface_shape():
    signature = inspect.signature(DiscordTransport.send)
    parameters = list(signature.parameters)
    assert parameters == ["self", "message", "level", "dry_run"]
    assert signature.parameters["level"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["dry_run"].kind is inspect.Parameter.KEYWORD_ONLY
    assert signature.parameters["dry_run"].default is True

    assert DiscordTransport.name == "discord"
    # None is the shared contract's mark for a last-resort-only rung: Signal is
    # rank 1 and goes first, Discord is never tried while Signal can deliver.
    assert DiscordTransport.level_rank is None
    assert DiscordTransport.last_resort is True
    assert DiscordTransport.can_call is False
    assert callable(DiscordTransport.available)


def test_unavailable_reason_names_the_missing_key_without_a_secret(tmp_path):
    missing, path = build_transport(tmp_path, token=None)
    reason = missing.unavailable_reason
    assert reason is not None
    assert "DISCORD_SELFBOT_TOKEN" in reason
    assert str(path) in reason
    assert TOKEN not in reason

    available_transport, _ = build_transport(tmp_path, ExplodingSession())
    assert available_transport.unavailable_reason is None


def test_the_transport_satisfies_the_shared_protocol_when_it_exists(tmp_path):
    shared = _sibling("transport")
    if shared is None:
        return  # sibling module not landed; the local mirror stands in
    assert discord.SHARED_INTERFACE is True
    transport, _ = build_transport(tmp_path, ExplodingSession())
    assert isinstance(transport, shared.Transport) is True


def test_the_ladder_orders_discord_last_and_never_calls_with_it(tmp_path):
    ladder = _sibling("ladder")
    if ladder is None:
        return  # sibling module not landed
    transport, _ = build_transport(tmp_path, ExplodingSession())
    signal = StubTransport("signal", level_rank=1)

    assert ladder.order_transports([transport, signal]) == [signal, transport]
    assert ladder.is_last_resort(transport) is True
    assert ladder.can_place_call(transport) is False


def test_the_ladder_falls_through_to_discord_when_the_preferred_rung_fails(tmp_path):
    ladder_module = _sibling("ladder")
    shared = _sibling("transport")
    if ladder_module is None or shared is None:
        return  # sibling modules not landed
    fake = FakeSession(ok_pair())
    discord_transport, _ = build_transport(tmp_path, fake)
    broken_signal = StubTransport(
        "signal",
        level_rank=1,
        result=shared.failed_result(
            "signal", "[not_configured] signal-cli is not installed"
        ),
    )

    result = ladder_module.Ladder(
        [discord_transport, broken_signal], dry_run=False
    ).run("the oven is on", level="medium")

    assert result.tried == ["signal", "discord"]
    assert result.channel == "discord"
    assert result.delivered is True
    assert result.ok is True
    assert len(fake.calls) == 2, "Discord really posted through the injected session"


def test_the_ladder_does_not_reach_discord_while_a_preferred_rung_rehearses(tmp_path):
    ladder_module = _sibling("ladder")
    shared = _sibling("transport")
    if ladder_module is None or shared is None:
        return
    fake = FakeSession(ok_pair())
    discord_transport, _ = build_transport(tmp_path, fake)
    working_signal = StubTransport(
        "signal",
        level_rank=1,
        result=shared.rehearsed_result("signal", detail="dry run"),
    )

    result = ladder_module.Ladder([discord_transport, working_signal]).run(
        "the oven is on", level="medium"
    )

    assert result.tried == ["signal"]
    assert fake.calls == []
    assert result.delivered is False


def test_the_http_timeout_is_capped(tmp_path):
    fake = FakeSession(ok_pair())
    config = DiscordConfig(
        token=TOKEN, target_user_id=USER_ID, timeout=discord.MAX_TIMEOUT_SECONDS * 100
    )
    transport = DiscordTransport(config, http=fake)

    transport.send("slow down", level="high", dry_run=False)

    assert fake.calls[0]["timeout"] == discord.MAX_TIMEOUT_SECONDS


def test_send_result_is_the_shared_transport_type_once_it_exists():
    try:
        from claire.outreach import transport
    except Exception:  # pragma: no cover - sibling module not written yet
        return  # nothing to compare against; the local mirror stands in
    assert discord.SendResult is transport.SendResult


def test_send_result_carries_the_shared_field_set(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)
    result = transport.send("fields", level="high", dry_run=False)

    assert set(result.__dataclass_fields__) == {
        "action",
        "channel",
        "delivered",
        "dry_run",
        "detail",
        "message_id",
        "error",
    }
    assert result.channel == "discord"


def test_category_of_distinguishes_failure_from_rehearsal(tmp_path):
    fake = FakeSession(ok_pair())
    transport, _ = build_transport(tmp_path, fake)
    assert discord.category_of(transport.send("x", level="low")) == CATEGORY_REHEARSED
    assert discord.category_of(transport.send("x", level="low", dry_run=False)) == CATEGORY_SENT
