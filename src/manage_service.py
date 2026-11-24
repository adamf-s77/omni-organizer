"""Service lifecycle, Startup shortcut, and monitored-path manager for Omni-Organizer."""

import os
import subprocess
import sys
import time
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from paths import (  # noqa: E402
    CONFIG_FILE,
    LOG_FILE,
    PROJECT_ROOT,
    PYTHONW_EXE,
    SHORTCUT_PATH,
    WATCHER_PY,
    ensure_runtime_dirs,
    load_monitored_paths,
    save_monitored_paths,
)

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

ONESHOT_TASK_NAME = "OmniOrganizer_OneShotStart"
PERMANENT_TASK_NAME = "OmniOrganizer_Watcher"
BRIDGE_HEALTH_URL = "http://127.0.0.1:8766/health"


def _run_ps(script: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )


def permanent_task_exists() -> bool:
    res = _run_ps(
        f"if (Get-ScheduledTask -TaskName '{PERMANENT_TASK_NAME}' -ErrorAction SilentlyContinue) "
        "{ 'YES' } else { 'NO' }"
    )
    return res.stdout.strip().endswith("YES")


def bridge_health() -> str:
    try:
        import urllib.request

        with urllib.request.urlopen(BRIDGE_HEALTH_URL, timeout=1.5) as resp:
            return "OK" if resp.status == 200 else f"HTTP_{resp.status}"
    except Exception:
        return "OFFLINE"


def is_watcher_running() -> list[str]:
    """Return running watcher.py Python process IDs via Get-CimInstance."""
    ps_cmd = (
        "Get-CimInstance Win32_Process | "
        "Where-Object { ($_.Name -like '*python*') -and ($_.CommandLine -like '*watcher.py*') "
        "-and (($_.CommandLine -like '*omni-organizer*') -or ($_.CommandLine -like '*organize*')) } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    res = subprocess.run(
        ["powershell", "-NoProfile", "-Command", ps_cmd],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )
    return [line.strip() for line in res.stdout.splitlines() if line.strip().isdigit()]


def _ps_quote(value: object) -> str:
    """Escape single quotes for PowerShell single-quoted literals."""
    return str(value).replace("'", "''")


def _launch_via_schtasks() -> bool:
    """Spawn watcher.py via a one-shot Scheduled Task outside caller Job Objects."""
    task = _ps_quote(ONESHOT_TASK_NAME)
    pyw = _ps_quote(PYTHONW_EXE)
    watcher = _ps_quote(WATCHER_PY)
    proj = _ps_quote(PROJECT_ROOT)

    ps_start = (
        f"$action = New-ScheduledTaskAction -Execute '{pyw}' -Argument '\"{watcher}\"' -WorkingDirectory '{proj}'; "
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries -ExecutionTimeLimit ([TimeSpan]::Zero); "
        f"Register-ScheduledTask -TaskName '{task}' -Action $action -Settings $settings -Force | Out-Null; "
        f"Start-ScheduledTask -TaskName '{task}'"
    )
    ps_cleanup = (
        f"Unregister-ScheduledTask -TaskName '{task}' -Confirm:$false -ErrorAction SilentlyContinue"
    )
    try:
        res = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_start],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if res.returncode != 0:
            return False

        for _ in range(10):
            time.sleep(0.5)
            if is_watcher_running():
                return True
        return False
    finally:
        subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cleanup],
            capture_output=True,
        )


def _launch_via_popen() -> bool:
    """Fallback launcher attempting Job-Object breakaway before plain detached Popen."""
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    CREATE_BREAKAWAY_FROM_JOB = 0x01000000
    cmd = [str(PYTHONW_EXE), str(WATCHER_PY)]

    try:
        subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            creationflags=CREATE_BREAKAWAY_FROM_JOB | DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    except PermissionError:
        subprocess.Popen(
            cmd,
            cwd=str(PROJECT_ROOT),
            creationflags=DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP,
            close_fds=True,
        )
    time.sleep(2)
    return bool(is_watcher_running())


def start_watcher() -> None:
    """Start the background watcher outside the current shell Job Object."""
    ensure_runtime_dirs()
    pids = is_watcher_running()
    if pids:
        print(f"[*] 監控服務已在運行中 (PID: {', '.join(pids)})")
        return

    launched = False
    if permanent_task_exists():
        res = _run_ps(f"Start-ScheduledTask -TaskName '{PERMANENT_TASK_NAME}'")
        if res.returncode == 0:
            for _ in range(12):
                time.sleep(0.5)
                if is_watcher_running():
                    launched = True
                    break
    if not launched:
        launched = _launch_via_schtasks()
    if not launched:
        launched = _launch_via_popen()

    pids = is_watcher_running()
    if launched and pids:
        print(f"[+] 監控服務啟動成功！(PID: {', '.join(pids)})")
    else:
        print(f"[-] 啟動失敗，請檢查日誌：{LOG_FILE}")


