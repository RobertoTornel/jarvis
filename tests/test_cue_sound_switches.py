"""The switches that silence Claire's cue sounds.

The thinking tune is the only cue she makes — a pad that pulses once a second
while she works, which is what the owner hears as "pinging". These cover the two
ways to stop it without editing ``config.json``: the environment variable (the
one that also reaches the installed service, via ``claire.env``) and the
command-line flag for a run started by hand.

They also pin the honest edges: an unrecognised value changes nothing rather
than being guessed at, and the flag never mutates the shared, frozen config.
"""

from __future__ import annotations

from dataclasses import replace

import pytest

from claire.cli import build_parser
from claire.config import Settings, _env_flag, load_settings

# pytestmark makes this whole file visible to the unit gate.
pytestmark = pytest.mark.unit  # the gate is `pytest tests/ -m unit`


class TestTheEnvironmentSwitch:
    def test_unset_leaves_the_configured_value_alone(self, monkeypatch):
        monkeypatch.delenv("CLAIRE_TUNE_ENABLED", raising=False)

        assert load_settings().tune_enabled is True

    @pytest.mark.parametrize("written", ["0", "false", "FALSE", "no", "off", " off "])
    def test_a_negative_value_silences_the_tune(self, monkeypatch, written):
        monkeypatch.setenv("CLAIRE_TUNE_ENABLED", written)

        assert load_settings().tune_enabled is False

    @pytest.mark.parametrize("written", ["1", "true", "YES", "on"])
    def test_a_positive_value_keeps_it(self, monkeypatch, written):
        monkeypatch.setenv("CLAIRE_TUNE_ENABLED", written)

        assert load_settings().tune_enabled is True

    @pytest.mark.parametrize("written", ["", "maybe", "2", "ture"])
    def test_an_unrecognised_value_is_not_guessed_at(self, monkeypatch, written):
        """A typo must not silently flip a switch whose purpose is to be trusted."""
        monkeypatch.setenv("CLAIRE_TUNE_ENABLED", written)

        assert load_settings().tune_enabled is True

    def test_the_helper_reports_the_default_for_an_unknown_word(self, monkeypatch):
        monkeypatch.setenv("CLAIRE_TUNE_ENABLED", "banana")

        assert _env_flag("CLAIRE_TUNE_ENABLED", default=False) is False
        assert _env_flag("CLAIRE_NOTHING_SET_THIS", default=True) is True


class TestTheCommandLineFlag:
    def test_the_flag_parses_and_defaults_to_off(self):
        assert build_parser().parse_args(["--no-tune"]).no_tune is True
        assert build_parser().parse_args([]).no_tune is False

    def test_the_flag_is_not_an_alias_for_smoke_test(self):
        args = build_parser().parse_args(["--no-tune"])

        assert args.smoke_test is False, "silencing the tune must not start a smoke test"

    def test_the_flag_produces_a_silenced_config(self):
        from claire.daemon import _apply_cli_overrides

        cfg = load_settings()

        silenced = _apply_cli_overrides(cfg, no_tune=True)

        assert silenced.tune_enabled is False
        assert cfg.tune_enabled is True, (
            "Settings is frozen and shared: a flag must not mutate the config "
            "that another thread already holds"
        )

    def test_without_the_flag_the_config_is_returned_unchanged(self):
        from claire.daemon import _apply_cli_overrides

        cfg = load_settings()

        assert _apply_cli_overrides(cfg) is cfg

    def test_the_flag_is_harmless_when_the_tune_is_already_off(self):
        from claire.daemon import _apply_cli_overrides

        cfg = replace(load_settings(), tune_enabled=False)

        assert _apply_cli_overrides(cfg, no_tune=True) is cfg

    def test_the_help_text_says_what_it_silences(self):
        help_text = build_parser().format_help()

        assert "--no-tune" in help_text
        assert "CLAIRE_TUNE_ENABLED" in help_text, (
            "the flag must point at the switch that also reaches the service"
        )


class TestTheListenerHonoursIt:
    """``tune_enabled`` is what the listener reads; a flag only sets that."""

    def test_a_silenced_config_never_reaches_the_player(self):
        from claire.listening.listener import VoiceListener

        cfg = replace(load_settings(), tune_enabled=False)
        listener = VoiceListener.__new__(VoiceListener)
        listener.cfg = cfg
        listener._tune_player = None

        # _start_thinking_tune is the only thing that builds a TunePlayer; with
        # the switch off it must decline before importing or playing anything.
        listener._start_thinking_tune()

        assert listener._tune_player is None
        assert listener._is_thinking_tune_active() is False


class TestTheWiringReachesTheDaemon:
    def test_main_hands_the_daemon_the_flag(self, monkeypatch):
        """The flag must survive the hand-off from the CLI to the daemon."""
        import claire.cli as cli
        import claire.daemon as daemon

        seen: dict[str, object] = {}

        def fake_daemon_main(*, smoke_test: bool = False, no_tune: bool = False) -> None:
            seen["smoke_test"] = smoke_test
            seen["no_tune"] = no_tune

        monkeypatch.setattr(daemon, "main", fake_daemon_main)

        assert cli.main(["--no-tune"]) == 0
        assert seen == {"smoke_test": False, "no_tune": True}
