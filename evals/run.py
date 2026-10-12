"""T-14 evaluation runner: golden set → real agent/gateway → validator comparison.

    uv run python -m evals.run            # live: TokenHub extractor + DeepSeek validator,
                                          # Langfuse dataset + two named experiment runs
    uv run python -m evals.run --fake     # dry run with fake models, no network, no Langfuse
    uv run python -m evals.run --case ord-01-transfer-payee --case adv-03-sanctioned-payee
    uv run python -m evals.run --no-langfuse   # live models, local results only

Phases (same captured drafts for both conditions):

A. ``extractor-only``: for each case, seed a fresh gateway DB, create the turns through
   the gateway routes, run the real agent once (live ``hy3``), capture the outcome, compare
   it with the reviewed expectation, probe the deterministic policy on any frozen draft,
   and build the deliberately wrong variants of exact drafts. Extractor-only accepts every
   frozen draft.
B. ``extractor+validator``: validate the captured natural draft through the gateway's
   validation coordinator and each wrong variant through the same ``ValidatorClient``
   (live DeepSeek via the in-process validator app). Accept only an OK ``MATCH``.

With Langfuse configured, the dataset is published (upsert) and each phase runs as a named
dataset run via the SDK's ``run_experiment``; every model call is inside the agent's or
validator's own telemetry spans, so the traces are linked to the run items. If any
Langfuse call fails, the error is recorded and the phase completes locally.

Outputs ``evals/results/<run_id>/results.json`` and ``report.md``. Credentials are read
from ``.env`` by the service settings at runtime and are never printed.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import time
import warnings
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from fastapi.testclient import TestClient
from pydantic import SecretStr

from agent.extraction import OpenAICompatibleExtractor
from agent.prompt import load_extractor_prompt
from evals.cases import (
    CASES_PATH,
    CLARIFY_FIELDS,
    GOLDEN_VERSION,
    Case,
    coverage_problems,
    file_sha256,
    load_cases,
)
from evals.experiments import LangfuseExperiments, error_text
from evals.fakes import OracleExtractor, OracleVerdictLLM
from evals.harness import (
    CaseEnv,
    NotConstructible,
    RecordingExtractor,
    build_wrong_request,
    case_env,
    create_turns,
    discard,
    policy_probe,
    run_agent_once,
    validate_natural,
    validate_wrong,
)
from evals.report import render_markdown, summarize, verdict_of
from evals.scoring import classify_outcome, compare_draft, draft_label
from shared.clients.validator import ValidationRequest, ValidatorClient
from shared.schemas import FrozenBundle
from shared.settings import (
    ValidatorSettings,
    load_agent_settings,
    load_gateway_settings,
    load_validator_settings,
)
from shared.telemetry import Telemetry, httpx_request_hook
from validator.app import PROMPT_VERSION as VALIDATOR_PROMPT_VERSION
from validator.app import create_app as create_validator_app
from validator.llm import build_llm
from validator.prompt import PROMPT_HASH as VALIDATOR_PROMPT_HASH

RESULTS_DIR = Path(__file__).resolve().parent / "results"
DATASET_NAME = f"dcta-{GOLDEN_VERSION}"
_NO_LANGFUSE = SimpleNamespace(
    langfuse_public_key=None, langfuse_secret_key=None, langfuse_base_url=None
)


# ── Runtime wiring ────────────────────────────────────────────────────


@dataclass
class Runtime:
    mode: str
    agent_tel: Telemetry | None
    gateway_tel: Telemetry
    eval_tel: Telemetry
    validator_client: ValidatorClient
    make_extractor: Callable[[Case], RecordingExtractor]
    fake_validator: OracleVerdictLLM | None
    meta: dict[str, Any]
    _owned: list[Telemetry] = field(default_factory=list)
    _extractor: OpenAICompatibleExtractor | None = None

    def close(self) -> None:
        if self._extractor is not None:
            self._extractor.close()
        for tel in self._owned:
            tel.shutdown()


def _validator_client(
    settings: ValidatorSettings, llm: Any, telemetry: Telemetry, timeout_s: float
) -> ValidatorClient:
    token = secrets.token_urlsafe(48)  # per run, in-process only, never printed
    run_settings = settings.model_copy(update={"validator_service_token": SecretStr(token)})
    app = create_validator_app(settings=run_settings, llm=llm, telemetry=telemetry)
    http = TestClient(app)
    http.event_hooks = {"request": [httpx_request_hook], "response": []}  # traceparent only
    return ValidatorClient("http://testserver", token, timeout_seconds=timeout_s, http_client=http)


def _telemetry(live: bool) -> dict[str, Telemetry]:
    """One facade per service role; disabled (no export) unless ``live``."""
    if not live:
        return {role: Telemetry(_NO_LANGFUSE, role) for role in ("agent", "validator",
                                                                  "gateway", "evals")}  # fmt: skip
    agent_settings = load_agent_settings()
    return {
        "agent": Telemetry(agent_settings, "agent"),
        "validator": Telemetry(load_validator_settings(), "validator"),
        "gateway": Telemetry(load_gateway_settings(), "gateway"),
        "evals": Telemetry(agent_settings, "evals"),
    }


def build_runtime(fake: bool, *, langfuse: bool) -> Runtime | str:
    """Wire models and telemetry. Returns an error string for a missing credential.

    ``fake`` replaces both models with fakes; ``langfuse`` decides whether telemetry is
    exported (a fake-model run with Langfuse is a labelled plumbing dry run)."""
    prompt = load_extractor_prompt()
    if fake:
        settings = ValidatorSettings(
            _env_file=None,
            VALIDATOR_SERVICE_TOKEN=None,
            VALIDATE_TIMEOUT_SECONDS=5,
            VALIDATE_PROVIDER="deepseek",
            VALIDATE_MODEL="fake-oracle-validator",
            deepseek_key=None,
        )
        fake_llm = OracleVerdictLLM()
        tel = _telemetry(langfuse)
        client = _validator_client(settings, fake_llm, tel["validator"], 10)
        return Runtime(
            mode="fake",
            agent_tel=tel["agent"] if langfuse else None,
            gateway_tel=tel["gateway"],
            eval_tel=tel["evals"],
            validator_client=client,
            make_extractor=lambda case: RecordingExtractor(
                OracleExtractor(case.oracle_extraction.model_dump(mode="json"))
            ),
            fake_validator=fake_llm,
            meta={
                "extractor": {
                    "provider": "fake",
                    "model": OracleExtractor.model,
                    "prompt_version": prompt.version,
                    "prompt_sha256": prompt.sha256,
                    "max_tokens": None,
                    "reasoning_effort": None,
                },
                "validator": {
                    "provider": "fake",
                    "model": settings.validate_model,
                    "prompt_version": VALIDATOR_PROMPT_VERSION,
                    "prompt_hash": VALIDATOR_PROMPT_HASH,
                    "timeout_s": settings.validate_timeout_seconds,
                },
            },
            _owned=list(tel.values()),
        )

    agent_settings = load_agent_settings()
    if not agent_settings.llm_enabled:
        return f"no API key for extractor provider '{agent_settings.llm_provider}'"
    vsettings = load_validator_settings()
    vllm = build_llm(vsettings)
    if vllm is None:
        return f"no API key for validator provider '{vsettings.llm_provider}'"
    tel = _telemetry(langfuse)
    client = _validator_client(
        vsettings, vllm, tel["validator"], vsettings.validate_timeout_seconds + 10
    )
    extractor = OpenAICompatibleExtractor.from_settings(agent_settings)
    return Runtime(
        mode="live",
        agent_tel=tel["agent"],
        gateway_tel=tel["gateway"],
        eval_tel=tel["evals"],
        validator_client=client,
        make_extractor=lambda case: RecordingExtractor(extractor),
        fake_validator=None,
        meta={
            "extractor": {
                "provider": agent_settings.llm_provider,
                "model": agent_settings.extract_model,
                "prompt_version": prompt.version,
                "prompt_sha256": prompt.sha256,
                "max_tokens": agent_settings.extract_max_tokens,
                "reasoning_effort": agent_settings.extract_reasoning_effort,
            },
            "validator": {
                "provider": vsettings.llm_provider,
                "model": vsettings.validate_model,
                "prompt_version": VALIDATOR_PROMPT_VERSION,
                "prompt_hash": VALIDATOR_PROMPT_HASH,
                "timeout_s": vsettings.validate_timeout_seconds,
            },
        },
        _owned=list(tel.values()),
        _extractor=extractor,
    )


# ── Phases ────────────────────────────────────────────────────────────


@dataclass
class State:
    cases: dict[str, Case]
    records: dict[str, dict[str, Any]] = field(default_factory=dict)
    envs: dict[str, CaseEnv] = field(default_factory=dict)
    bundles: dict[str, FrozenBundle] = field(default_factory=dict)
    wrong_requests: dict[tuple[str, str], ValidationRequest] = field(default_factory=dict)
    validated: set[str] = field(default_factory=set)
    #: Cases a phase has started (live calls are never repeated by the local fallback).
    attempted: dict[str, set[str]] = field(default_factory=lambda: {"A": set(), "B": set()})


def phase_a(state: State, rt: Runtime, case_id: str) -> dict[str, Any]:
    """Run the agent once on the case and capture everything about the outcome."""
    case = state.cases[case_id]
    llm = rt.make_extractor(case)
    with case_env(case.id) as env:
        state.envs[case.id] = env
        create_turns(env, case)
        outcome, latency, bundle = run_agent_once(env, llm, rt.agent_tel)
    expected = case.expected.model_dump(mode="json")
    cls, comparison = classify_outcome(
        expected, outcome, clarify_fields=CLARIFY_FIELDS, clarify_is_safe=case.clarify_is_safe
    )
    record: dict[str, Any] = {
        "case_id": case.id,
        "category": case.category,
        "split": case.split,
        "expected": expected,
        "clarify_is_safe": case.clarify_is_safe,
        "agent": {
            "status": outcome.get("status"),
            "reason": outcome.get("reason"),
            "question": outcome.get("question"),
            "error_code": outcome.get("code"),
            "bundle_id": outcome.get("bundle_id"),
            "legs": outcome.get("legs"),
            "trace_id": outcome.get("trace_id"),
            "latency_ms": latency,
            "extractor_calls": llm.calls,
            "class": cls,
            "comparison": comparison.as_dict() if comparison else None,
        },
        "label": draft_label(cls),
        "policy": None,
        "policy_expected": (
            case.expected_policy.model_dump(mode="json") if case.expected_policy else None
        ),
        "policy_expectation_met": None,
        "natural_validation": None,
        "wrong_drafts": [],
    }
    if bundle is not None:
        state.bundles[case.id] = bundle
        record["policy"] = policy_probe(env.db_path, bundle)
        if case.expected_policy is not None:
            pol, exp = record["policy"], case.expected_policy
            record["policy_expectation_met"] = pol["decision"] == exp.decision and set(
                exp.codes_include
            ) <= set(pol["codes"])
    elif case.expected_policy is not None:
        record["policy_expectation_met"] = False
    for wd in case.wrong_drafts:
        entry: dict[str, Any] = {"id": wd.id, "kind": wd.kind, "built": False, "validation": None}
        if cls != "exact" or bundle is None:
            entry["skip_reason"] = "captured draft was not exact (no reference draft)"
        else:
            try:
                state.wrong_requests[(case.id, wd.id)] = build_wrong_request(env, bundle, wd)
                entry["built"] = True
            except NotConstructible as exc:
                entry["skip_reason"] = str(exc)
        record["wrong_drafts"].append(entry)
    state.records[case.id] = record
    return record


def phase_b(state: State, rt: Runtime, case_id: str) -> dict[str, Any]:
    """Validate the captured natural draft and the case's wrong variants."""
    record = state.records[case_id]
    env = state.envs[case_id]
    bundle = state.bundles.get(case_id)
    if bundle is not None:
        if rt.fake_validator is not None:
            ok = record["label"] == "correct"
            rt.fake_validator.expect("MATCH" if ok else "MISMATCH", bundle.legs[0].leg_id)
        record["natural_validation"] = validate_natural(
            env, str(bundle.bundle_id), rt.validator_client, rt.gateway_tel
        )
    for entry in record["wrong_drafts"]:
        request = state.wrong_requests.get((case_id, entry["id"]))
        if request is None:
            continue
        if rt.fake_validator is not None:
            rt.fake_validator.expect("MISMATCH", request.bundle.legs[0].leg_id)
        entry["validation"] = validate_wrong(
            request, rt.validator_client, rt.eval_tel, case_id=case_id
        )
    state.validated.add(case_id)
    return record


