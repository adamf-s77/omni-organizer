"""Integration tests for BridgeServer, native Desktop Popup client, and optional Discord Cog."""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import httpx

REPO_ROOT = Path(__file__).resolve().parent.parent
OMNI_SRC = REPO_ROOT / "src"
DISCORD_INT = REPO_ROOT / "integrations" / "discord"
for p in (OMNI_SRC, DISCORD_INT):
    if str(p) not in sys.path:
        sys.path.insert(0, str(p))

import desktop_prompt  # noqa: E402
import guardrails  # noqa: E402
from bridge_api import BridgeServer  # noqa: E402
from control_dashboard import DashboardBridgeClient, DemoDashboardClient  # noqa: E402
from decision_engine import DecisionEngine  # noqa: E402
from desktop_prompt import (  # noqa: E402
    DesktopBridgeClient,
    acquire_prompt_lock,
    is_prompt_locked,
    launch_desktop_prompt_if_needed,
    release_prompt_lock,
)
from models import (  # noqa: E402
    AIProviderConfig,
    ClassificationResult,
    DestinationOption,
    MonitoredScope,
    V3Settings,
)
from organizer_cog import (  # noqa: E402
    DYNAMIC_ITEM_CLASSES,
    OrgAcceptButton,
    OrgFolderSelect,
    OrgIgnoreButton,
    OrgRememberButton,
    OrgUndoButton,
    OrganizerBridgeClient,
    OrganizerCog,
    build_automove_card,
    build_pending_card,
)
from providers.base import BaseClassifierProvider  # noqa: E402
from state_store import StateStore  # noqa: E402


class QuietProvider(BaseClassifierProvider):
    name = "quiet"

    def classify(self, req):
        return ClassificationResult(destination_id="inbox_docs", confidence=0.60, reason="uncertain", provider=self.name)

    def health_check(self):
        return {"provider": self.name, "available": True}


class FakeChannel:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, *, embed=None, view=None):
        self.sent.append({"embed": embed, "view": view})


class FakeBot:
    def __init__(self, cfg: dict) -> None:
        self.pc_config = cfg
        self.guilds = []
        self.dynamic_items = []

    def is_ready(self) -> bool:
        return True

    async def wait_until_ready(self) -> None:
        return None

    def get_channel(self, cid: int):
        return None

    def add_dynamic_items(self, *items) -> None:
        self.dynamic_items.extend(items)


