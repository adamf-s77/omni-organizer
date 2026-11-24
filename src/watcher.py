"""Omni-Organizer v3 background daemon: watchdog -> quiescence -> 3-tier engine + Bridge API."""

import os
import signal
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paths import (  # noqa: E402
    CONFIG_FILE,
    ERR_LOG,
    LOG_FILE,
    PROJECT_ROOT,
    PYTHON_EXE,
    ensure_runtime_dirs,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

from watchdog.events import FileSystemEventHandler  # noqa: E402
from watchdog.observers import Observer  # noqa: E402

from bridge_api import BridgeServer  # noqa: E402
from decision_engine import DecisionEngine  # noqa: E402
from desktop_prompt import launch_desktop_prompt_if_needed  # noqa: E402
from guardrails import is_forbidden_path, is_ignored_file  # noqa: E402
from lifecycle_archiver import scan_archive_candidates  # noqa: E402
from providers import create_provider  # noqa: E402
from scopes import resolve_scope_for_path  # noqa: E402
from settings import ensure_bridge_token, load_v3_settings  # noqa: E402
from stability import FileQuiescenceTracker, is_file_locked  # noqa: E402
from state_store import StateStore  # noqa: E402

RUN_LOCK = threading.Lock()
LOG_LOCK = threading.Lock()
STOP_EVENT = threading.Event()


def log(message: str) -> None:
    """Write a timestamped line to stdout and logs/organize.log."""
    ensure_runtime_dirs()
    line = f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {message}"
    with LOG_LOCK:
        try:
            print(line, flush=True)
        except Exception:
            pass
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as f:
                f.write(line + "\n")
        except Exception:
            pass


def log_err(err: str) -> None:
    ensure_runtime_dirs()
    try:
        with open(ERR_LOG, "a", encoding="utf-8") as f:
            f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {err}\n")
    except Exception:
        pass


def run_organize_cmd(tags: str | None = None, label: str = "organize") -> None:
    """Invoke `python -m organize run` (kept for the daily maintenance sweep)."""
    with RUN_LOCK:
        env = os.environ.copy()
        env.update(PYTHONIOENCODING="utf-8", PYTHONUTF8="1", PYTHONUNBUFFERED="1")
        cmd = [str(PYTHON_EXE), "-m", "organize", "run"]
        if tags:
            cmd.extend(["--tags", tags])
        cmd.append(str(CONFIG_FILE))
        log(f"開始執行 {label} (tags={tags or 'all'})...")
        try:
            kwargs = {"creationflags": subprocess.CREATE_NO_WINDOW} if sys.platform == "win32" else {}
            result = subprocess.run(
                cmd, capture_output=True, text=True, env=env, encoding="utf-8",
                errors="replace", timeout=300, **kwargs,
            )
            out = result.stdout.strip()
            if out and out != "Nothing to do":
                log(f"整理結果:\n{out}")
            if result.stderr.strip():
                log(f"訊息: {result.stderr.strip()}")
        except Exception as e:
            log(f"執行 organize 失敗: {e}")


class ScopedEventHandler(FileSystemEventHandler):
    """Filter raw watchdog events to in-scope, non-ignored files and feed the quiescence tracker."""

    def __init__(self, settings, store: StateStore, tracker: FileQuiescenceTracker) -> None:
        super().__init__()
        self.settings, self.store, self.tracker = settings, store, tracker

    def _consider(self, filepath: str) -> None:
        try:
            if resolve_scope_for_path(filepath, self.settings.monitored_scopes) is None:
                return
            if is_ignored_file(filepath)[0]:
                return
            if self.store.is_self_move(filepath):
                return
            self.tracker.notify(filepath)
        except Exception:
            log_err(f"Handler error: {traceback.format_exc()}")

    def on_created(self, event) -> None:
        if not event.is_directory:
            self._consider(event.src_path)

    def on_modified(self, event) -> None:
        if not event.is_directory:
            self._consider(event.src_path)

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self._consider(event.dest_path)


def maintenance_scheduler(interval: int, settings, store: StateStore) -> None:
    if STOP_EVENT.wait(60):
        return
    while not STOP_EVENT.is_set():
        try:
            run_organize_cmd(tags="maintenance", label="每日定期清道夫維護")
            queued = scan_archive_candidates(settings, store, logger=log)
            if queued:
                launch_desktop_prompt_if_needed(settings, logger=log)
        except Exception:
            log_err(f"Maintenance error: {traceback.format_exc()}")
        if STOP_EVENT.wait(interval):
            return


def main() -> None:
    ensure_runtime_dirs()
    observer = None
    tracker = None
    bridge = None
    try:
        settings = load_v3_settings()
        store = StateStore()
        provider = create_provider(settings.ai)
        engine = DecisionEngine(settings, store, provider, logger=log)

        def on_ready(path: Path) -> None:
            try:
                result = engine.process_file(path)
                action = result.get("action")
                if action not in {"ignored_self_move", "blocked", "missing", "out_of_scope",
                                  "already_correct", "kept_in_place", "cooldown"}:
                    log(f"[engine] {path.name}: {action}")
                if action in {"pending_review", "pending_sensitive"}:
                    launch_desktop_prompt_if_needed(settings, logger=log)
            except Exception:
                log_err(f"Engine error on {path}: {traceback.format_exc()}")

        tracker = FileQuiescenceTracker(settings.quiescence_seconds, on_ready=on_ready)

        def rescan_scope_roots(
            immediate: bool = True,
            clear_undo_cooldowns: bool = False,
        ) -> dict[str, int]:
            if clear_undo_cooldowns:
                cleared = store.clear_cooldowns_by_reason({"user_undo"})
                if cleared:
                    log(f"[rescan] 已清除 {cleared} 筆 user_undo 冷卻記錄以重新檢測")
            pending_norms = {
                os.path.normcase(os.path.abspath(item.src_path))
                for item in store.list_pending()
            }
            processed = 0
            enqueued = 0
            new_pending = 0
            now_ts = time.time()
            for scope in settings.monitored_scopes:
                if not scope.enabled:
                    continue
                root_dir = Path(scope.path)
                if not root_dir.is_dir():
                    continue
                try:
                    entries = list(root_dir.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    try:
                        if not entry.is_file():
                            continue
                        norm_entry = os.path.normcase(os.path.abspath(str(entry)))
                        if norm_entry in pending_norms:
                            continue
                        if is_forbidden_path(entry, scope_root=root_dir, extra_forbidden=settings.protected_paths)[0]:
                            continue
                        if is_ignored_file(entry)[0]:
                            continue
                        if store.is_self_move(entry):
                            continue
                        if store.is_on_cooldown(entry)[0]:
                            continue
                        st = entry.stat()
                        age = max(0.0, now_ts - float(st.st_mtime))
                        if immediate and age >= float(settings.quiescence_seconds) and not is_file_locked(entry):
                            result = engine.process_file(entry)
                            processed += 1
                            action = result.get("action")
                            if action not in {
                                "ignored_self_move",
                                "blocked",
                                "missing",
                                "out_of_scope",
                                "already_correct",
                                "kept_in_place",
                                "cooldown",
                            }:
                                log(f"[engine] {entry.name}: {action}")
                            if action in {"pending_review", "pending_sensitive"}:
                                new_pending += 1
                                pending_norms.add(norm_entry)
                        else:
                            tracker.notify(str(entry))
                            enqueued += 1
                    except OSError:
                        continue
            return {"processed": processed, "enqueued": enqueued, "new_pending": new_pending}

        log("=" * 60)
        log(f"Omni-Organizer v{settings.version} 三層決策智慧整理服務已啟動 (PID: {os.getpid()})")
        log(f"  + 專案根目錄: {PROJECT_ROOT}")
        log(f"  + AI Provider: {settings.ai.provider} / {settings.ai.model}")

        if settings.bridge.enabled:
            try:
                bridge = BridgeServer(
                    settings.bridge.host,
                    settings.bridge.port,
                    ensure_bridge_token(),
                    engine,
                    store,
                    settings,
                    logger=log,
                    rescan_callback=rescan_scope_roots,
                )
                host, port = bridge.start()
                log(f"  + Bridge API: http://{host}:{port} (僅本機、Bearer Token 保護)")
            except OSError as e:
                bridge = None
                log(f"  ! Bridge API 啟動失敗（Port 可能被佔用）: {e}")

        handler = ScopedEventHandler(settings, store, tracker)
        observer = Observer()
        for scope in settings.monitored_scopes:
            if not scope.enabled:
                continue
            if not os.path.isdir(scope.path):
                log(f"  ! 監控路徑不存在，略過: {scope.path}")
                continue
            observer.schedule(handler, path=scope.path, recursive=(scope.mode == "root_and_subdirs"))
            log(f"  + 監控範圍 [{scope.id}] {scope.path} ({scope.mode}, depth<={scope.max_depth})")
        observer.start()
        log(f"  + 每日清道夫排程: 每 {settings.maintenance_interval_seconds // 3600} 小時")
        log("=" * 60)

        threading.Thread(
            target=maintenance_scheduler,
            args=(settings.maintenance_interval_seconds, settings, store),
            daemon=True,
        ).start()

        def _stop(signum, frame):
            STOP_EVENT.set()

        for sig in (signal.SIGINT, signal.SIGTERM):
            try:
                signal.signal(sig, _stop)
            except (ValueError, OSError):
                pass

        while not STOP_EVENT.wait(1):
            pass
    except Exception as e:
        log_err(f"Main crashed: {traceback.format_exc()}")
        log(f"監控服務異常崩潰: {e}")
        raise SystemExit(1)  # non-zero so Task Scheduler restart policy kicks in
    finally:
        STOP_EVENT.set()
        if tracker is not None:
            tracker.stop()
        if observer is not None:
            observer.stop()
            observer.join(timeout=5)
        if bridge is not None:
            bridge.stop()
        log("監控服務已結束。")


if __name__ == "__main__":
    main()
