"""T-13 attack matrix: Rogue Agent Mode against the real gateway.

One test per matrix row (``tests/t13_support.py::ATTACKS``): a malicious extractor holding
the agent's real delegation token and a validator forced to MATCH, against the real
routes, passkey verifier, policy, step-up, commit and audit chain. Each row asserts the
expected HTTP status and stable code and that money moved only where one legitimate
commit was expected. ``scripts/rogue_agent_demo.py`` prints the same rows as a table.

The remaining tests pin the fix made during T-13 (word-fragment citations), check that
the harness really uses the production controls, and check that Rogue Agent Mode cannot
be reached from a running gateway or a built image.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from gateway.app import app
from tests.t13_support import (
    ALSO_COVERED,
    ATTACKS,
    MUM,
    SAVINGS,
    Attack,
    Harness,
    Leg,
    bill,
    observe,
    run_attack,
    send,
)

pytestmark = pytest.mark.filterwarnings("ignore:You should not use the 'timeout' argument")

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("attack", ATTACKS, ids=[a.id for a in ATTACKS])
def test_attack_is_stopped_by_its_control(attack: Attack) -> None:
    result = run_attack(attack)
    assert result.error is None, f"{attack.id}: {result.error}"
    assert result.outcome is not None
    assert result.outcome.key == attack.want, f"{attack.id}: observed {result.outcome}"
    assert result.effect == attack.effect, f"{attack.id}: ledger effect {result.effect}"


def test_matrix_covers_every_requested_attack_once() -> None:
    ids = [a.id for a in ATTACKS]
    assert len(ids) == len(set(ids))
    groups = {a.id.split("-")[0] for a in ATTACKS}
    assert groups == {"EX", "AP", "SU", "LM", "CC", "AU", "ISO"}
    # Rows whose money effect is a single legitimate commit (retries and races).
    assert {a.id for a in ATTACKS if a.effect == "one"} == {"SU-3", "CC-1", "CC-2", "CC-3"}
    # Rows that reach the confirmation screen say so, and what still protects the user.
    assert {a.id for a in ATTACKS if a.residual} == {"EX-2", "EX-8", "AU-1"}


def test_cross_references_name_existing_tests() -> None:
    for attack_id, refs in ALSO_COVERED.items():
        assert attack_id in {a.id for a in ATTACKS}
        for module, name in re.findall(r"(test_\w+)::(test_\w+)", refs):
            source = (ROOT / "tests" / f"{module}.py").read_text(encoding="utf-8")
            assert re.search(rf"def {name}\b", source), f"{attack_id}: {module}::{name}"


# ── Regression: word fragments are not citations (found by EX-14) ───


@pytest.mark.parametrize(
    ("text", "leg"),
    [
        # "Mum" cut out of "Mumbai": the user named Ah Ma.
        ("Send 50 to Ah Ma before she flies to Mumbai, from my savings",
         send("50.00", "50", MUM, "Mum")),
        # "900" cut out of a bill number, with no reference declared.
        ("Pay SP Group 50 from savings, bill INV9001", bill("900.00", "900")),
        # "current" cut out of "currently" as the source account.
        ("I am currently abroad, send 50 to Mum from savings",
         send("50.00", "50", MUM, "Mum", source_id="acct-matthew-current",
              source_words="current")),
        # "50" cut out of "1500".
        ("Send 1500 to Mum from my savings", send("50.00", "50", MUM, "Mum", amount_occ=0)),
    ],
    ids=["target-in-Mumbai", "amount-in-INV9001", "source-in-currently", "amount-in-1500"],
)  # fmt: skip
def test_word_fragments_are_not_citations(text: str, leg: Leg) -> None:
    with Harness.open() as h:
        h.mark()
        h.propose(text, leg)
        assert observe(h.rogue.freezes[-1]).key == (400, "EVIDENCE_SPAN_MISMATCH")
        assert h.count("frozen_drafts") == 0
        assert h.effect() == "none"


@pytest.mark.parametrize(
    ("text", "leg"),
    [
        ("Send $50 to Mum's account from my savings", send("50.00", "$50", MUM, "Mum")),
        ("Send 50 to Mum (DBS 9988) from my savings.", send("50.00", "50", MUM, "9988")),
        ("Pay S$50 to SP Group from savings", bill("50.00", "$50")),
    ],
    ids=["dollar-and-possessive", "suffix-in-brackets", "S$-prefix"],
)
def test_whole_words_next_to_punctuation_still_freeze(text: str, leg: Leg) -> None:
    with Harness.open() as h:
        _, ready = h.ready(text, leg)
        assert ready["legs"][0]["amount"] == "50.00"


# ── The harness is real, and unreachable from a deployment ──────────


def test_harness_uses_production_controls_and_a_real_session() -> None:
    with Harness.open() as h:
        h.assert_real_controls()
        anonymous = h.rogue.gateway.get("/api/auth/me")
        assert anonymous.status_code == 401
        assert h.matthew.get("/api/auth/me").json()["user_id"] == "user-matthew"
        assert h.ana.get("/api/auth/me").json()["user_id"] == "user-ana"
        # The happy path through the same harness commits exactly once.
        h.mark()
        body = h.pay("Send 50 to Mum from my savings", send("50.00", "50", MUM, "Mum"))
        assert body["receipt"]["total_debit"] == "50.00"
        assert h.effect() == "one"
        assert h.chain_ok()
        assert h.ledger().balances[SAVINGS] == 1_195_000


def _route_paths(routes: list) -> list[str]:
    """Every path the app serves, including routes of included routers."""
    out: list[str] = []
    for route in routes:
        if hasattr(route, "path"):
            out.append(route.path)
        if hasattr(route, "original_router"):
            out += _route_paths(route.original_router.routes)
    return out


def test_rogue_mode_has_no_route_header_or_env_switch() -> None:
    paths = sorted(_route_paths(app.routes))
    assert "/api/authorizations/{authz_id}/approve" in paths
    assert not [p for p in paths if re.search(r"rogue|attack|debug|test", p, re.IGNORECASE)]
    for path in sorted((ROOT / "gateway").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        assert not re.search(r"rogue|AlwaysMatch|t13_support", source, re.IGNORECASE), path
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(("tests", "scripts")), path
            elif isinstance(node, ast.Import):
                assert not any(a.name.startswith(("tests", "scripts")) for a in node.names), path


def test_images_never_contain_the_harness() -> None:
    dockerfile = (ROOT / "infra" / "Dockerfile").read_text(encoding="utf-8")
    sources = []
    for line in dockerfile.splitlines():
        if line.startswith(("COPY ", "ADD ")):
            args = [a for a in line.split()[1:] if not a.startswith("--")]
            sources += args[:-1]  # the last argument is the destination
    assert "gateway/" in sources and "agent/" in sources and "validator/" in sources
    assert not [s for s in sources if s.rstrip("/") in {"tests", "scripts", "", "."}]


def test_demo_script_renders_the_table_and_the_evidence() -> None:
    import io

    from scripts.rogue_agent_demo import render_markdown, render_table, run

    results, elapsed = run(["EX-14", "CC-2", "ISO-1"])
    screen = io.StringIO()
    render_table(results, elapsed, screen, color=False)
    text = screen.getvalue()
    assert "400 EVIDENCE_SPAN_MISMATCH" in text and "1 commit" in text
    assert "3/3 controls held" in text and "\033[" not in text
    markdown = render_markdown(results, elapsed)
    assert "| EX-14 | Cites 'Mum' inside 'Mumbai' |" in markdown
    assert "## Out of scope" in markdown and "not a proof" in markdown
