"""Unit tests for ``scripts/acceptance_check.py``.

The fail-proof is the first group here: a check that RAISES must be reported as a
problem for that capability, the run must still complete, and every other
capability must still be listed. The naive implementation let the exception
escape ``AcceptanceRunner.run`` -- so the owner, whose problem is already that
something is broken, got a traceback from the tool that was supposed to tell
them what was wrong with their install. That RED run is recorded in
``docs/ACCEPTANCE.md``; the test below is what keeps it GREEN.

Nothing in this file launches a real browser, reads a real screen, opens a
microphone or talks to a network: every capability is injected as a fake, and
the few tests that exercise a real default check only touch ones that are pure
(urgency classification, timers on a fake clock, a key store in ``tmp_path``).

The gate is ``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no
unit marker -- hence ``pytestmark = pytest.mark.unit`` below. Do not remove it
and do not add this file to the guard's baseline list.
"""

from __future__ import annotations

import importlib.util
import json
import stat
import sys
from pathlib import Path

import pytest

# The gate runs `pytest -m unit`. A test file without this marker is silently
# DESELECTED, so it can pass forever without ever running.
pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPT = _ROOT / "scripts" / "acceptance_check.py"
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

_NOW = "2026-01-01T00:00:00+00:00"
_LOAD_COUNTER = {"n": 0}


