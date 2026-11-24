"""Three-tier decision funnel: Tier 1 rules -> Tier 2 AI -> Tier 3 human (Discord)."""

from __future__ import annotations

import os
import re
import threading
import uuid
from pathlib import Path
from typing import Callable

from content_extractor import extract_safe_preview
from executor import compute_file_hash, safe_move_file
from guardrails import is_forbidden_path, is_ignored_file, is_sensitive_file
from models import (
    ClassificationRequest,
    ClassificationResult,
    DecisionPayload,
    LearnedRule,
    MonitoredScope,
    NotificationEvent,
    PendingDecision,
    V3Settings,
)
from providers.base import UNSURE, BaseClassifierProvider, unsure_result
from scopes import is_under, resolve_scope_for_path
from state_store import StateStore
from tier1_rules import Tier1Match, match_rules, match_tier1

MODERATE_SUGGESTION = 0.45
GENERIC_TOKENS = {
    "final", "copy", "new", "file", "files", "document", "doc", "download", "downloads",
    "image", "img", "photo", "screenshot", "untitled", "version", "draft", "test",
    "temp", "tmp", "backup", "output", "report", "data", "v1", "v2", "v3",
}
GENERIC_BLIND_STEMS = GENERIC_TOKENS | {
    "invite", "calendar", "event", "meeting", "attachment",
    "scan", "note", "notes", "pic", "video", "audio",
}
GENERIC_EMPTY_PREVIEW_CAP = 0.70


def _is_generic_stem(filename: str) -> bool:
    tokens = [
        t.lower()
        for t in re.split(r"[\s_\-\.\(\)\[\]（）【】,，]+", Path(filename).stem)
        if t and not t.isdigit()
    ]
    return (not tokens) or all(t in GENERIC_BLIND_STEMS for t in tokens)


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _new_id(prefix: str) -> str:
    return f"{prefix}_{uuid.uuid4().hex[:8]}"


def derive_keyword(filename: str, hint: str | None = None) -> str | None:
    """Choose a distinctive, non-generic token from the filename for a learned rule."""
    stem = Path(filename).stem
    if hint:
        h = hint.strip()
        if len(h) >= 2 and h.lower() in filename.lower() and h.lower() not in GENERIC_TOKENS:
            return h
    tokens = [t for t in re.split(r"[\s_\-\.\(\)\[\]（）【】,，]+", stem) if t]
    candidates = [
        t for t in tokens
        if len(t) >= 3 and not t.isdigit() and t.lower() not in GENERIC_TOKENS
    ]
    if not candidates:
        candidates = [t for t in tokens if len(t) >= 2 and not t.isdigit() and t.lower() not in GENERIC_TOKENS]
    if not candidates:
        return None
    return max(candidates, key=len)


