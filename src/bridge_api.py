"""Localhost-only, token-protected HTTP Bridge API (stdlib, no extra dependencies)."""

from __future__ import annotations

import hmac
import inspect
import json
import os
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any
from urllib.parse import parse_qs, urlparse

from decision_engine import DecisionEngine
from executor import undo_move
from models import DecisionPayload, V3Settings
from state_store import StateStore

LOOPBACK_HOSTS = {"127.0.0.1", "localhost"}
HOST_HEADER_RE = re.compile(r"^(127\.0\.0\.1|localhost)(:\d{1,5})?$", re.IGNORECASE)
SAFE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
MAX_BODY_BYTES = 64 * 1024


class BridgeServer:
    def __init__(
        self,
        host: str,
        port: int,
        token: str,
        engine: DecisionEngine,
        store: StateStore,
        settings: V3Settings,
        started_at: float | None = None,
        logger=None,
        rescan_callback=None,
    ) -> None:
        if host not in LOOPBACK_HOSTS:
            raise ValueError(f"Bridge must bind to loopback only, got {host!r}")
        if not token or len(token) < 16:
            raise ValueError("Bridge token must be at least 16 characters")
        self.host, self.port, self.token = host, port, token
        self.engine, self.store, self.settings = engine, store, settings
        self.started_at = started_at or time.time()
        self.log = logger or (lambda msg: None)
        self.rescan_callback = rescan_callback
        self._httpd: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # ------------------------------------------------------------------
    def status_payload(self) -> dict[str, Any]:
        try:
            provider_health = self.engine.provider.health_check()
        except Exception as exc:
            provider_health = {"available": False, "reason": type(exc).__name__}
        return {
            "ok": True,
            "version": self.settings.version,
            "pid": os.getpid(),
            "uptime_sec": int(time.time() - self.started_at),
            "monitored_scopes": [
                {"id": s.id, "path": s.path, "mode": s.mode, "exists": os.path.isdir(s.path)}
                for s in self.settings.monitored_scopes if s.enabled
            ],
            "ai": {
                "provider": self.settings.ai.provider,
                "model": self.settings.ai.model,
                "auto_move_threshold": self.settings.ai.auto_move_threshold,
                "health": provider_health,
            },
            "counts": {
                "pending": len(self.store.list_pending()),
                "learned_rules": len(self.store.list_learned_rules()),
                "undo_records": len(self.store.list_undo_records(limit=100000)),
                "active_cooldowns": self.store.active_cooldown_count(),
            },
        }

    # ------------------------------------------------------------------
    def _make_handler(self):
        bridge = self

        class Handler(BaseHTTPRequestHandler):
            server_version = "OmniOrganizerBridge/3"

            def log_message(self, fmt, *args):  # silence default stderr logging
                return

            def _send(self, code: int, body: Any) -> None:
                data = json.dumps(body, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(data)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionAbortedError, ConnectionResetError):
                    pass

            def _authorized(self) -> bool:
                header = self.headers.get("Authorization", "")
                if not header.startswith("Bearer "):
                    return False
                return hmac.compare_digest(header[7:].strip().encode(), bridge.token.encode())

            def _body(self) -> dict[str, Any]:
                length = int(self.headers.get("Content-Length") or 0)
                if length <= 0:
                    return {}
                if length > MAX_BODY_BYTES:
                    raise ValueError("body_too_large")
                raw = self.rfile.read(length)
                data = json.loads(raw.decode("utf-8") or "{}")
                if not isinstance(data, dict):
                    raise ValueError("body_must_be_object")
                return data

            def _route(self, method: str) -> None:
                host_hdr = (self.headers.get("Host") or "").strip()
                if host_hdr and not HOST_HEADER_RE.match(host_hdr):
                    return self._send(403, {"ok": False, "error": "forbidden_host"})

                parsed = urlparse(self.path)
                parts = [p for p in parsed.path.split("/") if p]
                query = parse_qs(parsed.query)

                if method == "GET" and parts == ["health"]:
                    return self._send(200, {"ok": True, "version": bridge.settings.version})
                if not self._authorized():
                    return self._send(401, {"ok": False, "error": "unauthorized"})

                try:
                    if method == "GET" and parts == ["status"]:
                        return self._send(200, bridge.status_payload())
                    if method == "GET" and parts == ["history"]:
                        raw_limit = query.get("limit", ["100"])[0]
                        try:
                            limit = int(raw_limit)
                        except ValueError:
                            return self._send(400, {"ok": False, "error": "invalid_limit"})
                        limit = max(1, min(500, limit))
                        records = bridge.store.list_undo_records(limit=limit)
                        return self._send(200, {
                            "ok": True,
                            "history": [r.model_dump() for r in records],
                        })
                    if method == "GET" and parts == ["destinations"]:
                        return self._send(200, {"ok": True, "destinations": [
                            d.model_dump() for d in bridge.settings.destinations]})
                    if method == "GET" and parts == ["pending"]:
                        only_new = query.get("unnotified", ["0"])[0] in {"1", "true"}
                        items = bridge.store.list_pending(only_unnotified=only_new)
                        return self._send(200, {"ok": True, "pending": [i.model_dump() for i in items]})
                    if method == "POST" and len(parts) == 3 and parts[0] == "pending" and parts[2] == "ack":
                        if not SAFE_ID_RE.match(parts[1]):
                            return self._send(400, {"ok": False, "error": "invalid_id"})
                        return self._send(200, {"ok": bridge.store.mark_pending_notified(parts[1])})
                    if method == "GET" and parts == ["notifications"]:
                        only_new = query.get("unacked", ["0"])[0] in {"1", "true"}
                        events = bridge.store.list_notifications(only_unacked=only_new, limit=20)
                        return self._send(200, {"ok": True, "notifications": [e.model_dump() for e in events]})
                    if method == "POST" and len(parts) == 3 and parts[0] == "notifications" and parts[2] == "ack":
                        if not SAFE_ID_RE.match(parts[1]):
                            return self._send(400, {"ok": False, "error": "invalid_id"})
                        return self._send(200, {"ok": bridge.store.ack_notification(parts[1])})
                    if method == "POST" and parts == ["decide"]:
                        payload = DecisionPayload.model_validate(self._body())
                        result = bridge.engine.apply_decision(payload)
                        return self._send(200 if result.get("ok") else 409, result)
                    if method == "POST" and parts == ["rescan"]:
                        body = self._body()
                        immediate = bool(body.get("immediate", True))
                        clear_undo_cooldowns = bool(body.get("clear_undo_cooldowns", False))
                        launch_popup = bool(body.get("launch_popup", False))
                        pruned = bridge.engine.prune_missing_pending()
                        processed = 0
                        enqueued = 0
                        if bridge.rescan_callback is not None:
                            try:
                                sig_params = inspect.signature(bridge.rescan_callback).parameters
                                cb_kwargs: dict[str, Any] = {}
                                if "immediate" in sig_params:
                                    cb_kwargs["immediate"] = immediate
                                if "clear_undo_cooldowns" in sig_params:
                                    cb_kwargs["clear_undo_cooldowns"] = clear_undo_cooldowns
                                cb_res = bridge.rescan_callback(**cb_kwargs)
                                if isinstance(cb_res, dict):
                                    processed = int(cb_res.get("processed") or 0)
                                    enqueued = int(cb_res.get("enqueued") or 0)
                                else:
                                    enqueued = int(cb_res or 0)
                            except Exception as exc:
                                bridge.log(f"[bridge] rescan_callback 失敗: {type(exc).__name__}: {exc}")
                        pending_count = len(bridge.store.list_pending())
                        if launch_popup and pending_count > 0:
                            from desktop_prompt import launch_desktop_prompt_if_needed

                            launch_desktop_prompt_if_needed(bridge.settings, logger=bridge.log)
                        return self._send(200, {
                            "ok": True,
                            "pruned_missing": pruned,
                            "processed": processed,
                            "enqueued": enqueued,
                            "pending_count": pending_count,
                        })
                    if method == "POST" and parts == ["undo"]:
                        tx_id = self._body().get("tx_id") or None
                        with bridge.engine._lock:
                            result = undo_move(bridge.store, bridge.settings, tx_id)
                        if result.get("ok"):
                            bridge.log(f"[undo] 已還原 tx={result['tx_id']} -> {result['restored_to']}")
                        return self._send(200 if result.get("ok") else 409, result)
                    if method == "GET" and parts == ["rules"]:
                        return self._send(200, {
                            "ok": True,
                            "learned_rules": [r.model_dump() for r in bridge.store.list_learned_rules()],
                            "tier1_rules": [r.model_dump() for r in bridge.settings.tier1_rules],
                        })
                    if method == "DELETE" and len(parts) == 2 and parts[0] == "rules":
                        if not SAFE_ID_RE.match(parts[1]):
                            return self._send(400, {"ok": False, "error": "invalid_id"})
                        ok = bridge.store.delete_learned_rule(parts[1])
                        return self._send(200 if ok else 404, {"ok": ok, "rule_id": parts[1]})
                    return self._send(404, {"ok": False, "error": "not_found"})
                except (ValueError, json.JSONDecodeError) as exc:
                    return self._send(400, {"ok": False, "error": f"bad_request:{exc}"[:300]})
                except Exception as exc:
                    bridge.log(f"[bridge] 內部錯誤 {method} {parsed.path}: {type(exc).__name__}: {exc}")
                    return self._send(500, {"ok": False, "error": "internal_error"})

            def do_GET(self):
                self._route("GET")

            def do_POST(self):
                self._route("POST")

            def do_DELETE(self):
                self._route("DELETE")

        return Handler

    # ------------------------------------------------------------------
    def start(self) -> tuple[str, int]:
        self._httpd = ThreadingHTTPServer((self.host, self.port), self._make_handler())
        self._httpd.daemon_threads = True
        self.port = self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="bridge", daemon=True)
        self._thread.start()
        return self.host, self.port

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
