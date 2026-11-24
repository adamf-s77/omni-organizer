"""Shared prompt building, JSON schema, and output validation for AI providers."""

from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from typing import Any

from models import ClassificationRequest, ClassificationResult

UNSURE = "UNSURE"

SYSTEM_PROMPT = (
    "You are a careful file-organization classifier for a Windows PC. "
    "Pick exactly ONE destination_id from the provided whitelist that best fits the file, "
    "based only on the filename, extension, current folder and optional short preview. "
    "Never invent IDs outside the whitelist. "
    "Do NOT guess blindly on generic filenames (such as invite.ics, document.pdf, untitled, file) "
    "when the preview does not clearly match a specific destination topic — in that case you MUST "
    f'return destination_id "{UNSURE}" with confidence <= 0.4. '
    "Only output confidence >= 0.85 when the filename or preview unmistakably matches a destination. "
    "confidence is a number between 0 and 1 reflecting how sure you are. "
    "reason MUST be written in Traditional Chinese (繁體中文, <= 60 characters). "
    "suggested_keyword is a short distinctive token from the filename that could be used as a "
    "future matching rule, or an empty string. Respond with JSON only."
)


def build_json_schema(req: ClassificationRequest) -> dict[str, Any]:
    """JSON schema constraining destination_id to the whitelist enum (+ UNSURE)."""
    ids = [d.id for d in req.destinations] + [UNSURE]
    return {
        "type": "object",
        "properties": {
            "destination_id": {"type": "string", "enum": ids},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "reason": {"type": "string"},
            "suggested_keyword": {"type": "string"},
        },
        "required": ["destination_id", "confidence", "reason", "suggested_keyword"],
    }


def build_classification_messages(req: ClassificationRequest) -> tuple[str, str]:
    """Return (system_prompt, user_prompt) containing only sanitized metadata."""
    dest_lines = "\n".join(f"- {d.id}: {d.label} — {d.description}" for d in req.destinations)
    preview = req.preview_text if req.preview_text else "(no preview available)"
    user = (
        f"File name: {req.filename}\n"
        f"Extension: {req.extension or '(none)'}\n"
        f"Size: {req.size_bytes} bytes\n"
        f"Current scope: {req.current_scope}\n"
        f"Current folder name: {req.current_dir}\n"
        f"Content preview (first chars, may be empty):\n{preview}\n\n"
        f"Whitelisted destinations:\n{dest_lines}\n- {UNSURE}: not sure / none fits\n\n"
        'Reply JSON: {"destination_id": str, "confidence": number, "reason": str, '
        '"suggested_keyword": str}'
    )
    return SYSTEM_PROMPT, user


_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL | re.IGNORECASE)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.IGNORECASE)


def parse_json_loose(text: str) -> dict[str, Any]:
    """Strip reasoning tags / code fences and parse the first JSON object."""
    cleaned = _THINK_RE.sub("", text or "").strip()
    cleaned = _FENCE_RE.sub("", cleaned).strip()
    try:
        data = json.loads(cleaned)
        if isinstance(data, dict):
            return data
    except json.JSONDecodeError:
        pass
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        data = json.loads(cleaned[start : end + 1])
        if isinstance(data, dict):
            return data
    raise ValueError("model output is not a JSON object")


def validate_classification_output(
    raw: dict[str, Any],
    req: ClassificationRequest,
    provider_name: str,
    model_name: str,
    latency_ms: int,
) -> ClassificationResult:
    """Enforce whitelist membership and clamp confidence; hallucinations become UNSURE."""
    allowed = {d.id for d in req.destinations}
    dest_id = str(raw.get("destination_id") or UNSURE).strip()
    try:
        confidence = float(raw.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0
    confidence = max(0.0, min(1.0, confidence))
    reason = str(raw.get("reason") or "")[:200]
    keyword = raw.get("suggested_keyword")
    keyword = str(keyword).strip() if keyword else None

    error = None
    if dest_id != UNSURE and dest_id not in allowed:
        error = f"hallucinated_destination:{dest_id}"
        dest_id, confidence = UNSURE, 0.0
    if dest_id == UNSURE:
        confidence = min(confidence, 0.5)

    return ClassificationResult(
        destination_id=dest_id,
        confidence=confidence,
        reason=reason,
        suggested_keyword=keyword or None,
        provider=provider_name,
        model=model_name,
        latency_ms=latency_ms,
        error=error,
    )


def unsure_result(provider: str, model: str, error: str, latency_ms: int = 0) -> ClassificationResult:
    return ClassificationResult(
        destination_id=UNSURE,
        confidence=0.0,
        reason="AI 無法判斷或離線，交由人工確認",
        provider=provider,
        model=model,
        latency_ms=latency_ms,
        error=error,
    )


class BaseClassifierProvider(ABC):
    """Abstract provider interface. classify() must never raise."""

    name: str = "base"

    @abstractmethod
    def classify(self, req: ClassificationRequest) -> ClassificationResult: ...

    @abstractmethod
    def health_check(self) -> dict[str, Any]: ...
