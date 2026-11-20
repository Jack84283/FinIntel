"""Ollama Phase 3A: opt-in loopback discovery with zero private-data access."""

from __future__ import annotations

import json

import httpx
import pytest
from pydantic import ValidationError

from finintel.models.ollama_local import (
    LocalOllamaConfig,
    LocalOllamaError,
    discover_local_models,
)


def test_disabled_default_never_touches_network():
    def forbidden(_request):
        pytest.fail("Ollama discovery must never connect when disabled")

    config = LocalOllamaConfig()
    assert not config.enabled
    result = discover_local_models(config, transport=httpx.MockTransport(forbidden))
    assert result.status == "disabled"
    assert result.models == ()
    assert result.selected_model_available is None


@pytest.mark.parametrize(
    "endpoint",
    [
        "http://example.com:11434",
        "https://127.0.0.1:11434",
        "http://localhost:11434",
        "http://0.0.0.0:11434",
        "http://127.0.0.2:11434",
        "http://169.254.169.254:80",
        "http://user:pass@127.0.0.1:11434",
        "http://127.0.0.1:11434/api/generate",
        "http://127.0.0.1:11434?query=private",
        "http://127.0.0.1:11434#fragment",
        "http://127.0.0.1:0",
        "http://127.0.0.1:65536",
        "http://[::ffff:127.0.0.1]:11434",
    ],
)
def test_nonloopback_or_complex_endpoint_is_rejected(endpoint):
    with pytest.raises(ValidationError):
        LocalOllamaConfig(enabled=True, endpoint=endpoint)


def test_loopback_v4_and_v6_config_are_allowed():
    assert LocalOllamaConfig(enabled=True, endpoint="http://127.0.0.1:11434").enabled
    assert LocalOllamaConfig(enabled=True, endpoint="http://[::1]:11434").enabled
    assert LocalOllamaConfig(enabled=True, endpoint="http://127.0.0.1:10001/").enabled


def test_only_one_bounded_tags_request_with_local_model_names():
    requests = []

    def local_transport(request):
        requests.append(request)
        assert request.method == "GET"
        assert str(request.url) == "http://127.0.0.1:11434/api/tags"
        assert request.content == b""
        return httpx.Response(
            200,
            json={
                "models": [
                    {"name": "qwen3:8b"},
                    {"name": "gemma2:2b"},
                    {"name": "qwen3:8b"},
                ]
            },
        )

    result = discover_local_models(
        LocalOllamaConfig(enabled=True, selected_model="qwen3:8b"),
        transport=httpx.MockTransport(local_transport),
    )
    assert len(requests) == 1
    assert result.status == "ready"
    assert result.models == ("gemma2:2b", "qwen3:8b")
    assert result.selected_model_available is True


def test_missing_selected_model_is_explicit_without_downloading():
    def handler(_request):
        return httpx.Response(200, json={"models": [{"name": "gemma2:2b"}]})

    result = discover_local_models(
        LocalOllamaConfig(enabled=True, selected_model="qwen3:8b"),
        transport=httpx.MockTransport(handler),
    )
    assert result.selected_model_available is False
    assert result.models == ("gemma2:2b",)


@pytest.mark.parametrize(
    "status,payload",
    [
        (302, "redirect denied"),
        (403, "private-provider-token"),
        (500, "private-local-stacktrace"),
        (200, "not-json"),
        (200, json.dumps({"models": "unexpected"})),
        (200, json.dumps({"models": [{"name": "/../../private"}]})),
        (200, json.dumps({"models": [{"name": "a" * 130}]})),
        (200, json.dumps({"models": [{"name": "unsafe\nheader"}]})),
        (200, json.dumps({"models": [{}]})),
        (200, json.dumps({"models": [None]})),
    ],
)
def test_malformed_response_and_redirect_fail_closed_without_leaking(status, payload):
    def handler(_request):
        return httpx.Response(status, text=payload, headers={"location": "https://example.test/"})

    with pytest.raises(LocalOllamaError) as error:
        discover_local_models(
            LocalOllamaConfig(enabled=True),
            transport=httpx.MockTransport(handler),
        )
    assert "private" not in str(error.value)
    assert "https:" not in str(error.value)


def test_oversized_payload_is_refused_during_streaming():
    def handler(_request):
        return httpx.Response(200, content=b" " * 65537)

    with pytest.raises(LocalOllamaError, match="too large"):
        discover_local_models(
            LocalOllamaConfig(enabled=True), transport=httpx.MockTransport(handler)
        )


@pytest.mark.parametrize("model", ["", "a" * 129, "../secret", "bad name", "bad\nline"])
def test_invalid_selected_model_is_rejected_before_any_connection(model):
    with pytest.raises(ValidationError):
        LocalOllamaConfig(enabled=True, selected_model=model)


def test_untyped_config_is_rejected_without_network():
    with pytest.raises(LocalOllamaError):
        discover_local_models({"enabled": True})
