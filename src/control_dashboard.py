"""Standalone native GUI Control Dashboard for Omni-Organizer v3 (tkinter + localhost Bridge)."""

from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

import httpx

SRC_DIR = Path(__file__).resolve().parent
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from desktop_prompt import acquire_prompt_lock, release_prompt_lock  # noqa: E402
from paths import (  # noqa: E402
    DASHBOARD_LOCK,
    DESKTOP_PROMPT_PY,
    PROJECT_ROOT,
    PYTHONW_EXE,
)
from settings import ensure_bridge_token, load_v3_settings  # noqa: E402


def _format_local_time(iso_ts: str) -> str:
    """Convert an ISO-8601 timestamp string to local YYYY-MM-DD HH:MM:SS display."""
    if not iso_ts:
        return "-"
    try:
        dt = datetime.fromisoformat(iso_ts)
        if dt.tzinfo is not None:
            dt = dt.astimezone()
        return dt.strftime("%Y-%m-%d %H:%M:%S")
    except Exception:
        return str(iso_ts)[:19]


def _spawn_detached_pythonw(script_path: Path, extra_args: list[str] | None = None) -> None:
    """Launch a sibling UI Python script detached without blocking the caller."""
    cmd = [str(PYTHONW_EXE), str(script_path), *(extra_args or [])]
    kwargs: dict[str, Any] = {"cwd": str(PROJECT_ROOT), "close_fds": True}
    if sys.platform == "win32":
        kwargs["creationflags"] = (
            subprocess.CREATE_NO_WINDOW
            | getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
        )
    subprocess.Popen(cmd, **kwargs)


def _open_folder_in_explorer(folder: Path) -> bool:
    """Open a directory in the OS file explorer if it exists."""
    if not folder.is_dir():
        return False
    if sys.platform == "win32":
        os.startfile(str(folder))  # type: ignore[attr-defined]
        return True
    opener = "open" if sys.platform == "darwin" else "xdg-open"
    subprocess.Popen([opener, str(folder)], close_fds=True)
    return True


class DashboardBridgeClient:
    """Synchronous localhost Bridge API client for the GUI Control Dashboard."""

    def __init__(self, base_url: str, token: str, timeout: float = 10.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.timeout = timeout

    def _headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self.token}"}

    def get_status(self) -> dict[str, Any]:
        r = httpx.get(
            f"{self.base_url}/status",
            headers=self._headers(),
            timeout=self.timeout,
            trust_env=False,
        )
        r.raise_for_status()
        return r.json()

    def get_history(self, limit: int = 100) -> list[dict[str, Any]]:
        r = httpx.get(
            f"{self.base_url}/history",
            params={"limit": int(limit)},
            headers=self._headers(),
            timeout=self.timeout,
            trust_env=False,
        )
        r.raise_for_status()
        return list(r.json().get("history") or [])

    def get_pending(self) -> list[dict[str, Any]]:
        r = httpx.get(
            f"{self.base_url}/pending",
            headers=self._headers(),
            timeout=self.timeout,
            trust_env=False,
        )
        r.raise_for_status()
        return list(r.json().get("pending") or [])

    def get_rules(self) -> dict[str, Any]:
        r = httpx.get(
            f"{self.base_url}/rules",
            headers=self._headers(),
            timeout=self.timeout,
            trust_env=False,
        )
        r.raise_for_status()
        return r.json()

    def rescan(
        self,
        *,
        immediate: bool = True,
        clear_undo_cooldowns: bool = False,
        launch_popup: bool = True,
    ) -> dict[str, Any]:
        r = httpx.post(
            f"{self.base_url}/rescan",
            headers=self._headers(),
            json={
                "immediate": immediate,
                "clear_undo_cooldowns": clear_undo_cooldowns,
                "launch_popup": launch_popup,
            },
            timeout=max(self.timeout, 90.0),
            trust_env=False,
        )
        if r.status_code == 404:
            return {"ok": False, "error": "rescan_not_supported"}
        r.raise_for_status()
        return r.json()

    def undo(self, tx_id: Optional[str] = None) -> dict[str, Any]:
        body: dict[str, Any] = {"tx_id": tx_id} if tx_id else {}
        r = httpx.post(
            f"{self.base_url}/undo",
            headers=self._headers(),
            json=body,
            timeout=self.timeout,
            trust_env=False,
        )
        return r.json()

    def delete_rule(self, rule_id: str) -> dict[str, Any]:
        r = httpx.delete(
            f"{self.base_url}/rules/{rule_id}",
            headers=self._headers(),
            timeout=self.timeout,
            trust_env=False,
        )
        return r.json()


