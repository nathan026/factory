"""Synthetic Slack destination and signed message actions for local qualification."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from urllib.parse import urlencode
from urllib.request import Request, urlopen
import hashlib
import hmac
import json
import secrets
import time


class SlackFixture:
    def __init__(self):
        self.secret = secrets.token_hex(24)
        self.messages = []
        self.calls = 0
        self.mode = "ok"
        self.entered, self.release = Event(), Event()
        self.lock = Lock()
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def handle(self):
                try:
                    super().handle()
                except ConnectionError:
                    pass  # The crash case kills the controller before it can receive headers.

            def log_message(self, *args):
                pass

            def do_POST(self):
                assert self.path == "/api/chat.postMessage"
                assert not self.headers.get("Authorization") and not self.headers.get("Cookie")
                if self.headers.get("Transfer-Encoding") == "chunked":
                    parts = []
                    while size := int(self.rfile.readline().strip(), 16):
                        parts.append(self.rfile.read(size))
                        self.rfile.readline()
                    self.rfile.readline()
                    body = b"".join(parts)
                else:
                    body = self.rfile.read(int(self.headers["Content-Length"]))
                message = json.loads(body)
                with fixture.lock:
                    fixture.calls += 1
                    ts = f"1700000000.{fixture.calls:06d}"
                    mode = fixture.mode
                    if mode != "rate":
                        fixture.messages.append((ts, message))
                fixture.entered.set()
                if mode == "hold":
                    fixture.release.wait(10)
                if mode == "drop":
                    self.close_connection = True
                    return
                self.send_response(429 if mode == "rate" else 200)
                if mode == "rate":
                    self.send_header("Retry-After", "2")
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                try:
                    self.wfile.write(json.dumps({"ok": True, "channel": message["channel"], "ts": ts}).encode())
                except OSError:
                    pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.profile = {"project": "shipping/v1", "teamId": "TLOCAL", "channelId": "CLOCAL", "appId": "ALOCAL",
                        "decisionUsers": ["UOPERATOR"], "apiOrigin": f"http://127.0.0.1:{self.server.server_port}"}

    def close(self):
        self.release.set()
        self.server.shutdown()
        self.thread.join()
        self.server.server_close()

    def payload(self, task, value="approve"):
        ts, message = next((ts, msg) for ts, msg in self.messages if task in msg["text"])
        block = next(b for b in message["blocks"] if b["type"] == "actions")
        button = next(e for e in block["elements"] if e["value"] == value)
        return {"type": "block_actions", "api_app_id": "ALOCAL", "team": {"id": "TLOCAL"}, "user": {"id": "UOPERATOR"},
                "channel": {"id": "CLOCAL"}, "container": {"type": "message", "channel_id": "CLOCAL", "message_ts": ts},
                "message": {"app_id":"ALOCAL", "ts":ts, "blocks":message["blocks"]},
                "actions": [{**button, "block_id": block["block_id"]}]}

    def click(self, base, payload, timestamp=None, secret=None):
        raw = urlencode({"payload": json.dumps(payload)}).encode()
        stamp = str(int(time.time()) if timestamp is None else timestamp)
        signature = hmac.new((secret or self.secret).encode(), b"v0:" + stamp.encode() + b":" + raw, hashlib.sha256).hexdigest()
        req = Request(base + "/callbacks/slack", data=raw, headers={"Content-Type": "application/x-www-form-urlencoded",
                      "X-Slack-Signature": "v0=" + signature, "X-Slack-Request-Timestamp": stamp})
        with urlopen(req, timeout=5) as response:
            return response.status


if __name__ == "__main__":
    fixture = SlackFixture()
    try:
        print("Local fixture origin", fixture.profile["apiOrigin"], flush=True)
        shown = 0
        while True:
            with fixture.lock:
                new = fixture.messages[shown:]
                shown += len(new)
            for ts, message in new:
                print(ts, message["text"], flush=True)
            time.sleep(.25)
    except KeyboardInterrupt:
        pass
    finally:
        fixture.close()
