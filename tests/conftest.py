import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

# Robustly locate repository root (directory containing src/claire)
_this_file = Path(__file__).resolve()
ROOT = None
for parent in _this_file.parents:
    if (parent / "src" / "claire").exists():
        ROOT = parent
        break
if ROOT is None:
    # Fallback to two levels up
    ROOT = _this_file.parent.parent

SRC = ROOT / "src"
# Both ROOT and SRC are on sys.path so tests can write either
#   ``from src.claire.x import ...``  (older style, ``src.`` prefix)
# or
#   ``from claire.x import ...``      (newer style, no prefix)
# CAUTION: those two import paths resolve to *distinct module instances*.
# A monkeypatch on ``src.claire.memory.conversation.X`` does NOT take
# effect on ``claire.memory.conversation.X`` and vice versa. When a test
# stubs out a symbol the production code calls, you MUST patch the same
# module instance the production code resolves at runtime. Production code
# in ``src/`` imports without the ``src.`` prefix (e.g. inside endpoint
# handlers it's ``from claire.memory.conversation import ...``), so a test
# that monkeypatches a symbol used by production should also import
# without the prefix. This is the convention going forward; the older
# ``from src.X`` style is left in place to avoid a churn-only sweep, but
# do not adopt it for new tests that monkeypatch.
# Add repository root so that 'src' is a package prefix.
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
# Also add the src directory (optional, for backwards compatibility with direct 'claire' imports)
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@dataclass
class MockConfig:
    """Minimal config object for unit tests that need a config."""
    # Provider-aware fields. Default to Ollama at localhost so tests
    # that don't care about providers keep the historical behaviour.
    llm_provider: str = "ollama"
    llm_base_url: str = "http://localhost:11434"
    llm_api_key: str = ""
    # ``llm_chat_model`` defaults to empty so tests that pin
    # ``ollama_chat_model = "gpt-oss:20b"`` to exercise the LARGE-model
    # branch get the legacy alias promoted into ``llm_chat_model`` by
    # ``__post_init__`` — same shape ``load_settings()`` produces.
    llm_chat_model: str = ""
    whisper_model: str = "small"
    embedding_provider: str = ""
    embedding_base_url: str = ""
    embedding_api_key: str = ""
    embedding_model: str = "nomic-embed-text"
    ollama_base_url: str = "http://localhost:11434"
    ollama_chat_model: str = "gemma4:e2b"
    ollama_embed_model: str = "nomic-embed-text"
    db_path: str = ":memory:"
    sqlite_vss_path: Optional[str] = None
    voice_debug: bool = True
    tts_enabled: bool = False
    tts_engine: str = "piper"
    tts_voice: Optional[str] = None
    tts_rate: int = 200
    tts_piper_model_path: Optional[str] = None
    tts_piper_speaker: Optional[int] = None
    tts_piper_length_scale: float = 1.0
    tts_piper_noise_scale: float = 0.667
    tts_piper_noise_w: float = 0.8
    tts_piper_sentence_silence: float = 0.2
    tts_chatterbox_device: str = "cpu"
    tts_chatterbox_audio_prompt: Optional[str] = None
    tts_chatterbox_exaggeration: float = 0.5
    tts_chatterbox_cfg_weight: float = 0.5
    web_search_enabled: bool = True
    brave_search_api_key: str = ""
    wikipedia_fallback_enabled: bool = True
    llm_tools_timeout_sec: float = 8.0
    llm_embedding_timeout_sec: float = 10.0
    llm_chat_timeout_sec: float = 45.0
    agentic_max_turns: int = 8
    tool_selection_strategy: str = "embedding"
    fast_model: str = ""
    memory_enrichment_max_results: int = 5
    memory_enrichment_source: str = "diary"
    location_enabled: bool = True
    location_ip_address: Optional[str] = None
    location_auto_detect: bool = False
    location_cgnat_resolve_public_ip: bool = False
    location_cache_minutes: int = 60
    dialogue_memory_timeout: int = 300
    llm_thinking_enabled: bool = False
    intent_judge_thinking_enabled: bool = False
    mcps: Dict[str, Any] = field(default_factory=dict)
    use_stdin: bool = True

    def __post_init__(self) -> None:
        # Mirror ``load_settings``: when the provider-aware fields are
        # left empty, promote the legacy ``ollama_*`` aliases. Tests can
        # set either pair and end up with consistent reads on either side.
        if not self.llm_chat_model:
            self.llm_chat_model = self.ollama_chat_model
        if not self.llm_base_url:
            self.llm_base_url = self.ollama_base_url
        if not self.embedding_model:
            self.embedding_model = self.ollama_embed_model


