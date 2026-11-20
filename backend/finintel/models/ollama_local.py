"""Optional loopback-only Ollama discovery; disabled until explicitly enabled.

Phase 3A provides configuration validation and model discovery only. It does
not send prompts, read private data, generate investment answers, persist
credentials, or expose an API route. Research retrieval is a later phase.
"""

from __future__ import annotations

import json
import re
from typing import Literal

import httpx
from pydantic import BaseModel, ConfigDict, Field, model_validator

_ENDPOINT = re.compile(r"http://(?:127\.0\.0\.1|\[::1\]):([0-9]{1,5})/?\Z")
_MODEL_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\Z")
_MAX_RESPONSE_BYTES = 65536
_MAX_MODELS = 100


class LocalOllamaError(RuntimeError):
    """Safe error: do not expose private paths, URLs, proxy or provider payloads."""


class LocalOllamaConfig(BaseModel):
    """Explicitly opt-in configuration with fixed loopback transport policy."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    enabled: bool = False
    endpoint: str = "http://127.0.0.1:11434"
    timeout_seconds: int = Field(default=5, ge=1, le=30)
    selected_model: str | None = None

    @model_validator(mode="after")
    def require_loopback(self) -> LocalOllamaConfig:
        match = _ENDPOINT.fullmatch(self.endpoint)
        if not match or not 1 <= int(match.group(1)) <= 65535:
            raise ValueError("Local Ollama requires an explicit HTTP loopback address and port")
        if self.selected_model is not None and not _MODEL_NAME.fullmatch(self.selected_model):
            raise ValueError("Invalid local Ollama model identifier")
        return self


class OllamaDiscovery(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["disabled", "ready"]
    models: tuple[str, ...] = ()
    selected_model_available: bool | None = None


def discover_local_models(
    config: LocalOllamaConfig, *, transport: httpx.BaseTransport | None = None
) -> OllamaDiscovery:
    """Discover installed model names via a single bounded GET /api/tags.

    Caller-supplied transport is for deterministic tests; production uses
    httpx's own transport with environment proxies and redirects disabled.
    No other host, scheme, endpoint path or request method is supported.
    """
    if not isinstance(config, LocalOllamaConfig):
        raise LocalOllamaError("Invalid local Ollama configuration")
    if not config.enabled:
        return OllamaDiscovery(status="disabled")
    try:
        with (
            httpx.Client(
                base_url=config.endpoint,
                timeout=config.timeout_seconds,
                follow_redirects=False,
                trust_env=False,
                transport=transport,
            ) as client,
            client.stream(
                "GET", "/api/tags", headers={"accept": "application/json"}
            ) as response,
        ):
            if response.status_code != 200:
                raise LocalOllamaError("Local Ollama model discovery unavailable")
            chunks = bytearray()
            for block in response.iter_bytes():
                if len(chunks) + len(block) > _MAX_RESPONSE_BYTES:
                    raise LocalOllamaError("Local Ollama model discovery response too large")
                chunks.extend(block)
        payload = json.loads(chunks)
        if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
            raise LocalOllamaError("Invalid local Ollama model discovery response")
        records = payload["models"]
        if len(records) > _MAX_MODELS:
            raise LocalOllamaError("Local Ollama model list exceeds configured limit")
        models = []
        for record in records:
            if not isinstance(record, dict):
                raise LocalOllamaError("Invalid local Ollama model metadata")
            name = record.get("name")
            if not isinstance(name, str) or not _MODEL_NAME.fullmatch(name):
                raise LocalOllamaError("Invalid local Ollama model identifier")
            models.append(name)
        unique = tuple(sorted(set(models)))
        return OllamaDiscovery(
            status="ready",
            models=unique,
            selected_model_available=(
                config.selected_model in unique if config.selected_model is not None else None
            ),
        )
    except (httpx.HTTPError, OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise LocalOllamaError("Local Ollama model discovery unavailable") from exc