def _load_module():
    """Load the script fresh, under a unique name, as its own module.

    It must be registered in ``sys.modules`` *before* ``exec_module``: the
    script defines dataclasses, and dataclasses resolves string annotations
    through ``sys.modules[cls.__module__]``.
    """

    _LOAD_COUNTER["n"] += 1
    name = f"acceptance_check_under_test_{_LOAD_COUNTER['n']}"
    spec = importlib.util.spec_from_file_location(name, _SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except BaseException:
        sys.modules.pop(name, None)
        raise
    return module


@pytest.fixture()
def mod():
    return _load_module()


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #


def _ready_probes(mod, **overrides):
    """One 'ready' fake per capability, with named overrides for the exceptions."""

    probes = {
        name: (lambda ctx, n=name: mod.ProbeResult.ready(f"{n} fake is ready"))
        for name in mod.CHECK_ORDER
    }
    probes.update(overrides)
    return probes


def _runner(mod, probes):
    return mod.AcceptanceRunner(
        probes=probes, context=mod.AcceptanceContext(now=lambda: _NOW)
    )


def _install_fakes(mod, monkeypatch, probes):
    """Make ``main()`` build a runner with the injected fakes."""

    real_runner = mod.AcceptanceRunner

    def factory(**kwargs):  # main() calls AcceptanceRunner() with no arguments
        return real_runner(
            probes=probes, context=mod.AcceptanceContext(now=lambda: _NOW)
        )

    monkeypatch.setattr(mod, "AcceptanceRunner", factory)


def _json_out(capsys) -> dict:
    captured = capsys.readouterr()
    return json.loads(captured.out)


def _row_for(table: str, name: str) -> str:
    rows = [line for line in table.splitlines() if line.startswith(f"{name} ")]
    assert rows, f"the table has no row for {name!r}"
    return rows[0]


# --------------------------------------------------------------------------- #
# The fail-proof: one exploding check must not take the scoreboard with it
# --------------------------------------------------------------------------- #


def test_a_crashing_check_is_reported_and_the_run_still_lists_every_other_check(mod):
    def exploding(ctx):
        raise RuntimeError("boom: this check is broken on purpose")

    probes = _ready_probes(mod, browser=exploding)
    report = _runner(mod, probes).run()

    # The run completed and every capability is accounted for.
    assert len(report.checks) == len(mod.CHECK_ORDER)
    assert [c.name for c in report.checks] == list(mod.CHECK_ORDER)

    crashed = next(c for c in report.checks if c.name == "browser")
    assert crashed.state == mod.STATE_BLOCKED
    assert crashed.problem is True
    assert "RuntimeError" in crashed.detail and "boom" in crashed.detail
    assert crashed.fix == mod.FIX_CHECK_CRASHED
    assert crashed.data["exception"] == "RuntimeError"

    # Every other capability still reported its own verdict.
    others = [c for c in report.checks if c.name != "browser"]
    assert all(c.state == mod.STATE_READY for c in others)
    assert len(others) == len(mod.CHECK_ORDER) - 1
    assert report.exit_code == mod.EXIT_BLOCKED
    assert report.counts["problems"] == 1
    assert report.to_dict()["problems"] == ["browser"]


def test_a_crashing_check_does_not_stop_main_from_printing_the_report(
    mod, monkeypatch, capsys
):
    def exploding(ctx):
        raise ValueError("kaboom")

    _install_fakes(mod, monkeypatch, _ready_probes(mod, ocr=exploding))

    exit_code = mod.main(["--json"])
    payload = _json_out(capsys)

    assert exit_code == mod.EXIT_BLOCKED
    assert payload["schema"] == mod.SCHEMA
    assert len(payload["checks"]) == len(mod.CHECK_ORDER)
    assert payload["problems"] == ["ocr"]
    states = {check["name"]: check["state"] for check in payload["checks"]}
    assert states["ocr"] == mod.STATE_BLOCKED
    assert states["browser"] == mod.STATE_READY


def test_a_probe_returning_rubbish_is_a_reported_problem_not_a_crash(mod):
    report = _runner(mod, _ready_probes(mod, timers=lambda ctx: None)).run()

    timers = next(c for c in report.checks if c.name == "timers")
    assert timers.state == mod.STATE_BLOCKED
    assert timers.problem is True
    assert timers.fix == mod.FIX_CHECK_MALFORMED
    assert len(report.checks) == len(mod.CHECK_ORDER)


def test_an_unknown_check_name_is_reported_rather_than_silently_skipped(mod):
    report = _runner(mod, _ready_probes(mod)).run(["browser", "not_a_capability"])

    assert [c.name for c in report.checks] == ["browser", "not_a_capability"]
    unknown = report.checks[1]
    assert unknown.state == mod.STATE_BLOCKED
    assert unknown.problem is True
    assert "not_a_capability" in unknown.detail


def test_ctrl_c_is_not_swallowed_by_the_guard(mod):
    def interrupted(ctx):
        raise KeyboardInterrupt

    with pytest.raises(KeyboardInterrupt):
        _runner(mod, _ready_probes(mod, config=interrupted)).run(["config"])


# --------------------------------------------------------------------------- #
# Exit codes
# --------------------------------------------------------------------------- #


def test_exit_code_is_zero_when_everything_is_ready(mod, monkeypatch, capsys):
    _install_fakes(mod, monkeypatch, _ready_probes(mod))
    assert mod.main(["--json"]) == mod.EXIT_READY
    payload = _json_out(capsys)
    assert payload["summary"] == {
        "total": len(mod.CHECK_ORDER),
        "ready": len(mod.CHECK_ORDER),
        "blocked": 0,
        "unmeasured": 0,
        "problems": 0,
        "exit_code": 0,
    }
    assert payload["blocked"] == [] and payload["unmeasured"] == []


def test_exit_code_is_nonzero_when_something_is_blocked(mod, monkeypatch, capsys):
    probes = _ready_probes(
        mod,
        ocr=lambda ctx: mod.ProbeResult.blocked(
            "tesseract is not installed", fix="sudo apt-get install -y tesseract-ocr"
        ),
    )
    _install_fakes(mod, monkeypatch, probes)
    assert mod.main(["--json"]) == mod.EXIT_BLOCKED
    payload = _json_out(capsys)
    assert payload["summary"]["blocked"] == 1
    assert payload["summary"]["exit_code"] == mod.EXIT_BLOCKED
    assert payload["blocked"] == ["ocr"]


def test_unmeasured_alone_exits_nonzero_and_is_never_ready(mod, monkeypatch, capsys):
    probes = _ready_probes(
        mod,
        wake_word=lambda ctx: mod.ProbeResult.unmeasured(
            "no microphone audio has ever been captured"
        ),
    )
    _install_fakes(mod, monkeypatch, probes)
    assert mod.main(["--json"]) == mod.EXIT_UNMEASURED
    payload = _json_out(capsys)
    assert payload["unmeasured"] == ["wake_word"]
    assert payload["summary"]["exit_code"] == mod.EXIT_UNMEASURED


def test_unknown_only_name_is_a_usage_error_with_the_known_names(
    mod, monkeypatch, capsys
):
    _install_fakes(mod, monkeypatch, _ready_probes(mod))
    assert mod.main(["--only", "nope"]) == mod.EXIT_USAGE
    captured = capsys.readouterr()
    assert "unknown capability" in captured.err
    assert "nope" in captured.err
    assert "browser" in captured.err and "ocr" in captured.err
    assert captured.out == ""


# --------------------------------------------------------------------------- #
# --json and the table must say the same thing
# --------------------------------------------------------------------------- #


def test_json_parses_and_carries_the_same_states_as_the_table(
    mod, monkeypatch, capsys
):
    probes = _ready_probes(
        mod,
        browser=lambda ctx: mod.ProbeResult.blocked(
            "no chromium binary was found", fix="playwright install chromium"
        ),
        wake_word=lambda ctx: mod.ProbeResult.unmeasured(
            "no microphone audio has ever been captured"
        ),
    )
    _install_fakes(mod, monkeypatch, probes)

    assert mod.main([]) == mod.EXIT_BLOCKED
    table = capsys.readouterr().out
    assert mod.main(["--json"]) == mod.EXIT_BLOCKED
    payload = _json_out(capsys)

    assert payload["schema"] == mod.SCHEMA
    assert payload["generated_at"] == _NOW
    assert len(payload["checks"]) == len(mod.CHECK_ORDER)
    states = {check["name"]: check["state"] for check in payload["checks"]}
    assert states == {c["name"]: c["state"] for c in payload["checks"]}
    for check in payload["checks"]:
        row = _row_for(table, check["name"])
        assert check["state"] in row, f"{check['name']} row disagrees with --json"
        assert check["title"]
    assert states["browser"] == mod.STATE_BLOCKED
    assert states["wake_word"] == mod.STATE_UNMEASURED
    assert states["timers"] == mod.STATE_READY
    assert payload["summary"]["total"] == len(mod.CHECK_ORDER)
    assert (
        payload["summary"]["ready"]
        + payload["summary"]["blocked"]
        + payload["summary"]["unmeasured"]
        == payload["summary"]["total"]
    )
    # The blocked capability's fix is in the machine-readable report too.
    browser = next(c for c in payload["checks"] if c["name"] == "browser")
    assert browser["fix"] == "playwright install chromium"


# --------------------------------------------------------------------------- #
# --only and --list
# --------------------------------------------------------------------------- #


def test_only_filters_to_the_named_capabilities(mod, monkeypatch, capsys):
    _install_fakes(mod, monkeypatch, _ready_probes(mod))
    assert mod.main(["--only", "browser,ocr", "--json"]) == mod.EXIT_READY
    payload = _json_out(capsys)
    names = [c["name"] for c in payload["checks"]]
    assert sorted(names) == ["browser", "ocr"]
    # Canonical CHECK_ORDER, not the order typed on the command line.
    assert names == [n for n in mod.CHECK_ORDER if n in ("browser", "ocr")]
    assert payload["summary"]["total"] == 2


def test_only_accepts_repeated_flags_and_dedupes(mod):
    names, unknown = mod.select_names(["browser", "ocr,browser"])
    assert unknown == []
    assert names == tuple(n for n in mod.CHECK_ORDER if n in ("browser", "ocr"))
    assert mod.select_names(None)[0] == mod.CHECK_ORDER


def test_list_prints_every_capability_and_exits_zero(mod, capsys):
    assert mod.main(["--list"]) == mod.EXIT_READY
    out = capsys.readouterr().out
    for name in mod.CHECK_ORDER:
        assert name in out
        assert mod.CHECK_TITLES[name] in out
    assert mod.CHECK_SUMMARIES["key_store"] in out


def test_every_default_probe_has_a_title_and_a_summary(mod):
    assert set(mod.DEFAULT_PROBES) == set(mod.CHECK_ORDER)
    for name in mod.CHECK_ORDER:
        assert name in mod.CHECK_TITLES
        assert name in mod.CHECK_SUMMARIES
        assert callable(mod.DEFAULT_PROBES[name])


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #


def test_no_secret_value_reaches_the_table_or_the_json(
    mod, monkeypatch, capsys, tmp_path
):
    from claire.platform import keys

    secret = "sk-acceptance-Sup3rSecretValue-42"
    store = tmp_path / "keys.json"
    store.write_text(
        json.dumps({"version": 1, "keys": {"OMNIROUTE_API_KEY": secret}}), encoding="utf-8"
    )
    store.chmod(0o600)
    monkeypatch.setenv("CLAIRE_KEY_STORE", str(store))

    assert mod.main(["--only", "key_store", "--json"]) == mod.EXIT_READY
    json_out = capsys.readouterr().out
    assert mod.main(["--only", "key_store"]) == mod.EXIT_READY
    table_out = capsys.readouterr().out

    assert secret not in json_out
    assert secret not in table_out
    assert secret not in "".join(table_out.split())
    assert "OMNIROUTE_API_KEY" in json_out
    assert keys.mask(secret) in json_out
    # The table wraps long tokens, so compare against the table with its
    # whitespace removed (the masked rendering itself contains no whitespace).
    assert keys.mask(secret) in "".join(table_out.split())
    # The check is read-only: the store file is untouched, value included.
    assert store.is_file()
    assert stat.S_IMODE(store.stat().st_mode) == 0o600
    assert json.loads(store.read_text(encoding="utf-8")) == {
        "version": 1,
        "keys": {"OMNIROUTE_API_KEY": secret},
    }


def test_the_script_never_calls_the_raw_key_accessor():
    from claire.platform import keys

    source = _SCRIPT.read_text(encoding="utf-8")
    assert keys.RAW_VALUE_ACCESSOR not in source
    assert "store.get(" not in source
    assert "describe()" in source  # the masked path is what it uses


def test_key_store_reports_names_masked_when_a_store_exists(
    mod, monkeypatch, capsys, tmp_path
):
    from claire.platform import keys as keys_module

    store = tmp_path / "keys.json"
    store.write_text(
        json.dumps({"version": 1, "keys": {"DISCORD_SELFBOT_TOKEN": "not-a-real-token-1234"}}),
        encoding="utf-8",
    )
    store.chmod(0o600)
    monkeypatch.setenv("CLAIRE_KEY_STORE", str(store))
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)

    assert mod.main(["--only", "key_store", "--json"]) == mod.EXIT_READY
    payload = _json_out(capsys)
    check = payload["checks"][0]
    assert check["state"] == mod.STATE_READY
    assert check["data"]["stored_names"] == ["DISCORD_SELFBOT_TOKEN"]
    entry = check["data"]["entries"][0]
    assert entry["name"] == "DISCORD_SELFBOT_TOKEN"
    assert entry["masked"] == keys_module.mask("not-a-real-token-1234")
    assert "not-a-real-token-1234" not in json.dumps(payload)


