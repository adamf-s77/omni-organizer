"""Pydantic v2 data models for Omni-Organizer v3."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Optional

from pydantic import BaseModel, Field


def utc_now_iso() -> str:
    """Return current UTC timestamp as an ISO-8601 string."""
    return datetime.now(timezone.utc).isoformat()


class MonitoredScope(BaseModel):
    """Filesystem monitoring scope definition."""

    id: str
    path: str
    mode: Literal["root_only", "root_and_subdirs", "misplacement_only"] = "root_only"
    allowed_subdirs: list[str] = Field(default_factory=list)
    misplacement_subdirs: list[str] = Field(default_factory=list)
    max_depth: int = 0
    enabled: bool = True


class DestinationOption(BaseModel):
    """Whitelisted destination directory for file classification."""

    id: str
    label: str
    path: str
    description: str
    group: str = "General"
    ai_selectable: bool = True


class DeterministicRule(BaseModel):
    """Built-in Tier 1 deterministic matching rule."""

    id: str
    name: str
    dest_id: str
    scopes: list[str] = Field(default_factory=list)
    extensions: list[str] = Field(default_factory=list)
    regex: Optional[str] = None
    keywords: list[str] = Field(default_factory=list)
    enabled: bool = True


class AIProviderConfig(BaseModel):
    """AI semantic classifier provider configuration."""

    provider: Literal["ollama", "gemini", "openai_compat", "none"] = "ollama"
    model: str = "qwen2.5:3b"
    base_url: str = "http://127.0.0.1:11434"
    api_key_env: str = ""
    timeout_seconds: float = 25.0
    keep_alive: str = "2m"
    auto_move_threshold: float = 0.85
    misplacement_threshold: float = 0.90
    preview_max_chars: int = 400
    disable_thinking: bool = True
    require_confirmation: bool = False


class ArchiveRule(BaseModel):
    """Age-based periodic archive rule (enqueues Discord HITL approval, never moves silently)."""

    id: str
    name: str
    source_path: str
    recursive: bool = True
    min_age_days: int = 30
    dest_id: str
    extensions: list[str] = Field(default_factory=list)
    ignore_cooldown_hours: float = 168.0
    enabled: bool = True


class BridgeConfig(BaseModel):
    """Localhost-only HTTP Bridge configuration."""

    host: str = "127.0.0.1"
    port: int = 8766
    enabled: bool = True


class UIConfig(BaseModel):
    """Native desktop approval popup settings."""

    desktop_popup_enabled: bool = True
    topmost: bool = True
    popup_cooldown_seconds: float = 3.0


class V3Settings(BaseModel):
    """Root configuration schema for Omni-Organizer v3."""

    version: str = "3.0.0"
    quiescence_seconds: float = 6.0
    cooldown_hours: float = 24.0
    maintenance_interval_seconds: int = 86400
    ai: AIProviderConfig = Field(default_factory=AIProviderConfig)
    bridge: BridgeConfig = Field(default_factory=BridgeConfig)
    ui: UIConfig = Field(default_factory=UIConfig)
    protected_paths: list[str] = Field(default_factory=list)
    monitored_scopes: list[MonitoredScope] = Field(default_factory=list)
    destinations: list[DestinationOption] = Field(default_factory=list)
    tier1_rules: list[DeterministicRule] = Field(default_factory=list)
    # Files inside these destinations are never content-read nor sent to any AI provider.
    privacy_zone_dest_ids: list[str] = Field(default_factory=list)
    # Extension buckets applied ONLY to scope-root files when the AI provider is unreachable.
    offline_fallback_rules: list[DeterministicRule] = Field(default_factory=list)
    archive_rules: list[ArchiveRule] = Field(default_factory=list)
    archive_max_per_sweep: int = 5

    def get_destination(self, dest_id: str) -> Optional[DestinationOption]:
        """Look up a destination option by its ID."""
        for dest in self.destinations:
            if dest.id == dest_id:
                return dest
        return None


class LearnedRule(BaseModel):
    """User-confirmed rule learned via Discord HITL feedback."""

    id: str
    keyword: Optional[str] = None
    regex: Optional[str] = None
    extensions: list[str] = Field(default_factory=list)
    scopes: list[str] = Field(default_factory=list)
    dest_id: str
    enabled: bool = True
    hit_count: int = 0
    created_at: str = Field(default_factory=utc_now_iso)
    source_filename: str = ""


class ClassificationRequest(BaseModel):
    """Sanitized context passed to Tier 2 AI classifiers (no secrets/full contents)."""

    filename: str
    extension: str
    current_scope: str
    current_dir: str
    size_bytes: int
    preview_text: Optional[str] = None
    destinations: list[DestinationOption] = Field(default_factory=list)


class ClassificationResult(BaseModel):
    """Structured output returned by Tier 2 AI classifiers."""

    destination_id: str
    confidence: float = 0.0
    reason: str = ""
    suggested_keyword: Optional[str] = None
    provider: str = "none"
    model: str = ""
    latency_ms: int = 0
    error: Optional[str] = None


class PendingDecision(BaseModel):
    """Pending Tier 3 human-in-the-loop review item."""

    id: str
    kind: Literal["classify", "archive"] = "classify"
    src_path: str
    filename: str
    extension: str
    scope_id: str
    size_bytes: int
    file_hash: str
    is_sensitive: bool = False
    is_misplaced_candidate: bool = False
    suggested_dest_id: Optional[str] = None
    suggested_dest_label: Optional[str] = None
    suggested_dest_path: Optional[str] = None
    confidence: float = 0.0
    reason: str = ""
    suggested_keyword: Optional[str] = None
    provider: str = "none"
    ignore_cooldown_hours: Optional[float] = None
    status: Literal["pending", "approved", "ignored", "missing"] = "pending"
    notified: bool = False
    created_at: str = Field(default_factory=utc_now_iso)
    updated_at: str = Field(default_factory=utc_now_iso)


class UndoRecord(BaseModel):
    """Append-only record of a file move for one-click undo & reversal detection."""

    tx_id: str
    src: str
    dst: str
    file_hash: str
    tier: str
    reason: str
    confidence: float = 1.0
    rule_id: Optional[str] = None
    timestamp: str = Field(default_factory=utc_now_iso)
    undone: bool = False
    undone_at: Optional[str] = None


class NotificationEvent(BaseModel):
    """Notification outbox event for Discord bot auto-move alerts with Undo button."""

    id: str
    kind: Literal["auto_moved", "info", "warning"] = "auto_moved"
    tx_id: Optional[str] = None
    filename: str
    src: str
    dst: str
    dest_id: Optional[str] = None
    dest_label: Optional[str] = None
    tier: str = ""
    reason: str = ""
    confidence: float = 1.0
    acked: bool = False
    created_at: str = Field(default_factory=utc_now_iso)


class DecisionPayload(BaseModel):
    """Payload accepted by POST /decide from Discord HITL interactions."""

    pending_id: str
    action: Literal["accept", "accept_and_remember", "choose", "ignore"]
    dest_id: Optional[str] = None
    remember: bool = False
    keyword: Optional[str] = None
    actor: str = "discord_user"
