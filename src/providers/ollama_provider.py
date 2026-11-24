"""Default pure-local offline provider: Ollama (/api/chat with JSON schema output)."""

from __future__ import annotations

import time
from typing import Any

import httpx

from models import AIProviderConfig, ClassificationRequest, ClassificationResult
from providers.base import (
    BaseClassifierProvider,
    build_classification_messages,
    build_json_schema,
    parse_json_loose,
    unsure_result,
    validate_classification_output,
)


class OllamaProvider(BaseClassifierProvider):
    name = "ollama"

    def __init__(self, cfg: AIProviderConfig) -> None:
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")

    def _payload(self, req: ClassificationRequest, include_think: bool) -> dict[str, Any]:
        system, user = build_classification_messages(req)
        payload: dict[str, Any] = {
            "model": self.cfg.model,
            "stream": False,
            "keep_alive": self.cfg.keep_alive,
            "format": build_json_schema(req),
            "options": {"temperature": 0.1, "num_predict": 220},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        if include_think and self.cfg.disable_thinking:
            payload["think"] = False
        return payload

    def classify(self, req: ClassificationRequest) -> ClassificationResult:
        started = time.perf_counter()
        try:
            timeout = httpx.Timeout(self.cfg.timeout_seconds, connect=1.5)
            with httpx.Client(timeout=timeout, trust_env=False) as client:
                resp = client.post(f"{self.base_url}/api/chat", json=self._payload(req, True))
                if resp.status_code == 400 and self.cfg.disable_thinking:
                    # Older Ollama builds / non-thinking models reject the "think" field.
                    resp = client.post(f"{self.base_url}/api/chat", json=self._payload(req, False))
                resp.raise_for_status()
                content = resp.json().get("message", {}).get("content", "")
            raw = parse_json_loose(content)
            latency = int((time.perf_counter() - started) * 1000)
            return validate_classification_output(raw, req, self.name, self.cfg.model, latency)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return unsure_result(self.name, self.cfg.model, f"{type(exc).__name__}: {exc}"[:300], latency)

    def health_check(self) -> dict[str, Any]:
        info: dict[str, Any] = {"provider": self.name, "model": self.cfg.model, "base_url": self.base_url}
        try:
            resp = httpx.get(f"{self.base_url}/api/tags", timeout=0.4, trust_env=False)
            resp.raise_for_status()
            models = [m.get("name", "") for m in resp.json().get("models", [])]
            wanted = self.cfg.model if ":" in self.cfg.model else f"{self.cfg.model}:latest"
            info.update(available=True, installed_models=models, model_installed=wanted in models)
        except Exception as exc:
            info.update(available=False, model_installed=False, reason=type(exc).__name__)
        return info
