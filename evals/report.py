"""Aggregate per-case records into the T-14 summary and render it as Markdown.

Everything here is computed from the captured records; nothing is estimated. Missing
usage is counted as unavailable, and cost is ``unavailable`` because no model price is
configured (``shared.telemetry.ModelPrice``).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable, Sequence
from typing import Any

from evals.scoring import latency_summary, usage_totals, validator_confusion

__all__ = ["CATEGORIES", "render_markdown", "summarize", "verdict_of"]

CATEGORIES = ("ordinary", "clarification", "multi_intent", "adversarial")
_UNSAFE = {"incorrect_draft", "drafted_instead_of_clarifying"}
_SAFE_NOT_EXACT = {"safe_clarification", "unneeded_clarification", "other_clarification"}


def verdict_of(validation: dict[str, Any] | None) -> str | None:
    if validation is None:
        return None
    if validation.get("status") != "OK":
        return "ERROR"
    return validation.get("verdict") or "ERROR"


def _extraction(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    classes = Counter(r["agent"]["class"] for r in records)
    draft_expected = [r for r in records if r["expected"]["outcome"] == "draft"]
    clarify_expected = [r for r in records if r["expected"]["outcome"] == "clarify"]
    calls = [c for r in records for c in r["agent"].get("extractor_calls", [])]
    comparisons = [r["agent"].get("comparison") for r in draft_expected]
    return {
        "n": len(records),
        "outcome_correct": classes["exact"] + classes["correct_clarification"],
        "draft_expected": len(draft_expected),
        "exact": classes["exact"],
        "clarify_expected": len(clarify_expected),
        "clarify_correct": classes["correct_clarification"],
        "safe_not_exact": sum(classes[c] for c in _SAFE_NOT_EXACT),
        "unsafe_drafts": sum(classes[c] for c in _UNSAFE),
        "agent_errors": classes["error"],
        "missing_leg_cases": sum(1 for c in comparisons if c and c["missing_legs"]),
        "extra_leg_cases": sum(1 for c in comparisons if c and c["extra_legs"]),
        "classes": dict(sorted(classes.items())),
        "agent_latency": latency_summary([r["agent"]["latency_ms"] for r in records]),
        "extractor_latency": latency_summary(
            [c["latency_ms"] for c in calls if c.get("latency_ms") is not None]
        ),
        "extractor_call_errors": sum(1 for c in calls if c.get("error")),
        "extractor_usage": usage_totals(c.get("usage") for c in calls),
    }


def _validations(records: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in records:
        if r.get("natural_validation") is not None:
            out.append(r["natural_validation"])
        out += [w["validation"] for w in r.get("wrong_drafts", []) if w.get("validation")]
    return out


def _validator(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    natural = [
        (r["label"], verdict_of(r["natural_validation"]))
        for r in records
        if r.get("natural_validation") is not None and r.get("label")
    ]
    wrong = [
        ("incorrect", verdict_of(w["validation"]))
        for r in records
        for w in r.get("wrong_drafts", [])
        if w.get("validation")
    ]
    calls = _validations(records)
    codes = Counter(c.get("code") for c in calls if verdict_of(c) == "ERROR")
    return {
        "natural": validator_confusion(natural),
        "wrong": validator_confusion(wrong),
        "calls": len(calls),
        "errors": sum(codes.values()),
        "timeouts": sum(n for code, n in codes.items() if code and "TIMEOUT" in code),
        "error_codes": dict(codes),
        "latency": latency_summary(
            [c["latency_ms"] for c in calls if c.get("latency_ms") is not None]
        ),
        "wall": latency_summary([c["wall_ms"] for c in calls if c.get("wall_ms") is not None]),
        "usage": usage_totals(c.get("usage") for c in calls),
        "wrong_not_built": sum(
            1 for r in records for w in r.get("wrong_drafts", []) if not w.get("built")
        ),
    }


def _policy(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    with_policy = [r for r in records if r.get("policy")]
    checks = [
        {
            "case_id": r["case_id"],
            "expected": r["policy_expected"],
            "observed": r.get("policy"),
            "met": r.get("policy_expectation_met"),
        }
        for r in records
        if r.get("policy_expected")
    ]
    return {
        "drafts_probed": len(with_policy),
        "decisions": dict(Counter(r["policy"]["decision"] for r in with_policy)),
        "codes": dict(Counter(c for r in with_policy for c in r["policy"]["codes"])),
        "expectations": checks,
    }


def _by(records: Iterable[dict[str, Any]], key: str) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for r in records:
        groups.setdefault(r[key], []).append(r)
    return groups


def summarize(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
    by_cat = _by(records, "category")
    by_split = _by(records, "split")
    return {
        "extraction": {
            **{c: _extraction(by_cat[c]) for c in CATEGORIES if c in by_cat},
            "all": _extraction(records),
        },
        "extraction_by_split": {s: _extraction(rs) for s, rs in sorted(by_split.items())},
        "validator": {
            **{c: _validator(by_cat[c]) for c in CATEGORIES if c in by_cat},
            "all": _validator(records),
        },
        "policy": _policy(records),
    }


# ── Markdown ──────────────────────────────────────────────────────────


def _frac(k: int, n: int) -> str:
    return f"{k}/{n}" if n else "–"


def _ms(summary: dict[str, Any]) -> str:
    if not summary["n"]:
        return "– (n=0)"
    return f"{summary['p50_ms']:,} / {summary['p95_ms']:,} (n={summary['n']})"


def _tokens(usage: dict[str, Any]) -> str:
    if not usage["calls"]:
        return "–"
    if not usage["calls_with_usage"]:
        return f"unavailable ({usage['calls']} calls)"
    missing = usage["calls"] - usage["calls_with_usage"]
    text = f"{usage['prompt_tokens']:,} in / {usage['completion_tokens']:,} out"
    return text + (f" ({missing} calls unreported)" if missing else "")


def _blocked(side: dict[str, Any] | None) -> str:
    if not side:
        return "–"
    b = side["blocked_with_validator"]
    detail = f"MISMATCH {b['MISMATCH']}, UNCERTAIN {b['UNCERTAIN']}, ERROR {b['ERROR']}"
    return f"{sum(b.values())} ({detail})"


def _arrow(side: dict[str, Any] | None) -> str:
    """``n → accepted with validator`` (extractor-only accepts all n)."""
    return f"{side['n']} → {side['accepted_with_validator']}" if side else "–"


def render_markdown(results: dict[str, Any]) -> str:
    meta, summary = results["meta"], results["summary"]
    lines: list[str] = []
    add = lines.append
    add(f"# T-14 evaluation results ({meta['mode']} run)\n")
    if meta["mode"] != "live":
        add(
            "**Dry run with fake models (oracle extractor, scripted validator). These numbers "
            "check the harness and fixtures only; they are not measurements.**\n"
        )
    add(f"- Run: `{meta['run_id']}`, started {meta['started_at']}, finished {meta['finished_at']}")
    add(
        f"- Golden set: `{meta['golden_version']}`, {meta['cases_run']} cases, "
        f"`cases.jsonl` sha256 `{meta['cases_sha256']}`"
    )
    add(
        f"- Extractor: {meta['extractor']['provider']} `{meta['extractor']['model']}`, prompt "
        f"`{meta['extractor']['prompt_version']}` sha256 `{meta['extractor']['prompt_sha256']}`, "
        f"max_tokens {meta['extractor']['max_tokens']}, temperature 0"
    )
    add(
        f"- Validator: {meta['validator']['provider']} `{meta['validator']['model']}`, prompt "
        f"`{meta['validator']['prompt_version']}` sha256 `{meta['validator']['prompt_hash']}`, "
        f"timeout {meta['validator']['timeout_s']} s, temperature 0"
    )
    add(
        "- Cost: **unavailable** (no model price configured; token counts below are "
        "provider-reported)\n"
    )

    add("## Extraction: transcript-to-draft (agent + gateway, one run per case)\n")
    add(
        "| Category | Cases | Outcome correct | Critical-field exact (draft expected) | "
        "Correct clarification (question expected) | Safe, not exact | Unsafe drafts | "
        "Agent errors | Agent latency p50 / p95 ms | Extractor call p50 / p95 ms | "
        "Extractor tokens |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|---|")
    for key, s in summary["extraction"].items():
        add(
            f"| {key} | {s['n']} | {_frac(s['outcome_correct'], s['n'])} | "
            f"{_frac(s['exact'], s['draft_expected'])} | "
            f"{_frac(s['clarify_correct'], s['clarify_expected'])} | {s['safe_not_exact']} | "
            f"{s['unsafe_drafts']} | {s['agent_errors']} | {_ms(s['agent_latency'])} | "
            f"{_ms(s['extractor_latency'])} | {_tokens(s['extractor_usage'])} |"
        )
    add("")
    add("By split (held-out vs scenarios overlapping earlier prompt examples/tests):\n")
    add("| Split | Cases | Outcome correct | Exact | Correct clarification | Unsafe drafts |")
    add("|---|---|---|---|---|---|")
    for key, s in summary["extraction_by_split"].items():
        add(
            f"| {key} | {s['n']} | {_frac(s['outcome_correct'], s['n'])} | "
            f"{_frac(s['exact'], s['draft_expected'])} | "
            f"{_frac(s['clarify_correct'], s['clarify_expected'])} | {s['unsafe_drafts']} |"
        )
    add("")

    add("## Validator comparison on the same captured drafts\n")
    add(
        "Extractor-only accepts every frozen draft. Extractor+validator accepts a draft only "
        "on an OK `MATCH`; `MISMATCH`, `UNCERTAIN` and `ERROR` block. Wrong drafts are "
        "deliberate variants of correct captured drafts (built only when the captured "
        "draft was exact).\n"
    )
    add(
        "| Category | Correct drafts | Correct accepted: extractor-only → +validator | "
        "Correct blocked by validator (false rejects) | Incorrect natural drafts accepted: "
        "extractor-only → +validator | Wrong drafts | Wrong accepted: extractor-only → "
        "+validator (false accepts) | Validator ERROR of calls | Validator p50 / p95 ms | "
        "Validator tokens |"
    )
    add("|---|---|---|---|---|---|---|---|---|---|")
    for key, v in summary["validator"].items():
        correct = v["natural"].get("correct")
        incorrect_n = sum(
            v["natural"].get(lbl, {}).get("n", 0) for lbl in ("incorrect", "should_have_clarified")
        )
        incorrect_acc = sum(
            v["natural"].get(lbl, {}).get("accepted_with_validator", 0)
            for lbl in ("incorrect", "should_have_clarified")
        )
        wrong = v["wrong"].get("incorrect")
        add(
            f"| {key} | {correct['n'] if correct else 0} | "
            f"{_arrow(correct)} | "
            f"{_blocked(correct)} | "
            f"{(str(incorrect_n) + ' → ' + str(incorrect_acc)) if incorrect_n else '0'} | "
            f"{wrong['n'] if wrong else 0} | "
            f"{_arrow(wrong)} | "
            f"{_frac(v['errors'], v['calls'])} (timeouts {v['timeouts']}) | {_ms(v['latency'])} | "
            f"{_tokens(v['usage'])} |"
        )
    add("")

    pol = summary["policy"]
    add("## Deterministic gateway policy on captured drafts (not validator metrics)\n")
    add(
        f"Probed {pol['drafts_probed']} frozen drafts with `gateway.policy.evaluate` "
        f"(read-only envelope, nothing executed). Decisions: {pol['decisions'] or '–'}. "
        f"Codes: {pol['codes'] or '–'}.\n"
    )
    if pol["expectations"]:
        add("| Case | Expected | Observed | Met |")
        add("|---|---|---|---|")
        for c in pol["expectations"]:
            obs = c["observed"]
            observed = f"{obs['decision']} {obs['codes']}" if obs else "no draft"
            exp = c["expected"]
            add(
                f"| {c['case_id']} | {exp['decision']} incl. {exp['codes_include']} | "
                f"{observed} | {c['met']} |"
            )
        add("")

    add("## Failures and notable outcomes (from the captured records)\n")
    failures = results.get("failures", [])
    if not failures:
        add("None.\n")
    for f in failures:
        add(f"- **{f['case_id']}** ({f['kind']}): {f['detail']}")
    add("")

    lf = results.get("langfuse", {})
    add("## Langfuse\n")
    if lf.get("dataset"):
        d = lf["dataset"]
        add(f"- Dataset `{d['name']}` ({d['items']} items): {d['url']}")
    for run in lf.get("runs", []):
        add(
            f"- Run `{run['run_name']}`: {run.get('dataset_run_url') or 'no URL returned'} "
            f"({run.get('items_completed')} items)"
        )
    for err in lf.get("errors", []):
        add(f"- Error: {err}")
    if lf.get("read_back"):
        for rb in lf["read_back"]:
            add(
                f"- Trace read-back `{rb['trace_id']}` ({rb['what']}): {rb['observations']} "
                f"observations {rb['names']} error={rb['error']}"
            )
    if not lf:
        add("- Not used in this run.")
    add("")
    return "\n".join(lines)
