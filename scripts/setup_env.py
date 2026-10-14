"""Create or update .env for local development.

- Copies .env.example to .env if .env does not exist yet.
- Generates the service secrets (VALIDATOR_SERVICE_TOKEN, OPERATOR_TOKEN,
  STEP_UP_SECRET, GATEWAY_DELEGATION_SECRET) when they are empty or shorter than
  32 characters. Existing long-enough values are kept, so it is safe to rerun.
- In a terminal, asks for each provider credential that is still a placeholder
  (input hidden; press Enter to skip and fill it in later).

Values are never printed. Standard library only, so it runs before `uv sync`.

    python3 scripts/setup_env.py              # interactive
    python3 scripts/setup_env.py --no-prompt  # secrets only, no questions
"""

from __future__ import annotations

import argparse
import getpass
import os
import re
import secrets
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MIN_SECRET_LEN = 32

GENERATED = (
    "VALIDATOR_SERVICE_TOKEN",
    "OPERATOR_TOKEN",
    "STEP_UP_SECRET",
    "GATEWAY_DELEGATION_SECRET",
)

# (variable, prompt, hidden input)
CREDENTIALS = (
    ("tokenhub_key", "TokenHub API key (extractor, hy3)", True),
    ("deepseek_key", "DeepSeek API key (validator)", True),
    ("SDKAppID", "TRTC SDKAppID (a number, not the cloud SecretId)", False),
    ("SDKSecretKey", "TRTC SDKSecretKey", True),
    ("LANGFUSE_PUBLIC_KEY", "Langfuse public key", False),
    ("LANGFUSE_SECRET_KEY", "Langfuse secret key", True),
)


def read_value(text: str, key: str) -> str | None:
    match = re.search(rf"^{re.escape(key)}=(.*)$", text, flags=re.MULTILINE)
    return match.group(1).strip() if match else None


def set_value(text: str, key: str, value: str) -> str:
    line = f"{key}={value}"
    pattern = rf"^{re.escape(key)}=.*$"
    if re.search(pattern, text, flags=re.MULTILINE):
        return re.sub(pattern, lambda _: line, text, count=1, flags=re.MULTILINE)
    return text.rstrip("\n") + f"\n{line}\n"


def is_placeholder(key: str, value: str | None) -> bool:
    if not value:
        return True
    return "placeholder" in value or (key == "SDKAppID" and value == "0")


def setup(env_path: Path, example_path: Path, prompt: bool) -> int:
    if env_path.exists():
        text = env_path.read_text(encoding="utf-8")
        print(f"Updating existing {env_path.name}")
    else:
        if not example_path.exists():
            print(f"Missing {example_path}", file=sys.stderr)
            return 1
        text = example_path.read_text(encoding="utf-8")
        print(f"Creating {env_path.name} from {example_path.name}")

    for key in GENERATED:
        current = read_value(text, key) or ""
        if len(current) >= MIN_SECRET_LEN:
            print(f"  kept       {key}")
        else:
            text = set_value(text, key, secrets.token_urlsafe(48))
            print(f"  generated  {key}")

    if prompt:
        print("\nProvider credentials (input is hidden where marked; press Enter to skip):")
    missing = []
    for key, label, hidden in CREDENTIALS:
        if not is_placeholder(key, read_value(text, key)):
            print(f"  kept       {key}")
            continue
        value = ""
        if prompt:
            ask = getpass.getpass if hidden else input
            value = ask(f"  {label}{' (hidden)' if hidden else ''}: ").strip()
        if value:
            text = set_value(text, key, value)
            print(f"  set        {key}")
        else:
            missing.append(key)

    env_path.write_text(text, encoding="utf-8")
    os.chmod(env_path, 0o600)

    if missing:
        print(f"\nStill to fill in {env_path.name}: {', '.join(missing)}")
        print("Rerun `make env` or edit the file; generated secrets are kept.")
    else:
        print(f"\n{env_path.name} is ready.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument(
        "--env", type=Path, default=ROOT / ".env", help="path to write (default: .env)"
    )
    parser.add_argument(
        "--example",
        type=Path,
        default=ROOT / ".env.example",
        help="template (default: .env.example)",
    )
    parser.add_argument(
        "--no-prompt", action="store_true", help="only generate secrets; never ask for credentials"
    )
    args = parser.parse_args(argv)
    prompt = not args.no_prompt and sys.stdin.isatty()
    return setup(args.env, args.example, prompt)


if __name__ == "__main__":
    raise SystemExit(main())
