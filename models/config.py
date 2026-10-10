"""Environment-backed configuration for the local model service."""

import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


@dataclass(frozen=True)
class ModelConfig:
    model: str = "nemotron3:33b"
    ollama_url: str = "http://localhost:11434"
    timeout_s: float = 120.0
    keep_alive: str = "5m"

    def __post_init__(self) -> None:
        url = urlsplit(self.ollama_url)
        if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username or url.password or url.path not in {"", "/"}
                or url.query or url.fragment):
            raise ValueError("OCE_OLLAMA_URL must be an HTTP loopback URL without credentials or a path")
        _ = url.port  # Validate the port before making a request.
        if not self.model.strip() or not self.keep_alive.strip():
            raise ValueError("Model name and keep_alive must not be empty")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("OCE_LLM_TIMEOUT_S must be a positive finite number")

    @classmethod
    def from_env(cls) -> "ModelConfig":
        return cls(
            model=os.getenv("OCE_MODEL", "nemotron3:33b"),
            ollama_url=os.getenv("OCE_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            timeout_s=float(os.getenv("OCE_LLM_TIMEOUT_S", "120")),
            keep_alive=os.getenv("OCE_MODEL_KEEP_ALIVE", "5m"),
        )
