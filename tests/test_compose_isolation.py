"""Test infra/compose.yaml for T-04 isolation properties.

Asserts that agent and validator services:
- have no volumes or bind mounts
- have no published ports
- have read_only: true
- are not on each other's network
- have cap_drop: [ALL]
- have security_opt: [no-new-privileges:true]

Also asserts .dockerignore contains .env.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_REPO_ROOT = Path(__file__).resolve().parent.parent
_COMPOSE_PATH = _REPO_ROOT / "infra" / "compose.yaml"
_DOCKERIGNORE = _REPO_ROOT / ".dockerignore"


def _load_compose():
    """Load compose.yaml.  Use yaml if available; else skip with reason."""
    try:
        import yaml  # type: ignore[import-not-found]
    except ImportError:
        pytest.skip("PyYAML not installed; compose YAML assertions skipped")
    text = _COMPOSE_PATH.read_text(encoding="utf-8")
    return yaml.safe_load(text)


def _get_service(compose: dict, name: str) -> dict:
    services = compose.get("services", {})
    assert name in services, f"service '{name}' not found in compose"
    return services[name]


def _network_names(service: dict) -> set[str]:
    """Extract network names from a service definition."""
    nets = service.get("networks", [])
    if isinstance(nets, list):
        return set(nets)
    if isinstance(nets, dict):
        return set(nets.keys())
    return set()


class TestComposeIsolation:
    """Verify agent/validator are isolated per T-04 requirements."""

    def test_compose_file_exists(self) -> None:
        assert _COMPOSE_PATH.exists(), "infra/compose.yaml must exist"

    def test_agent_no_volumes(self) -> None:
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        assert "volumes" not in agent, "agent must not have volumes"

    def test_validator_no_volumes(self) -> None:
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        assert "volumes" not in validator, "validator must not have volumes"

    def test_agent_no_published_ports(self) -> None:
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        assert "ports" not in agent, "agent must not publish ports"

    def test_validator_no_published_ports(self) -> None:
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        assert "ports" not in validator, "validator must not publish ports"

    def test_agent_read_only(self) -> None:
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        assert agent.get("read_only") is True, "agent must have read_only: true"

    def test_validator_read_only(self) -> None:
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        assert validator.get("read_only") is True, "validator must have read_only: true"

    def test_agent_cap_drop_all(self) -> None:
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        cap_drop = agent.get("cap_drop")
        assert cap_drop == ["ALL"], f"agent cap_drop must be [ALL], got {cap_drop}"

    def test_validator_cap_drop_all(self) -> None:
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        cap_drop = validator.get("cap_drop")
        assert cap_drop == ["ALL"], f"validator cap_drop must be [ALL], got {cap_drop}"

    def test_agent_no_new_privileges(self) -> None:
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        sec = agent.get("security_opt", [])
        assert "no-new-privileges:true" in sec, (
            f"agent security_opt must include no-new-privileges:true, got {sec}"
        )

    def test_validator_no_new_privileges(self) -> None:
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        sec = validator.get("security_opt", [])
        assert "no-new-privileges:true" in sec, (
            f"validator security_opt must include no-new-privileges:true, got {sec}"
        )

    def test_agent_not_on_validator_network(self) -> None:
        """Agent must not be on validator_net."""
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        nets = _network_names(agent)
        assert "validator_net" not in nets, "agent must not be on validator_net"

    def test_validator_not_on_agent_network(self) -> None:
        """Validator must not be on agent_net."""
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        nets = _network_names(validator)
        assert "agent_net" not in nets, "validator must not be on agent_net"

    def test_agent_tmpfs_only_tmp(self) -> None:
        """Agent tmpfs should only be /tmp (no arbitrary writable paths)."""
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        tmpfs = agent.get("tmpfs", [])
        for entry in tmpfs:
            path = entry.split(":")[0] if isinstance(entry, str) else ""
            assert path == "/tmp", f"agent tmpfs should only be /tmp, found: {path}"

    def test_validator_tmpfs_only_tmp(self) -> None:
        """Validator tmpfs should only be /tmp."""
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        tmpfs = validator.get("tmpfs", [])
        for entry in tmpfs:
            path = entry.split(":")[0] if isinstance(entry, str) else ""
            assert path == "/tmp", f"validator tmpfs should only be /tmp, found: {path}"

    def test_gateway_publishes_localhost_only(self) -> None:
        """Gateway ports must bind to 127.0.0.1 only."""
        compose = _load_compose()
        gateway = _get_service(compose, "gateway")
        ports = gateway.get("ports", [])
        for p in ports:
            assert "127.0.0.1" in str(p), f"gateway port must bind to 127.0.0.1, got: {p}"

    def test_gateway_has_volume(self) -> None:
        """Gateway must have the gateway_data volume."""
        compose = _load_compose()
        gateway = _get_service(compose, "gateway")
        volumes = gateway.get("volumes", [])
        assert any("gateway_data" in str(v) for v in volumes), (
            f"gateway must mount gateway_data, got: {volumes}"
        )

    def test_agent_no_docker_socket(self) -> None:
        """Agent must not mount docker.sock (no volumes at all)."""
        compose = _load_compose()
        agent = _get_service(compose, "agent")
        assert "volumes" not in agent, "agent has no volumes at all"

    def test_validator_no_docker_socket(self) -> None:
        """Validator must not mount docker.sock (no volumes at all)."""
        compose = _load_compose()
        validator = _get_service(compose, "validator")
        assert "volumes" not in validator, "validator has no volumes at all"

    def test_dockerignore_contains_env(self) -> None:
        """.dockerignore must contain .env."""
        text = _DOCKERIGNORE.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines()]
        assert ".env" in lines, ".dockerignore must contain .env"

    def test_dockerignore_contains_env_glob(self) -> None:
        """.dockerignore must contain .env.* to catch variants."""
        text = _DOCKERIGNORE.read_text(encoding="utf-8")
        lines = [line.strip() for line in text.splitlines()]
        assert ".env.*" in lines, ".dockerignore must contain .env.*"

    def test_dockerignore_excludes_data_and_docs(self) -> None:
        """.dockerignore must exclude data/, docs/, references/, __pycache__."""
        text = _DOCKERIGNORE.read_text(encoding="utf-8")
        lines = {line.strip() for line in text.splitlines()}
        required = {"data/", "docs/", "references/", "**/__pycache__/"}
        missing = required - lines
        assert not missing, f".dockerignore missing entries: {missing}"


def _env_names(service: dict) -> set[str]:
    env = service.get("environment", [])
    if isinstance(env, dict):
        return set(env)
    return {item.split("=", 1)[0] for item in env}


GATEWAY_ONLY_SECRETS = {
    "SDKSecretKey",
    "SDKAppID",
    "GATEWAY_DELEGATION_SECRET",
    "DCTA_DB_PATH",
    "OPERATOR_TOKEN",
    "STEP_UP_SECRET",
}


@pytest.mark.parametrize(
    ("service", "allowed_llm_key", "forbidden_llm_key"),
    [("agent", "tokenhub_key", "deepseek_key"), ("validator", "deepseek_key", "tokenhub_key")],
)
def test_service_gets_only_its_own_secrets(
    service: str, allowed_llm_key: str, forbidden_llm_key: str
) -> None:
    names = _env_names(_get_service(_load_compose(), service))
    assert allowed_llm_key in names
    assert forbidden_llm_key not in names, f"{service} must not receive {forbidden_llm_key}"
    assert not names & GATEWAY_ONLY_SECRETS, f"{service} must not receive gateway secrets"


def test_gateway_gets_no_llm_keys() -> None:
    names = _env_names(_get_service(_load_compose(), "gateway"))
    assert not names & {"tokenhub_key", "deepseek_key"}


def test_validator_service_token_only_for_gateway_and_validator() -> None:
    """T-10: the gateway->validator secret reaches exactly those two services."""
    compose = _load_compose()
    for service in ("gateway", "validator"):
        assert "VALIDATOR_SERVICE_TOKEN" in _env_names(_get_service(compose, service)), service
    for name, service in compose.get("services", {}).items():
        if name not in {"gateway", "validator"}:
            assert "VALIDATOR_SERVICE_TOKEN" not in _env_names(service), name