# ── Langfuse task/evaluator adapters ──────────────────────────────────


def _case_id(item: Any) -> str:
    data = item.get("input") if isinstance(item, dict) else getattr(item, "input", None)
    return str((data or {}).get("case_id"))


def _critical_legs(legs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    keys = ("action", "target_type", "target_id", "source_account_id", "amount")
    return [{k: leg.get(k) for k in keys} for leg in legs or []]


def _output_a(record: dict[str, Any]) -> dict[str, Any]:
    agent = record["agent"]
    return {
        "case_id": record["case_id"],
        "status": agent["status"],
        "reason": agent["reason"],
        "error_code": agent["error_code"],
        "class": agent["class"],
        "legs": _critical_legs(agent["legs"]),
        "latency_ms": agent["latency_ms"],
        "policy_decision": (record["policy"] or {}).get("decision"),
    }


def _output_b(record: dict[str, Any]) -> dict[str, Any]:
    natural = record["natural_validation"]
    return {
        "case_id": record["case_id"],
        "natural": None
        if natural is None
        else {
            "label": record["label"],
            "verdict": verdict_of(natural),
            "error_code": natural.get("code"),
        },
        "wrong": [
            {"id": w["id"], "kind": w["kind"], "verdict": verdict_of(w["validation"])}
            for w in record["wrong_drafts"]
            if w.get("validation")
        ],
    }


def _evaluators_a() -> list[Callable[..., Any]]:
    from langfuse import Evaluation

    def outcome_correct(*, output: dict, **_: Any) -> Any:
        ok = output["class"] in ("exact", "correct_clarification")
        return Evaluation(name="outcome_correct", value=1.0 if ok else 0.0, comment=output["class"])

    def critical_field_exact(*, output: dict, expected_output: dict, **_: Any) -> Any:
        if expected_output.get("outcome") != "draft":
            return []
        cmp = compare_draft(expected_output["legs"], output["legs"] or [])
        return Evaluation(name="critical_field_exact", value=1.0 if cmp.exact else 0.0)

    def clarification_correct(*, output: dict, expected_output: dict, **_: Any) -> Any:
        if expected_output.get("outcome") != "clarify":
            return []
        ok = output["class"] == "correct_clarification"
        return Evaluation(name="clarification_correct", value=1.0 if ok else 0.0)

    def unsafe_draft(*, output: dict, **_: Any) -> Any:
        bad = output["class"] in ("incorrect_draft", "drafted_instead_of_clarifying")
        return Evaluation(name="unsafe_draft", value=1.0 if bad else 0.0)

    def agent_latency_ms(*, output: dict, **_: Any) -> Any:
        return Evaluation(name="agent_latency_ms", value=float(output["latency_ms"]))

    return [outcome_correct, critical_field_exact, clarification_correct, unsafe_draft,
            agent_latency_ms]  # fmt: skip


def _evaluators_b() -> list[Callable[..., Any]]:
    from langfuse import Evaluation

    def natural_draft(*, output: dict, **_: Any) -> Any:
        natural = output.get("natural")
        if natural is None:
            return []
        accepted = natural["verdict"] == "MATCH"
        correct = natural["label"] == "correct"
        return [
            Evaluation(name="validator_verdict", value=natural["verdict"], data_type="CATEGORICAL"),
            Evaluation(name="natural_draft_accepted", value=1.0 if accepted else 0.0),
            Evaluation(name="false_reject", value=1.0 if correct and not accepted else 0.0),
            Evaluation(name="false_accept", value=1.0 if not correct and accepted else 0.0),
        ]

    def wrong_drafts(*, output: dict, **_: Any) -> Any:
        wrong = output.get("wrong") or []
        if not wrong:
            return []
        accepted = sum(1 for w in wrong if w["verdict"] == "MATCH")
        return [
            Evaluation(name="wrong_drafts_evaluated", value=float(len(wrong))),
            Evaluation(name="wrong_drafts_accepted", value=float(accepted)),
        ]

    return [natural_draft, wrong_drafts]


def _run_evaluators(state: State, phase: str) -> list[Callable[..., Any]]:
    from langfuse import Evaluation

    def aggregate(**_: Any) -> Any:
        summary = summarize([state.records[c] for c in state.cases if c in state.records])
        if phase == "A":
            s = summary["extraction"]["all"]
            out = [
                Evaluation(name="cases", value=float(s["n"])),
                Evaluation(name="outcome_correct_total", value=float(s["outcome_correct"])),
                Evaluation(name="exact_drafts_total", value=float(s["exact"])),
                Evaluation(name="unsafe_drafts_total", value=float(s["unsafe_drafts"])),
            ]
            if s["agent_latency"]["n"]:
                out.append(
                    Evaluation(name="agent_p50_ms", value=float(s["agent_latency"]["p50_ms"]))
                )
                out.append(
                    Evaluation(name="agent_p95_ms", value=float(s["agent_latency"]["p95_ms"]))
                )
            return out
        v = summary["validator"]["all"]
        wrong = v["wrong"].get("incorrect", {})
        correct = v["natural"].get("correct", {})
        out = [
            Evaluation(name="validator_calls", value=float(v["calls"])),
            Evaluation(name="validator_errors", value=float(v["errors"])),
            Evaluation(name="wrong_drafts", value=float(wrong.get("n", 0))),
            Evaluation(
                name="wrong_drafts_accepted", value=float(wrong.get("accepted_with_validator", 0))
            ),
            Evaluation(name="correct_drafts", value=float(correct.get("n", 0))),
            Evaluation(
                name="correct_drafts_accepted",
                value=float(correct.get("accepted_with_validator", 0)),
            ),
        ]
        if v["latency"]["n"]:
            out.append(Evaluation(name="validator_p50_ms", value=float(v["latency"]["p50_ms"])))
            out.append(Evaluation(name="validator_p95_ms", value=float(v["latency"]["p95_ms"])))
        return out

    return [aggregate]


# ── Orchestration ─────────────────────────────────────────────────────


def _dataset_items(
    cases: list[Case], dataset_name: str, golden_version: str = GOLDEN_VERSION
) -> list[dict[str, Any]]:
    items = []
    for case in cases:
        items.append(
            {
                "id": f"{dataset_name}-{case.id}",
                "input": {
                    "case_id": case.id,
                    "category": case.category,
                    "turns": [t.model_dump(mode="json", exclude_none=True) for t in case.turns],
                },
                "expected_output": case.expected.model_dump(mode="json"),
                "metadata": {
                    "golden_version": golden_version,
                    "category": case.category,
                    "split": case.split,
                    "clarify_is_safe": case.clarify_is_safe,
                    "expected_policy": (
                        case.expected_policy.model_dump(mode="json")
                        if case.expected_policy
                        else None
                    ),
                    "wrong_drafts": [
                        w.model_dump(mode="json", exclude_none=True) for w in case.wrong_drafts
                    ],  # fmt: skip
                    "synthetic": True,
                },
            }
        )
    return items


def _run_phase(
    label: str,
    fn: Callable[[State, Runtime, str], dict[str, Any]],
    output: Callable[[dict[str, Any]], dict[str, Any]],
    state: State,
    rt: Runtime,
    order: list[str],
    lf: LangfuseExperiments | None,
    lf_info: dict[str, Any],
    run_meta: dict[str, str],
    evaluators: Callable[[], list[Callable[..., Any]]],
    phase: str,
) -> None:
    attempted = state.attempted[phase]

    def task(*, item: Any, **_: Any) -> dict[str, Any]:
        case_id = _case_id(item)
        attempted.add(case_id)
        try:
            record = fn(state, rt, case_id)
        except Exception as exc:  # noqa: BLE001 - recorded, the run continues
            print(f"  [{label}] {case_id}: harness error {type(exc).__name__}: {exc}")
            raise
        _progress(label, record)
        return output(record)

    if lf is not None:
        try:
            result = lf.run(
                name=label if rt.mode == "live" else f"DRYRUN fake models {label}",
                run_name=f"{label} {run_meta['golden_version']} {run_meta['run_id']}",
                description=(
                    f"T-14 {label}: {run_meta['golden_version']}, {rt.mode} models, "
                    "same captured drafts"
                ),
                items=lf.items_for(order),
                task=task,
                evaluators=evaluators(),
                run_evaluators=_run_evaluators(state, phase),
                metadata={**run_meta, "condition": label},
            )
            lf_info["runs"].append(result)
        except Exception as exc:  # noqa: BLE001 - fall back to local completion
            lf_info["errors"].append(f"run_experiment({label}): {error_text(exc)}")
    for case_id in order:  # local completion of anything the experiment did not start
        if case_id not in attempted:
            attempted.add(case_id)
            try:
                _progress(label, fn(state, rt, case_id))
            except Exception as exc:  # noqa: BLE001
                print(f"  [{label}] {case_id}: harness error {type(exc).__name__}: {exc}")


def _progress(label: str, record: dict[str, Any]) -> None:
    agent = record["agent"]
    extra = ""
    if record.get("natural_validation") is not None and label != "extractor-only":
        extra = f" natural={verdict_of(record['natural_validation'])}"
        wrong = [verdict_of(w["validation"]) for w in record["wrong_drafts"] if w["validation"]]
        if wrong:
            extra += f" wrong={wrong}"
    detail = agent["reason"] or agent["error_code"] or ""
    print(
        f"  [{label}] {record['case_id']}: {agent['class']} {detail} "
        f"({agent['latency_ms']} ms){extra}",
        flush=True,
    )


def _failures(records: list[dict[str, Any]]) -> list[dict[str, str]]:
    out = []
    for r in records:
        agent = r["agent"]
        cls = agent["class"]
        if cls not in ("exact", "correct_clarification"):
            if cls in ("incorrect_draft", "drafted_instead_of_clarifying"):
                legs = [
                    f"{leg['action']} {leg['target_id']} from {leg['source_account_id']} "
                    f"{leg['amount']}"
                    for leg in agent["legs"] or []
                ]
                diff = agent["comparison"]["field_errors"] if agent["comparison"] else []
                detail = f"drafted {legs}; field differences {diff}"
            elif "clarification" in cls:
                detail = f"asked ({agent['reason']}): {agent['question']}"
            else:
                detail = f"agent error {agent['error_code']}"
            out.append({"case_id": r["case_id"], "kind": f"extraction: {cls}", "detail": detail})
        natural = r.get("natural_validation")
        if natural is not None:
            verdict = verdict_of(natural)
            if (r["label"] == "correct") != (verdict == "MATCH"):
                issues = [
                    f"{i.get('field')}@{i.get('leg_id')}: {i.get('reason')}"
                    for i in natural.get("issues") or []
                ]
                out.append(
                    {
                        "case_id": r["case_id"],
                        "kind": f"validator on natural {r['label']} draft: {verdict}",
                        "detail": f"code={natural.get('code')} issues={issues}",
                    }
                )
        for w in r["wrong_drafts"]:
            if w.get("validation") and verdict_of(w["validation"]) == "MATCH":
                out.append(
                    {
                        "case_id": r["case_id"],
                        "kind": "validator false accept (wrong draft)",
                        "detail": f"{w['id']} ({w['kind']}) returned MATCH",
                    }
                )
        if r.get("policy_expectation_met") is False:
            out.append(
                {
                    "case_id": r["case_id"],
                    "kind": "policy expectation not met",
                    "detail": f"expected {r['policy_expected']}, observed {r['policy']}",
                }
            )
    return out


def _git_head() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=5
        )
        return out.stdout.strip() or None
    except Exception:  # noqa: BLE001
        return None


