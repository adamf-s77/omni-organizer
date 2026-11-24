"""Tests for Three-Tier DecisionEngine, Undo, Cooldown, Anti-Tug-of-War, and HITL."""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import guardrails  # noqa: E402
from decision_engine import DecisionEngine  # noqa: E402
from executor import undo_move  # noqa: E402
from lifecycle_archiver import scan_archive_candidates  # noqa: E402
from models import (  # noqa: E402
    AIProviderConfig,
    ArchiveRule,
    ClassificationRequest,
    ClassificationResult,
    DecisionPayload,
    DestinationOption,
    DeterministicRule,
    MonitoredScope,
    V3Settings,
)
from providers.base import UNSURE, BaseClassifierProvider  # noqa: E402
from state_store import StateStore  # noqa: E402


class StubProvider(BaseClassifierProvider):
    name = "stub"

    def __init__(self, handler) -> None:
        self.handler = handler
        self.calls: list[ClassificationRequest] = []

    def classify(self, req: ClassificationRequest) -> ClassificationResult:
        self.calls.append(req)
        return self.handler(req)

    def health_check(self) -> dict:
        return {"provider": self.name, "available": True}


def _build_env(root: Path, provider: BaseClassifierProvider, *, with_fallback: bool = False):
    desktop = root / "Desktop"
    work = desktop / "01_Coursework"
    resume = desktop / "02_Career" / "_Pending"
    personal = desktop / "04_Personal"
    inbox_docs = desktop / "_Inbox" / "Docs"
    dl_installers = root / "Downloads" / "01_Installers"
    for d in (desktop, work, resume, personal, inbox_docs, dl_installers):
        d.mkdir(parents=True, exist_ok=True)

    scopes = [
        MonitoredScope(
            id="desktop",
            path=str(desktop),
            mode="root_and_subdirs",
            allowed_subdirs=["_Inbox", "_Inbox\\Docs", "01_Coursework", "02_Career", "04_Personal"],
            misplacement_subdirs=["01_Coursework", "02_Career", "04_Personal"],
            max_depth=2,
        )
    ]
    dests = [
        DestinationOption(id="desktop_work", label="Coursework", path=str(work), description="Coursework"),
        DestinationOption(id="desktop_resume", label="Career", path=str(resume), description="Resume files"),
        DestinationOption(id="desktop_personal", label="Personal", path=str(personal), description="Personal docs"),
        DestinationOption(id="inbox_docs", label="Inbox Docs", path=str(inbox_docs), description="General docs"),
        DestinationOption(id="dl_installers", label="Installers", path=str(dl_installers), description="Installers"),
    ]
    t1_rules = [
        DeterministicRule(
            id="t1_installers",
            name="Installers",
            dest_id="dl_installers",
            scopes=["desktop"],
            extensions=["exe", "msi"],
        )
    ]
    fb_rules = (
        [
            DeterministicRule(
                id="fb_docs",
                name="Fallback Docs",
                dest_id="inbox_docs",
                scopes=["desktop"],
                extensions=["pdf", "txt"],
            )
        ]
        if with_fallback
        else []
    )
    settings = V3Settings(
        ai=AIProviderConfig(provider="none", auto_move_threshold=0.85, misplacement_threshold=0.90),
        monitored_scopes=scopes,
        destinations=dests,
        tier1_rules=t1_rules,
        privacy_zone_dest_ids=["desktop_personal"],
        offline_fallback_rules=fb_rules,
    )
    store = StateStore(root / "state")
    engine = DecisionEngine(settings, store, provider)
    return desktop, work, personal, inbox_docs, dl_installers, settings, store, engine


