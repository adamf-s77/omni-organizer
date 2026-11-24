"""Monitored-scope resolution: which scope (and sub-folder) a file event belongs to."""

from __future__ import annotations

import os
from pathlib import Path

from models import MonitoredScope


def _norm(path: Path | str) -> str:
    return os.path.normcase(os.path.abspath(str(path)))


def _prefix(root_norm: str) -> str:
    return root_norm if root_norm.endswith(os.sep) else root_norm + os.sep


def resolve_scope_for_path(
    filepath: Path | str,
    scopes: list[MonitoredScope],
) -> tuple[MonitoredScope, str] | None:
    """Return (scope, rel_dir) for a file inside a monitored area, else None.

    rel_dir is "" for scope-root files, otherwise the parent folder relative to the
    scope root (must be in allowed_subdirs and within max_depth).
    """
    parent = _norm(os.path.dirname(os.path.abspath(str(filepath))))
    best: tuple[MonitoredScope, str] | None = None
    best_len = -1

    for scope in scopes:
        if not scope.enabled:
            continue
        root = _norm(scope.path)
        if parent == root:
            rel = ""
        elif scope.mode == "root_and_subdirs" and parent.startswith(_prefix(root)):
            rel = os.path.relpath(parent, root)
            depth = len(Path(rel).parts)
            allowed = {os.path.normcase(os.path.normpath(s)) for s in scope.allowed_subdirs}
            if depth > scope.max_depth or os.path.normcase(rel) not in allowed:
                continue
        else:
            continue
        if len(root) > best_len:
            best, best_len = (scope, rel), len(root)
    return best


def top_level_subdir(scope: MonitoredScope, path: Path | str) -> str | None:
    """Return the first path component below the scope root, or None for root files."""
    root = _norm(scope.path)
    p = _norm(path)
    if not p.startswith(_prefix(root)):
        return None
    rel_parts = Path(os.path.relpath(p, root)).parts
    return rel_parts[0] if len(rel_parts) > 1 else None


def is_under(path: Path | str, directory: Path | str) -> bool:
    p, d = _norm(path), _norm(directory)
    return p == d or p.startswith(_prefix(d))
