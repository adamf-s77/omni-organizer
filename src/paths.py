"""Shared path constants and configuration helpers for Omni-Organizer."""

import json
import os
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
CONFIG_DIR = PROJECT_ROOT / "config"
CONFIG_FILE = CONFIG_DIR / "config.yaml"
MONITORED_PATHS_FILE = CONFIG_DIR / "monitored_paths.json"
LOGS_DIR = PROJECT_ROOT / "logs"
LOG_FILE = LOGS_DIR / "organize.log"
ERR_LOG = LOGS_DIR / "error.log"
REPORTS_DIR = PROJECT_ROOT / "reports"
DOCS_DIR = PROJECT_ROOT / "docs"
ARCH_MAP_FILE = DOCS_DIR / "pc_architecture_map.md"
SCRIPTS_DIR = PROJECT_ROOT / "scripts"
AUDIT_SCRIPT = SCRIPTS_DIR / "deep_audit.py"
WATCHER_PY = SRC_DIR / "watcher.py"
DESKTOP_PROMPT_PY = SRC_DIR / "desktop_prompt.py"
CONTROL_DASHBOARD_PY = SRC_DIR / "control_dashboard.py"

STATE_DIR = PROJECT_ROOT / "state"
DESKTOP_PROMPT_LOCK = STATE_DIR / "desktop_prompt.lock"
DASHBOARD_LOCK = STATE_DIR / "control_dashboard.lock"
V3_SETTINGS_LOCAL = CONFIG_DIR / "v3_settings.local.json"
V3_SETTINGS_EXAMPLE = CONFIG_DIR / "v3_settings.example.json"
BRIDGE_SECRET_FILE = CONFIG_DIR / "bridge_secret.local.txt"
LEARNED_RULES_FILE = STATE_DIR / "learned_rules.json"
PENDING_QUEUE_FILE = STATE_DIR / "pending_queue.json"
UNDO_JOURNAL_FILE = STATE_DIR / "undo_journal.jsonl"
COOLDOWNS_FILE = STATE_DIR / "cooldowns.json"
NOTIFICATIONS_FILE = STATE_DIR / "notifications.json"

STARTUP_DIR = Path(
    os.path.join(
        os.environ.get("APPDATA", os.path.expanduser("~")),
        r"Microsoft\Windows\Start Menu\Programs\Startup",
    )
)
SHORTCUT_PATH = STARTUP_DIR / "DesktopAutoOrganizer.lnk"

PYTHON_DIR = Path(sys.executable).resolve().parent
PYTHON_EXE = PYTHON_DIR / "python.exe"
if not PYTHON_EXE.exists():
    PYTHON_EXE = Path(sys.executable).resolve()

PYTHONW_EXE = PYTHON_DIR / "pythonw.exe"
if not PYTHONW_EXE.exists():
    PYTHONW_EXE = PYTHON_EXE


def get_default_monitored_paths() -> list[str]:
    """Return default monitored directories for this Windows user."""
    home = Path.home()
    return [
        str(home / "Desktop"),
        str(home / "Downloads"),
        str(home / "Pictures" / "Screenshots"),
    ]


def ensure_runtime_dirs() -> None:
    """Ensure runtime directories exist before reading configs or writing logs."""
    for directory in (CONFIG_DIR, LOGS_DIR, REPORTS_DIR, STATE_DIR):
        directory.mkdir(parents=True, exist_ok=True)


def load_monitored_paths(filter_existing: bool = False) -> list[str]:
    """Load monitored directory paths from monitored_paths.json."""
    if MONITORED_PATHS_FILE.exists():
        try:
            with open(MONITORED_PATHS_FILE, "r", encoding="utf-8") as f:
                raw_paths = json.load(f)
            abs_paths = [os.path.abspath(p) for p in raw_paths if isinstance(p, str)]
            if filter_existing:
                return [p for p in abs_paths if os.path.exists(p)]
            return abs_paths
        except Exception:
            pass
    defaults = get_default_monitored_paths()
    if filter_existing:
        return [p for p in defaults if os.path.exists(p)]
    return defaults


def save_monitored_paths(paths: list[str]) -> None:
    """Persist monitored directory paths to monitored_paths.json."""
    ensure_runtime_dirs()
    with open(MONITORED_PATHS_FILE, "w", encoding="utf-8") as f:
        json.dump(paths, f, ensure_ascii=False, indent=2)
