"""Bounded, process-local progress; never an authorization or eligibility source."""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

RETENTION_SECONDS = 600
MAX_RECORDS = 256
_lock = threading.RLock()
_records: dict[tuple[str, str, str, str], Progress] = {}


@dataclass
class Progress:
    db_path: str
    user_id: str
    conversation_id: str
    revision: int
    operation_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    kind: str = "draft"
    stage: str = "captured"
    started: float = field(default_factory=time.monotonic)
    ended: float | None = None
    result: dict[str, Any] | None = None
    code: str | None = None
    retry_validation: bool = False

    def update(self, stage: str) -> None:
        with _lock:
            order = {
                "captured": 0,
                "transcribing": 1,
                "extracting": 1,
                "resolving": 2,
                "validating": 3,
            }
            if self.ended is None and order.get(stage, -1) >= order.get(self.stage, 0):
                self.stage = stage

    def finish(self, *, result: dict | None = None, code: str | None = None) -> None:
        with _lock:
            if self.ended is not None:
                return
            self.result, self.code = result, code
            self.stage = (
                "failed"
                if code
                else "ready"
                if result and result.get("status") == "ready"
                else "needs_input"
                if self.kind == "draft"
                else "captured"
            )
            self.ended = time.monotonic()

    def view(self) -> dict[str, Any]:
        with _lock:
            return {
                "operation_id": self.operation_id,
                "conversation_revision": self.revision,
                "kind": self.kind,
                "stage": self.stage,
                "elapsed_seconds": round((self.ended or time.monotonic()) - self.started, 1),
                "done": self.ended is not None,
                "code": self.code,
                "retry_validation": self.retry_validation,
                "result": self.result,
            }


def register(progress: Progress, operation_id: str | None = None) -> None:
    with _lock:
        now = time.monotonic()
        for key, record in list(_records.items()):
            if record.ended is not None and now - record.ended > RETENTION_SECONDS:
                del _records[key]
        while len(_records) >= MAX_RECORDS:
            del _records[next(iter(_records))]
        key = (
            progress.db_path,
            progress.user_id,
            progress.conversation_id,
            operation_id or progress.operation_id,
        )
        _records[key] = progress


def get(db_path: str, user_id: str, conversation_id: str, operation_id: str) -> Progress | None:
    with _lock:
        record = _records.get((db_path, user_id, conversation_id, operation_id))
        if (
            record
            and record.ended is not None
            and time.monotonic() - record.ended > RETENTION_SECONDS
        ):
            return None
        return record
