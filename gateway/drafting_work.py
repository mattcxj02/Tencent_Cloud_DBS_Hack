"""Single-process drafting admission, deduplication and delegation lifetime guards."""

from __future__ import annotations

import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from gateway.audit import append_audit
from gateway.progress import Progress, register
from ledger.schema import connect
from shared.deadline import Deadline, DeadlineExpired


@dataclass
class DraftWork:
    db_path: str
    user_id: str
    conversation_id: str
    revision: int
    budget: Deadline
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    done: threading.Event = field(default_factory=threading.Event)
    result: dict[str, Any] | None = None
    error: Exception | None = None
    superseded: bool = False
    progress: Progress = field(init=False)

    def __post_init__(self) -> None:
        self.progress = Progress(self.db_path, self.user_id, self.conversation_id, self.revision)

    def check(self, conn: sqlite3.Connection | None = None) -> None:
        self.budget.remaining()
        owned = conn is None
        conn = conn or connect(self.db_path)
        try:
            row = conn.execute(
                "SELECT revision FROM conversations WHERE conversation_id = ? "
                "AND owner_user_id = ?",
                (self.conversation_id, self.user_id),
            ).fetchone()
            if row is None or int(row["revision"]) != self.revision:
                self.superseded = True
                self.budget.cancelled.set()
                raise DeadlineExpired
        finally:
            if owned:
                conn.close()

    def audit_end(self, code: str) -> None:
        conn = connect(self.db_path)
        try:
            conn.execute("BEGIN IMMEDIATE")
            if code in ("DRAFT_CANCELLED", "ORCHESTRATION_TIMEOUT", "AGENT_TIMEOUT"):
                conn.execute(
                    "UPDATE validation_results SET state = 'SUPERSEDED' "
                    "WHERE conversation_id = ? AND conversation_revision = ? "
                    "AND state IN ('CURRENT', 'PENDING')",
                    (self.conversation_id, self.revision),
                )
            append_audit(
                conn,
                source="gateway",
                user_id=self.user_id,
                conversation_id=self.conversation_id,
                step="gateway.draft.ended",
                payload={
                    "run_id": self.run_id,
                    "conversation_revision": self.revision,
                    "code": code,
                },
            )
            conn.commit()
        finally:
            conn.close()


_lock = threading.Lock()
_work: dict[tuple[str, str, str, int], DraftWork] = {}
_runs: dict[str, DraftWork] = {}


def admit(work: DraftWork, operation_id: str | None = None) -> tuple[DraftWork, bool]:
    key = (work.db_path, work.user_id, work.conversation_id, work.revision)
    with _lock:
        existing = _work.get(key)
        if existing is not None:
            register(existing.progress, operation_id)
            return existing, False
        _work[key] = work
        _runs[work.run_id] = work
        register(work.progress, operation_id)
        return work, True


def finish(work: DraftWork) -> None:
    with _lock:
        _work.pop((work.db_path, work.user_id, work.conversation_id, work.revision), None)
        _runs.pop(work.run_id, None)
        work.done.set()


def check_run(run_id: str | None, conn: sqlite3.Connection | None = None) -> None:
    if run_id is None:
        return  # Standalone scoped tokens still use their ordinary TTL/revision checks.
    with _lock:
        work = _runs.get(run_id)
    if work is None:
        raise DeadlineExpired
    work.check(conn)


def report_stage(
    run_id: str | None, user_id: str, conversation_id: str, revision: int, stage: str
) -> None:
    with _lock:
        work = _runs.get(run_id or "")
    if work is None or (work.user_id, work.conversation_id, work.revision) != (
        user_id,
        conversation_id,
        revision,
    ):
        raise DeadlineExpired
    work.check()
    work.progress.update(stage)


def cancel_operation(progress: Progress) -> bool:
    with _lock:
        work = next((w for w in _runs.values() if w.progress is progress), None)
    if work is not None:
        work.budget.cancelled.set()
        progress.finish(code="DRAFT_CANCELLED")
        return True
    return False