def stop_watcher() -> None:
    """Terminate all running watcher.py processes."""
    pids = is_watcher_running()
    if not pids:
        print("[*] 監控服務未在運行。")
        return
    if permanent_task_exists():
        # Stop via Task Scheduler first so the restart-on-failure policy does not respawn it.
        _run_ps(f"Stop-ScheduledTask -TaskName '{PERMANENT_TASK_NAME}' -ErrorAction SilentlyContinue")
        time.sleep(1)
    for pid in is_watcher_running():
        subprocess.run(["taskkill", "/F", "/PID", pid], capture_output=True)
    print("[+] 已停止監控服務。")


def restart_watcher() -> None:
    """Stop then restart the background watcher."""
    stop_watcher()
    time.sleep(1)
    start_watcher()


def is_autostart() -> bool:
    """Autostart is the user-layer AtLogOn Scheduled Task (legacy .lnk still honoured)."""
    return permanent_task_exists() or SHORTCUT_PATH.exists()


def autostart_mode() -> str:
    if permanent_task_exists():
        return "TASK_SCHEDULER"
    if SHORTCUT_PATH.exists():
        return "LEGACY_STARTUP_LNK"
    return "NONE"


def _remove_legacy_shortcut() -> None:
    if SHORTCUT_PATH.exists():
        try:
            SHORTCUT_PATH.unlink()
            print(f"[*] 已移除舊版 Startup 捷徑（避免重複啟動）：{SHORTCUT_PATH}")
        except OSError as e:
            print(f"[-] 無法移除舊版捷徑：{e}")


def enable_autostart() -> None:
    """Register a per-user AtLogOn task: pythonw, battery-safe, no time limit, auto-restart."""
    task = _ps_quote(PERMANENT_TASK_NAME)
    pyw = _ps_quote(PYTHONW_EXE)
    watcher = _ps_quote(WATCHER_PY)
    proj = _ps_quote(PROJECT_ROOT)
    ps = (
        "$ErrorActionPreference = 'Stop'; "
        "$user = \"$env:USERDOMAIN\\$env:USERNAME\"; "
        f"$action = New-ScheduledTaskAction -Execute '{pyw}' -Argument '\"{watcher}\"' -WorkingDirectory '{proj}'; "
        "$trigger = New-ScheduledTaskTrigger -AtLogOn -User $user; "
        "$principal = New-ScheduledTaskPrincipal -UserId $user -LogonType Interactive -RunLevel Limited; "
        "$settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries "
        "-ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) "
        "-MultipleInstances IgnoreNew -StartWhenAvailable; "
        f"Register-ScheduledTask -TaskName '{task}' -Action $action -Trigger $trigger -Principal $principal "
        "-Settings $settings -Description 'Omni-Organizer v3 user-session watcher (AtLogOn, pythonw)' -Force | Out-Null; "
        "'REGISTERED'"
    )
    res = _run_ps(ps)
    if res.returncode == 0 and "REGISTERED" in res.stdout:
        _remove_legacy_shortcut()
        print(f"[+] 已註冊使用者層開機自啟排程任務：{PERMANENT_TASK_NAME}")
    else:
        print(f"[-] 註冊排程任務失敗：{(res.stderr or res.stdout).strip()[:500]}")


def disable_autostart() -> None:
    res = _run_ps(
        f"Unregister-ScheduledTask -TaskName '{_ps_quote(PERMANENT_TASK_NAME)}' -Confirm:$false "
        "-ErrorAction SilentlyContinue; 'DONE'"
    )
    _remove_legacy_shortcut()
    if res.returncode == 0:
        print("[+] 已停用開機自動啟動（排程任務已移除）。")
    else:
        print(f"[-] 移除排程任務時發生問題：{res.stderr.strip()[:300]}")


def add_path(new_path: str) -> None:
    """Add an existing directory to monitored_paths.json and restart if running."""
    p = os.path.abspath(new_path)
    if not os.path.exists(p):
        print(f"[-] 錯誤：路徑不存在 -> {p}")
        return
    paths = load_monitored_paths(filter_existing=False)
    if any(os.path.normcase(existing) == os.path.normcase(p) for existing in paths):
        print(f"[*] 該路徑已在監控名單中 -> {p}")
        return
    paths.append(p)
    save_monitored_paths(paths)
    print(f"[+] 成功新增監控路徑 -> {p}")
    if is_watcher_running():
        print("[*] 正在重啟監控服務以套用新路徑...")
        restart_watcher()