def test_key_store_without_a_file_is_blocked_with_a_named_fix(
    mod, monkeypatch, capsys, tmp_path
):
    monkeypatch.setenv("CLAIRE_KEY_STORE", str(tmp_path / "missing.json"))

    assert mod.main(["--only", "key_store", "--json"]) == mod.EXIT_BLOCKED
    payload = _json_out(capsys)
    check = payload["checks"][0]
    assert check["state"] == mod.STATE_BLOCKED
    assert "missing.json" in check["detail"]
    # The fix must be a command that actually runs from the repository root,
    # not a `claire keys set` CLI that this checkout does not install.
    assert "KeyStore().store(" in check["fix"]
    assert "PYTHONPATH=src" in check["fix"]


# --------------------------------------------------------------------------- #
# The hardware-facing checks are injectable (no real browser/screen needed)
# --------------------------------------------------------------------------- #


def _fake_module(name, **attributes):
    import types

    module = types.ModuleType(name)
    for key, value in attributes.items():
        setattr(module, key, value)
    return module


def test_browser_probe_reports_a_locked_profile_as_blocked_not_crashed(mod, monkeypatch):
    class FakeBrowserError(Exception):
        def __init__(self, message, *, fix=None):
            self.fix = fix
            super().__init__(message)

    class FakeLocked(FakeBrowserError):
        pass

    def locked(**kwargs):
        raise FakeLocked("profile is locked", fix="close the other chromium")

    fake = _fake_module(
        "claire.senses.browser",
        BrowserError=FakeBrowserError,
        CHROMIUM_INSTALL_COMMAND="playwright install chromium",
        probe_chromium_binary=lambda: "/fake/chrome",
        get_session=locked,
        close_session=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "claire.senses.browser", fake)

    outcome = (
        mod.AcceptanceRunner(
            probes={"browser": mod.probe_browser},
            context=mod.AcceptanceContext(now=lambda: _NOW),
        )
        .run(["browser"])
        .checks[0]
    )
    assert outcome.state == mod.STATE_BLOCKED
    assert outcome.problem is False  # a machine fact, not a defect in the check
    assert outcome.fix == "close the other chromium"
    assert "locked" in outcome.detail


