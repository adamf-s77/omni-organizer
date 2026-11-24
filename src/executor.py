"""Safe, journaled, reversible file moves for Omni-Organizer v3."""

from __future__ import annotations

import hashlib
import os
import shutil
import uuid
from pathlib import Path

from guardrails import is_forbidden_path, is_valid_destination
from models import UndoRecord, V3Settings
from state_store import StateStore

FULL_HASH_LIMIT = 100 * 1024 * 1024
PARTIAL_CHUNK = 1024 * 1024


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def compute_file_hash(path: Path | str) -> str:
    """Full SHA-256 for files <= 100MB, otherwise size + head/tail partial hash."""
    p = Path(path)
    size = p.stat().st_size
    h = hashlib.sha256()
    with open(p, "rb") as f:
        if size <= FULL_HASH_LIMIT:
            for chunk in iter(lambda: f.read(PARTIAL_CHUNK), b""):
                h.update(chunk)
            return h.hexdigest()
        h.update(str(size).encode())
        h.update(f.read(PARTIAL_CHUNK))
        f.seek(max(0, size - PARTIAL_CHUNK))
        h.update(f.read(PARTIAL_CHUNK))
    return "partial_sha256:" + h.hexdigest()


def safe_move_file(
    src: Path | str,
    dest_dir: Path | str,
    settings: V3Settings,
    store: StateStore,
    *,
    tier: str,
    reason: str,
    confidence: float = 1.0,
    rule_id: str | None = None,
    on_conflict: str = "skip",
    file_hash: str | None = None,
) -> dict:
    """Move src into a whitelisted dest_dir, never overwriting, and journal for undo."""
    src_p = Path(os.path.abspath(str(src)))
    dest_p = Path(os.path.abspath(str(dest_dir)))

    if not src_p.is_file():
        return {"status": "error", "error": "source_missing", "src": str(src_p)}

    forbidden, why = is_forbidden_path(src_p, extra_forbidden=settings.protected_paths)
    if forbidden:
        return {"status": "blocked", "error": f"source_forbidden:{why}", "src": str(src_p)}

    if not is_valid_destination(dest_p, settings.destinations, extra_forbidden=settings.protected_paths):
        return {"status": "blocked", "error": "destination_not_whitelisted", "dst": str(dest_p)}

    if _norm(src_p.parent) == _norm(dest_p):
        return {"status": "noop", "error": "already_in_destination", "src": str(src_p)}

    dest_p.mkdir(parents=True, exist_ok=True)
    target = dest_p / src_p.name
    if target.exists():
        if on_conflict == "skip":
            return {"status": "skipped_conflict", "src": str(src_p), "dst": str(target)}
        return {"status": "error", "error": f"unsupported_on_conflict:{on_conflict}"}

    digest = file_hash or compute_file_hash(src_p)
    store.mark_self_move(src_p)
    store.mark_self_move(target)
    shutil.move(str(src_p), str(target))

    record = UndoRecord(
        tx_id=f"tx_{uuid.uuid4().hex[:8]}",
        src=str(src_p),
        dst=str(target),
        file_hash=digest,
        tier=tier,
        reason=reason,
        confidence=float(confidence),
        rule_id=rule_id,
    )
    store.append_undo_record(record)
    return {"status": "moved", "tx_id": record.tx_id, "src": str(src_p), "dst": str(target)}


def undo_move(store: StateStore, settings: V3Settings, tx_id: str | None = None) -> dict:
    """Revert the latest (or specified) move and add an anti-tug-of-war cooldown."""
    record = store.get_undo_record(tx_id)
    if record is None:
        return {"ok": False, "error": "no_undo_record"}
    if record.undone:
        return {"ok": False, "error": "already_undone", "tx_id": record.tx_id}

    dst_p = Path(record.dst)
    src_p = Path(record.src)
    if not dst_p.is_file():
        return {"ok": False, "error": "moved_file_missing", "tx_id": record.tx_id}
    if src_p.exists():
        return {"ok": False, "error": "original_path_occupied", "tx_id": record.tx_id}

    src_p.parent.mkdir(parents=True, exist_ok=True)
    store.mark_self_move(dst_p)
    store.mark_self_move(src_p)
    shutil.move(str(dst_p), str(src_p))
    store.mark_undone(record.tx_id)
    store.add_cooldown(src_p, record.file_hash, settings.cooldown_hours, "user_undo")
    return {"ok": True, "tx_id": record.tx_id, "restored_to": str(src_p), "from": str(dst_p)}
