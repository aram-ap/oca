"""Environment-backed settings for the platform. Only ``OCA_*`` keys are read."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping, MutableMapping

from oca.schemas import Role

PREFIX = "OCA_"
TOKEN_ROLES = (Role.investigator, Role.engineer, Role.guardian, Role.orchestrator,
               Role.health, Role.jira, Role.slack)


def load_dotenv(path: str | os.PathLike = ".env",
                into: MutableMapping[str, str] | None = None) -> dict[str, str]:
    """Apply ``OCA_*`` lines from a dotenv file with setdefault semantics.

    Anything without the prefix (for example driftwood's compose variables in a
    shared ``.env``) is ignored. Returns the keys that were applied.
    """
    target = os.environ if into is None else into
    applied: dict[str, str] = {}
    try:
        text = Path(path).read_text(encoding="utf-8")
    except (FileNotFoundError, NotADirectoryError):
        return applied
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key.startswith(PREFIX) and key not in target:
            target[key] = value
            applied[key] = value
    return applied


def _bool(value: str) -> bool:
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _service_urls(value: str) -> dict[str, str]:
    urls = {}
    for item in value.split(","):
        if "=" in item:
            name, _, url = item.partition("=")
            urls[name.strip()] = url.strip().rstrip("/")
    return urls


@dataclass(frozen=True)
class Settings:
    db_path: Path = Path("data/oca.db")
    data_dir: Path = Path("data")
    fakeco_root: Path = Path("../driftwood")
    runner: str = "dry"
    port: int = 8001
    api_url: str = "http://localhost:8001"
    prom_url: str = "http://localhost:9090"
    sandbox_prom_url: str = "http://localhost:19090"
    sandbox_api_url: str = "http://localhost:18000"
    service_urls: dict[str, str] = field(default_factory=lambda: {
        "orders-api": "http://localhost:8000", "frontend": "http://localhost:8080"})
    ollama_url: str = "http://localhost:11434"
    model: str = "nemotron3:33b"
    llm_timeout_s: float = 120.0
    model_keep_alive: str = "5m"
    tool_timeout_s: float = 30.0
    hardware: str = "ASUS Ascent GX10"
    tokens: dict[Role, str] = field(default_factory=lambda: {
        Role.investigator: "agent-investigator", Role.engineer: "agent-engineer",
        Role.guardian: "agent-guardian", Role.orchestrator: "orchestrator",
        Role.health: "health", Role.jira: "jira", Role.slack: "slack"})
    admin_user: str = "admin"
    admin_password: str = "admin"
    secret_key: str = "dev-secret-change-me"
    require_human_approval: bool = True
    risk_threshold: int = 30
    check_interval_s: int = 10
    scheduler_tick_s: float = 2.0
    stage_delay_s: float = 3.0
    verify_runs: int = 3
    verify_interval_s: float = 10.0
    max_attempts: int = 2
    loadtest_requests: int = 2000
    autostart_pipeline: bool = True
    allowlist_path: Path = Path(__file__).with_name("allowlist.yaml")

    def __post_init__(self) -> None:
        if self.runner not in {"real", "dry"}:
            raise ValueError("OCA_RUNNER must be 'real' or 'dry'")
        if len(set(self.tokens.values())) != len(self.tokens) or not all(self.tokens.values()):
            raise ValueError("Every OCA_*_TOKEN must be set and distinct")
        if self.risk_threshold < 0 or self.risk_threshold > 100:
            raise ValueError("OCA_RISK_THRESHOLD must be 0-100")

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None, dotenv: str | None = ".env") -> "Settings":
        source: MutableMapping[str, str] = dict(os.environ if env is None else env)
        if dotenv:
            load_dotenv(dotenv, into=source)

        def get(key: str, default: str) -> str:
            return source.get(PREFIX + key, default)

        defaults = cls()
        tokens = {role: get(f"{role.value.upper()}_TOKEN", defaults.tokens[role]) for role in TOKEN_ROLES}
        return cls(
            db_path=Path(get("DB_PATH", str(defaults.db_path))),
            data_dir=Path(get("DATA_DIR", str(defaults.data_dir))),
            fakeco_root=Path(get("FAKECO_ROOT", str(defaults.fakeco_root))).expanduser(),
            runner=get("RUNNER", defaults.runner),
            port=int(get("PORT", str(defaults.port))),
            api_url=get("API_URL", defaults.api_url).rstrip("/"),
            prom_url=get("PROM_URL", defaults.prom_url).rstrip("/"),
            sandbox_prom_url=get("SANDBOX_PROM_URL", defaults.sandbox_prom_url).rstrip("/"),
            sandbox_api_url=get("SANDBOX_API_URL", defaults.sandbox_api_url).rstrip("/"),
            service_urls=_service_urls(get("SERVICE_URLS", "orders-api=http://localhost:8000,frontend=http://localhost:8080")),
            ollama_url=get("OLLAMA_URL", defaults.ollama_url).rstrip("/"),
            model=get("MODEL", defaults.model),
            llm_timeout_s=float(get("LLM_TIMEOUT_S", str(defaults.llm_timeout_s))),
            model_keep_alive=get("MODEL_KEEP_ALIVE", defaults.model_keep_alive),
            tool_timeout_s=float(get("TOOL_TIMEOUT_S", str(defaults.tool_timeout_s))),
            hardware=get("HARDWARE", defaults.hardware),
            tokens=tokens,
            admin_user=get("ADMIN_USER", defaults.admin_user),
            admin_password=get("ADMIN_PASSWORD", defaults.admin_password),
            secret_key=get("SECRET_KEY", defaults.secret_key),
            require_human_approval=_bool(get("REQUIRE_HUMAN_APPROVAL", "true")),
            risk_threshold=int(get("RISK_THRESHOLD", str(defaults.risk_threshold))),
            check_interval_s=int(get("CHECK_INTERVAL_S", str(defaults.check_interval_s))),
            scheduler_tick_s=float(get("SCHEDULER_TICK_S", str(defaults.scheduler_tick_s))),
            stage_delay_s=float(get("STAGE_DELAY_S", str(defaults.stage_delay_s))),
            verify_runs=int(get("VERIFY_RUNS", str(defaults.verify_runs))),
            verify_interval_s=float(get("VERIFY_INTERVAL_S", str(defaults.verify_interval_s))),
            max_attempts=int(get("MAX_ATTEMPTS", str(defaults.max_attempts))),
            loadtest_requests=int(get("LOADTEST_REQUESTS", str(defaults.loadtest_requests))),
            autostart_pipeline=_bool(get("AUTOSTART_PIPELINE", "true")),
            allowlist_path=Path(get("ALLOWLIST_PATH", str(defaults.allowlist_path))),
        )

    def role_for_token(self, token: str) -> Role | None:
        for role, value in self.tokens.items():
            if value == token:
                return role
        return None
