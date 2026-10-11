"""Unit tests for :mod:`claire.platform.service` (R-SERVICE-1).

The fail-proof is the first group here: **every** probe is injected as a fake
that raises, and the report must still be produced with that check marked
``problem``. A naive implementation lets the exception propagate out of
``SelfCheck.run`` and the owner - whose problem is already that something is
broken - gets a traceback from the diagnostic tool.

Nothing in this file starts a service, builds an image, opens a microphone,
launches a browser or talks to a live endpoint: the default probes are only ever
exercised with their world monkeypatched (``settings``, ``http_get``,
``sys.modules``, the project functions they call). The gate is
``pytest tests/ -m unit`` and it SILENTLY DESELECTS a file with no unit marker --
hence ``pytestmark = pytest.mark.unit`` below. Do not remove it and do not add
this file to the guard's baseline list.
"""

import json
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from urllib.error import URLError

import pytest

from claire.platform import service
from claire.platform.service import (
    CHECK_ORDER,
    EXIT_NOT_READY,
    EXIT_READY,
    STATUS_MISSING,
    STATUS_PROBLEM,
    STATUS_READY,
    CheckResult,
    HealthReport,
    SelfCheck,
)

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[1]
DEPLOY_DIR = REPO_ROOT / "deploy"
UNIT_FILE = DEPLOY_DIR / "claire.service"
DOCKERFILE = DEPLOY_DIR / "Dockerfile"


# --------------------------------------------------------------------------- #
# Fixtures and helpers
# --------------------------------------------------------------------------- #


class ProbeBoom(RuntimeError):
    """The exception every raising fake probe raises."""


def _boom(_check: SelfCheck) -> CheckResult:
    raise ProbeBoom("probe exploded on purpose")


def _fake_settings(**overrides):
    """A settings object with the fields the probes read, and no surprises."""

    values = {
        "stt_model_path": "",
        "stt_engine": "parakeet",
        "streaming_stt_model_dir": "",
        "voice_device": "default",
        "llm_base_url": "http://127.0.0.1:20129/v1",
        "llm_chat_model": "test-model",
        "llm_api_key": "",
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _ready_probe(name: str):
    """A probe for ``name`` that succeeds."""

    def probe(_check: SelfCheck) -> CheckResult:
        return CheckResult(name=name, status=STATUS_READY, detail=f"{name} is fine")

    return probe


def _ready_probes() -> dict:
    return {name: _ready_probe(name) for name in CHECK_ORDER}


def _module(name: str, **attributes):
    """A real module object, so ``import <name>`` finds it in ``sys.modules``."""

    module = types.ModuleType(name)
    for attribute, value in attributes.items():
        setattr(module, attribute, value)
    return module


def _all_ready_report() -> HealthReport:
    return HealthReport(
        checks=tuple(
            CheckResult(name=name, status=STATUS_READY, detail="ok")
            for name in CHECK_ORDER
        ),
        generated_at="2026-01-01T00:00:00+00:00",
    )


# -- a tiny systemd unit-file reader ---------------------------------------- #


@dataclass
class UnitFile:
    """The unit file, split into sections the way systemd reads it.

    Comment (``#``) and blank lines are dropped, exactly as systemd drops them,
    and a directive outside any section is an error - which is what makes this a
    *parse*, not a substring search.
    """

    raw: str
    comments: str
    sections: dict[str, list[tuple[str, str]]] = field(default_factory=dict)

    def values(self, section: str, key: str) -> list[str]:
        return [value for name, value in self.sections.get(section, []) if name == key]

    def one(self, section: str, key: str) -> str:
        found = self.values(section, key)
        assert len(found) == 1, f"expected exactly one {key}= in [{section}], got {found!r}"
        return found[0]


def _parse_unit(text: str) -> UnitFile:
    sections: dict[str, list[tuple[str, str]]] = {}
    comments: list[str] = []
    current: str | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if line.startswith("#") or line.startswith(";"):
            comments.append(line)
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, [])
            assert re.fullmatch(r"[A-Za-z]+", current), f"odd section header: {raw!r}"
            continue
        assert current is not None, f"directive outside any section: {raw!r}"
        name, sep, value = line.partition("=")
        assert sep == "=", f"directive is not KEY=VALUE: {raw!r}"
        assert value.strip(), f"directive with an empty value: {raw!r}"
        assert re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", name), f"odd directive name: {raw!r}"
        sections[current].append((name, value.strip()))
    return UnitFile(raw=text, comments="\n".join(comments), sections=sections)


# -- secret detection, usable on any text file ------------------------------ #

#: Shapes that are a credential *value* rather than a mention of one.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}\b"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"),
    re.compile(r"\bxox[abpcr]-[A-Za-z0-9\-]{10,}\b"),
    re.compile(r"\beyJ[A-Za-z0-9._\-]{20,}\b"),
    re.compile(r"\b[0-9a-fA-F]{40,}\b"),
)

#: ``SOMETHING_KEY=VALUE`` (or ``: VALUE``), capturing both halves.
_KEY_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Z0-9_]*(?:API_?KEY|TOKEN|SECRET|PASSWORD|PASSWD|CREDENTIAL)[A-Z0-9_]*)"
    r"\s*[=:]\s*([^\s\"'`,)]+)"
)

#: A value that is obviously a placeholder, not a credential.
_PLACEHOLDER = re.compile(
    r"""^(?:
        <[^>]*>              # <your key>
      | \.\.\.               # ...
      | \*+                  # ***
      | x{3,}                # xxxx
      | \$\{[^}]*\}          # ${VAR}
      | %[A-Za-z]            # %h (systemd specifier)
      | (?:your|my|the)[-_]?[\w]*   # your-key
      | none | null | absent | empty | unset | placeholder
      | change[-_]?me
    )$""",
    re.IGNORECASE | re.VERBOSE,
)