class DemoDashboardClient:
    """In-memory mock client for --demo previews without reading or mutating host files."""

    def __init__(self) -> None:
        home = Path.home()
        self._history: list[dict[str, Any]] = [
            {
                "tx_id": "tx_demo0002",
                "src": str(home / "Downloads" / "Algorithms_Lecture_04.pdf"),
                "dst": str(home / "Desktop" / "01_Work_Study" / "Algorithms_Lecture_04.pdf"),
                "file_hash": "demo_hash_2",
                "tier": "tier2_ai",
                "reason": "AI 語意分類：課程講義與演算法主題",
                "confidence": 0.91,
                "rule_id": None,
                "timestamp": "2026-11-24T14:25:00+00:00",
                "undone": False,
                "undone_at": None,
            },
            {
                "tx_id": "tx_demo0001",
                "src": str(home / "Desktop" / "installer_setup.exe"),
                "dst": str(home / "Downloads" / "01_Installers" / "installer_setup.exe"),
                "file_hash": "demo_hash_1",
                "tier": "tier1_rule",
                "reason": "安裝程式副檔名分流規則",
                "confidence": 1.0,
                "rule_id": "t1_installers",
                "timestamp": "2026-11-24T13:10:00+00:00",
                "undone": True,
                "undone_at": "2026-11-24T13:12:00+00:00",
            },
        ]
        self._pending: list[dict[str, Any]] = [
            {
                "id": "p_demo01",
                "kind": "classify",
                "filename": "invite.ics",
                "src_path": str(home / "Desktop" / "invite.ics"),
                "scope_id": "desktop",
                "suggested_dest_label": "Desktop / Career & Resume",
                "confidence": 0.88,
                "reason": "面試日程邀請，待人工確認後移動",
            }
        ]
        self._learned_rules: list[dict[str, Any]] = [
            {
                "id": "lr_demo01",
                "keyword": "algorithms",
                "extensions": ["pdf"],
                "dest_id": "desktop_work",
                "hit_count": 3,
                "source_filename": "Algorithms_Lecture_01.pdf",
            }
        ]

    def get_status(self) -> dict[str, Any]:
        return {
            "ok": True,
            "version": "3.0.0",
            "pid": 99999,
            "uptime_sec": 3600,
            "ai": {
                "provider": "ollama",
                "model": "qwen2.5:3b",
                "health": {"available": True},
            },
            "counts": {
                "pending": len(self._pending),
                "learned_rules": len(self._learned_rules),
                "undo_records": len(self._history),
                "active_cooldowns": 2,
            },
        }

    def get_history(self, limit: int = 100) -> list[dict[str, Any]]:
        return list(self._history[:limit])

    def get_pending(self) -> list[dict[str, Any]]:
        return list(self._pending)

    def get_rules(self) -> dict[str, Any]:
        return {"ok": True, "learned_rules": list(self._learned_rules), "tier1_rules": []}

    def rescan(
        self,
        *,
        immediate: bool = True,
        clear_undo_cooldowns: bool = False,
        launch_popup: bool = True,
    ) -> dict[str, Any]:
        return {
            "ok": True,
            "pruned_missing": 0,
            "processed": 1,
            "enqueued": 0,
            "pending_count": len(self._pending),
        }

    def undo(self, tx_id: Optional[str] = None) -> dict[str, Any]:
        for rec in self._history:
            if (tx_id is None and not rec["undone"]) or rec["tx_id"] == tx_id:
                if rec["undone"]:
                    return {"ok": False, "error": "already_undone", "tx_id": rec["tx_id"]}
                rec["undone"] = True
                return {"ok": True, "tx_id": rec["tx_id"], "restored_to": rec["src"]}
        return {"ok": False, "error": "no_undo_record"}

    def delete_rule(self, rule_id: str) -> dict[str, Any]:
        before = len(self._learned_rules)
        self._learned_rules = [r for r in self._learned_rules if r["id"] != rule_id]
        return {"ok": len(self._learned_rules) < before, "rule_id": rule_id}


