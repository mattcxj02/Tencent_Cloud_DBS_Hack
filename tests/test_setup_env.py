"""``scripts/setup_env.py``: create .env, generate service secrets, ask for credentials.

Runs only against temporary files; never touches the real .env.
"""

from __future__ import annotations

import stat
from pathlib import Path

import pytest
from scripts import setup_env

EXAMPLE = Path(__file__).resolve().parent.parent / ".env.example"


def _values(path: Path) -> dict[str, str]:
    out = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if "=" in line and not line.startswith("#"):
            key, value = line.split("=", 1)
            out[key] = value
    return out


def test_creates_env_with_generated_secrets(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    assert setup_env.setup(env, EXAMPLE, prompt=False) == 0
    values = _values(env)
    for key in setup_env.GENERATED:
        assert len(values[key]) >= setup_env.MIN_SECRET_LEN
    assert len({values[key] for key in setup_env.GENERATED}) == len(setup_env.GENERATED)
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    # Everything that is not a generated secret is copied unchanged.
    example = _values(EXAMPLE)
    assert {k: v for k, v in values.items() if k not in setup_env.GENERATED} == {
        k: v for k, v in example.items() if k not in setup_env.GENERATED
    }


def test_rerun_keeps_existing_values(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    setup_env.setup(env, EXAMPLE, prompt=False)
    first = env.read_text(encoding="utf-8")
    setup_env.setup(env, EXAMPLE, prompt=False)
    assert env.read_text(encoding="utf-8") == first


def test_short_secret_is_replaced(tmp_path: Path) -> None:
    env = tmp_path / ".env"
    env.write_text(
        "OPERATOR_TOKEN=short\nVALIDATOR_SERVICE_TOKEN=" + "x" * 40 + "\n", encoding="utf-8"
    )
    setup_env.setup(env, EXAMPLE, prompt=False)
    values = _values(env)
    assert values["OPERATOR_TOKEN"] != "short" and len(values["OPERATOR_TOKEN"]) >= 32
    assert values["VALIDATOR_SERVICE_TOKEN"] == "x" * 40


def test_prompt_fills_placeholders_and_skips_blanks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    answers = iter(["tk-test", "", "4242", "trtc-secret", "", ""])
    monkeypatch.setattr(setup_env.getpass, "getpass", lambda _prompt: next(answers))
    monkeypatch.setattr("builtins.input", lambda _prompt: next(answers))
    env = tmp_path / ".env"
    setup_env.setup(env, EXAMPLE, prompt=True)
    values = _values(env)
    assert values["tokenhub_key"] == "tk-test"
    assert values["SDKAppID"] == "4242"
    assert values["SDKSecretKey"] == "trtc-secret"
    assert "placeholder" in values["deepseek_key"]
    output = capsys.readouterr().out
    assert "tk-test" not in output and "trtc-secret" not in output
    assert "deepseek_key" in output.split("Still to fill in")[1]