def _secret_value_findings(text: str) -> list[str]:
    """Every place ``text`` looks like it carries a credential *value*."""

    findings: list[str] = []
    for line_no, line in enumerate(text.splitlines(), start=1):
        for pattern in _SECRET_VALUE_PATTERNS:
            for match in pattern.finditer(line):
                findings.append(f"line {line_no}: secret-shaped token {match.group(0)[:12]}...")
        for match in _KEY_ASSIGNMENT.finditer(line):
            name, value = match.group(1), match.group(2)
            if _PLACEHOLDER.match(value.strip("\"'")) or len(value) < 8:
                continue
            findings.append(f"line {line_no}: {name} appears to hold a literal value")
    return findings


# --------------------------------------------------------------------------- #
# 1. THE FAIL-PROOF: every probe, faked to raise, is reported as a problem
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("name", CHECK_ORDER)
def test_every_probe_that_raises_is_reported_as_a_problem(name: str) -> None:
    """A crashing probe must become a report line, not an exception.

    This is RED against a naive ``for probe in probes: results.append(probe())``:
    ``ProbeBoom`` escapes ``run()`` and the test errors instead of asserting.
    """

    probes = _ready_probes()
    probes[name] = _boom
    checker = SelfCheck(
        settings=_fake_settings(),
        probes=probes,
        http_get=lambda url, headers, timeout: (200, b"{}"),
        now=lambda: "2026-01-01T00:00:00+00:00",
    )

    report = checker.run()  # must not raise

    assert len(report.checks) == len(CHECK_ORDER)
    crashed = report.get(name)
    assert crashed is not None
    assert crashed.status == STATUS_PROBLEM, f"{name} should be reported as a problem"
    assert "probe crashed" in crashed.detail
    assert "ProbeBoom" in crashed.detail, "the report must name the exception type"
    assert "probe exploded on purpose" in crashed.detail
    assert crashed.fix, "a problem must still carry an actionable fix"
    assert crashed.data.get("exception") == "ProbeBoom"

    # The other checks are untouched, and the report is still serialisable.
    for other in CHECK_ORDER:
        if other != name:
            assert report.get(other).status == STATUS_READY
    json.loads(report.to_json())
    assert report.ok is False


def test_report_survives_every_probe_raising_at_once() -> None:
    """All seven probes crashing still produces a full report."""

    probes = {name: _boom for name in CHECK_ORDER}
    checker = SelfCheck(settings=_fake_settings(), probes=probes, now=lambda: "T")

    report = checker.run()

    assert len(report.checks) == len(CHECK_ORDER)
    assert all(check.status == STATUS_PROBLEM for check in report.checks)
    assert len(report.problems) == len(CHECK_ORDER)
    assert report.summary_line.endswith("NOT READY")
    assert report.ok is False
    # Readable form and JSON are both produced from a fully crashed run.
    rendered = report.render()
    assert rendered.count("[PROBLEM]") == len(CHECK_ORDER)
    json.loads(report.to_json())


def test_crashing_probe_does_not_stop_the_probes_after_it() -> None:
    """No short-circuit: a crash in an early check must not hide a later one."""

    order = ("stt_model", "vault", "key_store")
    probes = _ready_probes()
    probes["stt_model"] = _boom
    checker = SelfCheck(settings=_fake_settings(), probes=probes, now=lambda: "T")

    report = checker.run(order)

    statuses = {check.name: check.status for check in report.checks}
    assert statuses == {
        "stt_model": STATUS_PROBLEM,
        "vault": STATUS_READY,
        "key_store": STATUS_READY,
    }


def test_raising_settings_loader_becomes_a_problem_per_check() -> None:
    """A broken config file must not take the whole report down either."""

    def exploding_loader():
        raise ProbeBoom("config unreadable")

    checker = SelfCheck(settings_loader=exploding_loader, probes=_ready_probes(), now=lambda: "T")
    checker._probes["stt_model"] = service.probe_stt_model  # needs settings

    report = checker.run()

    stt = report.get("stt_model")
    assert stt.status == STATUS_PROBLEM
    assert "ProbeBoom" in stt.detail
    assert report.get("vault").status == STATUS_READY


def test_a_probe_that_returns_the_wrong_shape_is_a_problem() -> None:
    """The guard also normalises what a probe *returns*."""

    checker = SelfCheck(
        settings=_fake_settings(),
        probes={
            "stt_model": lambda _c: None,
            "vault": lambda _c: "all good",
            "key_store": lambda _c: CheckResult(
                name="wrong_name", status=STATUS_READY, detail="mislabelled"
            ),
        },
        now=lambda: "T",
    )

    report = checker.run(order=("stt_model", "vault", "key_store"))

    assert report.get("stt_model").status == STATUS_PROBLEM
    assert "no result" in report.get("stt_model").detail
    assert report.get("vault").status == STATUS_PROBLEM
    assert "str" in report.get("vault").detail
    assert report.get("key_store").status == STATUS_PROBLEM
    assert "wrong_name" in report.get("key_store").detail


def test_keyboard_interrupt_is_not_swallowed() -> None:
    """The guard catches everything except the owner saying stop."""

    def interrupt(_check: SelfCheck) -> CheckResult:
        raise KeyboardInterrupt

    checker = SelfCheck(settings=_fake_settings(), probes={"stt_model": interrupt}, now=lambda: "T")

    with pytest.raises(KeyboardInterrupt):
        checker.run(order=("stt_model",))


# --------------------------------------------------------------------------- #
# 2. A probe that succeeds is reported READY
# --------------------------------------------------------------------------- #


