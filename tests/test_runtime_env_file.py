"""Claire must read the same env file she tells the owner to write.

Regression: ``load_settings()`` called a bare ``load_dotenv()``, and python-dotenv
with no argument looks for ``./.env`` in the *current working directory*. Nothing
ever opened ``~/.config/claire/claire.env`` - the path that ``default_env_file()``
returns and that the self-check, the acceptance scoreboard and every "set
OMNIROUTE_API_KEY in <file>" message name.

So the diagnostics and the loader disagreed: the owner was pointed at a file the
program never read, and the key only ever arrived because systemd injected it via
``EnvironmentFile=`` or a harness sourced the file itself. Run plainly, Claire
insisted no key was configured while looking straight at one.

These tests pin both halves: that the file is read, and that the path read is the
path advertised.
"""

from __future__ import annotations

import os

import pytest

from claire import config as config_mod
from claire.platform import service as service_mod

pytestmark = pytest.mark.unit

FAKE_KEY = "sk-test-not-a-real-key-0000"


@pytest.fixture()
def clean_environ():
    """Restore ``os.environ`` after the test.

    ``load_dotenv`` writes into ``os.environ`` directly rather than through
    monkeypatch, so a test that let it leak would silently configure every later
    test in the session.
    """
    before = dict(os.environ)
    yield
    os.environ.clear()
    os.environ.update(before)


def _write_env(tmp_path, text: str):
    path = tmp_path / "claire" / "claire.env"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def test_the_runtime_env_file_actually_gets_read(monkeypatch, tmp_path, clean_environ):
    """The core regression: a key in the advisory file must reach the settings."""

    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    path = _write_env(tmp_path, f"OMNIROUTE_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)  # no ./.env here at all

    config_mod.load_runtime_env()

    assert os.environ.get("OMNIROUTE_API_KEY") == FAKE_KEY
    assert path.is_file()


def test_settings_pick_the_key_up_from_that_file(monkeypatch, tmp_path, clean_environ):
    """End to end through the real accessor the daemon uses."""

    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    _write_env(tmp_path, f"OMNIROUTE_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.chdir(tmp_path)

    settings = config_mod.load_settings()

    assert settings.llm_api_key == FAKE_KEY, (
        "the key is in the file Claire tells the owner to use, but load_settings "
        "did not pick it up"
    )


def test_a_real_environment_variable_still_outranks_the_file(monkeypatch, tmp_path, clean_environ):
    """A one-off ``OMNIROUTE_API_KEY=... python -m claire`` must win."""

    _write_env(tmp_path, f"OMNIROUTE_API_KEY={FAKE_KEY}\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setenv("OMNIROUTE_API_KEY", "sk-from-the-live-environment")

    config_mod.load_runtime_env()

    assert os.environ["OMNIROUTE_API_KEY"] == "sk-from-the-live-environment"


def test_a_missing_file_is_not_an_error(monkeypatch, tmp_path, clean_environ):
    """A fresh install has no env file yet, and must still start."""

    monkeypatch.delenv("OMNIROUTE_API_KEY", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "nothing-here"))
    monkeypatch.chdir(tmp_path)

    config_mod.load_runtime_env()  # must not raise

    assert not os.environ.get("OMNIROUTE_API_KEY")


def test_the_path_read_is_the_path_advertised(monkeypatch, tmp_path):
    """The advice and the behaviour must name the same file.

    This is the assertion that would have caught the original bug outright: the
    loader and the diagnostics each had their own idea of where the file lives.
    """

    monkeypatch.delenv("CLAIRE_ENV_FILE", raising=False)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))

    assert config_mod.runtime_env_file() == service_mod.default_env_file()


def test_claire_env_file_overrides_the_location(monkeypatch, tmp_path):
    """An operator can point the loader at a different file, and both agree."""

    explicit = tmp_path / "elsewhere" / "secrets.env"
    monkeypatch.setenv("CLAIRE_ENV_FILE", str(explicit))

    assert config_mod.runtime_env_file() == explicit
    assert service_mod.default_env_file() == explicit