class TestDecisionEngine(unittest.TestCase):
    def setUp(self) -> None:
        patcher = mock.patch.object(
            guardrails,
            "FORBIDDEN_PATH_SEGMENTS",
            guardrails.FORBIDDEN_PATH_SEGMENTS - {"appdata"},
        )
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_tier1_move_conflict_skip_and_undo_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = StubProvider(lambda req: ClassificationResult(destination_id=UNSURE, confidence=0.0))
            desktop, _, _, _, dl_installers, settings, store, engine = _build_env(Path(tmp), provider)

            installer = desktop / "setup_tool.exe"
            installer.write_bytes(b"MZ_FAKE_EXE")
            res = engine.process_file(installer)
            self.assertEqual(res["action"], "auto_moved")
            self.assertEqual(res["tier"], "tier1_rule")
            self.assertFalse(installer.exists())
            self.assertTrue((dl_installers / "setup_tool.exe").exists())
            self.assertEqual(len(provider.calls), 0)
            self.assertEqual(len(store.list_notifications(only_unacked=True)), 1)

            # Conflict skip: create duplicate filename after self-move window -> must NOT overwrite
            store._self_moves.clear()
            dup = desktop / "setup_tool.exe"
            dup.write_bytes(b"NEW_DATA")
            res2 = engine.process_file(dup)
            self.assertEqual(res2["action"], "skipped_conflict")
            self.assertTrue(dup.exists())
            self.assertEqual((dl_installers / "setup_tool.exe").read_bytes(), b"MZ_FAKE_EXE")
            dup.unlink()

            # Undo restores file and puts 24h cooldown so reprocessing does NOT re-move it
            undo_res = undo_move(store, settings, res["tx_id"])
            self.assertTrue(undo_res["ok"])
            self.assertTrue(installer.exists())
            self.assertFalse((dl_installers / "setup_tool.exe").exists())

            # Simulate self-move TTL expiry to verify persistent 24h cooldown
            store._self_moves.clear()
            recheck = engine.process_file(installer)
            self.assertEqual(recheck["action"], "cooldown")
            self.assertTrue(installer.exists())

    def test_manual_reversal_triggers_antitug_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = StubProvider(lambda req: ClassificationResult(destination_id=UNSURE, confidence=0.0))
            desktop, _, _, _, dl_installers, _, store, engine = _build_env(Path(tmp), provider)

            f = desktop / "driver.msi"
            f.write_bytes(b"MSI_BYTES")
            res = engine.process_file(f)
            self.assertEqual(res["action"], "auto_moved")

            # Simulate user manually dragging file back from dl_installers to Desktop
            moved_path = dl_installers / "driver.msi"
            store._self_moves.clear()
            shutil.move(str(moved_path), str(f))

            res2 = engine.process_file(f)
            self.assertEqual(res2["action"], "manual_reversal_cooldown")
            self.assertTrue(f.exists())
            self.assertEqual(engine.process_file(f)["action"], "cooldown")

    def test_tier2_high_confidence_and_low_confidence_hitl_learning(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            def classify(req: ClassificationRequest) -> ClassificationResult:
                if "computer_arch" in req.filename:
                    return ClassificationResult(
                        destination_id="desktop_work",
                        confidence=0.92,
                        reason="電腦結構課程檔案",
                        suggested_keyword="computer_arch",
                        provider="stub",
                    )
                return ClassificationResult(
                    destination_id="desktop_work",
                    confidence=0.60,
                    reason="可能是課程筆記",
                    suggested_keyword="hw_notes",
                    provider="stub",
                )

            provider = StubProvider(classify)
            desktop, work, _, _, _, _, store, engine = _build_env(Path(tmp), provider)

            # High confidence auto-move
            hi = desktop / "computer_arch_ch1.pdf"
            hi.write_text("Instruction pipelining", encoding="utf-8")
            res_hi = engine.process_file(hi)
            self.assertEqual(res_hi["action"], "auto_moved")
            self.assertEqual(res_hi["tier"], "tier2_ai")
            self.assertTrue((work / "computer_arch_ch1.pdf").exists())

            # Low confidence -> Tier 3 pending (stays on Desktop)
            lo = desktop / "hw_notes_week4.txt"
            lo.write_text("Notes", encoding="utf-8")
            res_lo = engine.process_file(lo)
            self.assertEqual(res_lo["action"], "pending_review")
            self.assertTrue(lo.exists())

            # User clicks "Move & Remember Rule" -> moves AND learns rule "hw_notes"
            dec = engine.apply_decision(
                DecisionPayload(pending_id=res_lo["pending_id"], action="accept_and_remember")
            )
            self.assertTrue(dec["ok"])
            self.assertIsNotNone(dec["rule_id"])
            self.assertTrue((work / "hw_notes_week4.txt").exists())

            # Next matching file hits Tier 1 learned rule WITHOUT calling AI
            calls_before = len(provider.calls)
            lo2 = desktop / "hw_notes_week5.txt"
            lo2.write_text("More notes", encoding="utf-8")
            res_lo2 = engine.process_file(lo2)
            self.assertEqual(res_lo2["action"], "auto_moved")
            self.assertEqual(res_lo2["tier"], "tier1_learned")
            self.assertEqual(len(provider.calls), calls_before)
            rules = store.list_learned_rules()
            self.assertEqual(rules[0].hit_count, 1)

    def test_privacy_redline_never_reads_or_calls_ai(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = StubProvider(
                lambda req: ClassificationResult(destination_id="inbox_docs", confidence=0.99)
            )
            desktop, _, personal, _, _, _, store, engine = _build_env(Path(tmp), provider)

            # Sensitive filename -> straight to Tier 3 pending, 0 AI calls
            secret = desktop / "bank_passwd.txt"
            secret.write_text("pw=1234", encoding="utf-8")
            res1 = engine.process_file(secret)
            self.assertEqual(res1["action"], "pending_sensitive")
            self.assertTrue(secret.exists())
            self.assertEqual(len(provider.calls), 0)

            # Innocent filename with sensitive content -> secondary scan catches before AI call
            leak = desktop / "notes.txt"
            leak.write_text("-----BEGIN " + "RSA PRIVATE KEY-----\nSECRET", encoding="utf-8")
            res2 = engine.process_file(leak)
            self.assertEqual(res2["action"], "pending_sensitive")
            self.assertTrue(leak.exists())
            self.assertEqual(len(provider.calls), 0)

            # Choose destination manually -> moves safely
            choose_res = engine.apply_decision(
                DecisionPayload(
                    pending_id=res1["pending_id"],
                    action="choose",
                    dest_id="desktop_personal",
                    remember=True,  # sensitive files must NOT auto-create learned rules
                )
            )
            self.assertTrue(choose_res["ok"])
            self.assertIsNone(choose_res["rule_id"])
            self.assertTrue((personal / "bank_passwd.txt").exists())

            # Ignore puts 24h cooldown
            ign_res = engine.apply_decision(
                DecisionPayload(pending_id=res2["pending_id"], action="ignore")
            )
            self.assertTrue(ign_res["ok"])
            self.assertEqual(engine.process_file(leak)["action"], "cooldown")

    def test_offline_provider_behavior_and_misplacement_guard(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            offline = StubProvider(
                lambda req: ClassificationResult(
                    destination_id=UNSURE,
                    confidence=0.0,
                    error="ConnectError: offline",
                    provider="stub",
                )
            )
            # Without fallback rules -> intake file waits in Tier 3 pending
            desktop, work, _, _, _, _, _, engine = _build_env(Path(tmp) / "no_fb", offline, with_fallback=False)
            doc = desktop / "random.pdf"
            doc.write_text("hello", encoding="utf-8")
            self.assertEqual(engine.process_file(doc)["action"], "pending_review")
            self.assertTrue(doc.exists())

            # Existing file in 01_Coursework is NOT spammed when AI is offline
            placed = work / "syllabus.pdf"
            placed.write_text("syllabus", encoding="utf-8")
            self.assertEqual(engine.process_file(placed)["action"], "kept_in_place")

            # With fallback rules -> intake file falls back cleanly to inbox_docs
            desktop2, _, _, inbox_docs2, _, _, _, engine2 = _build_env(
                Path(tmp) / "with_fb", offline, with_fallback=True
            )
            doc2 = desktop2 / "random2.txt"
            doc2.write_text("hello", encoding="utf-8")
            res2 = engine2.process_file(doc2)
            self.assertEqual(res2["action"], "auto_moved")
            self.assertEqual(res2["tier"], "tier1_offline_fallback")
            self.assertTrue((inbox_docs2 / "random2.txt").exists())

    def test_ai_selectable_filtering_and_generic_stem_cap(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            desktop = root / "Desktop"
            work = desktop / "01_Coursework"
            staging = root / "Downloads" / "03_Docs"
            for d in (desktop, work, staging):
                d.mkdir(parents=True, exist_ok=True)

            settings = V3Settings(
                ai=AIProviderConfig(provider="none", auto_move_threshold=0.85),
                monitored_scopes=[MonitoredScope(id="desktop", path=str(desktop), mode="root_only")],
                destinations=[
                    DestinationOption(
                        id="desktop_work", label="Coursework", path=str(work),
                        description="Coursework", ai_selectable=True,
                    ),
                    DestinationOption(
                        id="dl_docs", label="Downloads Docs", path=str(staging),
                        description="Staging", ai_selectable=False,
                    ),
                ],
            )
            store = StateStore(root / "state")

            # Overconfident provider picks desktop_work at 0.95
            provider = StubProvider(
                lambda req: ClassificationResult(
                    destination_id="desktop_work",
                    confidence=0.95,
                    reason="盲猜",
                    provider="stub",
                )
            )
            engine = DecisionEngine(settings, store, provider)

            # Empty generic file (invite.ics with no text) -> capped < 0.85 -> routes to Tier 3
            generic_ics = desktop / "invite.ics"
            generic_ics.write_text("", encoding="utf-8")
            res = engine.process_file(generic_ics)
            self.assertEqual(res["action"], "pending_review")
            self.assertTrue(generic_ics.exists())
            sent_ids = [d.id for d in provider.calls[-1].destinations]
            self.assertEqual(sent_ids, ["desktop_work"])
            self.assertNotIn("dl_docs", sent_ids)

            # Even if a buggy provider returns an ai_selectable=False destination, engine rejects it
            bad_provider = StubProvider(
                lambda req: ClassificationResult(
                    destination_id="dl_docs",
                    confidence=0.99,
                    reason="嘗試逆搬",
                    provider="stub",
                )
            )
            engine_bad = DecisionEngine(settings, store, bad_provider)
            doc = desktop / "lecture_slides.pdf"
            doc.write_text("Operating system notes", encoding="utf-8")
            res_bad = engine_bad.process_file(doc)
            self.assertEqual(res_bad["action"], "pending_review")
            self.assertTrue(doc.exists())
            self.assertFalse((staging / "lecture_slides.pdf").exists())

    def test_ai_require_confirmation_routes_tier2_to_discord(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            provider = StubProvider(
                lambda req: ClassificationResult(
                    destination_id="desktop_work",
                    confidence=0.95,
                    reason="電腦結構筆記",
                    suggested_keyword="arch_hw",
                    provider="stub",
                )
            )
            desktop, work, _, _, dl_installers, settings, store, engine = _build_env(Path(tmp), provider)
            settings.ai.require_confirmation = True

            # Tier 1 rule still auto-moves immediately
            exe = desktop / "setup.exe"
            exe.write_bytes(b"MZ")
            self.assertEqual(engine.process_file(exe)["action"], "auto_moved")
            self.assertTrue((dl_installers / "setup.exe").exists())

            # Tier 2 high confidence (0.95) stops at Tier 3 pending for confirmation
            hw = desktop / "arch_hw1.pdf"
            hw.write_text("MIPS pipeline", encoding="utf-8")
            res = engine.process_file(hw)
            self.assertEqual(res["action"], "pending_review")
            self.assertTrue(hw.exists())
            self.assertFalse((work / "arch_hw1.pdf").exists())
            pending = store.get_pending(res["pending_id"])
            self.assertIsNotNone(pending)
            self.assertEqual(pending.suggested_dest_id, "desktop_work")

    def test_lifecycle_archiver_queues_aged_files_and_respects_cooldown(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            inbox = root / "Desktop" / "_Inbox"
            archive_dest = root / "D_Archive"
            for d in (inbox, archive_dest):
                d.mkdir(parents=True, exist_ok=True)

            settings = V3Settings(
                archive_max_per_sweep=2,
                destinations=[
                    DestinationOption(id="d_archive", label="Archive", path=str(archive_dest), description="Archive")
                ],
                archive_rules=[
                    ArchiveRule(
                        id="arch_inbox_30d",
                        name="Inbox 30d",
                        source_path=str(inbox),
                        recursive=True,
                        min_age_days=30,
                        dest_id="d_archive",
                        ignore_cooldown_hours=168.0,
                    )
                ],
            )
            store = StateStore(root / "state")
            engine = DecisionEngine(
                settings,
                store,
                StubProvider(lambda req: ClassificationResult(destination_id=UNSURE, confidence=0.0)),
            )

            now = time.time()
            old1 = inbox / "old_slide.pdf"
            old2 = inbox / "old_notes.txt"
            old3 = inbox / "old_extra.csv"
            fresh = inbox / "today.txt"
            secret = inbox / "my_passwd.txt"
            for p, age_days in (
                (old1, 35),
                (old2, 40),
                (old3, 45),
                (fresh, 1),
                (secret, 60),
            ):
                p.write_text(f"content {p.name}", encoding="utf-8")
                ts = now - age_days * 86400
                os.utime(p, (ts, ts))

            queued = scan_archive_candidates(settings, store, now=now)
            # Capped at 2 per sweep; fresh and sensitive files never queued; nothing moved yet
            self.assertEqual(len(queued), 2)
            self.assertTrue(all(item.kind == "archive" for item in queued))
            queued_names = {item.filename for item in queued}
            self.assertNotIn("today.txt", queued_names)
            self.assertNotIn("my_passwd.txt", queued_names)
            self.assertEqual(list(archive_dest.iterdir()), [])

            # Ignoring one item puts 168h cooldown; accepting the other moves it cleanly without creating learned rule
            first, second = queued[0], queued[1]
            ign = engine.apply_decision(DecisionPayload(pending_id=first.id, action="ignore"))
            self.assertTrue(ign["ok"])
            acc = engine.apply_decision(DecisionPayload(pending_id=second.id, action="accept_and_remember"))
            self.assertTrue(acc["ok"])
            self.assertIsNone(acc["rule_id"])
            self.assertTrue((archive_dest / second.filename).exists())

            # Next sweep skips the ignored file (168h cooldown) and picks up the remaining aged file
            remaining = [name for name in ("old_slide.pdf", "old_notes.txt", "old_extra.csv") if name not in queued_names]
            queued2 = scan_archive_candidates(settings, store, now=now + 3600)
            self.assertEqual([q.filename for q in queued2], remaining)

    def test_custom_dest_path_and_guardrails(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            provider = StubProvider(lambda req: ClassificationResult(destination_id=UNSURE, confidence=0.0))
            desktop, _, _, _, _, settings, store, engine = _build_env(root, provider)

            protected_vault = root / "ProtectedVault"
            protected_vault.mkdir()
            settings.protected_paths = [str(protected_vault)]

            git_proj = root / "CodeRepo"
            (git_proj / ".git").mkdir(parents=True)

            # Relative path & forbidden targets must be rejected safely
            f1 = desktop / "custom_doc.pdf"
            f1.write_text("Project proposal", encoding="utf-8")
            p1 = engine.process_file(f1)
            self.assertEqual(p1["action"], "pending_review")
            pid = p1["pending_id"]

            for bad_target, expected_err in (
                ("relative/dir", "invalid_custom_destination"),
                (r"C:\Windows\Temp", "destination_forbidden"),
                (str(protected_vault), "destination_forbidden"),
                (str(git_proj), "destination_forbidden"),
            ):
                rej = engine.apply_decision(
                    DecisionPayload(pending_id=pid, action="choose", custom_dest_path=bad_target)
                )
                self.assertFalse(rej["ok"], f"Expected rejection for {bad_target}")
                self.assertIn(expected_err, rej["error"])
                self.assertTrue(f1.exists())
                self.assertEqual(store.get_pending(pid).status, "pending")

            # Safe arbitrary custom folder outside preset whitelist -> moves cleanly & is undoable
            custom_dir = root / "Arbitrary" / "CustomFolder"
            ok_res = engine.apply_decision(
                DecisionPayload(pending_id=pid, action="choose", custom_dest_path=str(custom_dir))
            )
            self.assertTrue(ok_res["ok"])
            self.assertFalse(f1.exists())
            self.assertTrue((custom_dir / "custom_doc.pdf").exists())

            undo_res = undo_move(store, settings, ok_res["tx_id"])
            self.assertTrue(undo_res["ok"])
            self.assertTrue(f1.exists())
            self.assertFalse((custom_dir / "custom_doc.pdf").exists())


if __name__ == "__main__":
    unittest.main()