@pytest.fixture(autouse=True)
def _isolate_user_config_path(tmp_path_factory, monkeypatch):
    """Redirect ``default_config_path`` to a per-session tempfile so a test
    that calls ``load_settings`` (or any other code path that resolves the
    user's config) cannot read or overwrite ``~/.config/claire/config.json``.

    Tests that need to exercise the loader against specific JSON should
    monkey-patch ``_load_json`` (and ``_save_json`` if the migration would
    trigger a write) directly. This fixture is a belt-and-braces guard so
    a half-mocked test cannot reach the real config file.
    """
    sandbox = tmp_path_factory.mktemp("claire_config_sandbox")
    monkeypatch.setattr(
        "claire.config.default_config_path", lambda: sandbox / "config.json"
    )
    monkeypatch.setenv("CLAIRE_CONFIG_PATH", str(sandbox / "config.json"))


@pytest.fixture(autouse=True)
def _isolate_the_memory_vault(tmp_path_factory, monkeypatch):
    """Never let a test write into the owner's real data directory.

    The vault backend auto-creates ``CLAIRE.md`` from the built-in template when
    the file is missing, which is the right behaviour for a fresh install.  It is
    the wrong behaviour here: the suite selects the vault backend explicitly for
    the legacy tests, so without this every run would recreate
    ``~/.local/share/claire/vault`` -- the directory the owner deliberately
    retired and archived when memory moved to Postgres.

    Tests that pass an explicit ``vault_dir=`` are unaffected (an explicit
    argument outranks the environment), and a test that monkeypatches
    ``CLAIRE_VAULT_DIR`` itself still wins, because its patch is applied later.
    """
    from claire.memory import notes as _notes

    monkeypatch.setenv(
        _notes.VAULT_ENV_VAR, str(tmp_path_factory.mktemp("claire-vault"))
    )


@pytest.fixture(autouse=True)
def _legacy_vault_backend_for_the_existing_suite(monkeypatch):
    """Pin the Markdown vault as the memory backend for the pre-existing suite.

    Long-term memory moved to Postgres and ``postgres`` is the product default,
    because a fresh install has no vault to read and a missing database should
    surface as a named ``[memory-unavailable]`` marker rather than silent
    amnesia.  Most of this suite, however, was written against the vault: those
    tests build a temporary vault and assert on the notes in it.  They exercise
    the legacy compatibility path, which still ships so that a rollback is a flag
    flip, so they must *select* it explicitly instead of relying on it being the
    default.

    Tests that exercise Postgres opt in with
    ``monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "postgres")``; the environment
    check in :func:`claire.system_prompt._memory_backend` is what makes that
    override win over this fixture.  The module-level cache is cleared too, so a
    test cannot inherit a backend chosen by an earlier test.
    """
    monkeypatch.setenv("CLAIRE_MEMORY_BACKEND", "vault")
    import claire.system_prompt as _system_prompt

    monkeypatch.setattr(_system_prompt, "_memory_backend_cached", None)


@pytest.fixture(autouse=True)
def _isolate_control_socket_path(tmp_path_factory, monkeypatch):
    """Keep test instances off the real daemon's control socket.

    ``claire.daemon.main()`` serves a Unix socket for attached UIs, whose
    default path is ``$XDG_RUNTIME_DIR/claire/control.sock`` — the same file the
    Claire the owner is actually running uses.  A test that boots the daemon far
    enough to reach that call must not create, replace or contend for it, so the
    path is redirected to a per-session tempfile here.  Tests that exercise the
    socket set their own path, which simply overrides this one.
    """
    sandbox = tmp_path_factory.mktemp("claire_control_socket_sandbox")
    monkeypatch.setenv("CLAIRE_CONTROL_SOCKET", str(sandbox / "control.sock"))


@pytest.fixture
def mock_config():
    """Provide a mock configuration for unit tests."""
    return MockConfig()


@pytest.fixture
def db():
    """Provide an in-memory database for unit tests."""
    from claire.memory.db import Database
    database = Database(":memory:", sqlite_vss_path=None)
    yield database
    database.close()


@pytest.fixture
def dialogue_memory():
    """Provide a dialogue memory instance for unit tests."""
    from claire.memory.conversation import DialogueMemory
    return DialogueMemory(inactivity_timeout=300, max_interactions=20)


@pytest.fixture(scope="session")
def _qapplication():
    """Keep one QApplication alive for the entire test process.

    Qt requires exactly one QApplication per process.  Re-uses an existing
    instance when present so repeated test runs inside a single session
    don't error.  The offscreen platform is set as a default so headless CI
    (no DISPLAY, no xvfb) can construct a real QApplication instead of
    aborting; machines with a real display are unaffected because the
    platform is only set when it is not already configured.
    """
    import os

    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PyQt6.QtWidgets import QApplication
    app = QApplication.instance()
    if app is None:
        app = QApplication([])
    app.setQuitOnLastWindowClosed(False)
    yield app


@pytest.fixture
def qapp(_qapplication):
    """Dispose each test's widgets while the shared application is still alive."""
    from PyQt6 import sip
    from PyQt6.QtCore import QCoreApplication, QEvent

    existing = set(_qapplication.topLevelWidgets())
    yield _qapplication
    for widget in _qapplication.topLevelWidgets():
        if widget not in existing and not sip.isdeleted(widget):
            widget.deleteLater()
    # Flush deferred destruction, not unrelated timers or application callbacks.
    QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
