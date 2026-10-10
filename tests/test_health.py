"""Health endpoint tests for all three DCTA services.

Each service must respond to GET /health with a JSON body containing
"service" and "status" keys. No secrets should appear in the response.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from agent.app import app as agent_app
from gateway.app import app as gateway_app
from validator.app import app as validator_app


class TestAgentHealth:
    def test_returns_ok(self) -> None:
        client = TestClient(agent_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "agent"
        assert body["status"] == "ok"

    def test_reports_llm_status(self) -> None:
        client = TestClient(agent_app)
        resp = client.get("/health")
        body = resp.json()
        assert body["llm"] in ("configured", "missing-credentials")

    def test_reports_langfuse_status(self) -> None:
        client = TestClient(agent_app)
        resp = client.get("/health")
        body = resp.json()
        assert body["langfuse"] in ("configured", "missing-credentials")

    def test_no_secrets_in_response(self) -> None:
        client = TestClient(agent_app)
        resp = client.get("/health")
        text = resp.text
        # No secret-like values should appear
        assert "REDACTED" not in text
        assert "Bearer " not in text
        assert "sk-" not in text


class TestValidatorHealth:
    def test_returns_ok(self) -> None:
        client = TestClient(validator_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "validator"
        assert body["status"] == "ok"

    def test_reports_llm_status(self) -> None:
        client = TestClient(validator_app)
        resp = client.get("/health")
        body = resp.json()
        assert body["llm"] in ("configured", "missing-credentials")

    def test_no_secrets_in_response(self) -> None:
        client = TestClient(validator_app)
        resp = client.get("/health")
        text = resp.text
        assert "sk-" not in text


class TestGatewayHealth:
    def test_returns_ok(self) -> None:
        client = TestClient(gateway_app)
        resp = client.get("/health")
        assert resp.status_code == 200
        body = resp.json()
        assert body["service"] == "gateway"
        assert body["status"] == "ok"

    def test_reports_trtc_status(self) -> None:
        client = TestClient(gateway_app)
        resp = client.get("/health")
        body = resp.json()
        assert body["trtc"] in ("configured", "missing-credentials")

    def test_reports_langfuse_status(self) -> None:
        client = TestClient(gateway_app)
        resp = client.get("/health")
        body = resp.json()
        assert body["langfuse"] in ("configured", "missing-credentials")

    def test_no_secrets_in_response(self) -> None:
        client = TestClient(gateway_app)
        resp = client.get("/health")
        text = resp.text
        assert "REDACTED" not in text
        assert "Bearer " not in text
        assert "sk-" not in text
