"""Age-based lifecycle archive scanner (proposes moves via Discord HITL, never moves silently)."""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path
from typing import Callable

from executor import compute_file_hash
from guardrails import is_forbidden_path, is_ignored_file, is_sensitive_file
from models import PendingDecision, V3Settings
from scopes import is_under
from state_store import StateStore


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _in_privacy_zone(path: Path, settings: V3Settings) -> bool:
    for dest_id in settings.privacy_zone_dest_ids:
        dest = settings.get_destination(dest_id)
        if dest and is_under(path, dest.path):
            return True
    return False


def scan_archive_candidates(
    settings: V3Settings,
    store: StateStore,
    *,
    now: float | None = None,
    logger: Callable[[str], None] | None = None,
) -> list[PendingDecision]:
    """Scan archive_rules for aged files and enqueue Discord HITL confirmation cards."""
    cap = int(settings.archive_max_per_sweep)
    if cap <= 0 or not settings.archive_rules:
        return []

    log = logger or (lambda msg: None)
    current_ts = time.time() if now is None else float(now)
    active_paths = {_norm(item.src_path) for item in store.list_pending()}
    queued: list[PendingDecision] = []

    for rule in settings.archive_rules:
        if len(queued) >= cap:
            break
        if not rule.enabled:
            continue
        dest = settings.get_destination(rule.dest_id)
        if dest is None:
            continue
        root = Path(os.path.abspath(rule.source_path))
        if not root.is_dir():
            continue

        min_age_sec = max(1, int(rule.min_age_days)) * 86400
        ext_filter = {e.lower().lstrip(".") for e in rule.extensions if e}
        iterator = root.rglob("*") if rule.recursive else root.iterdir()

        for candidate in iterator:
            if len(queued) >= cap:
                break
            if not candidate.is_file():
                continue
            cand_norm = _norm(candidate)
            if cand_norm in active_paths:
                continue
            if _norm(dest.path) == _norm(candidate.parent):
                continue
            if is_forbidden_path(candidate, scope_root=root, extra_forbidden=settings.protected_paths)[0]:
                continue
            if is_ignored_file(candidate)[0]:
                continue
            if is_sensitive_file(candidate)[0] or _in_privacy_zone(candidate, settings):
                continue
            if ext_filter and candidate.suffix.lower().lstrip(".") not in ext_filter:
                continue

            try:
                st = candidate.stat()
            except OSError:
                continue
            age_sec = current_ts - st.st_mtime
            if age_sec < min_age_sec:
                continue

            try:
                file_hash = compute_file_hash(candidate)
            except OSError:
                continue
            if store.is_on_cooldown(candidate, file_hash)[0]:
                continue

            age_days = int(age_sec // 86400)
            item = PendingDecision(
                id=f"p_{uuid.uuid4().hex[:8]}",
                kind="archive",
                src_path=str(candidate),
                filename=candidate.name,
                extension=candidate.suffix.lower().lstrip("."),
                scope_id=rule.id,
                size_bytes=st.st_size,
                file_hash=file_hash,
                is_sensitive=False,
                is_misplaced_candidate=False,
                suggested_dest_id=dest.id,
                suggested_dest_label=dest.label,
                suggested_dest_path=dest.path,
                confidence=1.0,
                reason=f"超過 {age_days} 天未變動（規則：{rule.name}），建議封存至 D 槽",
                suggested_keyword=None,
                provider="lifecycle",
                ignore_cooldown_hours=float(rule.ignore_cooldown_hours),
            )
            saved = store.upsert_pending(item)
            active_paths.add(cand_norm)
            queued.append(saved)
            log(f"[archive] 待確認封存 {candidate.name} (閒置 {age_days} 天) -> {dest.label}")

    return queued
