"""Optional OpenAI-compatible provider (Groq, OpenRouter, LM Studio, llama-server)."""

from __future__ import annotations

import os
import time
from typing import Any

import httpx

from models import AIProviderConfig, ClassificationRequest, ClassificationResult
from providers.base import (
    BaseClassifierProvider,
    build_classification_messages,
    parse_json_loose,
    unsure_result,
    validate_classification_output,
)


class OpenAICompatibleProvider(BaseClassifierProvider):
    name = "openai_compat"

    def __init__(self, cfg: AIProviderConfig) -> None:
        self.cfg = cfg
        self.base_url = cfg.base_url.rstrip("/")

    def _headers(self) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        key = os.environ.get(self.cfg.api_key_env, "").strip() if self.cfg.api_key_env else ""
        if key:
            headers["Authorization"] = f"Bearer {key}"
        return headers

    def classify(self, req: ClassificationRequest) -> ClassificationResult:
        started = time.perf_counter()
        system, user = build_classification_messages(req)
        payload = {
            "model": self.cfg.model,
            "temperature": 0.1,
            "max_tokens": 220,
            "response_format": {"type": "json_object"},
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            resp = httpx.post(
                f"{self.base_url}/chat/completions",
                json=payload,
                headers=self._headers(),
                timeout=self.cfg.timeout_seconds,
            )
            resp.raise_for_status()
            content = resp.json()["choices"][0]["message"]["content"]
            raw = parse_json_loose(content)
            latency = int((time.perf_counter() - started) * 1000)
            return validate_classification_output(raw, req, self.name, self.cfg.model, latency)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return unsure_result(self.name, self.cfg.model, f"{type(exc).__name__}: {exc}"[:300], latency)

    def health_check(self) -> dict[str, Any]:
        info: dict[str, Any] = {"provider": self.name, "model": self.cfg.model, "base_url": self.base_url}
        if self.cfg.api_key_env and not os.environ.get(self.cfg.api_key_env):
            info.update(available=False, reason="missing_api_key")
            return info
        try:
            resp = httpx.get(f"{self.base_url}/models", headers=self._headers(), timeout=3.0)
            info.update(available=resp.status_code < 500, status_code=resp.status_code)
        except Exception as exc:
            info.update(available=False, reason=type(exc).__name__)
        return info