def test_probe_that_succeeds_is_reported_ready() -> None:
    checker = SelfCheck(
        settings=_fake_settings(),
        probes=_ready_probes(),
        now=lambda: "2026-01-01T00:00:00+00:00",
    )

    report = checker.run()

    assert len(report.ready) == len(CHECK_ORDER)
    assert report.ok is True
    assert report.missing == ()
    assert report.problems == ()
    assert report.summary_line == f"{len(CHECK_ORDER)} ready, 0 missing, 0 problem - READY"
    assert report.generated_at == "2026-01-01T00:00:00+00:00"
    for check in report.checks:
        assert check.status == STATUS_READY
        assert check.fix is None


def test_self_check_helper_returns_a_report() -> None:
    report = service.self_check(settings=_fake_settings(), probes=_ready_probes(), now=lambda: "T")

    assert isinstance(report, HealthReport)
    assert report.ok is True


def test_a_missing_check_makes_the_whole_report_not_ok() -> None:
    probes = _ready_probes()
    probes["tesseract"] = lambda _c: CheckResult(
        name="tesseract", status=STATUS_MISSING, detail="absent", fix="sudo apt-get install -y tesseract-ocr"
    )
    report = SelfCheck(settings=_fake_settings(), probes=probes, now=lambda: "T").run()

    assert report.ok is False
    assert len(report.missing) == 1


# --------------------------------------------------------------------------- #
# 3. A missing thing reports its fix command
# --------------------------------------------------------------------------- #


def test_stt_model_missing_reports_its_fix(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "config.json")

    unset = SelfCheck(settings=_fake_settings(stt_model_path=""), now=lambda: "T").run(
        order=("stt_model",)
    )
    assert unset.get("stt_model").status == STATUS_MISSING
    assert "stt_model_path" in unset.get("stt_model").fix
    assert "check_stt_model.py" in unset.get("stt_model").fix

    missing_file = tmp_path / "parakeet.nemo"
    gone = SelfCheck(settings=_fake_settings(stt_model_path=str(missing_file)), now=lambda: "T").run(
        order=("stt_model",)
    )
    assert gone.get("stt_model").status == STATUS_MISSING
    assert str(missing_file) in gone.get("stt_model").detail
    assert "parakeet" in gone.get("stt_model").fix.lower()


def test_stt_model_present_is_ready(tmp_path: Path) -> None:
    model = tmp_path / "parakeet.nemo"
    model.write_bytes(b"not really a model")

    report = SelfCheck(settings=_fake_settings(stt_model_path=str(model)), now=lambda: "T").run(
        order=("stt_model",)
    )

    check = report.get("stt_model")
    assert check.status == STATUS_READY
    assert check.fix is None
    assert check.data["kind"] == "file"


def test_vault_missing_reports_mkdir_fix(monkeypatch, tmp_path: Path) -> None:
    from claire.memory import notes

    absent = tmp_path / "vault"
    monkeypatch.setattr(notes, "resolve_vault_dir", lambda *a, **k: absent)

    report = SelfCheck(now=lambda: "T").run(order=("vault",))

    check = report.get("vault")
    assert check.status == STATUS_MISSING
    assert f"mkdir -p {absent}" in check.fix
    assert "chmod 700" in check.fix


def test_key_store_missing_reports_its_fix(monkeypatch, tmp_path: Path) -> None:
    """No keychain and no env file: MISSING, with the commands that fix it."""

    monkeypatch.setitem(sys.modules, "keyring", None)  # `import keyring` now raises
    monkeypatch.setattr(service, "default_env_file", lambda: tmp_path / "claire.env")

    report = SelfCheck(now=lambda: "T").run(order=("key_store",))

    check = report.get("key_store")
    assert check.status == STATUS_MISSING
    assert "keyring" in check.detail
    assert "pip install -r requirements.txt" in check.fix
    assert "install -m 600" in check.fix
    assert str(tmp_path / "claire.env") in check.fix


def test_key_store_names_only_the_variable_names(monkeypatch, tmp_path: Path) -> None:
    """The key file's variable *names* are reported; its values never are."""

    secret = "SUPERSECRETVALUE-should-never-be-printed"
    env_file = tmp_path / "claire.env"
    env_file.write_text(f"# keys\nOMNIROUTE_API_KEY={secret}\nexport FISH_API_KEY={secret}\n")
    env_file.chmod(0o600)
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setattr(service, "default_env_file", lambda: env_file)

    report = SelfCheck(now=lambda: "T").run(order=("key_store",))

    check = report.get("key_store")
    assert check.data["env_file_names"] == ["FISH_API_KEY", "OMNIROUTE_API_KEY"]
    assert secret not in check.detail
    assert secret not in json.dumps(check.to_dict())
    assert secret not in report.render()
    assert secret not in report.to_json()


def test_world_readable_key_file_is_a_problem(monkeypatch, tmp_path: Path) -> None:
    env_file = tmp_path / "claire.env"
    env_file.write_text("OMNIROUTE_API_KEY=whatever\n")
    env_file.chmod(0o644)
    monkeypatch.setitem(sys.modules, "keyring", None)
    monkeypatch.setattr(service, "default_env_file", lambda: env_file)

    check = SelfCheck(now=lambda: "T").run(order=("key_store",)).get("key_store")

    assert check.status == STATUS_PROBLEM
    assert "group/other bits set" in check.detail
    assert f"chmod 600 {env_file}" in check.fix


