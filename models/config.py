"""Environment-backed configuration for the local model service."""

import math
import os
from dataclasses import dataclass
from urllib.parse import urlsplit


def load_dotenv(path: str = ".env") -> None:
    """Apply OCA_* lines from a dotenv file without overriding real environment variables."""
    try:
        with open(path, encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                key = key.strip()
                if key.startswith("OCA_"):
                    os.environ.setdefault(key, value.strip().strip('"').strip("'"))
    except OSError:
        return


@dataclass(frozen=True)
class ModelConfig:
    model: str = "nemotron3:33b"
    ollama_url: str = "http://localhost:11434"
    timeout_s: float = 120.0
    keep_alive: str = "5m"
    max_output_tokens: int = 8192  # Ollama num_predict: stops a runaway reply before it eats the whole timeout

    def __post_init__(self) -> None:
        url = urlsplit(self.ollama_url)
        if (url.scheme != "http" or url.hostname not in {"localhost", "127.0.0.1", "::1"}
                or url.username or url.password or url.path not in {"", "/"}
                or url.query or url.fragment):
            raise ValueError("OCA_OLLAMA_URL must be an HTTP loopback URL without credentials or a path")
        _ = url.port  # Validate the port before making a request.
        if not self.model.strip() or not self.keep_alive.strip():
            raise ValueError("Model name and keep_alive must not be empty")
        if not math.isfinite(self.timeout_s) or self.timeout_s <= 0:
            raise ValueError("OCA_LLM_TIMEOUT_S must be a positive finite number")
        if type(self.max_output_tokens) is not int or self.max_output_tokens < 1:
            raise ValueError("OCA_LLM_MAX_OUTPUT_TOKENS must be a positive integer")

    @classmethod
    def from_env(cls) -> "ModelConfig":
        return cls(
            model=os.getenv("OCA_MODEL", "nemotron3:33b"),
            ollama_url=os.getenv("OCA_OLLAMA_URL", "http://localhost:11434").rstrip("/"),
            timeout_s=float(os.getenv("OCA_LLM_TIMEOUT_S", "120")),
            keep_alive=os.getenv("OCA_MODEL_KEEP_ALIVE", "5m"),
            max_output_tokens=int(os.getenv("OCA_LLM_MAX_OUTPUT_TOKENS", "8192")),
        )
