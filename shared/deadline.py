"""Request budgets: wire timestamps become local monotonic deadlines, never authority."""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Callable
from concurrent.futures import Executor, TimeoutError
from contextvars import ContextVar, copy_context
from dataclasses import dataclass, field

DEADLINE_HEADER = "X-DCTA-Deadline"
current_deadline: ContextVar[Deadline | None] = ContextVar("dcta_deadline", default=None)


class DeadlineExpired(Exception):
    """No more work may start or be published for this request."""


@dataclass
class Deadline:
    end: float
    cancelled: threading.Event = field(default_factory=threading.Event)

    @classmethod
    def after(cls, seconds: float) -> Deadline:
        return cls(time.monotonic() + seconds)

    @classmethod
    def from_header(cls, value: str | None, maximum: float) -> Deadline:
        if value is None:
            return cls.after(maximum)
        try:
            remaining = float(value) - time.time()
            if not math.isfinite(remaining):
                raise ValueError
        except ValueError:
            remaining = 0.0
        return cls.after(min(maximum, remaining))

    def remaining(self) -> float:
        remaining = self.end - time.monotonic()
        if self.cancelled.is_set() or remaining <= 0:
            raise DeadlineExpired
        return remaining

    def active(self) -> bool:
        return not self.cancelled.is_set() and time.monotonic() < self.end

    def child(self, maximum: float) -> Deadline:
        return Deadline(min(self.end, time.monotonic() + maximum), self.cancelled)

    def header(self) -> str:
        return str(time.time() + self.remaining())


def remaining_timeout(maximum: float) -> float:
    budget = current_deadline.get()
    return min(maximum, budget.remaining()) if budget is not None else maximum


def check_deadline() -> None:
    budget = current_deadline.get()
    if budget is not None:
        budget.remaining()


def bounded_call[T](executor: Executor, budget: Deadline, call: Callable[[], T]) -> T:
    """Bound queue waiting and calls. A running remote request may continue after timeout."""
    budget.remaining()
    context = copy_context()

    def invoke() -> T:
        budget.remaining()  # Also checked when admitted from the executor queue.
        token = current_deadline.set(budget)
        try:
            return call()
        finally:
            current_deadline.reset(token)

    future = executor.submit(context.run, invoke)
    try:
        while True:
            try:
                result = future.result(timeout=min(0.05, budget.remaining()))
                budget.remaining()  # Never accept a late successful response.
                return result
            except TimeoutError:
                if future.done():
                    raise
                budget.remaining()
    except DeadlineExpired:
        future.cancel()
        raise
