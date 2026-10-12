"""Name matching rules shared by the agent and the gateway.

One copy, so the agent's resolution and the gateway's evidence check cannot drift apart.

- :func:`mentions` (**exact**, the rule the gateway enforces): a name appears in the text as
  a whole word or phrase, ignoring case and punctuation. Spacing is also ignored *across
  whole words*: "ahma", "ah ma" and "Ah-Ma" all name "Ah Ma", and "johndoe" names
  "John Doe", but "mumbai" never names "Mum" (no partial words).
- :func:`fuzzy_mentions` (**suggestion only**): a name is within a small spelling distance
  of some of the user's words ("mom" ~ "Mum", "jon" ~ "John"). The gateway never accepts
  a fuzzy match as evidence; the agent only uses it to ask "Did you mean …?", and the
  customer's answer then names the record exactly.
"""

from __future__ import annotations

import re
from collections.abc import Iterable

__all__ = [
    "FILLER_WORDS",
    "compact",
    "edit_distance",
    "fuzzy_mentions",
    "mentions",
    "normalize",
    "words",
]

_WORD_RE = re.compile(r"[a-z0-9]+")

#: Words that never take part in a fuzzy match (they would match short names by accident).
FILLER_WORDS = frozenset(
    {
        "a", "an", "and", "account", "acct", "bank", "ending", "for", "from", "her", "his",
        "in", "my", "of", "our", "pay", "paynow", "please", "s", "send", "the", "their",
        "to", "transfer", "with",
    }
)  # fmt: skip

#: Longest run of the user's words joined together when looking for a spacing-free match.
_MAX_JOIN = 4
#: Names shorter than this (after removing spaces) are never fuzzy-matched.
_MIN_FUZZY_LEN = 3


def words(text: str) -> list[str]:
    """Lower-case words and numbers, punctuation dropped."""
    return _WORD_RE.findall(text.casefold())


def normalize(text: str) -> str:
    return " ".join(words(text))


def compact(text: str) -> str:
    """The text with case, spaces and punctuation removed ("Ah-Ma" -> "ahma")."""
    return "".join(words(text))


def _runs(tokens: list[str], max_len: int) -> Iterable[list[str]]:
    for i in range(len(tokens)):
        for j in range(i + 1, min(i + max_len, len(tokens)) + 1):
            yield tokens[i:j]


def mentions(text: str, names: Iterable[str]) -> bool:
    """True if any name appears in *text* as whole words, ignoring case, punctuation and
    the spacing between words. This is the exact rule the gateway enforces."""
    tokens = words(text)
    if not tokens:
        return False
    haystack = f" {' '.join(tokens)} "
    joined = {"".join(run) for run in _runs(tokens, _MAX_JOIN)}
    for name in names:
        name_words = words(name)
        if not name_words:
            continue
        if f" {' '.join(name_words)} " in haystack or "".join(name_words) in joined:
            return True
    return False


def edit_distance(a: str, b: str) -> int:
    """Optimal string alignment distance (insert, delete, substitute, swap neighbours)."""
    if a == b:
        return 0
    prev2: list[int] = []
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cost = 0 if ca == cb else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if i > 1 and j > 1 and ca == b[j - 2] and a[i - 2] == cb:
                cur[j] = min(cur[j], prev2[j - 2] + 1)
        prev2, prev = prev, cur
    return prev[-1]


def _max_edits(length: int) -> int:
    if length < _MIN_FUZZY_LEN:
        return 0
    if length <= 5:
        return 1
    if length <= 10:
        return 2
    return 3


def _close(candidate: str, name: str) -> bool:
    limit = _max_edits(len(name))
    if limit == 0 or candidate[0] != name[0] or abs(len(candidate) - len(name)) > limit:
        return False
    return edit_distance(candidate, name) <= limit


def fuzzy_mentions(text: str, names: Iterable[str]) -> bool:
    """True if some of the user's words are a near spelling of a name.

    Suggestion only: never use this to decide who is paid. Runs of up to four words are
    compared with spaces removed; filler words ("my", "to", ...) and numbers are skipped,
    names shorter than three letters are never fuzzy-matched, and the first letter must
    agree.
    """
    tokens = words(text)
    candidates = {
        "".join(run)
        for run in _runs(tokens, _MAX_JOIN)
        if run[0] not in FILLER_WORDS
        and run[-1] not in FILLER_WORDS
        and not any(t.isdigit() for t in run)
    }
    if not candidates:
        return False
    for name in names:
        target = compact(name)
        if not target or target.isdigit():
            continue
        if any(_close(c, target) for c in candidates):
            return True
    return False
