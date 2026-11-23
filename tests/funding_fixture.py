"""Loopback FCT013 qualification provider. Its receipts live for the lifetime of this process."""
import json
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from threading import Event, Lock, Thread
from uuid import uuid4


class Provider:
    def __init__(self):
        self.calls = []
        self.lookups = 0
        self.probes = 0
        self.receipts = {}
        self.mode = "ready"
        self.ready = True
        self.started = Event()
        self.lock = Lock()
        self.release = Event()
        provider = self

        class Handler(BaseHTTPRequestHandler):
            def handle(self):
                try:
                    super().handle()
                except ConnectionError:
                    pass  # The interruption case kills the controller after the request is accepted.

            def log_message(self, *_):
                pass

            def send(self, status, data):
                payload = json.dumps(data).encode()
                self.send_response(status)
                if status == 429 and provider.mode == "timed-quota":
                    self.send_header("Retry-After","600")
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                if self.path == "/availability":
                    provider.probes += 1
                    self.send(200, {"account":"pilot", "model":"fixture-owner", "authenticated":provider.ready,
                                    "quotaAvailable":provider.ready})
                else:
                    provider.lookups += 1
                    receipt = provider.receipts.get(self.path)
                    self.send(200 if receipt else 404, receipt or {})

            def do_POST(self):
                length = self.headers.get("Content-Length")
                if length:
                    raw = self.rfile.read(int(length))
                else:
                    chunks = []
                    while size := int(self.rfile.readline().strip(), 16):
                        chunks.append(self.rfile.read(size))
                        self.rfile.readline()
                    self.rfile.readline()
                    raw = b"".join(chunks)
                req = json.loads(raw)
                if self.path.startswith("/cancellations/"):
                    _, _, request_id, attempt = self.path.split("/")
                    path = f"/receipts/{request_id}/{attempt}"
                    with provider.lock:
                        terminal = provider.receipts.setdefault(path, {"requestId":request_id,"attempt":int(attempt),**req,
                            "status":"cancelled","final":True,"inputTokens":0,"outputTokens":0})
                    self.send(200, terminal if provider.mode != "cancel-lost" else {})
                    return
                assert self.path == "/owner/requests" and req["tools"] == [] and req["maxOutputTokens"] == 32
                assert "Authorization" not in self.headers and "Cookie" not in self.headers
                provider.calls.append(req)
                provider.started.set()
                if provider.mode == "delay":
                    assert provider.release.wait(8)
                key = {k:req[k] for k in ("requestId", "attempt", "model")}
                if provider.mode in ("quota", "timed-quota", "auth"):
                    self.send(401 if provider.mode == "auth" else 429, {**key, "account":"pilot", "executed":False})
                    return
                receipt = {**key, "account":req["account"], "responseId":str(uuid4()), "inputTokens":10,
                           "outputTokens":1000 if provider.mode == "overrun" else 4, "answer":"Inspect the retained verification receipt."}
                if provider.mode != "defer":
                    with provider.lock:
                        receipt = provider.receipts.setdefault(f"/receipts/{key['requestId']}/{key['attempt']}", receipt)
                if provider.mode in ("drop", "defer"):
                    self.close_connection = True
                    self.connection.shutdown(socket.SHUT_RDWR)
                    return
                if provider.mode == "foreign":
                    receipt = {**receipt, "requestId":str(uuid4())}
                self.send(200, receipt)

        self.http = ThreadingHTTPServer(("127.0.0.1",0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.thread = Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.release.set()
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()


if __name__ == "__main__":
    provider = Provider()
    try:
        print("Fixture endpoint " + provider.url, flush=True)
        Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        provider.close()
