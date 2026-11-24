"""Thread-safe persistent JSON/JSONL state store for Omni-Organizer v3."""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from models import (
    LearnedRule,
    NotificationEvent,
    PendingDecision,
    UndoRecord,
    utc_now_iso,
)
from paths import STATE_DIR


def _norm_path(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _parse_iso(ts: str) -> datetime:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


class StateStore:
    """Manages learned rules, pending queue, undo journal, cooldowns, and notifications."""

    def __init__(self, state_dir: Path | str = STATE_DIR) -> None:
        self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

        self.learned_rules_file = self.state_dir / "learned_rules.json"
        self.pending_queue_file = self.state_dir / "pending_queue.json"
        self.undo_journal_file = self.state_dir / "undo_journal.jsonl"
        self.cooldowns_file = self.state_dir / "cooldowns.json"
        self.notifications_file = self.state_dir / "notifications.json"

        self._self_moves: dict[str, float] = {}

    # ------------------------------------------------------------------
    # Atomic IO Helpers
    # ------------------------------------------------------------------
    def _atomic_write_text(self, target: Path, text: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(
            dir=str(target.parent),
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(text)
            os.replace(tmp_name, target)
        finally:
            if os.path.exists(tmp_name):
                try:
                    os.unlink(tmp_name)
                except OSError:
                    pass

    def _read_json_list(self, path: Path) -> list[dict[str, Any]]:
        if not path.exists():
            return []
        try:
            raw = json.loads(path.read_text(encoding="utf-8"))
            return raw if isinstance(raw, list) else []
        except Exception:
            return []

    def _write_json_list(self, path: Path, items: list[dict[str, Any]]) -> None:
        payload = json.dumps(items, ensure_ascii=False, indent=2)
        self._atomic_write_text(path, payload)

    # ------------------------------------------------------------------
    # Self-Move Suppression
    # ------------------------------------------------------------------
    def mark_self_move(self, path: Path | str, ttl_seconds: float = 30.0) -> None:
        """Record a path recently moved by Omni-Organizer so watcher ignores echo events."""
        with self._lock:
            now = time.monotonic()
            self._prune_self_moves(now)
            self._self_moves[_norm_path(path)] = now + max(1.0, float(ttl_seconds))

    def is_self_move(self, path: Path | str) -> bool:
        """Return True if path has an active self-move suppression entry."""
        with self._lock:
            now = time.monotonic()
            self._prune_self_moves(now)
            exp = self._self_moves.get(_norm_path(path))
            return exp is not None and exp > now

    def _prune_self_moves(self, now: float) -> None:
        expired = [k for k, exp in self._self_moves.items() if exp <= now]
        for k in expired:
            del self._self_moves[k]

    # ------------------------------------------------------------------
    # Anti-Tug-of-War Cooldowns
    # ------------------------------------------------------------------
    def _load_valid_cooldowns(self) -> list[dict[str, Any]]:
        now = datetime.now(timezone.utc)
        raw = self._read_json_list(self.cooldowns_file)
        valid: list[dict[str, Any]] = []
        changed = False
        for item in raw:
            try:
                exp = _parse_iso(item["expires_at"])
                if exp > now:
                    valid.append(item)
                else:
                    changed = True
            except Exception:
                changed = True
        if changed:
            self._write_json_list(self.cooldowns_file, valid)
        return valid

    def add_cooldown(
        self,
        path: Path | str,
        file_hash: str | None,
        hours: float,
        reason: str,
    ) -> None:
        """Add or refresh a cooldown suppressing automatic moves for a file/path."""
        with self._lock:
            items = self._load_valid_cooldowns()
            norm = _norm_path(path)
            expires_at = (
                datetime.now(timezone.utc) + timedelta(hours=max(0.01, float(hours)))
            ).isoformat()
            items = [
                it
                for it in items
                if not (
                    it.get("path_norm") == norm
                    or (file_hash and it.get("file_hash") == file_hash and it.get("path_norm") == norm)
                )
            ]
            items.append(
                {
                    "path_norm": norm,
                    "file_hash": file_hash or "",
                    "reason": reason,
                    "expires_at": expires_at,
                    "created_at": utc_now_iso(),
                }
            )
            self._write_json_list(self.cooldowns_file, items)

    def is_on_cooldown(
        self,
        path: Path | str,
        file_hash: str | None = None,
    ) -> tuple[bool, str]:
        """Return (True, reason) if path/hash is currently suppressed by cooldown."""
        with self._lock:
            norm = _norm_path(path)
            for item in self._load_valid_cooldowns():
                if item.get("path_norm") == norm:
                    return True, str(item.get("reason") or "cooldown")
                if file_hash and item.get("file_hash") == file_hash and item.get("path_norm") == norm:
                    return True, str(item.get("reason") or "cooldown")
            return False, ""

    def active_cooldown_count(self) -> int:
        with self._lock:
            return len(self._load_valid_cooldowns())

    def clear_cooldowns_by_reason(self, reasons: set[str]) -> int:
        """Remove active cooldown entries matching any reason in `reasons`."""
        with self._lock:
            items = self._load_valid_cooldowns()
            kept = [it for it in items if str(it.get("reason") or "") not in reasons]
            removed = len(items) - len(kept)
            if removed > 0:
                self._write_json_list(self.cooldowns_file, kept)
            return removed

    # ------------------------------------------------------------------
    # Undo Journal (JSONL)
    # ------------------------------------------------------------------
    def _read_undo_all(self) -> list[UndoRecord]:
        if not self.undo_journal_file.exists():
            return []
        records: list[UndoRecord] = []
        for line in self.undo_journal_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                records.append(UndoRecord.model_validate_json(line))
            except Exception:
                continue
        return records

    def _rewrite_undo_all(self, records: list[UndoRecord]) -> None:
        body = "\n".join(r.model_dump_json() for r in records)
        if body:
            body += "\n"
        self._atomic_write_text(self.undo_journal_file, body)

    def append_undo_record(self, record: UndoRecord) -> UndoRecord:
        """Append a move record to undo_journal.jsonl."""
        with self._lock:
            self.undo_journal_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.undo_journal_file, "a", encoding="utf-8") as f:
                f.write(record.model_dump_json() + "\n")
            return record

    def list_undo_records(self, limit: int = 50) -> list[UndoRecord]:
        """Return most recent undo journal entries (newest first)."""
        with self._lock:
            records = self._read_undo_all()
            records.reverse()
            return records[:limit]

    def get_undo_record(self, tx_id: str | None = None) -> UndoRecord | None:
        """Return a specific record by tx_id, or the latest non-undone record."""
        with self._lock:
            records = self._read_undo_all()
            if tx_id:
                for r in reversed(records):
                    if r.tx_id == tx_id:
                        return r
                return None
            for r in reversed(records):
                if not r.undone:
                    return r
            return None

    def mark_undone(self, tx_id: str) -> UndoRecord | None:
        """Mark an undo record as undone and persist."""
        with self._lock:
            records = self._read_undo_all()
            updated: UndoRecord | None = None
            for r in records:
                if r.tx_id == tx_id:
                    r.undone = True
                    r.undone_at = utc_now_iso()
                    updated = r
                    break
            if updated is not None:
                self._rewrite_undo_all(records)
            return updated

    def find_recent_move_from_dir(
        self,
        src_dir: Path | str,
        filename: str,
        file_hash: str | None,
        within_hours: float = 24.0,
    ) -> UndoRecord | None:
        """Detect if a file was moved OUT of src_dir recently (anti-tug-of-war signal)."""
        with self._lock:
            cutoff = datetime.now(timezone.utc) - timedelta(hours=max(0.1, float(within_hours)))
            dir_norm = _norm_path(src_dir)
            fname_norm = filename.lower()
            for rec in reversed(self._read_undo_all()):
                if rec.undone:
                    continue
                try:
                    if _parse_iso(rec.timestamp) < cutoff:
                        continue
                except Exception:
                    continue
                rec_src = Path(rec.src)
                if _norm_path(rec_src.parent) != dir_norm:
                    continue
                # Prefer exact content identity; fall back to filename only without a hash.
                if file_hash:
                    if rec.file_hash == file_hash:
                        return rec
                elif rec_src.name.lower() == fname_norm:
                    return rec
            return None

    # ------------------------------------------------------------------
    # Learned Rules (Tier 1 Online Learning)
    # ------------------------------------------------------------------
    def list_learned_rules(self) -> list[LearnedRule]:
        with self._lock:
            rules: list[LearnedRule] = []
            for item in self._read_json_list(self.learned_rules_file):
                try:
                    rules.append(LearnedRule.model_validate(item))
                except Exception:
                    continue
            return rules

    def add_learned_rule(self, rule: LearnedRule) -> LearnedRule:
        with self._lock:
            rules = self.list_learned_rules()
            # Deduplicate identical keyword+dest_id
            for existing in rules:
                if (
                    existing.dest_id == rule.dest_id
                    and (existing.keyword or "").lower() == (rule.keyword or "").lower()
                    and (existing.regex or "") == (rule.regex or "")
                ):
                    existing.enabled = True
                    self._write_json_list(
                        self.learned_rules_file, [r.model_dump() for r in rules]
                    )
                    return existing

            rules.append(rule)
            self._write_json_list(self.learned_rules_file, [r.model_dump() for r in rules])
            return rule

    def delete_learned_rule(self, rule_id: str) -> bool:
        with self._lock:
            rules = self.list_learned_rules()
            filtered = [r for r in rules if r.id != rule_id]
            if len(filtered) == len(rules):
                return False
            self._write_json_list(self.learned_rules_file, [r.model_dump() for r in filtered])
            return True

    def increment_rule_hit(self, rule_id: str) -> None:
        with self._lock:
            rules = self.list_learned_rules()
            changed = False
            for r in rules:
                if r.id == rule_id:
                    r.hit_count += 1
                    changed = True
                    break
            if changed:
                self._write_json_list(self.learned_rules_file, [r.model_dump() for r in rules])

    # ------------------------------------------------------------------
    # Pending Queue (Tier 3 Human-in-the-Loop)
    # ------------------------------------------------------------------
    def _load_pending_all(self) -> list[PendingDecision]:
        items: list[PendingDecision] = []
        for raw in self._read_json_list(self.pending_queue_file):
            try:
                items.append(PendingDecision.model_validate(raw))
            except Exception:
                continue
        return items

    def _save_pending_all(self, items: list[PendingDecision]) -> None:
        self._write_json_list(self.pending_queue_file, [i.model_dump() for i in items])

    def upsert_pending(self, item: PendingDecision) -> PendingDecision:
        """Insert or update an active pending item deduplicated by normalized src_path."""
        with self._lock:
            items = self._load_pending_all()
            src_norm = _norm_path(item.src_path)
            for idx, existing in enumerate(items):
                if existing.status == "pending" and _norm_path(existing.src_path) == src_norm:
                    updated = item.model_copy(
                        update={
                            "id": existing.id,
                            "notified": existing.notified,
                            "created_at": existing.created_at,
                            "updated_at": utc_now_iso(),
                        }
                    )
                    items[idx] = updated
                    self._save_pending_all(items)
                    return updated

            items.append(item)
            self._save_pending_all(items)
            return item

    def list_pending(self, only_unnotified: bool = False) -> list[PendingDecision]:
        with self._lock:
            items = [i for i in self._load_pending_all() if i.status == "pending"]
            if only_unnotified:
                items = [i for i in items if not i.notified]
            return items

    def get_pending(self, pending_id: str) -> PendingDecision | None:
        with self._lock:
            for item in self._load_pending_all():
                if item.id == pending_id:
                    return item
            return None

    def mark_pending_notified(self, pending_id: str) -> bool:
        with self._lock:
            items = self._load_pending_all()
            changed = False
            for item in items:
                if item.id == pending_id:
                    item.notified = True
                    item.updated_at = utc_now_iso()
                    changed = True
                    break
            if changed:
                self._save_pending_all(items)
            return changed

    def resolve_pending(self, pending_id: str, status: str) -> PendingDecision | None:
        with self._lock:
            items = self._load_pending_all()
            target: PendingDecision | None = None
            for item in items:
                if item.id == pending_id:
                    item.status = status  # type: ignore[assignment]
                    item.notified = True
                    item.updated_at = utc_now_iso()
                    target = item
                    break
            if target is not None:
                self._save_pending_all(items)
            return target

    # ------------------------------------------------------------------
    # Notification Outbox
    # ------------------------------------------------------------------
    def _load_notifications_all(self) -> list[NotificationEvent]:
        events: list[NotificationEvent] = []
        for raw in self._read_json_list(self.notifications_file):
            try:
                events.append(NotificationEvent.model_validate(raw))
            except Exception:
                continue
        return events

    def enqueue_notification(self, event: NotificationEvent) -> NotificationEvent:
        with self._lock:
            events = self._load_notifications_all()
            events.append(event)
            # Cap stored history to latest 200 entries to prevent unbounded growth.
            if len(events) > 200:
                events = events[-200:]
            self._write_json_list(self.notifications_file, [e.model_dump() for e in events])
            return event

    def list_notifications(
        self,
        only_unacked: bool = False,
        limit: int = 20,
    ) -> list[NotificationEvent]:
        with self._lock:
            events = self._load_notifications_all()
            if only_unacked:
                events = [e for e in events if not e.acked]
            return events[:limit]

    def ack_notification(self, event_id: str) -> bool:
        with self._lock:
            events = self._load_notifications_all()
            changed = False
            for ev in events:
                if ev.id == event_id:
                    ev.acked = True
                    changed = True
                    break
            if changed:
                self._write_json_list(self.notifications_file, [e.model_dump() for e in events])
            return changed