class DecisionEngine:
    def __init__(
        self,
        settings: V3Settings,
        store: StateStore,
        provider: BaseClassifierProvider,
        logger: Callable[[str], None] | None = None,
    ) -> None:
        self.settings = settings
        self.store = store
        self.provider = provider
        self.log = logger or (lambda msg: None)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _resolve_scope(self, path: Path, scope_id: str | None) -> tuple[MonitoredScope | None, str]:
        resolved = resolve_scope_for_path(path, self.settings.monitored_scopes)
        if resolved is not None:
            return resolved
        if scope_id:
            for s in self.settings.monitored_scopes:
                if s.id == scope_id:
                    return s, ""
        return None, ""

    def _in_privacy_zone(self, path: Path) -> bool:
        for dest_id in self.settings.privacy_zone_dest_ids:
            dest = self.settings.get_destination(dest_id)
            if dest and is_under(path, dest.path):
                return True
        return False

    def _do_move(
        self,
        path: Path,
        dest_id: str,
        *,
        tier: str,
        reason: str,
        confidence: float,
        file_hash: str,
        rule_id: str | None = None,
    ) -> dict:
        dest = self.settings.get_destination(dest_id)
        if dest is None:
            return {"action": "error", "error": f"unknown_destination:{dest_id}"}
        res = safe_move_file(
            path, dest.path, self.settings, self.store,
            tier=tier, reason=reason, confidence=confidence,
            rule_id=rule_id, file_hash=file_hash,
        )
        status = res.get("status")
        if status == "moved":
            self.store.enqueue_notification(
                NotificationEvent(
                    id=_new_id("ev"), kind="auto_moved", tx_id=res["tx_id"],
                    filename=path.name, src=res["src"], dst=res["dst"],
                    dest_id=dest.id, dest_label=dest.label, tier=tier,
                    reason=reason, confidence=confidence,
                )
            )
            self.log(f"[{tier}] 已搬移 {path.name} -> {dest.label} ({confidence:.2f}) tx={res['tx_id']}")
            return {"action": "auto_moved", "tier": tier, **res, "dest_id": dest.id}
        if status == "skipped_conflict":
            self.store.enqueue_notification(
                NotificationEvent(
                    id=_new_id("ev"), kind="warning", filename=path.name,
                    src=str(path), dst=res.get("dst", ""), dest_id=dest.id,
                    dest_label=dest.label, tier=tier,
                    reason="目的地已有同名檔案，為避免覆蓋已略過搬移", confidence=confidence,
                )
            )
            self.log(f"[{tier}] 目的地同名衝突，略過 {path.name}")
            return {"action": "skipped_conflict", "tier": tier, **res}
        self.log(f"[{tier}] 搬移未執行 {path.name}: {res}")
        return {"action": status or "error", "tier": tier, **res}

    def _enqueue_pending(
        self,
        path: Path,
        scope_id: str,
        file_hash: str,
        *,
        reason: str,
        is_sensitive: bool = False,
        is_misplaced: bool = False,
        result: ClassificationResult | None = None,
    ) -> dict:
        dest = None
        if result is not None and result.destination_id != UNSURE:
            dest = self.settings.get_destination(result.destination_id)
        item = PendingDecision(
            id=_new_id("p"),
            src_path=str(path),
            filename=path.name,
            extension=path.suffix.lower().lstrip("."),
            scope_id=scope_id,
            size_bytes=path.stat().st_size if path.exists() else 0,
            file_hash=file_hash,
            is_sensitive=is_sensitive,
            is_misplaced_candidate=is_misplaced,
            suggested_dest_id=dest.id if dest else None,
            suggested_dest_label=dest.label if dest else None,
            suggested_dest_path=dest.path if dest else None,
            confidence=result.confidence if result else 0.0,
            reason=reason,
            suggested_keyword=result.suggested_keyword if result else None,
            provider=result.provider if result else "none",
        )
        saved = self.store.upsert_pending(item)
        self.log(f"[tier3] 待人工確認 {path.name} (pending={saved.id}) {reason}")
        return {"action": "pending_sensitive" if is_sensitive else "pending_review", "pending_id": saved.id}

    # ------------------------------------------------------------------
    # Main pipeline
    # ------------------------------------------------------------------
    def process_file(self, path: Path | str, scope_id: str | None = None) -> dict:
        with self._lock:
            return self._process(Path(os.path.abspath(str(path))), scope_id)

    def _process(self, path: Path, scope_id: str | None) -> dict:
        if self.store.is_self_move(path):
            return {"action": "ignored_self_move"}
        if not path.is_file():
            return {"action": "missing"}

        scope, rel_dir = self._resolve_scope(path, scope_id)
        if scope is None:
            return {"action": "out_of_scope"}
        sid = scope.id
        is_intake = rel_dir == ""

        forbidden, why = is_forbidden_path(
            path,
            scope_root=Path(scope.path),
            extra_forbidden=self.settings.protected_paths,
        )
        if forbidden:
            return {"action": "blocked", "reason": why}
        ignored, why = is_ignored_file(path)
        if ignored:
            return {"action": "blocked", "reason": why}

        try:
            file_hash = compute_file_hash(path)
        except OSError as exc:
            return {"action": "error", "error": f"hash_failed:{exc}"}

        on_cd, cd_reason = self.store.is_on_cooldown(path, file_hash)
        if on_cd:
            return {"action": "cooldown", "reason": cd_reason}

        reversed_rec = self.store.find_recent_move_from_dir(
            path.parent, path.name, file_hash, within_hours=self.settings.cooldown_hours
        )
        if reversed_rec is not None:
            self.store.add_cooldown(path, file_hash, self.settings.cooldown_hours, "manual_reversal_detected")
            self.log(f"[guard] 偵測到使用者手動移回 {path.name}，暫停自動搬移 {self.settings.cooldown_hours}h")
            return {"action": "manual_reversal_cooldown", "tx_id": reversed_rec.tx_id}

        # ---- Privacy redline pre-gate: never read, never send to AI, never auto-move.
        sensitive, s_reason = is_sensitive_file(path)
        if sensitive:
            if self._in_privacy_zone(path):
                return {"action": "already_safe_sensitive"}
            if not is_intake:
                return {"action": "kept_in_place_sensitive"}
            return self._enqueue_pending(
                path, sid, file_hash, is_sensitive=True,
                reason="偵測到敏感／機密關鍵字，已依隱私紅線略過內容讀取與 AI 分析，請手動指定去處或忽略",
            )

        # ---- Tier 1: learned + deterministic rules
        t1: Tier1Match | None = match_tier1(path, sid, self.settings, self.store, scope)
        if t1 is not None:
            dest = self.settings.get_destination(t1.dest_id)
            if dest and _norm(dest.path) == _norm(path.parent):
                return {"action": "already_correct", "tier": t1.tier}
            result = self._do_move(path, t1.dest_id, tier=t1.tier, reason=t1.reason,
                                   confidence=1.0, file_hash=file_hash, rule_id=t1.rule_id)
            if t1.tier == "tier1_learned" and result.get("action") == "auto_moved":
                self.store.increment_rule_hit(t1.rule_id)
            return result

        # ---- Privacy zone: Tier 1 only, never AI.
        if self._in_privacy_zone(path):
            return {"action": "kept_in_privacy_zone"}

        # ---- Tier 2: AI semantic classification
        preview, content_sensitive, note = extract_safe_preview(path, self.settings.ai.preview_max_chars)
        if content_sensitive:
            if not is_intake:
                return {"action": "kept_in_place_sensitive"}
            return self._enqueue_pending(
                path, sid, file_hash, is_sensitive=True,
                reason="檔案內容偵測到疑似金鑰／敏感個資，已中止 AI 傳送，請手動確認",
            )

        ai_destinations = [d for d in self.settings.destinations if d.ai_selectable] or self.settings.destinations
        req = ClassificationRequest(
            filename=path.name,
            extension=path.suffix.lower().lstrip("."),
            current_scope=sid,
            current_dir=path.parent.name or str(path.parent),
            size_bytes=path.stat().st_size,
            preview_text=preview,
            destinations=ai_destinations,
        )
        try:
            result = self.provider.classify(req)
        except Exception as exc:  # providers should not raise, but stay defensive
            result = unsure_result(getattr(self.provider, "name", "unknown"), "", f"{type(exc).__name__}: {exc}")

        if not preview and _is_generic_stem(path.name) and result.confidence > GENERIC_EMPTY_PREVIEW_CAP:
            result = result.model_copy(
                update={
                    "confidence": GENERIC_EMPTY_PREVIEW_CAP,
                    "reason": (result.reason + "（泛用檔名且無內文，轉人工確認）")[:200],
                }
            )

        threshold = self.settings.ai.auto_move_threshold if is_intake else self.settings.ai.misplacement_threshold
        allowed_ai_ids = {d.id for d in ai_destinations}
        dest = (
            self.settings.get_destination(result.destination_id)
            if result.destination_id != UNSURE and result.destination_id in allowed_ai_ids
            else None
        )

        if dest is not None and _norm(dest.path) == _norm(path.parent):
            return {"action": "already_correct", "tier": "tier2_ai", "confidence": result.confidence}

        if dest is not None and result.confidence >= threshold:
            if self.settings.ai.require_confirmation:
                return self._enqueue_pending(
                    path, sid, file_hash, is_misplaced=(not is_intake), result=result,
                    reason=f"AI 建議歸檔 ({result.confidence:.2f})：{result.reason}（待人工確認）",
                )
            return self._do_move(
                path, dest.id, tier="tier2_ai",
                reason=f"AI 判斷：{result.reason}" if result.reason else "AI 語意分類",
                confidence=result.confidence, file_hash=file_hash,
            )

        # ---- Provider unreachable: keep v2 behaviour for scope-root intake files.
        if result.error and is_intake and not result.error.startswith("hallucinated"):
            fb = match_rules(path, sid, self.settings, self.settings.offline_fallback_rules,
                             "tier1_offline_fallback", scope)
            if fb is not None:
                fb_dest = self.settings.get_destination(fb.dest_id)
                if fb_dest and _norm(fb_dest.path) != _norm(path.parent):
                    return self._do_move(path, fb.dest_id, tier=fb.tier,
                                         reason=f"AI 離線，依副檔名分流（{fb.reason}）",
                                         confidence=1.0, file_hash=file_hash, rule_id=fb.rule_id)

        # ---- Tier 3: human-in-the-loop
        if not is_intake:
            # Already-organised folder: only ask when AI actually proposes a different home.
            if dest is None or result.confidence < MODERATE_SUGGESTION:
                return {"action": "kept_in_place", "confidence": result.confidence}
            return self._enqueue_pending(
                path, sid, file_hash, is_misplaced=True, result=result,
                reason=f"疑似放錯位置：{result.reason}" if result.reason else "疑似放錯位置",
            )

        if result.error:
            reason = "AI 暫時無法使用，請手動指定去處"
        elif dest is None:
            reason = result.reason or "AI 無法判斷合適去處"
        else:
            reason = f"AI 信心不足 ({result.confidence:.2f})：{result.reason}"
        return self._enqueue_pending(path, sid, file_hash, result=result, reason=reason)

    # ------------------------------------------------------------------
    # Tier 3 decisions (Desktop Popup / Discord)
    # ------------------------------------------------------------------
    def prune_missing_pending(self) -> int:
        """Resolve any pending items whose source file no longer exists on disk."""
        pruned = 0
        with self._lock:
            for item in self.store.list_pending():
                if not Path(item.src_path).is_file():
                    if self.store.resolve_pending(item.id, "missing") is not None:
                        pruned += 1
        return pruned

    def apply_decision(self, payload: DecisionPayload) -> dict:
        with self._lock:
            return self._apply(payload)

    def _apply(self, payload: DecisionPayload) -> dict:
        pending = self.store.get_pending(payload.pending_id)
        if pending is None:
            return {"ok": False, "error": "pending_not_found"}
        if pending.status != "pending":
            return {"ok": False, "error": f"already_{pending.status}"}

        src = Path(pending.src_path)
        if payload.action == "ignore":
            cooldown_h = pending.ignore_cooldown_hours or self.settings.cooldown_hours
            self.store.add_cooldown(src, pending.file_hash, cooldown_h, "user_ignored")
            self.store.resolve_pending(pending.id, "ignored")
            return {"ok": True, "action": "ignore", "pending_id": pending.id}

        if not src.is_file():
            self.store.resolve_pending(pending.id, "missing")
            return {"ok": False, "error": "source_missing", "pending_id": pending.id}

        if payload.action == "choose" and payload.custom_dest_path is not None:
            raw_custom = payload.custom_dest_path.strip().strip('"')
            if not raw_custom or not os.path.isabs(raw_custom):
                return {"ok": False, "error": "invalid_custom_destination"}
            custom_label = str(Path(os.path.abspath(raw_custom)))
            res = safe_move_file(
                src, raw_custom, self.settings, self.store,
                tier="tier3_hitl", reason=f"使用者自訂資料夾（{payload.actor}）",
                confidence=1.0, allow_custom_dest=True,
            )
            if res.get("status") != "moved":
                return {"ok": False, "error": res.get("error") or res.get("status"), "detail": res}
            self.store.resolve_pending(pending.id, "approved")
            self.log(f"[tier3] 使用者自訂搬移 {src.name} -> {custom_label} tx={res['tx_id']}")
            return {
                "ok": True, "action": "choose", "tx_id": res["tx_id"],
                "dst": res["dst"], "dest_label": custom_label, "rule_id": None,
            }

        if payload.action == "choose":
            dest_id = payload.dest_id
        else:
            dest_id = payload.dest_id or pending.suggested_dest_id
        if not dest_id:
            return {"ok": False, "error": "no_destination_selected"}
        dest = self.settings.get_destination(dest_id)
        if dest is None:
            return {"ok": False, "error": f"unknown_destination:{dest_id}"}

        res = safe_move_file(
            src, dest.path, self.settings, self.store,
            tier="tier3_hitl", reason=f"使用者確認（{payload.actor}）", confidence=1.0,
        )
        if res.get("status") != "moved":
            return {"ok": False, "error": res.get("error") or res.get("status"), "detail": res}
        self.store.resolve_pending(pending.id, "approved")
        self.log(f"[tier3] 使用者確認搬移 {src.name} -> {dest.label} tx={res['tx_id']}")

        rule_id = None
        remember = payload.action == "accept_and_remember" or payload.remember
        if remember and not pending.is_sensitive and pending.kind != "archive":
            keyword = derive_keyword(src.name, payload.keyword or pending.suggested_keyword)
            if keyword:
                rule = self.store.add_learned_rule(
                    LearnedRule(
                        id=_new_id("lr"), keyword=keyword,
                        extensions=[pending.extension] if pending.extension else [],
                        dest_id=dest.id, source_filename=src.name,
                    )
                )
                rule_id = rule.id
                self.log(f"[learn] 新增學習規則 '{keyword}' (.{pending.extension}) -> {dest.label}")

        return {
            "ok": True, "action": payload.action, "tx_id": res["tx_id"],
            "dst": res["dst"], "dest_label": dest.label, "rule_id": rule_id,
        }
