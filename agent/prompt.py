"""Load the fixed, version-controlled extractor system prompt and its SHA-256 hash.

The prompt file is sent to the model byte for byte; the hash covers exactly those bytes
and is logged with every extraction. Nothing at runtime (user text, request fields,
environment) can change the instructions.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

__all__ = ["ExtractorPrompt", "EXTRACTOR_PROMPT_PATH", "load_extractor_prompt"]

EXTRACTOR_PROMPT_PATH = Path(__file__).resolve().parent.parent / "prompts" / "extractor.md"

_VERSION_RE = re.compile(r"^Version:\s*(\S+)\s*$", re.MULTILINE)


@dataclass(frozen=True)
class ExtractorPrompt:
    text: str
    version: str
    sha256: str


@lru_cache(maxsize=1)
def load_extractor_prompt(path: Path = EXTRACTOR_PROMPT_PATH) -> ExtractorPrompt:
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    match = _VERSION_RE.search(text)
    if match is None:
        raise ValueError(f"{path.name} has no 'Version:' line")
    return ExtractorPrompt(
        text=text, version=match.group(1), sha256=hashlib.sha256(raw).hexdigest()
    )
