"""Tests for AI classifier providers using a local mock HTTP server."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

SRC_DIR = Path(__file__).resolve().parent.parent / "src"
if str(SRC_DIR) not in sys.path:
    sys.path.insert(0, str(SRC_DIR))

from models import AIProviderConfig, ClassificationRequest, DestinationOption  # noqa: E402
from providers.base import UNSURE  # noqa: E402
from providers.factory import NoneProvider, create_provider  # noqa: E402
from providers.ollama_provider import OllamaProvider  # noqa: E402
from providers.openai_compat_provider import OpenAICompatibleProvider  # noqa: E402


def _make_request() -> ClassificationRequest:
    return ClassificationRequest(
        filename="computer_arch_hw2.pdf",
        extension="pdf",
        current_scope="downloads",
        current_dir="Downloads",
        size_bytes=2048,
        preview_text="MIPS pipeline hazards and forwarding",
        destinations=[
            DestinationOption(id="desktop_work", label="Work", path=r"C:\Temp\Work", description="Courses"),
            DestinationOption(id="inbox_docs", label="Inbox", path=r"C:\Temp\Inbox", description="General docs"),
        ],
    )


class TestProviders(unittest.TestCase):
    def setUp(self) -> None:
        self.received_payloads: list[dict] = []
        self.received_headers: list[dict] = []
        self.chat_handler = None
        parent = self

        class MockHandler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):
                return

            def do_GET(self):
                if self.path == "/api/tags":
                    data = {"models": [{"name": "qwen2.5:3b"}]}
                elif self.path.endswith("/models"):
                    data = {"data": [{"id": "mock-model"}]}
                else:
                    self.send_response(404)
                    self.end_headers()
                    return
                raw = json.dumps(data).encode("utf-8")
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length).decode("utf-8"))
                parent.received_payloads.append(body)
                parent.received_headers.append(dict(self.headers))
                code, resp_obj = parent.chat_handler(self.path, body)
                raw = json.dumps(resp_obj).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), MockHandler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def test_ollama_schema_think_false_and_hallucination_guard(self) -> None:
        def handler(path, body):
            self.assertEqual(path, "/api/chat")
            return 200, {
                "message": {
                    "content": '<think>internal</think>```json\n'
                    '{"destination_id": "desktop_work", "confidence": 0.94, '
                    '"reason": "電腦架構課程作業", "suggested_keyword": "computer_arch"}\n```'
                }
            }

        self.chat_handler = handler
        cfg = AIProviderConfig(
            provider="ollama",
            model="qwen2.5:3b",
            base_url=f"http://127.0.0.1:{self.port}",
            keep_alive="2m",
            disable_thinking=True,
        )
        provider = OllamaProvider(cfg)
        health = provider.health_check()
        self.assertTrue(health["available"])
        self.assertTrue(health["model_installed"])

        res = provider.classify(_make_request())
        self.assertEqual(res.destination_id, "desktop_work")
        self.assertAlmostEqual(res.confidence, 0.94)
        self.assertEqual(res.suggested_keyword, "computer_arch")
        self.assertIsNone(res.error)

        sent = self.received_payloads[-1]
        self.assertIs(sent["think"], False)
        self.assertEqual(sent["keep_alive"], "2m")
        self.assertIn("desktop_work", sent["format"]["properties"]["destination_id"]["enum"])

        # Hallucinated destination_id must be rejected back to UNSURE
        self.chat_handler = lambda p, b: (
            200,
            {"message": {"content": '{"destination_id": "invented_dir", "confidence": 0.99, "reason": "x", "suggested_keyword": ""}'}},
        )
        bad = provider.classify(_make_request())
        self.assertEqual(bad.destination_id, UNSURE)
        self.assertEqual(bad.confidence, 0.0)
        self.assertIn("hallucinated_destination", bad.error or "")

    def test_ollama_400_retries_without_think_and_offline_graceful(self) -> None:
        calls = 0

        def handler(path, body):
            nonlocal calls
            calls += 1
            if "think" in body:
                return 400, {"error": "unknown field think"}
            return 200, {
                "message": {
                    "content": '{"destination_id": "inbox_docs", "confidence": 0.88, "reason": "文件", "suggested_keyword": ""}'
                }
            }

        self.chat_handler = handler
        cfg = AIProviderConfig(provider="ollama", model="qwen2.5:3b", base_url=f"http://127.0.0.1:{self.port}")
        res = OllamaProvider(cfg).classify(_make_request())
        self.assertEqual(calls, 2)
        self.assertEqual(res.destination_id, "inbox_docs")
        self.assertAlmostEqual(res.confidence, 0.88)

        # Unreachable port -> returns UNSURE without raising
        offline_cfg = AIProviderConfig(provider="ollama", model="qwen2.5:3b", base_url="http://127.0.0.1:1", timeout_seconds=1.0)
        off_res = OllamaProvider(offline_cfg).classify(_make_request())
        self.assertEqual(off_res.destination_id, UNSURE)
        self.assertIsNotNone(off_res.error)

    def test_openai_compat_and_fallbacks(self) -> None:
        self.chat_handler = lambda p, b: (
            200,
            {
                "choices": [
                    {
                        "message": {
                            "content": '{"destination_id": "desktop_work", "confidence": 0.91, "reason": "課程", "suggested_keyword": "arch"}'
                        }
                    }
                ]
            },
        )
        cfg = AIProviderConfig(
            provider="openai_compat",
            model="llama-3.3-70b",
            base_url=f"http://127.0.0.1:{self.port}/v1",
        )
        p = OpenAICompatibleProvider(cfg)
        self.assertTrue(p.health_check()["available"])
        res = p.classify(_make_request())
        self.assertEqual(res.destination_id, "desktop_work")

        # Gemini without API key & NoneProvider degrade cleanly
        gem = create_provider(AIProviderConfig(provider="gemini", api_key_env="NONEXISTENT_KEY_XYZ_123"))
        self.assertFalse(gem.health_check()["available"])
        self.assertEqual(gem.classify(_make_request()).destination_id, UNSURE)

        none_p = create_provider(AIProviderConfig(provider="none"))
        self.assertIsInstance(none_p, NoneProvider)
        self.assertEqual(none_p.classify(_make_request()).destination_id, UNSURE)


if __name__ == "__main__":
    unittest.main()