def test_audio_device_missing_module_reports_its_fix(monkeypatch) -> None:
    """No `sounddevice` at all: the fix installs the module and libportaudio."""

    monkeypatch.setitem(sys.modules, "sounddevice", None)

    check = SelfCheck(settings=_fake_settings(), now=lambda: "T").run(order=("audio_device",)).get(
        "audio_device"
    )

    assert check.status == STATUS_MISSING
    assert "sounddevice" in check.detail
    assert "libportaudio2" in check.fix


def test_audio_device_unavailable_reports_the_session_bus_fix(monkeypatch) -> None:
    """No device: the fix has to name the user-session trap."""

    def no_device(kind="input"):
        raise OSError("No input device available")

    fake = _module("sounddevice", query_devices=no_device)
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    check = SelfCheck(settings=_fake_settings(), now=lambda: "T").run(order=("audio_device",)).get(
        "audio_device"
    )

    assert check.status == STATUS_MISSING
    assert "No input device available" in check.detail
    assert "session bus" in check.detail
    assert "USER unit" in check.fix
    assert "pactl list short sources" in check.fix


def test_audio_device_present_is_ready(monkeypatch) -> None:
    fake = _module(
        "sounddevice",
        query_devices=lambda kind="input": {
            "name": "Test Microphone",
            "index": 3,
            "max_input_channels": 1,
        },
    )
    monkeypatch.setitem(sys.modules, "sounddevice", fake)

    check = SelfCheck(settings=_fake_settings(), now=lambda: "T").run(order=("audio_device",)).get(
        "audio_device"
    )

    assert check.status == STATUS_READY
    assert "Test Microphone" in check.detail
    assert check.data["index"] == 3


def test_tesseract_missing_uses_the_diagnosis_own_fix(monkeypatch) -> None:
    from claire.senses import screen

    diagnosis = SimpleNamespace(
        available=False,
        reason="The 'tesseract' binary is not installed",
        fix_command="sudo apt-get install -y tesseract-ocr",
        binary="tesseract",
        binary_path=None,
        python_module_present=True,
        package="tesseract-ocr",
    )
    monkeypatch.setattr(screen, "diagnose", lambda *a, **k: diagnosis)

    check = SelfCheck(now=lambda: "T").run(order=("tesseract",)).get("tesseract")

    assert check.status == STATUS_MISSING
    assert check.fix == "sudo apt-get install -y tesseract-ocr"
    assert "binary is not installed" in check.detail


def test_browser_missing_reports_playwright_install(monkeypatch) -> None:
    from claire.senses import browser

    monkeypatch.setattr(browser, "probe_chromium_binary", lambda: None)

    check = SelfCheck(now=lambda: "T").run(order=("browser",)).get("browser")

    assert check.status == STATUS_MISSING
    assert check.fix.startswith("playwright install chromium")


def test_browser_present_is_ready(monkeypatch, tmp_path: Path) -> None:
    from claire.senses import browser

    monkeypatch.setattr(browser, "probe_chromium_binary", lambda: str(tmp_path / "chrome"))

    check = SelfCheck(now=lambda: "T").run(order=("browser",)).get("browser")

    assert check.status == STATUS_READY
    assert check.data["binary_path"].endswith("chrome")