def test_browser_probe_reads_a_page_through_an_injected_session(mod, monkeypatch):
    class FakePage:
        title = "claire-acceptance"
        text = "claire-acceptance-probe"

    class FakeSession:
        def open(self, url):
            self.url = url

        def read(self):
            return FakePage()

    fake = _fake_module(
        "claire.senses.browser",
        BrowserError=RuntimeError,
        CHROMIUM_INSTALL_COMMAND="playwright install chromium",
        probe_chromium_binary=lambda: "/fake/chrome",
        get_session=lambda **kwargs: FakeSession(),
        close_session=lambda: None,
    )
    monkeypatch.setitem(sys.modules, "claire.senses.browser", fake)

    outcome = (
        mod.AcceptanceRunner(
            probes={"browser": mod.probe_browser},
            context=mod.AcceptanceContext(now=lambda: _NOW),
        )
        .run(["browser"])
        .checks[0]
    )
    assert outcome.state == mod.STATE_READY
    assert outcome.data["binary"] == "/fake/chrome"
    assert outcome.data["title"] == "claire-acceptance"


def test_screen_capture_probe_reports_the_install_commands_when_it_fails(
    mod, monkeypatch
):
    class FakeUnavailable(RuntimeError):
        def __init__(self, attempts=(), fixes=()):
            self.attempts = list(attempts)
            self.fixes = list(fixes)
            super().__init__("no capture mechanism")

    def never_works(destination):
        raise FakeUnavailable(
            attempts=[("grim", "not installed")], fixes=["sudo apt install grim"]
        )

    fake = _fake_module(
        "claire.senses.screen",
        ScreenCaptureUnavailableError=FakeUnavailable,
        capture_screen=never_works,
    )
    monkeypatch.setitem(sys.modules, "claire.senses.screen", fake)

    outcome = (
        mod.AcceptanceRunner(
            probes={"screen_capture": mod.probe_screen_capture},
            context=mod.AcceptanceContext(now=lambda: _NOW),
        )
        .run(["screen_capture"])
        .checks[0]
    )
    assert outcome.state == mod.STATE_BLOCKED
    assert outcome.problem is False
    assert "sudo apt install grim" in outcome.fix
    assert outcome.data["attempts"][0]["mechanism"] == "grim"


