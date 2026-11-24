"""Native cross-platform Desktop Approval Popup (tkinter + localhost Bridge API)."""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

import httpx

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from models import V3Settings  # noqa: E402
from paths import DESKTOP_PROMPT_LOCK, DESKTOP_PROMPT_PY, PYTHONW_EXE  # noqa: E402
from settings import ensure_bridge_token, load_v3_settings  # noqa: E402

_LAUNCH_LOCK = threading.Lock()
_LAST_LAUNCH_TS = 0.0


def _fmt_size(size_bytes: int) -> str:
    size = float(max(0, int(size_bytes or 0)))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024.0 or unit == "GB":
            return f"{int(size)} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024.0
    return f"{size_bytes} B"


def _is_pid_running(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        try:
            k32 = ctypes.windll.kernel32
            handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong(0)
                if k32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return exit_code.value == STILL_ACTIVE
                return True
            finally:
                k32.CloseHandle(handle)
        except Exception:
            return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def is_prompt_locked(lock_file: Path = DESKTOP_PROMPT_LOCK) -> bool:
    """Return True if another desktop_prompt instance currently owns the lockfile."""
    if not lock_file.is_file():
        return False
    try:
        raw = lock_file.read_text(encoding="utf-8").strip()
        if not raw.isdigit():
            return False
        pid = int(raw)
    except OSError:
        return False

    if pid == os.getpid():
        return True
    if _is_pid_running(pid):
        return True
    try:
        lock_file.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def acquire_prompt_lock(lock_file: Path = DESKTOP_PROMPT_LOCK) -> bool:
    """Acquire single-instance lockfile for this process."""
    if is_prompt_locked(lock_file):
        return False
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    lock_file.write_text(str(os.getpid()), encoding="utf-8")
    return True


def release_prompt_lock(lock_file: Path = DESKTOP_PROMPT_LOCK) -> None:
    if not lock_file.is_file():
        return
    try:
        raw = lock_file.read_text(encoding="utf-8").strip()
        if raw == str(os.getpid()):
            lock_file.unlink(missing_ok=True)
    except OSError:
        pass


def launch_desktop_prompt_if_needed(
    settings: V3Settings,
    logger: Callable[[str], None] | None = None,
    *,
    lock_file: Path = DESKTOP_PROMPT_LOCK,
    now: float | None = None,
    spawner: Optional[Callable[[list[str]], Any]] = None,
) -> bool:
    """Spawn desktop_prompt.py detached if enabled, not locked, and outside cooldown."""
    global _LAST_LAUNCH_TS
    if not settings.ui.desktop_popup_enabled or not settings.bridge.enabled:
        return False

    ts = time.monotonic() if now is None else float(now)
    with _LAUNCH_LOCK:
        if is_prompt_locked(lock_file):
            return False
        cooldown = max(0.5, float(settings.ui.popup_cooldown_seconds))
        if _LAST_LAUNCH_TS and (ts - _LAST_LAUNCH_TS) < cooldown:
            return False
        _LAST_LAUNCH_TS = ts

    cmd = [str(PYTHONW_EXE), str(DESKTOP_PROMPT_PY)]
    try:
        if spawner is not None:
            spawner(cmd)
        else:
            kwargs: dict[str, Any] = {"close_fds": True}
            if sys.platform == "win32":
                kwargs["creationflags"] = (
                    subprocess.CREATE_NO_WINDOW
                    | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                )
            subprocess.Popen(cmd, cwd=str(SRC_DIR.parent), **kwargs)
        if logger:
            logger("[ui] 已喚起桌面審核彈窗 (desktop_prompt.py)")
        return True
    except Exception as exc:
        if logger:
            logger(f"[ui] 喚起桌面審核視窗失敗: {type(exc).__name__}: {exc}")
        return False


class DesktopBridgeClient:
    """Synchronous localhost Bridge client for the native desktop UI."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get_pending(self) -> list[dict[str, Any]]:
        r = httpx.get(
            f"{self.base_url}/pending",
            headers=self._headers(),
            timeout=self.timeout,
            trust_env=False,
        )
        r.raise_for_status()
        return list(r.json().get("pending") or [])

    def get_destinations(self) -> list[dict[str, Any]]:
        r = httpx.get(
            f"{self.base_url}/destinations",
            headers=self._headers(),
            timeout=self.timeout,
            trust_env=False,
        )
        r.raise_for_status()
        return list(r.json().get("destinations") or [])

    def decide(
        self,
        pending_id: str,
        action: str,
        dest_id: Optional[str] = None,
        remember: bool = False,
        custom_dest_path: Optional[str] = None,
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "pending_id": pending_id,
            "action": action,
            "remember": remember,
            "actor": "desktop_ui",
        }
        if dest_id:
            payload["dest_id"] = dest_id
        if custom_dest_path:
            payload["custom_dest_path"] = custom_dest_path
        r = httpx.post(
            f"{self.base_url}/decide",
            headers=self._headers(),
            json=payload,
            timeout=self.timeout,
            trust_env=False,
        )
        return r.json()

    def rescan(
        self,
        *,
        immediate: bool = True,
        clear_undo_cooldowns: bool = False,
    ) -> dict[str, Any]:
        r = httpx.post(
            f"{self.base_url}/rescan",
            headers=self._headers(),
            json={
                "immediate": immediate,
                "clear_undo_cooldowns": clear_undo_cooldowns,
            },
            timeout=max(self.timeout, 90.0),
            trust_env=False,
        )
        if r.status_code == 404:
            return {"ok": False, "error": "rescan_not_supported"}
        r.raise_for_status()
        return r.json()

    def undo(self) -> dict[str, Any]:
        r = httpx.post(
            f"{self.base_url}/undo",
            headers=self._headers(),
            json={},
            timeout=self.timeout,
            trust_env=False,
        )
        return r.json()


class DemoBridgeClient:
    """In-memory mock client for --demo screenshots without touching real files."""

    def __init__(self) -> None:
        self._items = [
            {
                "id": "p_demo01",
                "kind": "classify",
                "filename": "Algorithms_Lecture_04.pdf",
                "src_path": str(Path.home() / "Downloads" / "Algorithms_Lecture_04.pdf"),
                "scope_id": "downloads",
                "size_bytes": 1_842_120,
                "is_sensitive": False,
                "suggested_dest_id": "desktop_work",
                "suggested_dest_label": "Desktop / Work & Study",
                "confidence": 0.89,
                "reason": "AI 建議歸檔 (0.89)：檔名與內容符合課程講義主題（先經視窗確認）",
            },
            {
                "id": "p_demo02",
                "kind": "archive",
                "filename": "old_project_draft.zip",
                "src_path": str(Path.home() / "Desktop" / "_Inbox" / "old_project_draft.zip"),
                "scope_id": "arch_inbox_30d",
                "size_bytes": 12_400_000,
                "is_sensitive": False,
                "suggested_dest_id": "archive_misc",
                "suggested_dest_label": "Archive / General",
                "confidence": 1.0,
                "reason": "超過 34 天未變動，建議移至長期封存資料夾",
            },
        ]
        self._dests = [
            {"id": "desktop_work", "label": "Desktop / Work & Study"},
            {"id": "desktop_career", "label": "Desktop / Career & Resume"},
            {"id": "archive_misc", "label": "Archive / General"},
        ]

    def get_pending(self) -> list[dict[str, Any]]:
        return list(self._items)

    def get_destinations(self) -> list[dict[str, Any]]:
        return list(self._dests)

    def decide(
        self,
        pending_id: str,
        action: str,
        dest_id: Optional[str] = None,
        remember: bool = False,
        custom_dest_path: Optional[str] = None,
    ) -> dict[str, Any]:
        self._items = [i for i in self._items if i["id"] != pending_id]
        chosen_label = custom_dest_path or dest_id or "Desktop / Work & Study"
        return {"ok": True, "action": action, "tx_id": "tx_demo0001", "dest_label": chosen_label}

    def rescan(
        self,
        *,
        immediate: bool = True,
        clear_undo_cooldowns: bool = False,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "pruned_missing": 0,
            "processed": 0,
            "enqueued": 0,
            "pending_count": len(self._items),
        }

    def undo(self) -> dict[str, Any]:
        return {"ok": True, "tx_id": "tx_demo0001", "restored_to": str(Path.home() / "Downloads" / "Algorithms_Lecture_04.pdf")}


def run_approval_window(client: Any, *, topmost: bool = True) -> None:
    """Launch the native tkinter approval dialog."""
    import tkinter as tk
    from tkinter import filedialog, ttk

    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.title("Omni-Organizer v3 — 檔案歸位確認")
    root.geometry("680x440")
    root.minsize(600, 390)
    if topmost:
        root.attributes("-topmost", True)

    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    elif "clam" in style.theme_names():
        style.theme_use("clam")

    skipped_ids: set[str] = set()
    items: list[dict[str, Any]] = []
    destinations: list[dict[str, Any]] = []
    dest_choices: list[str] = []
    dest_id_by_choice: dict[str, str] = {}

    container = ttk.Frame(root, padding=16)
    container.pack(fill="both", expand=True)

    # Header
    hdr = ttk.Frame(container)
    hdr.pack(fill="x", pady=(0, 8))
    count_var = tk.StringVar(value="載入中...")
    ttk.Label(hdr, text="Omni-Organizer 待確認檔案", font=("Segoe UI", 13, "bold")).pack(side="left")
    ttk.Label(hdr, textvariable=count_var, font=("Segoe UI", 10)).pack(side="right")

    # Category Banner
    banner_lbl = tk.Label(
        container,
        text="",
        font=("Segoe UI", 10, "bold"),
        anchor="w",
        padx=10,
        pady=6,
    )
    banner_lbl.pack(fill="x", pady=(0, 10))

    # File metadata grid
    card = ttk.LabelFrame(container, text="檔案資訊與建議", padding=12)
    card.pack(fill="both", expand=True, pady=(0, 10))
    card.columnconfigure(1, weight=1)

    name_var = tk.StringVar()
    path_var = tk.StringVar()
    meta_var = tk.StringVar()
    sugg_var = tk.StringVar()
    reason_var = tk.StringVar()

    ttk.Label(card, text="檔案名稱：", font=("Segoe UI", 9, "bold")).grid(row=0, column=0, sticky="nw", pady=3)
    ttk.Label(card, textvariable=name_var, font=("Segoe UI", 10, "bold"), wraplength=500).grid(row=0, column=1, sticky="w", pady=3)

    ttk.Label(card, text="目前路徑：", font=("Segoe UI", 9, "bold")).grid(row=1, column=0, sticky="nw", pady=3)
    path_entry = ttk.Entry(card, textvariable=path_var, state="readonly")
    path_entry.grid(row=1, column=1, sticky="ew", pady=3)

    ttk.Label(card, text="範圍／大小：", font=("Segoe UI", 9, "bold")).grid(row=2, column=0, sticky="nw", pady=3)
    ttk.Label(card, textvariable=meta_var).grid(row=2, column=1, sticky="w", pady=3)

    ttk.Label(card, text="建議去處：", font=("Segoe UI", 9, "bold")).grid(row=3, column=0, sticky="nw", pady=3)
    ttk.Label(card, textvariable=sugg_var, wraplength=500).grid(row=3, column=1, sticky="w", pady=3)

    ttk.Label(card, text="判斷說明：", font=("Segoe UI", 9, "bold")).grid(row=4, column=0, sticky="nw", pady=3)
    ttk.Label(card, textvariable=reason_var, wraplength=500).grid(row=4, column=1, sticky="w", pady=3)

    # Primary actions
    act_row = ttk.Frame(container)
    act_row.pack(fill="x", pady=(0, 8))

    btn_accept = ttk.Button(act_row, text="✅ 接受建議")
    btn_remember = ttk.Button(act_row, text="🧠 移動並記住規則")
    btn_ignore = ttk.Button(act_row, text="🙈 忽略 (冷卻)")
    btn_later = ttk.Button(act_row, text="⏭️ 稍後決定")

    btn_accept.pack(side="left", padx=(0, 6))
    btn_remember.pack(side="left", padx=(0, 6))
    btn_ignore.pack(side="left", padx=(0, 6))
    btn_later.pack(side="right")

    # Manual / custom destination selector
    choose_row = ttk.Frame(container)
    choose_row.pack(fill="x", pady=(0, 10))
    ttk.Label(choose_row, text="改選／自訂資料夾：").pack(side="left")
    combo_var = tk.StringVar()
    combo = ttk.Combobox(choose_row, textvariable=combo_var, state="normal", width=34)
    combo.pack(side="left", fill="x", expand=True, padx=(6, 4))
    btn_browse = ttk.Button(choose_row, text="📁 瀏覽...")
    btn_browse.pack(side="left", padx=(0, 4))
    btn_choose = ttk.Button(choose_row, text="📂 移至此處")
    btn_choose.pack(side="left")

    # Footer
    footer = ttk.Frame(container)
    footer.pack(fill="x")
    status_var = tk.StringVar(value="就緒")
    btn_undo = ttk.Button(footer, text="↩️ 還原上一筆搬移")
    btn_undo.pack(side="right")
    ttk.Label(footer, textvariable=status_var, foreground="#444444", wraplength=470).pack(
        side="left", fill="x", expand=True
    )

    refreshing = False

    def current_item() -> Optional[dict[str, Any]]:
        visible = [i for i in items if i["id"] not in skipped_ids]
        return visible[0] if visible else None

    def _apply_fetched_state(
        new_items: list[dict[str, Any]],
        new_dests: list[dict[str, Any]],
        *,
        manual: bool = False,
        rescan_info: Optional[dict[str, Any]] = None,
    ) -> None:
        nonlocal items, destinations, dest_choices, dest_id_by_choice
        items = new_items
        destinations = new_dests
        dest_choices = [f"{d['label']}  [{d['id']}]" for d in destinations]
        dest_id_by_choice = {f"{d['label']}  [{d['id']}]": d["id"] for d in destinations}
        combo["values"] = dest_choices
        if dest_choices and not combo_var.get().strip():
            combo_var.set(dest_choices[0])
        render_current()
        if manual:
            now_str = time.strftime("%H:%M:%S")
            visible_count = len([i for i in items if i["id"] not in skipped_ids])
            msg = f"已掃描完成 ({now_str}) — 待確認 {visible_count} 筆"
            info = rescan_info or {}
            if info.get("ok"):
                pruned = int(info.get("pruned_missing") or 0)
                processed = int(info.get("processed") or 0)
                enqueued = int(info.get("enqueued") or 0)
                extras: list[str] = []
                if processed > 0:
                    extras.append(f"立即檢測 {processed} 筆")
                if pruned > 0:
                    extras.append(f"清理遺失 {pruned} 筆")
                if enqueued > 0:
                    extras.append(f"排入穩定檢查 {enqueued} 筆")
                if extras:
                    msg += f"（{'、'.join(extras)}）"
            status_var.set(msg)

    def refresh_data(manual: bool = False) -> None:
        nonlocal items, refreshing
        if not manual:
            try:
                new_items = client.get_pending()
                new_dests = client.get_destinations()
            except Exception as exc:
                status_var.set(f"無法連線至 Bridge: {type(exc).__name__}")
                items = []
                render_current()
                return
            _apply_fetched_state(new_items, new_dests, manual=False)
            return

        if refreshing:
            return
        refreshing = True
        btn_refresh.config(state="disabled")
        status_var.set("⏳ 正在立即掃描與檢測中，請稍候...")
        skipped_ids.clear()

        def _worker() -> None:
            rescan_info: dict[str, Any] = {}
            err_name: Optional[str] = None
            new_items: list[dict[str, Any]] = []
            new_dests: list[dict[str, Any]] = []
            try:
                if hasattr(client, "rescan"):
                    try:
                        rescan_info = client.rescan(immediate=True, clear_undo_cooldowns=False)
                    except TypeError:
                        rescan_info = client.rescan()
                new_items = client.get_pending()
                new_dests = client.get_destinations()
            except Exception as exc:
                err_name = type(exc).__name__

            def _finish() -> None:
                nonlocal items, refreshing
                refreshing = False
                btn_refresh.config(state="normal")
                if err_name is not None:
                    status_var.set(f"無法連線至 Bridge: {err_name}")
                    items = []
                    render_current()
                    return
                _apply_fetched_state(new_items, new_dests, manual=True, rescan_info=rescan_info)

            try:
                root.after(0, _finish)
            except Exception:
                pass

        threading.Thread(target=_worker, name="desktop-rescan", daemon=True).start()

    def render_current() -> None:
        visible = [i for i in items if i["id"] not in skipped_ids]
        item = visible[0] if visible else None
        if item is None:
            count_var.set("0 / 0")
            banner_lbl.config(text="✨ 目前沒有待確認的檔案", bg="#e6f4ea", fg="#137333")
            name_var.set("(無)")
            path_var.set("")
            meta_var.set("-")
            sugg_var.set("-")
            reason_var.set("你可以關閉視窗，或等待新檔案抵達時自動彈出。")
            for b in (btn_accept, btn_remember, btn_ignore, btn_later, btn_browse, btn_choose):
                b.config(state="disabled")
            return

        count_var.set(f"剩餘 {len(visible)} 筆")
        is_sens = bool(item.get("is_sensitive"))
        is_arch = item.get("kind") == "archive"
        is_misp = bool(item.get("is_misplaced_candidate"))

        if is_sens:
            banner_lbl.config(
                text="🔒 敏感檔案保護 — 系統未讀取內容，亦未送交 AI",
                bg="#fce8e6",
                fg="#a50e0e",
            )
        elif is_arch:
            banner_lbl.config(
                text="🗄️ 定期封存詢問 — 長時間未變動，確認後才會搬移",
                bg="#fef7e0",
                fg="#8a5a00",
            )
        elif is_misp:
            banner_lbl.config(
                text="🧭 疑似放錯位置 — 請確認是否移動歸位",
                bg="#feefe3",
                fg="#b06000",
            )
        else:
            banner_lbl.config(
                text="📂 待確認歸檔檔案 — 請確認目標資料夾",
                bg="#e8f0fe",
                fg="#1967d2",
            )

        name_var.set(str(item.get("filename") or ""))
        path_var.set(str(item.get("src_path") or ""))
        meta_var.set(f"{item.get('scope_id', '-')}  •  {_fmt_size(item.get('size_bytes', 0))}")
        sugg_label = item.get("suggested_dest_label") or item.get("suggested_dest_id")
        if is_sens:
            sugg_var.set("（敏感檔請手動從下方選單指定）")
        elif sugg_label:
            conf = float(item.get("confidence") or 0.0)
            sugg_var.set(f"{sugg_label}  （信心 {conf:.2f}）")
        else:
            sugg_var.set("（AI 未提供建議，請從下方選單指定）")
        reason_var.set(str(item.get("reason") or ""))

        has_sugg = bool(item.get("suggested_dest_id"))
        btn_accept.config(state="normal" if has_sugg else "disabled")
        btn_remember.config(
            state="normal" if (has_sugg and not is_sens and not is_arch) else "disabled"
        )
        btn_ignore.config(state="normal")
        btn_later.config(state="normal")
        btn_browse.config(state="normal")
        btn_choose.config(state="normal")

    def run_decision(
        action: str,
        dest_id: Optional[str] = None,
        remember: bool = False,
        custom_dest_path: Optional[str] = None,
    ) -> None:
        item = current_item()
        if item is None:
            return
        res = client.decide(
            item["id"],
            action=action,
            dest_id=dest_id,
            remember=remember,
            custom_dest_path=custom_dest_path,
        )
        if res.get("ok"):
            if action == "ignore":
                status_var.set(f"已忽略 {item['filename']}")
            else:
                status_var.set(f"已搬移至 {res.get('dest_label') or res.get('dst', '')}")
        else:
            status_var.set(f"操作失敗：{res.get('error', 'unknown')}")
        refresh_data(manual=False)

    def on_later() -> None:
        item = current_item()
        if item is not None:
            skipped_ids.add(item["id"])
            status_var.set(f"已略過 {item['filename']}（下次再詢問）")
            render_current()

    def on_browse() -> None:
        raw = combo_var.get().strip().strip('"')
        init_dir = str(Path.home())
        if raw and os.path.isdir(raw):
            init_dir = raw
        else:
            item = current_item()
            if item and item.get("src_path"):
                parent = Path(item["src_path"]).parent
                if parent.is_dir():
                    init_dir = str(parent)
        if topmost:
            root.attributes("-topmost", False)
        try:
            chosen = filedialog.askdirectory(
                parent=root,
                title="選擇要移入的目標資料夾",
                initialdir=init_dir,
            )
        finally:
            if topmost:
                root.attributes("-topmost", True)
                root.lift()
        if chosen:
            norm_chosen = os.path.normpath(chosen)
            combo_var.set(norm_chosen)
            status_var.set(f"已選取自訂資料夾：{norm_chosen}（請按「📂 移至此處」確認）")

    def on_choose() -> None:
        raw = combo_var.get().strip().strip('"')
        did = dest_id_by_choice.get(raw)
        if did:
            run_decision("choose", dest_id=did, remember=False)
            return
        if raw and os.path.isabs(raw):
            run_decision("choose", custom_dest_path=raw, remember=False)
            return
        status_var.set("請從選單挑選資料夾，或按「📁 瀏覽...」選擇完整路徑")

    def on_undo() -> None:
        res = client.undo()
        if res.get("ok"):
            status_var.set(f"已還原 {res.get('tx_id')} -> {res.get('restored_to')}")
        else:
            status_var.set(f"還原失敗：{res.get('error', 'unknown')}")
        refresh_data(manual=False)

    btn_accept.config(command=lambda: run_decision("accept"))
    btn_remember.config(command=lambda: run_decision("accept_and_remember", remember=True))
    btn_ignore.config(command=lambda: run_decision("ignore"))
    btn_later.config(command=on_later)
    btn_browse.config(command=on_browse)
    btn_choose.config(command=on_choose)
    btn_undo.config(command=on_undo)
    btn_refresh = ttk.Button(hdr, text="🔄 重新整理", command=lambda: refresh_data(manual=True))
    btn_refresh.pack(side="right", padx=(0, 8))

    refresh_data(manual=False)
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Omni-Organizer v3 Native Desktop Approval Popup")
    parser.add_argument("--demo", action="store_true", help="Preview popup with demo data")
    parser.add_argument("--show-empty", action="store_true", help="Open window even when pending queue is empty")
    args = parser.parse_args()

    if args.demo:
        run_approval_window(DemoBridgeClient(), topmost=False)
        return

    if not acquire_prompt_lock(DESKTOP_PROMPT_LOCK):
        return
    try:
        settings = load_v3_settings()
        token = ensure_bridge_token()
        base_url = f"http://{settings.bridge.host}:{settings.bridge.port}"
        client = DesktopBridgeClient(base_url, token)
        if not args.show_empty:
            try:
                if not client.get_pending():
                    return
            except Exception:
                return
        run_approval_window(client, topmost=settings.ui.topmost)
    finally:
        release_prompt_lock(DESKTOP_PROMPT_LOCK)


if __name__ == "__main__":
    main()
