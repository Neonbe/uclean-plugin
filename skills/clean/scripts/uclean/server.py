"""Loopback-only UI. No remote assets, CORS, shell, or arbitrary file endpoints."""
import json
import secrets
import signal
import threading
import time
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

from .fs import Rejected

WEB = Path(__file__).parent.parent.parent / "assets"


class Session:
    def __init__(self, engine):
        self.engine = engine
        self.token = secrets.token_urlsafe(32)
        self.started = time.monotonic()
        self.lock = threading.Lock()
        self.busy = False
        self.result = None
        self.error = None
        self.progress = {}
        self.plan = None
        self.scan = None

    def snapshot(self):
        with self.lock:
            return dict(busy=self.busy, result=self.result, error=self.error, progress=self.progress)

    def action(self, request):
        if request.get("action") == "cancel":
            self.engine.cancel.set()
            return
        with self.lock:
            if self.busy:
                raise Rejected("operation_in_progress")
            action = request.get("action")
            if action not in ("scan", "plan", "apply", "restore"):
                raise Rejected("unknown_action")
            if action == "plan" and (not self.scan or request.get("scan_id") != self.scan["id"]):
                raise Rejected("scan_is_not_current")
            if action == "apply" and (not self.plan or request.get("plan_id") != self.plan["id"] or request.get("confirmation") != self.plan["confirmation"]):
                raise Rejected("plan_is_not_current")
            if action == "scan":
                self.plan = None
            self.engine.cancel.clear()
            self.busy, self.error, self.progress = True, None, {}

        def progress(value):
            with self.lock:
                self.progress = value

        def work():
            try:
                if action == "scan":
                    result = self.engine.scan(request.get("level", "quick"), request.get("target_free_bytes", 10 * 1024**3), progress=progress, reset_cancel=False)
                    self.scan = result
                elif action == "plan":
                    ids = request.get("items")
                    if not isinstance(ids, list) or not ids or len(ids) > 1000 or not all(isinstance(i, str) for i in ids):
                        raise Rejected("select_items_first")
                    result = self.engine.plan(request["scan_id"], ids, reset_cancel=False)
                    self.plan = result
                elif action == "apply":
                    result = self.engine.apply(request["plan_id"], request["confirmation"], progress, reset_cancel=False)
                    self.plan = None
                else:
                    # Restore only from the receipt currently displayed in this session.
                    if not self.result or self.result.get("kind") != "receipt" or self.result["id"] != request.get("receipt_id"):
                        raise Rejected("receipt_is_not_current")
                    self.engine.restore(request["receipt_id"], request["item_id"])
                    result = self.engine.verify(request["receipt_id"])
                with self.lock:
                    self.result = result
            except Exception as exc:
                if action == "restore":
                    try:
                        updated = self.engine.verify(request["receipt_id"])
                        with self.lock:
                            self.result = updated
                    except (OSError, Rejected, KeyError):
                        pass
                with self.lock:
                    self.error = str(exc) if isinstance(exc, Rejected) else type(exc).__name__
            finally:
                with self.lock:
                    self.busy = False
        threading.Thread(target=work, daemon=True, name="uclean-job").start()


def make_server(engine, port=0):
    session = Session(engine)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Session tokens, filenames and URLs must never enter HTTP logs.

        def reply(self, status, body, content_type="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; img-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'none'")
            self.end_headers()
            try:
                self.wfile.write(data)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def host_ok(self):
            return self.headers.get("Host") == self.server.origin.removeprefix("http://")

        def authorized(self):
            return (self.host_ok() and time.monotonic() - session.started < 4 * 3600 and
                    secrets.compare_digest(self.headers.get("X-UClean-Token", ""), session.token))

        def do_GET(self):
            if not self.host_ok():
                return self.reply(403, {"error": "invalid_host"})
            route = urlsplit(self.path).path
            if route == "/api/state":
                return self.reply(200, session.snapshot()) if self.authorized() else self.reply(403, {"error": "session_expired_or_missing"})
            files = {"/": ("index.html", "text/html; charset=utf-8"), "/app.js": ("app.js", "text/javascript"), "/style.css": ("style.css", "text/css")}
            if route not in files:
                return self.reply(404, {"error": "not_found"})
            filename, mime = files[route]
            return self.reply(200, (WEB / filename).read_bytes(), mime)

        def do_POST(self):
            if not self.authorized() or self.headers.get("Origin") != self.server.origin:
                return self.reply(403, {"error": "invalid_origin_or_session"})
            if self.path != "/api/action":
                return self.reply(404, {"error": "not_found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 65536 or self.headers.get("Content-Type") != "application/json":
                    raise ValueError("invalid_request")
                data = json.loads(self.rfile.read(length))
                if not isinstance(data, dict):
                    raise ValueError("invalid_request")
                session.action(data)
                self.reply(202, {"accepted": True})
            except (ValueError, Rejected) as exc:
                self.reply(409 if isinstance(exc, Rejected) else 400, {"error": str(exc)})

    server = ThreadingHTTPServer(("127.0.0.1", port), Handler)
    server.daemon_threads = True
    server.origin = "http://127.0.0.1:" + str(server.server_port)
    server.session = session
    return server


def serve(engine, port=0, open_browser=True):
    server = make_server(engine, port)
    address = server.origin + "/#" + server.session.token
    print(json.dumps({"local_url": address, "expires_in_hours": 4, "notice": "私有本机会话地址，请勿分享。关闭终端服务即可结束。"}), flush=True)
    if open_browser:
        webbrowser.open(address)
    def stop(*_):
        engine.cancel.set()
        threading.Thread(target=server.shutdown, daemon=True).start()
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        engine.cancel.set()
    finally:
        server.server_close()