# --------------------------------------------------------------------------- #
# Never overclaim: what has genuinely never been measured stays unmeasured
# --------------------------------------------------------------------------- #


def test_capabilities_that_cannot_be_measured_here_are_never_ready(
    mod, monkeypatch, capsys, tmp_path
):
    directory = tmp_path
    # Point every path-owning subsystem at a scratch directory so this test
    # touches no real install and writes only inside pytest's tmp area.
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(directory / "config.json"))
    monkeypatch.setenv("CLAIRE_KEY_STORE", str(directory / "keys.json"))
    monkeypatch.setenv("CLAIRE_BACKUP_ROOT", str(directory / "snapshots"))
    monkeypatch.setenv("CLAIRE_VAULT_DIR", str(directory / "vault"))

    unproven = ("wake_word", "outreach_live", "tts_synthesis")
    exit_code = mod.main(["--only", ",".join(unproven), "--json"])
    payload = _json_out(capsys)
    states = {check["name"]: check["state"] for check in payload["checks"]}

    assert exit_code != mod.EXIT_READY
    for name in unproven:
        assert states[name] in (mod.STATE_UNMEASURED, mod.STATE_BLOCKED), name
        assert states[name] != mod.STATE_READY, name

    # The wake-word verdict is unconditional on this machine: no microphone
    # audio has ever been captured, so it can never be READY.
    wake = next(c for c in payload["checks"] if c["name"] == "wake_word")
    assert wake["state"] == mod.STATE_UNMEASURED
    assert "microphone" in wake["detail"]