class TestBridgeAndCog(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            guardrails,
            "FORBIDDEN_PATH_SEGMENTS",
            guardrails.FORBIDDEN_PATH_SEGMENTS - {"appdata"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        self.desktop = root / "Desktop"
        self.inbox_docs = self.desktop / "_Inbox" / "Docs"
        self.work = self.desktop / "01_Coursework"
        for d in (self.desktop, self.inbox_docs, self.work):
            d.mkdir(parents=True, exist_ok=True)

        self.settings = V3Settings(
            ai=AIProviderConfig(provider="none"),
            monitored_scopes=[MonitoredScope(id="desktop", path=str(self.desktop), mode="root_only")],
            destinations=[
                DestinationOption(id="inbox_docs", label="Inbox Docs", path=str(self.inbox_docs), description="Docs"),
                DestinationOption(id="desktop_work", label="Coursework", path=str(self.work), description="Courses"),
            ],
        )
        self.store = StateStore(root / "state")
        self.engine = DecisionEngine(self.settings, self.store, QuietProvider())
        self.token = "test_bridge_secret_token_32chars_ok"
        self.bridge = BridgeServer("127.0.0.1", 0, self.token, self.engine, self.store, self.settings)
        _, self.port = self.bridge.start()
        self.base_url = f"http://127.0.0.1:{self.port}"

    def tearDown(self) -> None:
        self.bridge.stop()
        self.tmp.cleanup()

    def test_non_loopback_host_rejected(self) -> None:
        with self.assertRaises(ValueError):
            BridgeServer("0.0.0.0", 0, self.token, self.engine, self.store, self.settings)

    def test_auth_and_rest_endpoints(self) -> None:
        # /health is open; /status rejects missing or wrong Bearer tokens
        r_health = httpx.get(f"{self.base_url}/health", timeout=3.0)
        self.assertEqual(r_health.status_code, 200)
        self.assertTrue(r_health.json()["ok"])

        self.assertEqual(httpx.get(f"{self.base_url}/status", timeout=3.0).status_code, 401)
        self.assertEqual(
            httpx.get(f"{self.base_url}/status", headers={"Authorization": "Bearer wrong"}, timeout=3.0).status_code,
            401,
        )
        # DNS rebinding guard rejects non-loopback Host headers
        self.assertEqual(
            httpx.get(f"{self.base_url}/health", headers={"Host": "evil.example.com"}, timeout=3.0).status_code,
            403,
        )

        hdrs = {"Authorization": f"Bearer {self.token}"}
        self.assertEqual(
            httpx.post(f"{self.base_url}/pending/bad!id/ack", headers=hdrs, timeout=3.0).status_code,
            400,
        )
        r_status = httpx.get(f"{self.base_url}/status", headers=hdrs, timeout=3.0)
        self.assertEqual(r_status.status_code, 200)
        self.assertEqual(r_status.json()["ai"]["health"]["provider"], "quiet")

        # Create pending item and resolve via /decide, then revert via /undo
        sample = self.desktop / "lecture_notes.pdf"
        sample.write_text("Operating systems", encoding="utf-8")
        proc = self.engine.process_file(sample)
        self.assertEqual(proc["action"], "pending_review")
        pid = proc["pending_id"]

        r_pend = httpx.get(f"{self.base_url}/pending?unnotified=1", headers=hdrs, timeout=3.0)
        self.assertEqual(len(r_pend.json()["pending"]), 1)

        r_decide = httpx.post(
            f"{self.base_url}/decide",
            headers=hdrs,
            json={"pending_id": pid, "action": "accept_and_remember"},
            timeout=3.0,
        )
        self.assertEqual(r_decide.status_code, 200)
        dec_data = r_decide.json()
        self.assertTrue(dec_data["ok"])
        self.assertTrue((self.inbox_docs / "lecture_notes.pdf").exists())
        rule_id = dec_data["rule_id"]
        self.assertIsNotNone(rule_id)

        r_rules = httpx.get(f"{self.base_url}/rules", headers=hdrs, timeout=3.0)
        self.assertEqual(len(r_rules.json()["learned_rules"]), 1)

        r_del = httpx.delete(f"{self.base_url}/rules/{rule_id}", headers=hdrs, timeout=3.0)
        self.assertEqual(r_del.status_code, 200)
        self.assertEqual(len(httpx.get(f"{self.base_url}/rules", headers=hdrs, timeout=3.0).json()["learned_rules"]), 0)

        r_undo = httpx.post(
            f"{self.base_url}/undo",
            headers=hdrs,
            json={"tx_id": dec_data["tx_id"]},
            timeout=3.0,
        )
        self.assertEqual(r_undo.status_code, 200)
        self.assertTrue(sample.exists())

    async def test_discord_cog_client_cards_and_poll_once(self) -> None:
        # DynamicItem custom_id template matching
        samples = [
            (OrgAcceptButton, "org:accept:p_ab12cd34"),
            (OrgRememberButton, "org:remember:p_ab12cd34"),
            (OrgIgnoreButton, "org:ignore:p_ab12cd34"),
            (OrgUndoButton, "org:undo:tx_ab12cd34"),
            (OrgFolderSelect, "org:select:p_ab12cd34"),
        ]
        for cls, cid in samples:
            self.assertIn(cls, DYNAMIC_ITEM_CLASSES)
            self.assertIsNotNone(re.fullmatch(cls.__discord_ui_compiled_template__.pattern, cid))

        # Card builders respect Discord's 25-option Select limit
        many_dests = [
            {"id": f"d_{i}", "label": f"Folder {i}", "description": f"Desc {i}"}
            for i in range(30)
        ]
        embed, view = build_pending_card(
            {
                "id": "p_1",
                "filename": "report.pdf",
                "src_path": str(self.desktop / "report.pdf"),
                "scope_id": "desktop",
                "size_bytes": 1024,
                "suggested_dest_id": "d_0",
                "suggested_dest_label": "Folder 0",
                "confidence": 0.62,
                "reason": "低信心確認",
            },
            many_dests,
        )
        self.assertIn("report.pdf", embed.title)
        selects = [c for c in view.children if hasattr(c, "item") and hasattr(c.item, "options")]
        self.assertEqual(len(selects), 1)
        self.assertLessEqual(len(selects[0].item.options), 25)

        arch_embed, arch_view = build_pending_card(
            {
                "id": "p_arch",
                "kind": "archive",
                "filename": "old_notes.pdf",
                "src_path": str(self.desktop / "old_notes.pdf"),
                "scope_id": "arch_inbox_30d",
                "size_bytes": 2048,
                "suggested_dest_id": "desktop_work",
                "suggested_dest_label": "Coursework",
                "confidence": 1.0,
                "reason": "超過 35 天未變動，建議封存",
            },
            many_dests[:5],
        )
        self.assertIn("定期歸檔詢問", arch_embed.title)
        self.assertFalse(any(isinstance(c, OrgRememberButton) for c in arch_view.children))
        self.assertTrue(any(isinstance(c, OrgAcceptButton) for c in arch_view.children))
        self.assertTrue(any(isinstance(c, OrgIgnoreButton) for c in arch_view.children))

        ev_embed, ev_view = build_automove_card(
            {
                "kind": "auto_moved",
                "filename": "a.exe",
                "src": "src/a.exe",
                "dst": "dst/a.exe",
                "dest_label": "Installers",
                "tier": "tier1_rule",
                "confidence": 1.0,
                "reason": "規則命中",
                "tx_id": "tx_9999aaaa",
            }
        )
        self.assertIn("a.exe", ev_embed.title)
        self.assertIsNotNone(ev_view)

        # End-to-end async client + OrganizerCog.poll_once against live BridgeServer
        f = self.desktop / "paper_draft.pdf"
        f.write_text("Quantum draft", encoding="utf-8")
        proc = self.engine.process_file(f)
        self.assertEqual(proc["action"], "pending_review")

        client = OrganizerBridgeClient(base_url=self.base_url, token=self.token, token_file=None)
        cog = OrganizerCog(FakeBot({"channel_id": "12345"}), client=client)
        channel = FakeChannel()
        try:
            posted = await cog.poll_once(channel=channel)
            self.assertEqual(posted, 1)
            self.assertEqual(len(channel.sent), 1)
            # Pending item was acked so a second poll produces 0 duplicates
            self.assertEqual(await cog.poll_once(channel=channel), 0)

            res = await client.decide(proc["pending_id"], "choose", dest_id="desktop_work")
            self.assertTrue(res["ok"])
            self.assertTrue((self.work / "paper_draft.pdf").exists())
        finally:
            await client.close()

    def test_desktop_prompt_client_lock_and_launcher(self) -> None:
        sample = self.desktop / "ui_sample.pdf"
        sample.write_text("Algorithm study notes", encoding="utf-8")
        proc = self.engine.process_file(sample)
        self.assertEqual(proc["action"], "pending_review")

        ui_client = DesktopBridgeClient(self.base_url, self.token)
        pending = ui_client.get_pending()
        self.assertEqual(len(pending), 1)
        dests = ui_client.get_destinations()
        self.assertEqual(len(dests), 2)

        dec = ui_client.decide(pending[0]["id"], "choose", dest_id="desktop_work")
        self.assertTrue(dec["ok"])
        self.assertTrue((self.work / "ui_sample.pdf").exists())

        undo_res = ui_client.undo()
        self.assertTrue(undo_res["ok"])
        self.assertTrue(sample.exists())

        # Single-instance lockfile and launcher cooldown checks
        lock_file = Path(self.tmp.name) / "prompt.lock"
        self.assertTrue(acquire_prompt_lock(lock_file))
        self.assertTrue(is_prompt_locked(lock_file))
        release_prompt_lock(lock_file)
        self.assertFalse(is_prompt_locked(lock_file))

        spawned: list[list[str]] = []
        desktop_prompt._LAST_LAUNCH_TS = 0.0
        self.assertTrue(
            launch_desktop_prompt_if_needed(
                self.settings, lock_file=lock_file, now=100.0, spawner=spawned.append
            )
        )
        self.assertEqual(len(spawned), 1)
        # Inside cooldown window -> suppressed
        self.assertFalse(
            launch_desktop_prompt_if_needed(
                self.settings, lock_file=lock_file, now=101.0, spawner=spawned.append
            )
        )
        # While locked -> suppressed
        lock_file.write_text(str(os.getpid()), encoding="utf-8")
        self.assertFalse(
            launch_desktop_prompt_if_needed(
                self.settings, lock_file=lock_file, now=200.0, spawner=spawned.append
            )
        )
        release_prompt_lock(lock_file)

    def test_bridge_rescan_prunes_missing_and_custom_dest_over_http(self) -> None:
        ui_client = DesktopBridgeClient(self.base_url, self.token)

        # Missing file in pending queue gets pruned on /rescan, and rescan_callback count is returned
        ghost = self.desktop / "ghost_temp.pdf"
        ghost.write_text("will vanish", encoding="utf-8")
        proc_ghost = self.engine.process_file(ghost)
        self.assertEqual(proc_ghost["action"], "pending_review")
        ghost.unlink()

        orig_cb = self.bridge.rescan_callback
        self.bridge.rescan_callback = lambda: 3
        try:
            scan_res = ui_client.rescan()
        finally:
            self.bridge.rescan_callback = orig_cb

        self.assertTrue(scan_res["ok"])
        self.assertEqual(scan_res["pruned_missing"], 1)
        self.assertEqual(scan_res["enqueued"], 3)
        self.assertEqual(scan_res["pending_count"], 0)
        self.assertEqual(ui_client.get_pending(), [])

        # Custom destination folder works end-to-end via DesktopBridgeClient.decide + undo
        custom_doc = self.desktop / "custom_http.pdf"
        custom_doc.write_text("Bridge custom destination", encoding="utf-8")
        proc_custom = self.engine.process_file(custom_doc)
        self.assertEqual(proc_custom["action"], "pending_review")

        custom_dir = Path(self.tmp.name) / "PickedCustomDir"
        dec_res = ui_client.decide(
            proc_custom["pending_id"],
            "choose",
            custom_dest_path=str(custom_dir),
        )
        self.assertTrue(dec_res["ok"])
        self.assertFalse(custom_doc.exists())
        self.assertTrue((custom_dir / "custom_http.pdf").exists())

        undo_res = ui_client.undo()
        self.assertTrue(undo_res["ok"])
        self.assertTrue(custom_doc.exists())

    def test_clear_undo_cooldowns_preserves_ignored_and_dict_rescan(self) -> None:
        # user_undo cooldown should be cleared selectively while user_ignored remains intact
        undo_target = self.desktop / "undo_item.pdf"
        ignored_target = self.desktop / "ignored_item.pdf"
        self.store.add_cooldown(undo_target, "hash_u", 24.0, "user_undo")
        self.store.add_cooldown(ignored_target, "hash_i", 168.0, "user_ignored")

        removed = self.store.clear_cooldowns_by_reason({"user_undo"})
        self.assertEqual(removed, 1)
        self.assertFalse(self.store.is_on_cooldown(undo_target, "hash_u")[0])
        self.assertTrue(self.store.is_on_cooldown(ignored_target, "hash_i")[0])

        # Dict callback with immediate/clear_undo_cooldowns kwargs works cleanly over POST /rescan
        seen_args: list[tuple[bool, bool]] = []

        def fake_rescan(immediate: bool = True, clear_undo_cooldowns: bool = False) -> dict[str, int]:
            seen_args.append((immediate, clear_undo_cooldowns))
            return {"processed": 2, "enqueued": 1, "new_pending": 1}

        ui_client = DesktopBridgeClient(self.base_url, self.token)
        orig_cb = self.bridge.rescan_callback
        self.bridge.rescan_callback = fake_rescan
        try:
            res = ui_client.rescan(immediate=True, clear_undo_cooldowns=True)
        finally:
            self.bridge.rescan_callback = orig_cb

        self.assertTrue(res["ok"])
        self.assertEqual(seen_args, [(True, True)])
        self.assertEqual(res["processed"], 2)
        self.assertEqual(res["enqueued"], 1)
        self.assertEqual(res["pruned_missing"], 0)
        self.assertEqual(res["pending_count"], 0)

    def test_history_endpoint_and_dashboard_client(self) -> None:
        hdrs = {"Authorization": f"Bearer {self.token}"}
        # Auth & limit validation on /history
        self.assertEqual(httpx.get(f"{self.base_url}/history", timeout=3.0).status_code, 401)
        self.assertEqual(
            httpx.get(f"{self.base_url}/history?limit=abc", headers=hdrs, timeout=3.0).status_code,
            400,
        )

        # Move two files sequentially so history has newest-first ordering
        f1 = self.desktop / "hist_one.pdf"
        f2 = self.desktop / "hist_two.pdf"
        f1.write_text("first file", encoding="utf-8")
        f2.write_text("second file", encoding="utf-8")

        p1 = self.engine.process_file(f1)
        p2 = self.engine.process_file(f2)
        dash = DashboardBridgeClient(self.base_url, self.token)

        d1 = self.engine.apply_decision(
            desktop_prompt_decision := __import__("models").DecisionPayload(
                pending_id=p1["pending_id"], action="accept_and_remember"
            )
        )
        d2 = self.engine.apply_decision(
            __import__("models").DecisionPayload(
                pending_id=p2["pending_id"], action="choose", dest_id="desktop_work"
            )
        )
        self.assertTrue(d1["ok"] and d2["ok"])
        _ = desktop_prompt_decision

        all_hist = dash.get_history(limit=100)
        self.assertEqual(len(all_hist), 2)
        # Newest-first ordering
        self.assertEqual(all_hist[0]["tx_id"], d2["tx_id"])
        self.assertEqual(all_hist[1]["tx_id"], d1["tx_id"])
        self.assertFalse(all_hist[0]["undone"])

        # Limit parameter bounds cleanly
        self.assertEqual(len(dash.get_history(limit=1)), 1)
        self.assertEqual(len(dash.get_history(limit=9999)), 2)

        # Undo specific historical tx_id updates undone state in /history
        undo_res = dash.undo(tx_id=d1["tx_id"])
        self.assertTrue(undo_res["ok"])
        self.assertTrue(f1.exists())
        updated_hist = dash.get_history(limit=50)
        tx1_row = next(r for r in updated_hist if r["tx_id"] == d1["tx_id"])
        self.assertTrue(tx1_row["undone"])

        # Status, pending, rules, rescan, delete_rule work cleanly
        self.assertTrue(dash.get_status()["ok"])
        self.assertEqual(dash.get_pending(), [])
        rules = dash.get_rules()
        self.assertEqual(len(rules["learned_rules"]), 1)
        rid = rules["learned_rules"][0]["id"]
        self.assertTrue(dash.delete_rule(rid)["ok"])
        self.assertEqual(len(dash.get_rules()["learned_rules"]), 0)
        self.assertTrue(dash.rescan(immediate=True, clear_undo_cooldowns=False, launch_popup=False)["ok"])

        # Demo client smoke check (all paths under home, zero host mutations)
        demo = DemoDashboardClient()
        self.assertTrue(demo.get_status()["ok"])
        self.assertGreaterEqual(len(demo.get_history()), 2)
        self.assertGreaterEqual(len(demo.get_pending()), 1)
        self.assertTrue(demo.rescan()["ok"])
        demo_undo = demo.undo(tx_id="tx_demo0002")
        self.assertTrue(demo_undo["ok"])
        self.assertTrue(demo.delete_rule("lr_demo01")["ok"])


if __name__ == "__main__":
    unittest.main()
