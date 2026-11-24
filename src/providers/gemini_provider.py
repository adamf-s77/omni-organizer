"""Optional Google Gemini provider (free AI Studio tier supported via API key env)."""

from __future__ import annotations

import os
import time
from typing import Any

from models import AIProviderConfig, ClassificationRequest, ClassificationResult
from providers.base import (
    BaseClassifierProvider,
    build_classification_messages,
    parse_json_loose,
    unsure_result,
    validate_classification_output,
)


class GeminiProvider(BaseClassifierProvider):
    name = "gemini"

    def __init__(self, cfg: AIProviderConfig) -> None:
        self.cfg = cfg
        self.key_env = cfg.api_key_env or "GEMINI_API_KEY"

    def _api_key(self) -> str:
        return os.environ.get(self.key_env, "").strip()

    def classify(self, req: ClassificationRequest) -> ClassificationResult:
        started = time.perf_counter()
        key = self._api_key()
        if not key:
            return unsure_result(self.name, self.cfg.model, "missing_api_key")
        try:
            from google import genai
            from google.genai import types

            client = genai.Client(api_key=key)
            system, user = build_classification_messages(req)
            resp = client.models.generate_content(
                model=self.cfg.model,
                contents=user,
                config=types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                    temperature=0.1,
                    max_output_tokens=300,
                ),
            )
            raw = parse_json_loose(resp.text or "")
            latency = int((time.perf_counter() - started) * 1000)
            return validate_classification_output(raw, req, self.name, self.cfg.model, latency)
        except Exception as exc:
            latency = int((time.perf_counter() - started) * 1000)
            return unsure_result(self.name, self.cfg.model, f"{type(exc).__name__}: {exc}"[:300], latency)

    def health_check(self) -> dict[str, Any]:
        info: dict[str, Any] = {"provider": self.name, "model": self.cfg.model}
        if not self._api_key():
            info.update(available=False, reason="missing_api_key")
        else:
            info.update(available=True, note="api_key_present_not_probed")
        return info