def test_a_real_probe_that_is_pure_reports_its_verdict_without_side_effects(mod):
    """``outreach_live`` and ``outreach_ladder`` need no hardware: measure them."""

    report = _runner(mod, dict(mod.DEFAULT_PROBES)).run(
        ["outreach_ladder", "outreach_live"]
    )
    ladder, live = report.checks
    assert ladder.state == mod.STATE_READY
    assert ladder.data["dry_run_default"] is True
    assert ladder.data["low_urgency_sent_anything"] is False
    assert live.state == mod.STATE_UNMEASURED
    assert "never sends" in live.fix or "does not send" in live.fix


def test_service_check_results_map_onto_the_three_states(mod):
    from claire.platform import service

    ready = mod._service_result_to_probe(
        service.CheckResult(name="vault", status=service.STATUS_READY, detail="ok")
    )
    missing = mod._service_result_to_probe(
        service.CheckResult(
            name="vault",
            status=service.STATUS_MISSING,
            detail="gone",
            fix="mkdir -p it",
        )
    )
    problem = mod._service_result_to_probe(
        service.CheckResult(
            name="vault",
            status=service.STATUS_PROBLEM,
            detail="probe crashed",
            data={"exception": "ValueError"},
        )
    )
    assert ready.state == mod.STATE_READY
    assert missing.state == mod.STATE_BLOCKED and missing.fix == "mkdir -p it"
    assert problem.state == mod.STATE_BLOCKED
    assert problem.data["service_status"] == service.STATUS_PROBLEM
    # A missing result with no fix must still carry an actionable one.
    assert mod._service_result_to_probe(
        service.CheckResult(name="vault", status=service.STATUS_MISSING, detail="gone")
    ).fix


def test_report_rendering_never_crashes_on_long_text(mod):
    probes = _ready_probes(
        mod,
        config=lambda ctx: mod.ProbeResult.blocked(
            "x" * 5000, fix="y" * 5000
        ),
    )
    report = _runner(mod, probes).run(["config"])
    text = report.render(width=80)
    assert "config" in text
    assert "BLOCKED" in text
    assert "Exit code: 1" in text
    assert report.verdict