def remove_path(target_path: str) -> None:
    """Remove a directory from monitored_paths.json and restart if running."""
    paths = load_monitored_paths(filter_existing=False)
    target_norm = os.path.normcase(os.path.abspath(target_path))
    found = next(
        (p for p in paths if os.path.normcase(os.path.abspath(p)) == target_norm),
        None,
    )
    if not found:
        print(f"[-] 找不到該監控路徑: {target_path}")
        return
    paths.remove(found)
    save_monitored_paths(paths)
    print(f"[+] 已從監控名單中移除 -> {found}")
    if is_watcher_running():
        print("[*] 正在重啟監控服務以套用變更...")
        restart_watcher()


def list_paths() -> None:
    """Print semicolon-delimited monitored paths for menu.ps1 consumption."""
    paths = load_monitored_paths(filter_existing=False)
    print("CURRENT_PATHS=" + ";".join(paths))


def trigger_scan(*, clear_undo_cooldowns: bool = False, launch_popup: bool = True) -> int:
    """Trigger an immediate synchronous root-folder rescan via the local Bridge API."""
    import json
    import urllib.error
    import urllib.request
    from settings import ensure_bridge_token, load_v3_settings

    settings = load_v3_settings()
    if not settings.bridge.enabled:
        print("[-] Bridge API 未啟用，無法執行即時掃描。")
        return 1

    token = ensure_bridge_token()
    url = f"http://{settings.bridge.host}:{settings.bridge.port}/rescan"
    payload = json.dumps(
        {
            "immediate": True,
            "clear_undo_cooldowns": clear_undo_cooldowns,
            "launch_popup": launch_popup,
        }
    ).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        headers={
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=120) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        print(f"[-] 即時掃描失敗：HTTP {exc.code}")
        return 1
    except Exception as exc:
        print(f"[-] 無法連線至背景服務 Bridge ({type(exc).__name__})，請先確認服務已啟動。")
        return 1

    processed = int(data.get("processed") or 0)
    enqueued = int(data.get("enqueued") or 0)
    pruned = int(data.get("pruned_missing") or 0)
    pending = int(data.get("pending_count") or 0)
    print(f"RESCAN_OK processed={processed} enqueued={enqueued} pruned={pruned} pending={pending}")
    print(
        f"[+] 掃描完成：立即檢測 {processed} 筆、排入穩定檢查 {enqueued} 筆、"
        f"清理遺失 {pruned} 筆、待確認 {pending} 筆。"
    )
    return 0


def main() -> None:
    """CLI entrypoint for service and path management."""
    if len(sys.argv) < 2:
        print(
            "Usage: manage_service.py "
            "[status|start|stop|restart|scan|scan_force|enable_auto|disable_auto|list_paths|add_path|remove_path]"
        )
        return

    arg = sys.argv[1].lower()
    if arg == "status":
        pids = is_watcher_running()
        auto = is_autostart()
        paths = load_monitored_paths(filter_existing=False)
        print("SERVICE_RUNNING=" + ("YES" if pids else "NO"))
        print("SERVICE_PIDS=" + (",".join(pids) if pids else "NONE"))
        print("AUTOSTART=" + ("YES" if auto else "NO"))
        print("AUTOSTART_MODE=" + autostart_mode())
        print("BRIDGE_HEALTH=" + bridge_health())
        print("PATHS_COUNT=" + str(len(paths)))
        print(f"CONFIG_FILE={CONFIG_FILE}")
        print(f"LOG_FILE={LOG_FILE}")
    elif arg == "start":
        start_watcher()
    elif arg == "stop":
        stop_watcher()
    elif arg == "restart":
        restart_watcher()
    elif arg == "scan":
        raise SystemExit(trigger_scan(clear_undo_cooldowns=False, launch_popup=True))
    elif arg == "scan_force":
        raise SystemExit(trigger_scan(clear_undo_cooldowns=True, launch_popup=True))
    elif arg == "enable_auto":
        enable_autostart()
    elif arg == "disable_auto":
        disable_autostart()
    elif arg == "list_paths":
        list_paths()
    elif arg == "add_path" and len(sys.argv) > 2:
        add_path(sys.argv[2])
    elif arg == "remove_path" and len(sys.argv) > 2:
        remove_path(sys.argv[2])


if __name__ == "__main__":
    main()
