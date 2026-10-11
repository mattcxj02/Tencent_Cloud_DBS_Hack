"""Langfuse dataset publishing and experiment runs (langfuse 4.17 SDK).

Uses the official SDK calls read from the installed package:
``Langfuse.create_dataset`` (upsert by name), ``Langfuse.create_dataset_item`` (upsert by
``id``), ``Langfuse.get_dataset`` and ``Langfuse.run_experiment(data=<DatasetItems>)``,
which links each item's trace to a named dataset run (``dataset_run_items.create``),
stores item-level evaluator results as scores and run-level evaluator results as
dataset-run scores.

The Langfuse client is the one inside the shared ``Telemetry`` backend, so experiment
spans pass through the same export-time allowlist and redaction as every other span
(``shared/telemetry``). That allowlist drops the SDK's ``langfuse.experiment.*`` span
attributes; the dataset-run link is made by the REST call, not by those attributes.

Everything published is synthetic and passes ``redact`` first. Any failure is returned
as a short error string (redacted, bounded) and the caller falls back to local runs.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from typing import Any

from shared.telemetry import Telemetry, redact, redact_text

__all__ = ["LangfuseExperiments", "error_text"]


def error_text(exc: BaseException) -> str:
    """A short, redacted description of an SDK/API failure."""
    status = getattr(exc, "status_code", None)
    detail = redact_text(str(exc))[:240]
    prefix = f"{type(exc).__name__}" + (f" (HTTP {status})" if isinstance(status, int) else "")
    return f"{prefix}: {detail}" if detail else prefix


class LangfuseExperiments:
    def __init__(self, client: Any, base_url: str) -> None:
        self._client = client
        self._base_url = base_url.rstrip("/")
        self._dataset: Any = None

    @classmethod
    def from_telemetry(cls, telemetry: Telemetry) -> LangfuseExperiments | None:
        """The SDK client behind an enabled Telemetry facade (private attribute; the
        facade exposes no public accessor), or None when Langfuse is disabled."""
        backend = getattr(telemetry, "_backend", None)
        if not telemetry.enabled or backend is None:
            return None
        return cls(backend.client, backend.base_url)

    # ── dataset ──

    def publish(
        self,
        name: str,
        *,
        description: str,
        metadata: dict[str, Any],
        items: Sequence[dict[str, Any]],
    ) -> dict[str, Any]:
        """Create/upsert the dataset and its items; return ``{name, id, url, items}``."""
        self._client.create_dataset(name=name, description=description, metadata=redact(metadata))
        for item in items:
            self._client.create_dataset_item(
                dataset_name=name,
                id=item["id"],
                input=redact(item["input"]),
                expected_output=redact(item["expected_output"]),
                metadata=redact(item["metadata"]),
            )
        self._dataset = self._client.get_dataset(name)
        url = f"{self._base_url}/project/{self._dataset.project_id}/datasets/{self._dataset.id}"
        return {
            "name": name,
            "id": self._dataset.id,
            "url": url,
            "items": len(self._dataset.items),
        }

    def items_for(self, case_ids: Sequence[str]) -> list[Any]:
        """Dataset items for these cases, in the given order."""
        assert self._dataset is not None, "publish() first"
        by_case = {}
        for item in self._dataset.items:
            data = item.input if isinstance(item.input, dict) else {}
            by_case[data.get("case_id")] = item
        return [by_case[c] for c in case_ids if c in by_case]

    # ── experiment run ──

    def run(
        self,
        *,
        name: str,
        run_name: str,
        description: str,
        items: Sequence[Any],
        task: Callable[..., Any],
        evaluators: Sequence[Callable[..., Any]],
        run_evaluators: Sequence[Callable[..., Any]],
        metadata: dict[str, str],
    ) -> dict[str, Any]:
        """One named dataset run, items processed sequentially (max_concurrency=1)."""
        result = self._client.run_experiment(
            name=name,
            run_name=run_name,
            description=description,
            data=list(items),
            task=task,
            evaluators=list(evaluators),
            run_evaluators=list(run_evaluators),
            max_concurrency=1,
            metadata={k: str(v) for k, v in metadata.items()},
        )
        return {
            "name": name,
            "run_name": result.run_name,
            "dataset_run_id": result.dataset_run_id,
            "dataset_run_url": result.dataset_run_url,
            "items_completed": len(result.item_results),
            "run_scores": {e.name: e.value for e in result.run_evaluations if e.name is not None},
        }

    def flush(self) -> None:
        self._client.flush()
