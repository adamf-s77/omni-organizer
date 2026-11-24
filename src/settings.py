"""Settings loader and local Bridge secret management for Omni-Organizer v3."""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from models import V3Settings
from paths import BRIDGE_SECRET_FILE, V3_SETTINGS_EXAMPLE, V3_SETTINGS_LOCAL, ensure_runtime_dirs


def _expand_path_str(raw_path: str) -> str:
    """Expand user home shortcuts and normalize path strings without resolving symlinks."""
    if not raw_path:
        return raw_path
    return os.path.normpath(os.path.expanduser(raw_path))


def load_v3_settings(path: Path | str | None = None) -> V3Settings:
    """Load V3Settings from explicit path, env override, local config, or example config."""
    candidates: list[Path] = []
    if path is not None:
        candidates.append(Path(path))
    env_path = os.environ.get("OMNI_SETTINGS_PATH", "").strip()
    if env_path:
        candidates.append(Path(env_path))
    candidates.extend([V3_SETTINGS_LOCAL, V3_SETTINGS_EXAMPLE])

    chosen: Path | None = None
    for candidate in candidates:
        if candidate.exists():
            chosen = candidate
            break

    if chosen is None:
        return V3Settings()

    with open(chosen, "r", encoding="utf-8") as f:
        raw = json.load(f)

    settings = V3Settings.model_validate(raw)
    for scope in settings.monitored_scopes:
        scope.path = _expand_path_str(scope.path)
    for dest in settings.destinations:
        dest.path = _expand_path_str(dest.path)
    for arch in settings.archive_rules:
        arch.source_path = _expand_path_str(arch.source_path)
    settings.protected_paths = [_expand_path_str(p) for p in settings.protected_paths if p]
    return settings


def ensure_bridge_token(secret_file: Path = BRIDGE_SECRET_FILE) -> str:
    """Return existing Bridge Bearer token or generate and persist a fresh random token."""
    ensure_runtime_dirs()
    secret_file.parent.mkdir(parents=True, exist_ok=True)
    if secret_file.exists():
        token = secret_file.read_text(encoding="utf-8").strip()
        if token:
            return token

    token = secrets.token_urlsafe(32)
    secret_file.write_text(token + "\n", encoding="utf-8")
    if os.name != "nt":
        try:
            os.chmod(secret_file, 0o600)
        except OSError:
            pass
    return token
