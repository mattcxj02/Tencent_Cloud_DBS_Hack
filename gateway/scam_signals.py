"""Deterministic scam signals derived from the gateway-held conversation.

The input is the text of the authenticated user's own captured turns
(``user_text``, ``user_asr_final``, ``user_transcript_edit``, ``user_answer``),
read from the gateway database by conversation ID.  Agent-authored
``app_question`` turns are ignored, and no caller can supply a risk flag.
Edited-away originals are still scanned: a phrase the user said and later
corrected is kept as a (conservative) signal.

Rules are fixed phrase patterns over normalized text (case-folded, apostrophes
removed, punctuation collapsed to single spaces):

======================  =======================================================
Code                    Fires on
======================  =======================================================
SCAM_SIGNAL_SAFE_ACCOUNT    "safe / safety / secure / protected account"
SCAM_SIGNAL_IMPERSONATION   an authority word (police, officer, inspector, MAS,
                            government, customs, court, investigator) followed
                            within four words by told/asked/instructed/ordered/
                            said/wants/directed; or "the bank/police called me"
SCAM_SIGNAL_SECRECY         "don't / do not / never / not to tell|inform anyone",
                            "keep this secret / confidential / between us"
SCAM_SIGNAL_REMOTE_ACCESS   AnyDesk, TeamViewer, "remote access"
SCAM_SIGNAL_URGENCY         urgent, immediately, right now, ASAP, "account will be
                            frozen" ... — reported only together with another
                            signal; urgency alone never fires
======================  =======================================================

A signal only escalates an otherwise-eligible payment to simulated step-up
(STEP_UP).  It never blocks and never relaxes a block.

Known limits (measure both in evaluations, do not over-claim):

- False negatives: paraphrases ("move it somewhere the scammers can't reach"),
  other languages or Singlish spellings, misspellings and ASR errors, and a
  victim who has been coached to say nothing unusual all evade the rules.
- False positives: benign uses such as "my officer friend said dinner is on
  him", "keep this secret, it's a surprise party", or "a secure account for
  my savings" escalate to step-up.  The cost is one extra simulated code.
- The rules see only the captured conversation, not call logs, payee age or
  device signals that a production fraud engine would use.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Iterable

__all__ = [
    "SCAM_SIGNAL_CODES",
    "USER_TURN_ROLES",
    "scam_signals",
    "conversation_scam_signals",
]

USER_TURN_ROLES = ("user_text", "user_asr_final", "user_transcript_edit", "user_answer")

_AUTHORITY = r"(?:police|policeman|officer|inspector|investigator|mas|government|customs|court)"
_INSTRUCT = r"(?:told|tells|asked|asks|instructed|ordered|said|says|wants|wanted|directed)"
_CONFIDANT = (
    r"(?:anyone|anybody|nobody|my family|family|my wife|my husband|my son|my daughter"
    r"|my children|my kids|the bank|bank)"
)

#: Strong signals: each one alone escalates to step-up.
_STRONG_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    (
        "SCAM_SIGNAL_SAFE_ACCOUNT",
        re.compile(r"\b(?:safe|safety|secure|protected)(?: bank)? accounts?\b"),
    ),
    (
        "SCAM_SIGNAL_IMPERSONATION",
        re.compile(
            rf"\b{_AUTHORITY}\b(?: \w+){{0,4}} {_INSTRUCT}\b"
            r"|\b(?:the )?(?:bank|police|government) (?:called|phoned|rang|messaged)\b"
        ),
    ),
    (
        "SCAM_SIGNAL_SECRECY",
        re.compile(
            rf"\b(?:dont|do not|never|not to|must not|mustnt) (?:tell|inform|let) {_CONFIDANT}\b"
            r"|\bkeep (?:this|it) (?:a )?(?:secret|confidential|between us)\b"
        ),
    ),
    (
        "SCAM_SIGNAL_REMOTE_ACCESS",
        re.compile(r"\b(?:anydesk|teamviewer|remote access|screen ?share)\b"),
    ),
)

#: Weak signal: reported only alongside a strong one.
_URGENCY = (
    "SCAM_SIGNAL_URGENCY",
    re.compile(
        r"\b(?:urgent|urgently|immediately|right now|right away|asap|as soon as possible"
        r"|before its too late|within (?:the|an|one) hour"
        r"|accounts? (?:will be|is being|has been) (?:frozen|blocked|closed|suspended))\b"
    ),
)

SCAM_SIGNAL_CODES: tuple[str, ...] = (*(code for code, _ in _STRONG_RULES), _URGENCY[0])


def _normalize(text: str) -> str:
    text = text.casefold().replace("’", "'").replace("‘", "'").replace("'", "")
    return " ".join(re.findall(r"[a-z0-9]+", text))


def scam_signals(texts: Iterable[str]) -> tuple[str, ...]:
    """Return the scam signal codes that fire for these user texts (stable order)."""
    joined = " | ".join(_normalize(t) for t in texts)
    strong = tuple(code for code, pattern in _STRONG_RULES if pattern.search(joined))
    if not strong:
        return ()
    urgency = (_URGENCY[0],) if _URGENCY[1].search(joined) else ()
    return strong + urgency


def conversation_scam_signals(conn: sqlite3.Connection, conversation_id: str) -> tuple[str, ...]:
    """Scan the user's own captured turns of *conversation_id*."""
    placeholders = ",".join("?" for _ in USER_TURN_ROLES)
    rows = conn.execute(
        f"SELECT text FROM conversation_turns WHERE conversation_id = ? "
        f"AND role IN ({placeholders}) ORDER BY seq",
        (conversation_id, *USER_TURN_ROLES),
    ).fetchall()
    return scam_signals(row[0] for row in rows)