def run_dashboard_window(client: Any) -> None:
    """Launch the native tkinter Control Dashboard."""
    import tkinter as tk
    from tkinter import ttk

    if os.name == "nt":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(1)
        except Exception:
            pass

    root = tk.Tk()
    root.title("Omni-Organizer v3 — 圖形化控制平台")
    root.geometry("960x620")
    root.minsize(820, 520)

    style = ttk.Style()
    if "vista" in style.theme_names():
        style.theme_use("vista")
    elif "clam" in style.theme_names():
        style.theme_use("clam")

    history_rows: list[dict[str, Any]] = []
    history_by_iid: dict[str, dict[str, Any]] = {}
    rule_by_iid: dict[str, dict[str, Any]] = {}
    scan_running = False

    container = ttk.Frame(root, padding=14)
    container.pack(fill="both", expand=True)

    # Top Header & Live Status Summary
    hdr = ttk.Frame(container)
    hdr.pack(fill="x", pady=(0, 8))
    ttk.Label(hdr, text="Omni-Organizer v3 控制平台", font=("Segoe UI", 14, "bold")).pack(side="left")

    summary_var = tk.StringVar(value="連線檢查中...")
    summary_lbl = ttk.Label(hdr, textvariable=summary_var, font=("Segoe UI", 9))
    summary_lbl.pack(side="right")

    # Quick Actions Bar
    action_bar = ttk.Frame(container)
    action_bar.pack(fill="x", pady=(0, 10))
    btn_scan = ttk.Button(action_bar, text="🔍 立即檢測掃描")
    btn_force_scan = ttk.Button(action_bar, text="🔁 強制重測 (含已還原)")
    btn_open_popup = ttk.Button(action_bar, text="📂 開啟審核視窗")
    btn_refresh = ttk.Button(action_bar, text="🔄 重新整理")

    btn_scan.pack(side="left", padx=(0, 6))
    btn_force_scan.pack(side="left", padx=(0, 6))
    btn_open_popup.pack(side="left", padx=(0, 6))
    btn_refresh.pack(side="right")

    # Tabbed Notebook
    notebook = ttk.Notebook(container)
    notebook.pack(fill="both", expand=True, pady=(0, 8))

    # --- Tab 1: History ---
    tab_history = ttk.Frame(notebook, padding=8)
    notebook.add(tab_history, text="📜 歷史移動紀錄")

    hist_filter = ttk.Frame(tab_history)
    hist_filter.pack(fill="x", pady=(0, 6))
    ttk.Label(hist_filter, text="搜尋關鍵字：").pack(side="left")
    search_var = tk.StringVar()
    search_entry = ttk.Entry(hist_filter, textvariable=search_var, width=32)
    search_entry.pack(side="left", padx=(4, 14))
    ttk.Label(hist_filter, text="顯示筆數：").pack(side="left")
    limit_var = tk.StringVar(value="100")
    limit_combo = ttk.Combobox(
        hist_filter, textvariable=limit_var, values=("50", "100", "200"), state="readonly", width=6
    )
    limit_combo.pack(side="left", padx=4)

    hist_tree_wrap = ttk.Frame(tab_history)
    hist_tree_wrap.pack(fill="both", expand=True, pady=(0, 6))
    hist_tree_wrap.columnconfigure(0, weight=1)
    hist_tree_wrap.rowconfigure(0, weight=1)

    hist_cols = ("time", "status", "filename", "src", "dst", "tier", "reason")
    hist_tree = ttk.Treeview(hist_tree_wrap, columns=hist_cols, show="headings", selectmode="browse")
    hist_headers = {
        "time": ("時間", 140),
        "status": ("狀態", 82),
        "filename": ("檔案名稱", 190),
        "src": ("原始位置", 190),
        "dst": ("目標位置", 190),
        "tier": ("分類層級", 95),
        "reason": ("說明", 220),
    }
    for col, (title, width) in hist_headers.items():
        hist_tree.heading(col, text=title)
        hist_tree.column(col, width=width, minwidth=70, stretch=(col == "reason"))

    hist_vsb = ttk.Scrollbar(hist_tree_wrap, orient="vertical", command=hist_tree.yview)
    hist_hsb = ttk.Scrollbar(hist_tree_wrap, orient="horizontal", command=hist_tree.xview)
    hist_tree.configure(yscrollcommand=hist_vsb.set, xscrollcommand=hist_hsb.set)
    hist_tree.grid(row=0, column=0, sticky="nsew")
    hist_vsb.grid(row=0, column=1, sticky="ns")
    hist_hsb.grid(row=1, column=0, sticky="ew")

    hist_actions = ttk.Frame(tab_history)
    hist_actions.pack(fill="x")
    btn_hist_undo = ttk.Button(hist_actions, text="↩️ 還原選取項目", state="disabled")
    btn_hist_open = ttk.Button(hist_actions, text="📁 開啟目前位置", state="disabled")
    btn_hist_undo.pack(side="left", padx=(0, 6))
    btn_hist_open.pack(side="left")

    # --- Tab 2: Pending ---
    tab_pending = ttk.Frame(notebook, padding=8)
    notebook.add(tab_pending, text="📥 待確認佇列")

    pend_wrap = ttk.Frame(tab_pending)
    pend_wrap.pack(fill="both", expand=True, pady=(0, 6))
    pend_wrap.columnconfigure(0, weight=1)
    pend_wrap.rowconfigure(0, weight=1)

    pend_cols = ("filename", "scope_id", "kind", "suggested", "confidence", "reason")
    pend_tree = ttk.Treeview(pend_wrap, columns=pend_cols, show="headings", selectmode="browse")
    pend_headers = {
        "filename": ("檔案名稱", 200),
        "scope_id": ("來源範圍", 95),
        "kind": ("類型", 75),
        "suggested": ("建議去處", 210),
        "confidence": ("信心", 65),
        "reason": ("說明", 240),
    }
    for col, (title, width) in pend_headers.items():
        pend_tree.heading(col, text=title)
        pend_tree.column(col, width=width, minwidth=60, stretch=(col == "reason"))

    pend_vsb = ttk.Scrollbar(pend_wrap, orient="vertical", command=pend_tree.yview)
    pend_hsb = ttk.Scrollbar(pend_wrap, orient="horizontal", command=pend_tree.xview)
    pend_tree.configure(yscrollcommand=pend_vsb.set, xscrollcommand=pend_hsb.set)
    pend_tree.grid(row=0, column=0, sticky="nsew")
    pend_vsb.grid(row=0, column=1, sticky="ns")
    pend_hsb.grid(row=1, column=0, sticky="ew")

    pend_actions = ttk.Frame(tab_pending)
    pend_actions.pack(fill="x")
    btn_pend_open = ttk.Button(pend_actions, text="📂 開啟審核視窗處理")
    btn_pend_open.pack(side="left")

    # --- Tab 3: Learned Rules ---
    tab_rules = ttk.Frame(notebook, padding=8)
    notebook.add(tab_rules, text="🧠 已學習規則")

    rules_wrap = ttk.Frame(tab_rules)
    rules_wrap.pack(fill="both", expand=True, pady=(0, 6))
    rules_wrap.columnconfigure(0, weight=1)
    rules_wrap.rowconfigure(0, weight=1)

    rule_cols = ("keyword", "extensions", "dest_id", "hit_count", "source_filename")
    rules_tree = ttk.Treeview(rules_wrap, columns=rule_cols, show="headings", selectmode="browse")
    rule_headers = {
        "keyword": ("關鍵字", 180),
        "extensions": ("副檔名", 110),
        "dest_id": ("目標 ID", 160),
        "hit_count": ("命中次數", 85),
        "source_filename": ("來源範例檔名", 260),
    }
    for col, (title, width) in rule_headers.items():
        rules_tree.heading(col, text=title)
        rules_tree.column(col, width=width, minwidth=65, stretch=(col == "source_filename"))

    rules_vsb = ttk.Scrollbar(rules_wrap, orient="vertical", command=rules_tree.yview)
    rules_hsb = ttk.Scrollbar(rules_wrap, orient="horizontal", command=rules_tree.xview)
    rules_tree.configure(yscrollcommand=rules_vsb.set, xscrollcommand=rules_hsb.set)
    rules_tree.grid(row=0, column=0, sticky="nsew")
    rules_vsb.grid(row=0, column=1, sticky="ns")
    rules_hsb.grid(row=1, column=0, sticky="ew")

    rules_actions = ttk.Frame(tab_rules)
    rules_actions.pack(fill="x")
    btn_rule_del = ttk.Button(rules_actions, text="🗑️ 刪除選取規則", state="disabled")
    btn_rule_del.pack(side="left")

    # Footer Status Bar
    footer = ttk.Frame(container)
    footer.pack(fill="x")
    status_var = tk.StringVar(value="就緒")
    ttk.Label(footer, textvariable=status_var, foreground="#444444", wraplength=820).pack(
        side="left", fill="x", expand=True
    )

    def render_history_rows() -> None:
        history_by_iid.clear()
        for iid in hist_tree.get_children():
            hist_tree.delete(iid)
        kw = search_var.get().strip().lower()
        for idx, rec in enumerate(history_rows):
            src = str(rec.get("src") or "")
            dst = str(rec.get("dst") or "")
            reason = str(rec.get("reason") or "")
            fname = Path(src).name or Path(dst).name
            if kw and kw not in f"{fname} {src} {dst} {reason} {rec.get('tier', '')}".lower():
                continue
            undone = bool(rec.get("undone"))
            iid = f"{rec.get('tx_id') or 'tx'}_{idx}"
            history_by_iid[iid] = rec
            hist_tree.insert(
                "",
                "end",
                iid=iid,
                values=(
                    _format_local_time(str(rec.get("timestamp") or "")),
                    "↩️ 已還原" if undone else "✅ 已搬移",
                    fname,
                    str(Path(src).parent) if src else "",
                    str(Path(dst).parent) if dst else "",
                    str(rec.get("tier") or ""),
                    reason,
                ),
            )
        on_history_select()

    def on_history_select(_event=None) -> None:
        sel = hist_tree.selection()
        if not sel:
            btn_hist_undo.config(state="disabled")
            btn_hist_open.config(state="disabled")
            return
        rec = history_by_iid.get(sel[0])
        if rec is None:
            btn_hist_undo.config(state="disabled")
            btn_hist_open.config(state="disabled")
            return
        btn_hist_undo.config(state="disabled" if rec.get("undone") else "normal")
        btn_hist_open.config(state="normal")

    def on_rule_select(_event=None) -> None:
        sel = rules_tree.selection()
        btn_rule_del.config(state="normal" if (sel and sel[0] in rule_by_iid) else "disabled")

    def refresh_all(status_message: Optional[str] = None) -> None:
        nonlocal history_rows
        try:
            lim = int(limit_var.get() or 100)
        except ValueError:
            lim = 100

        try:
            st = client.get_status()
            history_rows = client.get_history(limit=lim)
            pending_rows = client.get_pending()
            rules_data = client.get_rules()
        except Exception as exc:
            summary_var.set("🔴 未連線至背景服務 (Bridge)")
            status_var.set(f"無法連線至 Bridge：{type(exc).__name__}（請先確認背景服務已啟動）")
            return

        ai = st.get("ai") or {}
        counts = st.get("counts") or {}
        pid = st.get("pid", "-")
        avail = bool((ai.get("health") or {}).get("available"))
        ai_tag = f"{ai.get('provider', '-')}/{ai.get('model', '-')}"
        if not avail and ai.get("provider") not in {"none", ""}:
            ai_tag += " (離線)"
        summary_var.set(
            f"🟢 運作中 (PID: {pid})  |  AI: {ai_tag}  |  "
            f"待確認 {counts.get('pending', len(pending_rows))} • "
            f"歷史 {counts.get('undo_records', len(history_rows))} • "
            f"規則 {counts.get('learned_rules', 0)} • "
            f"冷卻 {counts.get('active_cooldowns', 0)}"
        )

        render_history_rows()

        for iid in pend_tree.get_children():
            pend_tree.delete(iid)
        for idx, p in enumerate(pending_rows):
            conf = p.get("confidence")
            conf_str = f"{float(conf):.2f}" if conf is not None else "-"
            pend_tree.insert(
                "",
                "end",
                iid=f"{p.get('id') or 'p'}_{idx}",
                values=(
                    str(p.get("filename") or ""),
                    str(p.get("scope_id") or ""),
                    "封存" if p.get("kind") == "archive" else "分類",
                    str(p.get("suggested_dest_label") or p.get("suggested_dest_id") or "（待指定）"),
                    conf_str,
                    str(p.get("reason") or ""),
                ),
            )

        rule_by_iid.clear()
        for iid in rules_tree.get_children():
            rules_tree.delete(iid)
        for idx, r in enumerate(rules_data.get("learned_rules") or []):
            rid = str(r.get("id") or f"r_{idx}")
            rule_by_iid[rid] = r
            exts = ", ".join(r.get("extensions") or []) or "*"
            rules_tree.insert(
                "",
                "end",
                iid=rid,
                values=(
                    str(r.get("keyword") or ""),
                    exts,
                    str(r.get("dest_id") or ""),
                    str(r.get("hit_count") or 0),
                    str(r.get("source_filename") or ""),
                ),
            )
        on_rule_select()

        if status_message:
            status_var.set(status_message)
        else:
            status_var.set(
                f"已更新 ({time.strftime('%H:%M:%S')}) — 顯示歷史搬移 {len(history_rows)} 筆、"
                f"待確認 {len(pending_rows)} 筆"
            )

    def start_scan(*, clear_undo_cooldowns: bool) -> None:
        nonlocal scan_running
        if scan_running:
            return
        scan_running = True
        btn_scan.config(state="disabled")
        btn_force_scan.config(state="disabled")
        mode_lbl = "強制重測（含已還原）" if clear_undo_cooldowns else "立即掃描與 AI 檢測"
        status_var.set(f"⏳ 正在執行{mode_lbl}，請稍候...")

        def _worker() -> None:
            res: dict[str, Any] = {}
            err_name: Optional[str] = None
            try:
                res = client.rescan(
                    immediate=True,
                    clear_undo_cooldowns=clear_undo_cooldowns,
                    launch_popup=True,
                )
            except Exception as exc:
                err_name = type(exc).__name__

            def _done() -> None:
                nonlocal scan_running
                scan_running = False
                btn_scan.config(state="normal")
                btn_force_scan.config(state="normal")
                if err_name is not None:
                    status_var.set(f"掃描失敗：{err_name}")
                    return
                if not res.get("ok"):
                    status_var.set(f"掃描失敗：{res.get('error', 'unknown')}")
                    return
                msg = (
                    f"掃描完成 ({time.strftime('%H:%M:%S')}) — "
                    f"立即檢測 {int(res.get('processed') or 0)} 筆、"
                    f"待確認 {int(res.get('pending_count') or 0)} 筆、"
                    f"清理遺失 {int(res.get('pruned_missing') or 0)} 筆"
                )
                refresh_all(status_message=msg)

            try:
                root.after(0, _done)
            except Exception:
                pass

        threading.Thread(target=_worker, name="dashboard-scan", daemon=True).start()

    def on_hist_undo() -> None:
        sel = hist_tree.selection()
        if not sel:
            return
        rec = history_by_iid.get(sel[0])
        if not rec or rec.get("undone"):
            return
        res = client.undo(tx_id=rec["tx_id"])
        if res.get("ok"):
            refresh_all(f"已還原 {res.get('tx_id')} -> {res.get('restored_to')}")
        else:
            refresh_all(f"還原失敗：{res.get('error', 'unknown')}")

    def on_hist_open() -> None:
        sel = hist_tree.selection()
        if not sel:
            return
        rec = history_by_iid.get(sel[0])
        if not rec:
            return
        target = Path(str(rec.get("src") if rec.get("undone") else rec.get("dst") or "")).parent
        try:
            if _open_folder_in_explorer(target):
                status_var.set(f"已開啟資料夾：{target}")
            else:
                status_var.set(f"找不到資料夾：{target}")
        except Exception as exc:
            status_var.set(f"開啟資料夾失敗：{type(exc).__name__}")

    def on_open_popup() -> None:
        if isinstance(client, DemoDashboardClient):
            status_var.set("展示模式下請另執行：python src\\desktop_prompt.py --demo")
            return
        try:
            _spawn_detached_pythonw(DESKTOP_PROMPT_PY, ["--show-empty"])
            status_var.set("已喚起桌面審核確認視窗")
        except Exception as exc:
            status_var.set(f"開啟審核視窗失敗：{type(exc).__name__}")

    def on_delete_rule() -> None:
        sel = rules_tree.selection()
        if not sel:
            return
        rid = sel[0]
        if rid not in rule_by_iid:
            return
        res = client.delete_rule(rid)
        if res.get("ok"):
            refresh_all(f"已刪除規則：{rid}")
        else:
            refresh_all(f"刪除規則失敗：{res.get('error', 'unknown')}")

    hist_tree.bind("<<TreeviewSelect>>", on_history_select)
    rules_tree.bind("<<TreeviewSelect>>", on_rule_select)
    limit_combo.bind("<<ComboboxSelected>>", lambda _e: refresh_all())
    search_var.trace_add("write", lambda *_args: render_history_rows())

    btn_scan.config(command=lambda: start_scan(clear_undo_cooldowns=False))
    btn_force_scan.config(command=lambda: start_scan(clear_undo_cooldowns=True))
    btn_open_popup.config(command=on_open_popup)
    btn_pend_open.config(command=on_open_popup)
    btn_refresh.config(command=lambda: refresh_all())
    btn_hist_undo.config(command=on_hist_undo)
    btn_hist_open.config(command=on_hist_open)
    btn_rule_del.config(command=on_delete_rule)

    refresh_all()
    root.mainloop()


def main() -> None:
    parser = argparse.ArgumentParser(description="Omni-Organizer v3 Native GUI Control Dashboard")
    parser.add_argument("--demo", action="store_true", help="Preview dashboard with mock data")
    args = parser.parse_args()

    if args.demo:
        run_dashboard_window(DemoDashboardClient())
        return

    if not acquire_prompt_lock(DASHBOARD_LOCK):
        return
    try:
        settings = load_v3_settings()
        token = ensure_bridge_token()
        base_url = f"http://{settings.bridge.host}:{settings.bridge.port}"
        run_dashboard_window(DashboardBridgeClient(base_url, token))
    finally:
        release_prompt_lock(DASHBOARD_LOCK)


if __name__ == "__main__":
    main()
