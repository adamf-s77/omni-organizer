"""Provider factory and the no-AI fallback provider."""

from __future__ import annotations

from typing import Any

from models import AIProviderConfig, ClassificationRequest, ClassificationResult
from providers.base import UNSURE, BaseClassifierProvider


class NoneProvider(BaseClassifierProvider):
    """AI disabled: every Tier 2 decision falls through to Tier 3 human review."""

    name = "none"

    def classify(self, req: ClassificationRequest) -> ClassificationResult:
        return ClassificationResult(
            destination_id=UNSURE,
            confidence=0.0,
            reason="AI 已停用，交由人工確認",
            provider=self.name,
        )

    def health_check(self) -> dict[str, Any]:
        return {"provider": self.name, "available": False, "reason": "ai_disabled"}


def create_provider(cfg: AIProviderConfig) -> BaseClassifierProvider:
    if cfg.provider == "ollama":
        from providers.ollama_provider import OllamaProvider

        return OllamaProvider(cfg)
    if cfg.provider == "openai_compat":
        from providers.openai_compat_provider import OpenAICompatibleProvider

        return OpenAICompatibleProvider(cfg)
    if cfg.provider == "gemini":
        from providers.gemini_provider import GeminiProvider

        return GeminiProvider(cfg)
    return NoneProvider()