def test_llm_endpoint_unset_reports_its_fix(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "config.json")

    check = (
        SelfCheck(settings=_fake_settings(llm_base_url=""), now=lambda: "T")
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_MISSING
    assert "llm_base_url" in check.fix
    assert "omniroute" in check.fix.lower() or "20129" in check.fix


def test_llm_endpoint_unreachable_reports_its_fix() -> None:
    def refusing(url, headers, timeout):
        raise URLError("connection refused")

    check = (
        SelfCheck(settings=_fake_settings(), http_get=refusing, now=lambda: "T")
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_MISSING
    assert "connection refused" in check.detail
    assert "curl" in check.fix
    assert "127.0.0.1:20129" in check.fix


def test_llm_endpoint_reachable_is_ready_and_reports_no_key_value() -> None:
    key = "sk-live-THIS-MUST-NOT-APPEAR-9876543210"
    body = json.dumps({"data": [{"id": "a"}, {"id": "b"}]}).encode()

    check = (
        SelfCheck(
            settings=_fake_settings(llm_api_key=key),
            http_get=lambda url, headers, timeout: (200, body),
            now=lambda: "T",
        )
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_READY
    assert check.data["served_models"] == 2
    assert key not in check.detail
    assert key not in json.dumps(check.to_dict())
    assert "last4=3210" in check.data["api_key"]
    assert "sha256=" in check.data["api_key"]


def test_non_loopback_endpoint_without_a_key_is_missing(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setattr(service, "default_env_file", lambda: tmp_path / "claire.env")
    monkeypatch.setattr(service, "config_path", lambda: tmp_path / "config.json")

    check = (
        SelfCheck(settings=_fake_settings(llm_base_url="https://llm.example.com/v1"), now=lambda: "T")
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_MISSING
    assert "OMNIROUTE_API_KEY" in check.fix
    assert check.data["api_key"] == "absent"


def test_llm_endpoint_http_error_is_a_problem_not_a_missing_thing() -> None:
    check = (
        SelfCheck(
            settings=_fake_settings(),
            http_get=lambda url, headers, timeout: (404, b"nope"),
            now=lambda: "T",
        )
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_PROBLEM
    assert "404" in check.detail
    assert check.fix


def test_the_key_is_sent_in_a_header_and_never_in_the_url(monkeypatch) -> None:
    """The URL must stay key-free, or redaction would be the only defence."""

    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    key = "sk-live-header-only-key-1234567890"
    seen: dict = {}

    def record(url, headers, timeout):
        seen["url"] = url
        seen["headers"] = dict(headers)
        return 200, b"{}"

    check = (
        SelfCheck(settings=_fake_settings(llm_api_key=key), http_get=record, now=lambda: "T")
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_READY
    assert key not in seen["url"]
    assert seen["headers"]["Authorization"] == f"Bearer {key}"
    assert key not in json.dumps(check.to_dict())


# --------------------------------------------------------------------------- #
# 4. The report's contract: fixes, rendering, JSON
# --------------------------------------------------------------------------- #


def test_missing_without_a_fix_is_given_an_explicit_probe_defect_fix() -> None:
    checker = SelfCheck(
        settings=_fake_settings(),
        probes={
            "vault": lambda _c: CheckResult(
                name="vault", status=STATUS_MISSING, detail="gone", fix=None
            )
        },
        now=lambda: "T",
    )

    check = checker.run(order=("vault",)).get("vault")

    assert check.status == STATUS_MISSING
    assert check.fix
    assert "defect in the probe" in check.fix


def test_unknown_check_name_is_reported_not_raised() -> None:
    check = SelfCheck(settings=_fake_settings(), now=lambda: "T").run(order=("nonsense",)).get(
        "nonsense"
    )

    assert check.status == STATUS_PROBLEM
    assert "no probe is registered" in check.detail
    assert check.fix


def test_render_is_parseable_and_matches_the_checks() -> None:
    probes = _ready_probes()
    probes["browser"] = lambda _c: CheckResult(
        name="browser", status=STATUS_MISSING, detail="no chromium", fix="playwright install chromium"
    )
    report = SelfCheck(
        settings=_fake_settings(), probes=probes, now=lambda: "2026-01-01T00:00:00+00:00"
    ).run()

    text = service.render_report(report)
    lines = text.splitlines()
    assert lines[0] == "Claire self-check - 2026-01-01T00:00:00+00:00"
    assert lines[-1] == "Summary: 6 ready, 1 missing, 0 problem - NOT READY"

    parsed = {}
    fixes = {}
    current_title = None
    for line in lines[1:-1]:
        if line.startswith("    fix: "):
            assert current_title is not None, f"fix line before any check: {line!r}"
            fixes[current_title] = line[len("    fix: ") :]
            continue
        match = re.fullmatch(r"\[(?P<status>[A-Z]+)\] (?P<title>[^:]+): (?P<detail>.*)", line)
        assert match, f"unparseable check line: {line!r}"
        current_title = match.group("title")
        parsed[current_title] = match.group("status")
    assert parsed["Browser (chromium)"] == "MISSING"
    assert parsed["Memory vault"] == "READY"
    assert fixes["Browser (chromium)"] == "playwright install chromium"


def test_json_output_is_valid_json_and_matches_the_report() -> None:
    report = _all_ready_report()

    payload = json.loads(report.to_json())

    assert payload == report.to_dict()
    assert payload["ok"] is True
    assert payload["summary"] == {"ready": len(CHECK_ORDER), "missing": 0, "problem": 0, "total": len(CHECK_ORDER)}
    assert [check["name"] for check in payload["checks"]] == list(CHECK_ORDER)
    assert all(check["status"] == STATUS_READY for check in payload["checks"])


def test_secret_fingerprint_reveals_only_the_last_four_and_a_hash() -> None:
    fingerprint = service.secret_fingerprint("abcdefgh-ijkl-1234")

    assert "1234" in fingerprint
    assert "abcdefgh" not in fingerprint
    assert "present" in fingerprint
    assert re.search(r"sha256=[0-9a-f]{16}\b", fingerprint)
    assert service.secret_fingerprint("") == "absent"
    assert service.secret_fingerprint(None) == "absent"


def test_redact_url_strips_userinfo_and_query() -> None:
    assert (
        service.redact_url("https://user:tok3n@example.com:8443/v1/models?api_key=abc#frag")
        == "https://example.com:8443/v1/models"
    )
    assert service.redact_url("http://127.0.0.1:20129/v1") == "http://127.0.0.1:20129/v1"


# --------------------------------------------------------------------------- #
# 5. The command line
# --------------------------------------------------------------------------- #


def test_main_returns_zero_when_everything_is_ready(monkeypatch, capsys) -> None:
    monkeypatch.setattr(SelfCheck, "run", lambda self, order=None: _all_ready_report())

    assert service.main(["--self-check"]) == EXIT_READY
    out = capsys.readouterr().out
    assert out.startswith("Claire self-check")
    assert "READY" in out


def test_main_returns_one_and_prints_fixes_when_something_is_missing(monkeypatch, capsys) -> None:
    report = HealthReport(
        checks=(
            CheckResult(name="tesseract", status=STATUS_MISSING, detail="absent",
                        fix="sudo apt-get install -y tesseract-ocr"),
        ),
        generated_at="T",
    )
    monkeypatch.setattr(SelfCheck, "run", lambda self, order=None: report)

    assert service.main([]) == EXIT_NOT_READY
    out = capsys.readouterr().out
    assert "fix: sudo apt-get install -y tesseract-ocr" in out
    assert "NOT READY" in out


def test_main_json_flag_prints_valid_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(SelfCheck, "run", lambda self, order=None: _all_ready_report())

    assert service.main(["--self-check", "--json"]) == EXIT_READY

    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["summary"]["total"] == len(CHECK_ORDER)


def test_main_only_flag_narrows_the_checks(monkeypatch, capsys) -> None:
    seen: dict = {}

    def fake_run(self, order=None):
        seen["order"] = order
        return _all_ready_report()

    monkeypatch.setattr(SelfCheck, "run", fake_run)

    service.main(["--only", "vault", "--only", "key_store"])

    assert seen["order"] == ("vault", "key_store")


def test_bad_check_name_is_a_usage_error(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        service.main(["--only", "nonsense"])

    assert excinfo.value.code == service.EXIT_USAGE
    assert "nonsense" in capsys.readouterr().err


def test_parser_exposes_the_documented_flags() -> None:
    parser = service.build_parser()
    options = {action.dest for action in parser._actions}

    assert {"self_check", "json", "only", "timeout"} <= options


# --------------------------------------------------------------------------- #
# 6. deploy/claire.service parses as a systemd USER unit, with no inline secret
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def unit_text() -> str:
    assert UNIT_FILE.is_file(), f"missing deployment unit: {UNIT_FILE}"
    return UNIT_FILE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def unit(unit_text: str) -> UnitFile:
    return _parse_unit(unit_text)


def test_unit_file_has_the_required_sections(unit: UnitFile) -> None:
    assert set(unit.sections) == {"Unit", "Service", "Install"}
    assert unit.sections["Unit"], "[Unit] must not be empty"
    assert unit.sections["Service"], "[Service] must not be empty"
    assert unit.sections["Install"], "[Install] must not be empty"


def test_unit_starts_the_daemon_and_restarts_it_with_a_backoff(unit: UnitFile) -> None:
    exec_start = unit.one("Service", "ExecStart")
    assert exec_start.endswith("-m claire")
    assert "python" in exec_start

    assert unit.one("Service", "Type") == "simple"
    assert unit.one("Service", "Restart") == "on-failure"
    restart_sec = unit.one("Service", "RestartSec")
    assert re.fullmatch(r"\d+s?", restart_sec), restart_sec
    assert float(restart_sec.rstrip("s")) >= 1

    # Rate-limited rather than an infinite hot loop.
    assert int(unit.one("Unit", "StartLimitBurst")) >= 1
    assert int(unit.one("Unit", "StartLimitIntervalSec")) >= 60


def test_unit_uses_an_environment_file_not_inline_secrets(unit: UnitFile) -> None:
    env_file = unit.one("Service", "EnvironmentFile")
    assert env_file.rstrip().endswith(".env")
    assert ".config/claire/" in env_file
    assert "OMNIROUTE_API_KEY" not in env_file

    # Every Environment= line must be a non-secret (path, flag or plain value).
    for value in unit.values("Service", "Environment"):
        assert not re.search(r"(?i)(API_?KEY|TOKEN|SECRET|PASSWORD)", value), value


def test_unit_is_a_user_unit_that_can_reach_the_microphone(unit: UnitFile) -> None:
    assert unit.one("Install", "WantedBy") == "default.target"
    # A user unit must not ask systemd to switch user; that is a system-unit idea.
    assert unit.values("Service", "User") == []
    assert unit.values("Service", "Group") == []

    # The classic failure must be written down in the file itself.
    comments = unit.comments.lower()
    assert "user unit" in comments
    assert "microphone" in comments
    assert "system unit" in comments
    assert "cannot" in comments
    assert "session" in comments


def test_unit_logs_to_the_journal(unit: UnitFile) -> None:
    assert unit.one("Service", "StandardOutput") == "journal"
    assert unit.one("Service", "StandardError") == "journal"
    assert unit.one("Service", "SyslogIdentifier") == "claire"


def test_unit_file_contains_no_secret_value(unit_text: str) -> None:
    findings = _secret_value_findings(unit_text)
    assert findings == [], "the world-readable unit file must not carry a secret: " + "; ".join(
        findings
    )
    assert "EnvironmentFile=" in unit_text


# --------------------------------------------------------------------------- #
# 7. The Dockerfile: slim base, non-root, no secret, honest about audio/display
# --------------------------------------------------------------------------- #


@pytest.fixture(scope="module")
def docker_text() -> str:
    assert DOCKERFILE.is_file(), f"missing deployment file: {DOCKERFILE}"
    return DOCKERFILE.read_text(encoding="utf-8")


def _docker_instructions(text: str) -> list[tuple[str, str]]:
    """(INSTRUCTION, rest) pairs, line continuations joined, comments dropped."""

    joined = re.sub(r"\\\s*\n\s*", " ", text)
    instructions: list[tuple[str, str]] = []
    for raw in joined.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        head, _, rest = line.partition(" ")
        instructions.append((head.upper(), rest.strip()))
    return instructions


def test_dockerfile_uses_a_slim_python_base(docker_text: str) -> None:
    froms = [rest for head, rest in _docker_instructions(docker_text) if head == "FROM"]

    assert froms, "the Dockerfile must have a FROM line"
    assert froms[0].startswith("python:3.12-slim"), froms[0]


def test_dockerfile_runs_as_a_non_root_user(docker_text: str) -> None:
    instructions = _docker_instructions(docker_text)
    users = [rest for head, rest in instructions if head == "USER"]

    assert users, "the image must drop privileges with USER"
    final_user = users[-1].split()[0]
    assert final_user.lower() not in {"root", "0", "0:0"}
    assert any("useradd" in rest for head, rest in instructions if head == "RUN")


def test_dockerfile_installs_the_system_deps_it_needs(docker_text: str) -> None:
    assert "tesseract-ocr" in docker_text
    assert "apt-get install" in docker_text


def test_dockerfile_has_no_secret_and_no_secret_file_copy(docker_text: str) -> None:
    findings = _secret_value_findings(docker_text)
    assert findings == [], "no key may be baked into the image: " + "; ".join(findings)

    for head, rest in _docker_instructions(docker_text):
        if head in {"COPY", "ADD"}:
            sources = rest.split()[:-1] or rest.split()
            for source in sources:
                assert not source.endswith((".env", ".pem", ".key", "config.json")), source
                assert "secret" not in source.lower(), source
    assert ".dockerignore" in docker_text


def test_dockerfile_documents_that_audio_and_display_do_not_work(docker_text: str) -> None:
    lowered = docker_text.lower()
    assert "no audio" in lowered
    assert "no display" in lowered
    # And it must say where voice actually runs.
    assert "claire.service" in lowered


def test_dockerfile_healthcheck_asks_the_self_check(docker_text: str) -> None:
    healthchecks = [rest for head, rest in _docker_instructions(docker_text) if head == "HEALTHCHECK"]

    assert healthchecks, "the image should report its own health"
    assert any("claire.platform.service" in rest and "--self-check" in rest for rest in healthchecks)


# --------------------------------------------------------------------------- #
# 3b. A 401 is a credential problem, and must be diagnosed as one
# --------------------------------------------------------------------------- #

class TestUnauthorisedIsDiagnosedAsACredentialProblem:
    """Regression: a 401 used to be reported as a URL problem.

    Measured on this machine: OmniRouter answers 401 on loopback when no key is
    set. The generic branch told the owner that the endpoint was "reachable but
    not serving an OpenAI-compatible model list" and to check ``llm_base_url`` and
    the server log - sending them after the wrong fault entirely. A refused
    credential and a wrong URL need different fixes and must not share a message.
    """

    def _check(self, status_code, **settings):
        return (
            SelfCheck(
                settings=_fake_settings(**settings),
                http_get=lambda url, headers, timeout: (status_code, b""),
                # A 5xx sends the probe on to a real turn; answer that here so
                # these tests stay hermetic (and never touch the network).
                http_post=lambda url, headers, payload, timeout: (500, b""),
                now=lambda: "T",
            )
            .run(order=("llm_endpoint",))
            .get("llm_endpoint")
        )

    def test_401_with_no_key_configured_names_the_missing_credential(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
        monkeypatch.setattr(service, "default_env_file", lambda: tmp_path / "claire.env")
        monkeypatch.setattr(service, "config_path", lambda: tmp_path / "config.json")

        check = self._check(401)

        assert check.status == STATUS_MISSING
        assert "credential" in check.detail.lower()
        assert "OMNIROUTE_API_KEY" in check.fix
        # It must NOT send the owner hunting for a URL fault.
        assert "llm_base_url" not in check.fix

    def test_401_with_a_key_configured_says_the_key_was_rejected(self, monkeypatch):
        monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
        key = "sk-live-rejected-key-abcdef123456"

        check = self._check(401, llm_api_key=key)

        assert check.status == STATUS_MISSING
        assert "reject" in check.detail.lower()
        assert key not in json.dumps(check.to_dict())
        assert check.fix

    def test_403_gets_the_same_credential_treatment(self, monkeypatch, tmp_path):
        monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
        monkeypatch.setattr(service, "default_env_file", lambda: tmp_path / "claire.env")
        monkeypatch.setattr(service, "config_path", lambda: tmp_path / "config.json")

        check = self._check(403)

        assert check.status == STATUS_MISSING
        assert "credential" in check.detail.lower()

    def test_other_http_errors_stay_a_problem_not_a_missing_credential(self):
        """A 500 is the server's fault, not a missing key - keep them distinct."""

        check = self._check(500)

        assert check.status == STATUS_PROBLEM
        assert "500" in check.detail
        assert "credential" not in check.detail.lower()


# --------------------------------------------------------------------------- #
# 7. The other two units: the panel is a service, the tray app is a session
# --------------------------------------------------------------------------- #
#
# Splitting the panel and the tray out of claire.service is what makes "Claire
# starts on startup" true for all three parts: a panel bug no longer takes the
# voice assistant down, and the tray app - the one GUI - is started by the
# session instead of being enabled at boot where it would fail for want of a
# display and then be left permanently failed by StartLimitBurst.

PANEL_UNIT_FILE = DEPLOY_DIR / "claire-panel.service"
DESKTOP_UNIT_FILE = DEPLOY_DIR / "claire-desktop.service"
DESKTOP_AUTOSTART_FILE = DEPLOY_DIR / "claire-desktop.desktop"


@pytest.fixture(scope="module")
def panel_unit() -> UnitFile:
    assert PANEL_UNIT_FILE.is_file(), f"missing deployment unit: {PANEL_UNIT_FILE}"
    return _parse_unit(PANEL_UNIT_FILE.read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def desktop_unit() -> UnitFile:
    assert DESKTOP_UNIT_FILE.is_file(), f"missing deployment unit: {DESKTOP_UNIT_FILE}"
    return _parse_unit(DESKTOP_UNIT_FILE.read_text(encoding="utf-8"))


def test_panel_and_desktop_units_have_the_required_sections(panel_unit: UnitFile, desktop_unit: UnitFile) -> None:
    for unit in (panel_unit, desktop_unit):
        assert set(unit.sections) == {"Unit", "Service", "Install"}
        assert unit.sections["Unit"], "[Unit] must not be empty"
        assert unit.sections["Service"], "[Service] must not be empty"
        assert unit.sections["Install"], "[Install] must not be empty"


def test_panel_unit_starts_the_panel_and_restarts_it(panel_unit: UnitFile) -> None:
    assert panel_unit.one("Service", "ExecStart").endswith("-m claire.panel.server")
    assert panel_unit.one("Service", "Type") == "simple"
    assert panel_unit.one("Service", "Restart") == "on-failure"
    assert int(panel_unit.one("Unit", "StartLimitBurst")) >= 1
    assert int(panel_unit.one("Unit", "StartLimitIntervalSec")) >= 60


def test_desktop_unit_starts_the_tray_app_and_restarts_it(desktop_unit: UnitFile) -> None:
    assert desktop_unit.one("Service", "ExecStart").endswith("-m desktop_app")
    assert desktop_unit.one("Service", "Type") == "simple"
    assert desktop_unit.one("Service", "Restart") == "on-failure"
    assert int(desktop_unit.one("Unit", "StartLimitBurst")) >= 1


def test_only_the_panel_is_enabled_at_default_target(panel_unit: UnitFile, desktop_unit: UnitFile) -> None:
    """The panel is a headless HTTP server, so being up early is free.

    The tray app needs a display, so enabling it at default.target is the trap
    this test exists to prevent: with linger that target is reached at boot,
    before any session, and the failures spend the start-limit burst.
    """
    assert panel_unit.one("Install", "WantedBy") == "default.target"
    assert desktop_unit.one("Install", "WantedBy") == "graphical-session.target"
    assert desktop_unit.one("Unit", "PartOf") == "graphical-session.target"


def test_new_units_use_an_environment_file_not_inline_secrets(panel_unit: UnitFile, desktop_unit: UnitFile) -> None:
    for unit in (panel_unit, desktop_unit):
        env_file = unit.one("Service", "EnvironmentFile")
        assert env_file.rstrip().endswith(".env")
        assert ".config/claire/" in env_file
        for value in unit.values("Service", "Environment"):
            assert not re.search(r"(?i)(API_?KEY|TOKEN|SECRET|PASSWORD)", value), value


def test_new_units_carry_no_bind_port_of_their_own(panel_unit: UnitFile) -> None:
    """The port belongs to the panel's source and its environment, not to a unit
    that would silently disagree with it after a change."""
    assert "8765" not in PANEL_UNIT_FILE.read_text(encoding="utf-8")
    assert "--port" not in panel_unit.one("Service", "ExecStart")


def test_autostart_entry_asks_systemd_and_is_portable() -> None:
    text = DESKTOP_AUTOSTART_FILE.read_text(encoding="utf-8")
    directives = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, _, value = line.partition("=")
        directives[key.strip()] = value.strip()

    assert directives["Type"] == "Application"
    assert directives["Exec"] == "systemctl --user start claire-desktop.service"
    assert directives["X-GNOME-Autostart-enabled"] == "true"
    # No checkout path reaches the Exec line: the unit owns the interpreter.
    assert "@CLAIRE_REPO_ROOT@" not in directives["Exec"]


# ---------------------------------------------------------------------------
# A 5xx from the model list is settled by a real turn
# ---------------------------------------------------------------------------

def _five_hundred_models(url, headers, timeout):
    """What OmniRouter answers while it builds a 9,508-entry catalogue."""
    return 503, json.dumps({"error": {"message": "catalog_build_timeout"}}).encode()


def test_a_503_catalogue_with_a_working_turn_is_ready() -> None:
    """Measured on this machine: /models answers 503 catalog_build_timeout
    while /chat/completions answers 200 in ~1 s. Calling that a PROBLEM sent
    the owner to check a URL that was perfectly correct."""
    turn_calls = []

    def chat(url, headers, payload, timeout):
        turn_calls.append((url, payload))
        return 200, json.dumps({"choices": [{"message": {"content": "ok"}}]}).encode()

    check = (
        SelfCheck(
            settings=_fake_settings(),
            http_get=_five_hundred_models,
            http_post=chat,
            now=lambda: "T",
        )
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_READY, check.detail
    assert check.data["models_list_unavailable"] is True
    assert "chat turn" in check.detail
    assert turn_calls and turn_calls[0][0].endswith("/chat/completions")
    assert turn_calls[0][1]["max_tokens"] == 1, "the probe must stay minimal"


def test_a_503_catalogue_and_a_failing_turn_is_a_problem() -> None:
    def chat(url, headers, payload, timeout):
        return 400, b'{"error": {"message": "model not found"}}'

    check = (
        SelfCheck(
            settings=_fake_settings(),
            http_get=_five_hundred_models,
            http_post=chat,
            now=lambda: "T",
        )
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_PROBLEM
    assert "400" in check.detail and "chat turn" in check.detail


def test_a_transport_failure_on_the_turn_is_a_problem_not_a_crash() -> None:
    def chat(url, headers, payload, timeout):
        raise URLError("connection refused")

    check = (
        SelfCheck(
            settings=_fake_settings(),
            http_get=_five_hundred_models,
            http_post=chat,
            now=lambda: "T",
        )
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_PROBLEM
    assert "chat turn" in check.detail


def test_a_503_is_not_settled_by_a_turn_when_no_model_is_configured() -> None:
    """Without a model there is nothing to prove readiness with, so the
    endpoint's own explanation stands rather than being replaced by a guess."""
    def chat(url, headers, payload, timeout):  # pragma: no cover - must not run
        raise AssertionError("no turn can be sent without a model")

    check = (
        SelfCheck(
            settings=_fake_settings(llm_chat_model=""),
            http_get=_five_hundred_models,
            http_post=chat,
            now=lambda: "T",
        )
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_PROBLEM
    assert "503" in check.detail


def test_a_404_model_list_is_still_a_problem_without_a_turn_probe() -> None:
    """Only 5xx means "the list is temporarily unavailable"; a 404 is the
    endpoint saying it has no such route, and must not be papered over."""
    def chat(url, headers, payload, timeout):  # pragma: no cover - must not run
        raise AssertionError("a 404 must not trigger a turn probe")

    check = (
        SelfCheck(
            settings=_fake_settings(),
            http_get=lambda url, headers, timeout: (404, b""),
            http_post=chat,
            now=lambda: "T",
        )
        .run(order=("llm_endpoint",))
        .get("llm_endpoint")
    )

    assert check.status == STATUS_PROBLEM
    assert "404" in check.detail
