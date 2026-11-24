"""Tests for safety guardrails, privacy redlines, and content extraction."""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

import guardrails  # noqa: E402
from content_extractor import extract_safe_preview  # noqa: E402
from guardrails import (  # noqa: E402
    has_sensitive_content,
    is_forbidden_path,
    is_ignored_file,
    is_sensitive_file,
    is_valid_destination,
)
from models import DestinationOption  # noqa: E402


class TestGuardrails(unittest.TestCase):
    def test_hardcoded_forbidden_prefixes(self) -> None:
        forbidden_samples = [
            r"C:\Windows\System32\cmd.exe",
            r"C:\Program Files\Git\bin\git.exe",
            r"C:\ProgramData\Vendor\service.dat",
            str(Path("C:/") / "Users" / "demo" / "AppData" / "Local" / "Temp" / "cache.dat"),
        ]
        for sample in forbidden_samples:
            blocked, reason = is_forbidden_path(sample)
            self.assertTrue(blocked, f"Expected forbidden path: {sample} (got {reason})")

    def test_extra_forbidden_protected_paths_and_symlink(self) -> None:
        with mock.patch.object(
            guardrails,
            "FORBIDDEN_PATH_SEGMENTS",
            guardrails.FORBIDDEN_PATH_SEGMENTS - {"appdata"},
        ), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            vault = root / "EncryptedVault"
            vault.mkdir()
            secret = vault / "keys.7z"
            secret.write_bytes(b"7z")

            blocked, reason = is_forbidden_path(secret, extra_forbidden=[str(vault)])
            self.assertTrue(blocked)
            self.assertIn("user_protected_prefix", reason)

            dests = [DestinationOption(id="vault", label="Vault", path=str(vault), description="Vault")]
            self.assertFalse(is_valid_destination(vault, dests, extra_forbidden=[str(vault)]))

            link_target = root / "shortcut.txt"
            link_target.write_text("hi", encoding="utf-8")
            with mock.patch.object(Path, "is_symlink", return_value=True):
                s_blocked, s_reason = is_forbidden_path(link_target)
                self.assertTrue(s_blocked)
                self.assertEqual(s_reason, "symlink_rejected")

    def test_project_markers_protected_inside_temp_workspace(self) -> None:
        with mock.patch.object(
            guardrails,
            "FORBIDDEN_PATH_SEGMENTS",
            guardrails.FORBIDDEN_PATH_SEGMENTS - {"appdata"},
        ), tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for marker in (".git", "package.json", "pyproject.toml", ".venv"):
                proj = root / f"proj_{marker.strip('.')}"
                sub = proj / "nested"
                sub.mkdir(parents=True)
                marker_path = proj / marker
                if marker in {".git", ".venv"}:
                    marker_path.mkdir()
                else:
                    marker_path.write_text("{}", encoding="utf-8")
                target = sub / "main.py"
                target.write_text("print('hi')\n", encoding="utf-8")

                blocked, reason = is_forbidden_path(target, scope_root=root)
                self.assertTrue(blocked, f"Project marker {marker} should protect {target}")
                self.assertEqual(reason, "inside_project_directory")

            loose = root / "loose_script.py"
            loose.write_text("print('loose')\n", encoding="utf-8")
            blocked, reason = is_forbidden_path(loose, scope_root=root)
            self.assertFalse(blocked, f"Loose file should be allowed, got {reason}")

    def test_sensitive_filenames_and_extensions(self) -> None:
        sensitive_names = [
            ".env",
            ".env.production",
            "server_key.pem",
            "id_rsa.key",
            "my_passwd.txt",
            "id_card_身分證.jpg",
            "bank_存摺.pdf",
            "sample_契約.docx",
            "gemini_api_key.txt",
        ]
        for name in sensitive_names:
            flagged, _ = is_sensitive_file(Path(r"C:\temp") / name)
            self.assertTrue(flagged, f"Expected sensitive detection for {name}")
            ignored, _ = is_ignored_file(Path(r"C:\temp") / name)
            self.assertFalse(ignored, f"Sensitive file {name} must not be swallowed as ignored")

    def test_sensitive_content_and_safe_extractor(self) -> None:
        self.assertTrue(has_sensitive_content("-----BEGIN " + "RSA PRIVATE KEY-----\nABC"))
        self.assertTrue(has_sensitive_content("api = AIzaSyDummyTestKey12345678901234567890"))
        self.assertTrue(has_sensitive_content("銀行帳號：123-456-7890"))
        self.assertFalse(has_sensitive_content("CSIE Computer Architecture Lecture 3 notes"))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Sensitive filename is NEVER read
            secret_file = root / "passwd_backup.txt"
            secret_file.write_text("Normal harmless words inside", encoding="utf-8")
            snippet, flagged, note = extract_safe_preview(secret_file, max_chars=400)
            self.assertIsNone(snippet)
            self.assertTrue(flagged)
            self.assertIn("sensitive_filename", note)

            # Innocent filename with sensitive body triggers secondary scan
            innocent_name = root / "meeting_notes.txt"
            innocent_name.write_text(
                "Notes\n-----BEGIN " + "OPENSSH PRIVATE KEY-----\nabcdefg\n",
                encoding="utf-8",
            )
            snippet, flagged, note = extract_safe_preview(innocent_name, max_chars=400)
            self.assertIsNone(snippet)
            self.assertTrue(flagged)
            self.assertEqual(note, "sensitive_content")

            # Clean file extracts <= max_chars
            clean = root / "lecture01.txt"
            clean.write_text("Distributed systems " * 50, encoding="utf-8")
            snippet, flagged, note = extract_safe_preview(clean, max_chars=120)
            self.assertFalse(flagged)
            self.assertEqual(note, "ok")
            self.assertIsNotNone(snippet)
            self.assertLessEqual(len(snippet), 120)

    def test_ignored_temp_files_and_valid_destinations(self) -> None:
        for name in ("movie.mp4.crdownload", "dl.part", "patch.tmp", "~$thesis.docx", "desktop.ini"):
            ignored, _ = is_ignored_file(Path(r"C:\temp") / name)
            self.assertTrue(ignored, f"Expected ignored for {name}")

        with mock.patch.object(
            guardrails,
            "FORBIDDEN_PATH_SEGMENTS",
            guardrails.FORBIDDEN_PATH_SEGMENTS - {"appdata"},
        ), tempfile.TemporaryDirectory() as tmp:
            allowed_dir = Path(tmp) / "Docs"
            dests = [DestinationOption(id="docs", label="Docs", path=str(allowed_dir), description="Docs")]
            self.assertTrue(is_valid_destination(allowed_dir, dests))
            self.assertFalse(is_valid_destination(Path(tmp) / "Elsewhere", dests))
            # Even if someone put a forbidden path in dests, guardrail blocks it
            bad_dests = [DestinationOption(id="win", label="Win", path=r"C:\Windows\Temp", description="Bad")]
            self.assertFalse(is_valid_destination(r"C:\Windows\Temp", bad_dests))

    def test_ics_preview_extraction_and_privacy(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ics = root / "invite.ics"
            # Long VTIMEZONE preamble before VEVENT so naive byte slicing would miss SUMMARY
            vtz = "BEGIN:VTIMEZONE\r\nTZID:Asia/Taipei\r\n" + ("X-FILLER:padding\r\n" * 300) + "END:VTIMEZONE\r\n"
            ics.write_text(
                "BEGIN:VCALENDAR\r\n"
                + vtz
                + "BEGIN:VEVENT\r\n"
                "DTSTART;TZID=Asia/Taipei:20261201T140000\r\n"
                "ORGANIZER;CN=Acme Recruiting:mailto:recruiting@example.com\r\n"
                "SUMMARY:Software Intern \r\n"
                " Technical Interview\r\n"
                "LOCATION:Microsoft Teams\r\n"
                "END:VEVENT\r\n"
                "END:VCALENDAR\r\n",
                encoding="utf-8",
                newline="",
            )
            snippet, flagged, note = extract_safe_preview(ics, max_chars=400)
            self.assertFalse(flagged)
            self.assertEqual(note, "ok")
            self.assertIsNotNone(snippet)
            self.assertIn("Software Intern Technical Interview", snippet)
            self.assertIn("Acme Recruiting", snippet)

            secret_ics = root / "calendar_event.ics"
            secret_ics.write_text(
                "BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Account\r\n"
                "DESCRIPTION:密碼：mysecret123\r\nEND:VEVENT\r\nEND:VCALENDAR\r\n",
                encoding="utf-8",
            )
            snippet2, flagged2, note2 = extract_safe_preview(secret_ics, max_chars=400)
            self.assertIsNone(snippet2)
            self.assertTrue(flagged2)
            self.assertEqual(note2, "sensitive_content")


if __name__ == "__main__":
    unittest.main()