def _read_back(tel: Telemetry, state: State) -> list[dict[str, Any]]:
    """Read one agent trace and one validation trace back from Langfuse (v2 API)."""
    picks: list[tuple[str, str]] = []
    for r in state.records.values():
        if r["agent"].get("trace_id") and r["agent"]["status"] == "draft_frozen":
            picks.append(("agent run + extractor generation", r["agent"]["trace_id"]))
            break
    for r in state.records.values():
        nv = r.get("natural_validation")
        if nv and nv.get("trace_id"):
            picks.append(("gateway validation + validator generation", nv["trace_id"]))
            break
    out = []
    for what, trace_id in picks:
        back = tel.read_back(trace_id, wait_s=90, min_count=3)
        out.append(
            {
                "what": what,
                "trace_id": trace_id,
                "url": tel.trace_url(trace_id),
                "observations": len(back.observations),
                "names": sorted({o.get("name") or "" for o in back.observations}),
                "error": back.error,
            }
        )
    return out


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m evals.run", description=__doc__.split("\n")[0])
    parser.add_argument("--fake", action="store_true", help="fake models, no network")
    parser.add_argument("--no-langfuse", action="store_true", help="skip dataset/experiments")
    parser.add_argument("--case", action="append", default=[], help="run only these case ids")
    parser.add_argument("--out", type=Path, default=None, help="results directory")
    parser.add_argument("--trial", type=int, default=1, help="trial number (run naming)")
    parser.add_argument(
        "--suite", choices=(GOLDEN_VERSION, "phase2-dev-v1"), default=GOLDEN_VERSION,
        help="original baseline or separately reported development-exposed follow-ups",
    )
    parser.add_argument(
        "--langfuse-dry-run",
        action="store_true",
        help="with --fake: export to Langfuse under a separate '-dryrun' dataset (plumbing check)",
    )
    args = parser.parse_args(argv)
    if args.langfuse_dry_run and not args.fake:
        parser.error("--langfuse-dry-run requires --fake")

    warnings.filterwarnings("ignore", message="You should not use the 'timeout' argument")
    cases_path = (
        CASES_PATH if args.suite == GOLDEN_VERSION else CASES_PATH.with_name("phase2-cases.jsonl")
    )
    all_cases = load_cases(cases_path)
    cases = all_cases
    problems = coverage_problems(cases) if args.suite == GOLDEN_VERSION else []
    if args.case:
        unknown = set(args.case) - {c.id for c in cases}
        if unknown:
            print(f"unknown case ids: {sorted(unknown)}")
            return 2
        cases = [c for c in cases if c.id in set(args.case)]

    started = datetime.now(UTC)
    mode = "fake" if args.fake else "live"
    run_id = f"{started.strftime('%Y%m%dT%H%M%S.%fZ')}-{mode}-t{args.trial}"
    out_dir = args.out or (RESULTS_DIR / run_id)
    if out_dir.exists() and any(out_dir.iterdir()):
        print(
            "[BLOCKER] output directory is not empty; historical results will not be overwritten."
        )
        return 2
    out_dir.mkdir(parents=True, exist_ok=True)
    use_langfuse = args.langfuse_dry_run or (not args.fake and not args.no_langfuse)
    dataset_name = f"dcta-{args.suite}" + ("-dryrun" if args.fake else "")
    rt = build_runtime(args.fake, langfuse=use_langfuse)
    if isinstance(rt, str):
        print(f"[BLOCKER] {rt}; nothing was run.")
        return 2

    print(f"T-14 eval {run_id}: {len(cases)} cases, mode={mode}")
    if problems:
        print(f"  coverage gaps: {problems}")
    state = State(cases={c.id: c for c in cases})
    order = [c.id for c in cases]
    run_meta = {
        "run_id": run_id,
        "golden_version": args.suite,
        "cases_sha256": file_sha256(cases_path),
        "mode": mode,
        "trial": str(args.trial),
        "extract_model": rt.meta["extractor"]["model"],
        "extract_prompt_sha256": rt.meta["extractor"]["prompt_sha256"],
        "extract_reasoning_effort": rt.meta["extractor"]["reasoning_effort"] or "unset",
        "extract_max_tokens": str(rt.meta["extractor"]["max_tokens"]),
        "validate_model": rt.meta["validator"]["model"],
        "validator_prompt_hash": rt.meta["validator"]["prompt_hash"],
    }

    lf_info: dict[str, Any] = {"runs": [], "errors": []}
    lf = None
    if use_langfuse:
        lf = LangfuseExperiments.from_telemetry(rt.eval_tel)
        if lf is None:
            lf_info["errors"].append(f"Langfuse disabled: {rt.eval_tel.health()['last_error']}")
        else:
            try:
                lf_info["dataset"] = lf.publish(
                    dataset_name,
                    description=f"DCTA T-14 golden set {args.suite} (synthetic data only)"
                    + (" - DRY RUN with fake models, not results" if args.fake else ""),
                    metadata={
                        "golden_version": GOLDEN_VERSION,
                        "cases_sha256": run_meta["cases_sha256"],
                    },  # fmt: skip
                    items=_dataset_items(all_cases, dataset_name, args.suite),
                )
            except Exception as exc:  # noqa: BLE001
                lf_info["errors"].append(f"publish dataset: {error_text(exc)}")
                lf = None

    t0 = time.monotonic()
    try:
        _run_phase("extractor-only", phase_a, _output_a, state, rt, order, lf, lf_info,
                   run_meta, _evaluators_a, "A")  # fmt: skip
        order_b = [c for c in order if c in state.records]
        _run_phase("extractor+validator", phase_b, _output_b, state, rt, order_b, lf, lf_info,
                   run_meta, _evaluators_b, "B")  # fmt: skip
        if lf is not None:
            lf.flush()
        rt.eval_tel.flush()
        if use_langfuse and rt.eval_tel.enabled:
            lf_info["read_back"] = _read_back(rt.eval_tel, state)
        telemetry_health = rt.eval_tel.health()
    finally:
        rt.close()
        for env in state.envs.values():
            discard(env)

    records = [state.records[c] for c in order if c in state.records]
    finished = datetime.now(UTC)
    results = {
        "meta": {
            "run_id": run_id,
            "mode": mode,
            "trial": args.trial,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": finished.isoformat(timespec="seconds"),
            "wall_s": round(time.monotonic() - t0, 1),
            "git_head": _git_head(),
            "golden_version": args.suite,
            "cases_sha256": run_meta["cases_sha256"],
            "cases_run": len(records),
            "cases_selected": len(order),
            "coverage_gaps": problems,
            "cost": "unavailable (no model price configured)",
            "telemetry_health": telemetry_health,
            **rt.meta,
        },
        "summary": summarize(records),
        "failures": _failures(records),
        "langfuse": lf_info if use_langfuse else {},
        "records": records,
    }
    with (out_dir / "results.json").open("x") as output:
        output.write(json.dumps(results, indent=2, ensure_ascii=False))
    with (out_dir / "report.md").open("x") as output:
        output.write(render_markdown(results))
    s = results["summary"]["extraction"]["all"]
    v = results["summary"]["validator"]["all"]
    print(
        f"done in {results['meta']['wall_s']} s: outcome correct {s['outcome_correct']}/{s['n']}, "
        f"exact {s['exact']}/{s['draft_expected']}, unsafe drafts {s['unsafe_drafts']}, "
        f"validator calls {v['calls']} (errors {v['errors']})"
    )
    for err in lf_info["errors"]:
        print(f"  langfuse error: {err}")
    for run in lf_info["runs"]:
        print(f"  langfuse run: {run['run_name']} {run.get('dataset_run_url')}")
    print(f"results: {out_dir / 'results.json'}")
    print(f"report:  {out_dir / 'report.md'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
