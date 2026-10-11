"""Operator-supplied environment settings; never supplied or edited by an agent."""

from pathlib import Path, PurePosixPath
import re
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class ComposeProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(pattern=r"^[a-z][a-z0-9-]{0,40}$")
    compose_file: str
    version_file: str
    test_path: str
    loadtest_service: str
    loadtest_url: str
    database_service: str
    cache_service: str
    api_service: str
    api_target_port: int = Field(ge=1, le=65535)
    prometheus_service: str
    prometheus_target_port: int = Field(ge=1, le=65535)
    frontend_service: str
    frontend_port: int = Field(ge=1024, le=65535)
    environment_allowlist: dict[str, list[str]]
    environment_overrides: dict[str, dict[str, str]]
    compose_variables: dict[str, str] = Field(default_factory=dict)
    log_targets: list[str] = Field(default_factory=list)

    @field_validator("compose_file", "version_file", "test_path")
    @classmethod
    def relative_path(cls, value: str) -> str:
        path = PurePosixPath(value)
        if not value or path.is_absolute() or ".." in path.parts or "\\" in value or ":" in value:
            raise ValueError("Profile paths must be relative to the company checkout")
        return value

    @field_validator("loadtest_service", "database_service", "cache_service", "api_service",
                     "prometheus_service", "frontend_service")
    @classmethod
    def service_name(cls, value: str) -> str:
        if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", value):
            raise ValueError("Invalid Compose service name")
        return value

    @model_validator(mode="after")
    def internal_loadtest_url(self):
        url = urlsplit(self.loadtest_url)
        if url.scheme != "http" or url.hostname not in {self.frontend_service, self.api_service} or url.username:
            raise ValueError("Load tests must target a sandbox service")
        return self


DEFAULT_PROFILE = Path(__file__).parent / "profiles" / "driftwood.json"


def load_profile(path: Path | None = None) -> ComposeProfile:
    return ComposeProfile.model_validate_json((path or DEFAULT_PROFILE).read_text(encoding="utf-8"))
