"""Loopback GitHub REST fixture backed by a disposable bare Git repository."""
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock, Thread
from urllib.parse import parse_qs, unquote, urlsplit
import base64
import hashlib
import json
import os
import re
from uuid import uuid4
import socket
import subprocess
import tempfile


class GitHubFixture:
    repo = "fixture/checkout"
    repo_id = 723

    def __init__(self, directory, files):
        self.git_dir = Path(directory) / "remote.git"
        fresh = not self.git_dir.exists()
        subprocess.run(["git", "init", "-q", "--bare", str(self.git_dir)], check=True)
        self.lock = RLock()
        self.issues = {}
        self._pulls = []
        self.writes = []
        self.drop_pr = False
        self.drop_ref = False
        self.hide_pulls = False
        self.files = {p.name: self.blob(p.read_bytes().replace(b"\r\n", b"\n")) for p in Path(files).iterdir() if p.is_file()}
        tree = self.tree(self.files)
        self.base = self.git("commit-tree", tree, "-m", "Admitted fixture")
        if fresh:
            self.git("update-ref", "refs/heads/main", self.base)
            self.git("update-ref","refs/factory/journal",self.blob(uuid4().hex.encode()),"0"*40)
        else:
            self.base = self.git("rev-parse","refs/heads/main")
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def handle(self):
                try:
                    super().handle()
                except ConnectionResetError:
                    # The restart cases deliberately kill a controller with open keep-alive connections.
                    pass

            def log_message(self, *_):
                pass

            def handle_request(self):
                url = urlsplit(self.path)
                prefix = "/repos/" + fixture.repo
                path = unquote(url.path).removeprefix(prefix).strip("/")
                length = int(self.headers.get("Content-Length", 0))
                # JsonContent may choose HTTP/1.1 chunked transfer.
                if self.headers.get("Transfer-Encoding", "").lower() == "chunked":
                    chunks = bytearray()
                    while True:
                        size = int(self.rfile.readline().split(b";")[0].strip(), 16)
                        if not size:
                            self.rfile.readline()
                            break
                        chunks.extend(self.rfile.read(size))
                        self.rfile.readline()
                    raw = bytes(chunks)
                else:
                    raw = self.rfile.read(length)
                try:
                    with fixture.lock:
                        if not url.path.startswith(prefix) or self.headers.get("Authorization"):
                            code, obj = 403, {}
                        else:
                            code, obj = fixture.respond(self.command, path, parse_qs(url.query), json.loads(raw) if raw else {})
                        if code == 0:
                            self.close_connection = True
                            self.connection.shutdown(socket.SHUT_RDWR)
                            return
                        data = json.dumps(obj).encode()
                except Exception as err:
                    code, data = 500, json.dumps({"error": repr(err)}).encode()
                    fixture.error = repr(err)
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = handle_request

        self.error = None
        self.http = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.http.server_port}"
        self.thread = Thread(target=self.http.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.http.shutdown()
        self.http.server_close()
        self.thread.join()

    def git(self, *args, data=None, date="2026-01-01T00:00:00Z"):
        env = {**os.environ, "GIT_AUTHOR_NAME":"Factory fixture", "GIT_COMMITTER_NAME":"Factory fixture",
               "GIT_AUTHOR_EMAIL":"factory@users.noreply.github.com", "GIT_COMMITTER_EMAIL":"factory@users.noreply.github.com",
               "GIT_AUTHOR_DATE":date, "GIT_COMMITTER_DATE":date}
        return subprocess.run(["git", "--git-dir", str(self.git_dir), *args], input=data, capture_output=True,
                              env=env, check=True, timeout=10).stdout.decode().strip()

    def blob(self, content):
        return self.git("hash-object", "-w", "--stdin", data=content)

    def tree(self, files):
        return self.git("mktree", data="".join(f"100644 blob {sha}\t{name}\n" for name, sha in files.items()).encode())

    def entries(self, sha):
        return [{"mode": m, "type": t, "sha": oid, "path": name}
                for line in self.git("ls-tree", sha).splitlines()
                for m, t, oid, name in [line.split(None, 3)]]

    def add_issue(self, number):
        issue = {"id": 1000 + number, "number": number, "state":"open", "title":f"Boundary correction {number}",
                 "body":"Fix the exact free shipping threshold under the accepted decision.", "labels":[{"name":"bug"}],
                 "updated_at":datetime.now(timezone.utc).isoformat()}
        self.issues[number] = issue
        return {"number":number, "id":issue["id"], "scenario":"known-decision",
                "scopeHash":hashlib.sha256((issue["title"]+"\n"+issue["body"]).encode()).hexdigest()}

    @property
    def pulls(self):
        refs = self.git("for-each-ref","--format=%(objectname)","refs/factory/drafts").splitlines()
        return self._pulls + [json.loads(self.git("cat-file","blob",oid)) for oid in refs]

    def atomic_draft(self, method, task, obj):
        ref = "refs/factory/drafts/"+task
        oid = self.git("for-each-ref","--format=%(objectname)",ref)
        saved = json.loads(self.git("cat-file","blob",oid)) if oid else None
        if saved:
            number_ref = "refs/factory/numbers/"+str(saved["number"])
            if self.git("for-each-ref","--format=%(objectname)",number_ref) != oid:
                return 409, {}
        if method=="GET":
            return 200, {"format":"factory-git-draft/v1","pull":saved}
        branch = "factory/"+task
        pull = obj["pull"]
        head, base = obj["headSha"], obj["baseSha"]
        if (not all(re.fullmatch("[a-f0-9]{40}",sha) for sha in (head,base)) or
                pull["head"]!=branch or pull["base"]!="main" or pull["draft"] is not True or
                pull["maintainer_can_modify"] is not False or obj["repoId"]!=self.repo_id or
                "<!-- factory-publication:"+task+" -->" not in pull["body"].splitlines()):
            return 409, {}
        if saved:
            if saved["head"]["sha"]!=head: return 409, {}
            return 200, saved
        issue = self.issues.get(obj["issue"])
        if not issue or issue["id"]!=obj["issueId"] or issue["state"]!="open" or issue.get("pull_request"):
            return 409, {}
        labels = {label["name"].lower() for label in issue["labels"]}
        scope = hashlib.sha256((issue["title"]+"\n"+issue["body"]).encode()).hexdigest()
        if ("bug" not in labels or labels & {"hold","factory:hold","duplicate","superseded","factory:covered"}
                or scope!=obj["scopeHash"] or self.git("rev-parse",head+"^")!=base
                or any(p["head"]["ref"]==branch for p in self.pulls)):
            return 409, {}
        number = max([19]+[p["number"] for p in self.pulls])+1
        record = {"number":number,"state":"open","draft":True,"body":pull["body"],"title":pull["title"],
                  "html_url":f"{self.url}/{self.repo}/pull/{number}",
                  "head":{"ref":branch,"repo":{"id":self.repo_id},"sha":head},
                  "base":{"ref":"main","repo":{"id":self.repo_id},"sha":base}}
        blob = self.blob(json.dumps(record,sort_keys=True).encode())
        transaction = (f"verify refs/factory/journal {obj['journalOid']}\nverify refs/heads/main {base}\ncreate refs/heads/{branch} {head}\n"
                       f"create {ref} {blob}\ncreate refs/factory/numbers/{number} {blob}\n")
        try:
            self.git("update-ref","--stdin",data=transaction.encode())
        except subprocess.CalledProcessError:
            return 409, {}
        self.writes.append("atomic-draft")
        return 201, record

    def respond(self, method, path, query, obj):
        if path=="factory/journal" and method=="GET":
            return 200, {"format":"factory-git-draft/v1","journal":self.git("cat-file","blob","refs/factory/journal")}
        if match := re.fullmatch(r"factory/journals/([a-f0-9]{32})/drafts/([a-f0-9]{32})",path):
            if self.git("cat-file","blob","refs/factory/journal")!=match[1]:
                return 409, {}
            # Recheck the very same epoch inside the Git write transaction.
            return self.atomic_draft(method,match[2],{**(obj or {}),"journalOid":self.git("hash-object","--stdin",data=match[1].encode())})
        if method == "GET":
            if not path:
                return 200, {"id":self.repo_id, "full_name":self.repo}
            if path.startswith("issues/"):
                issue = self.issues.get(int(path.split("/")[1]))
                return (200, issue) if issue else (404, {})
            if path.startswith("git/ref/heads/"):
                ref = path.removeprefix("git/ref/")
                try:
                    sha = self.git("rev-parse", "--verify", "refs/" + ref)
                except subprocess.CalledProcessError:
                    return 404, {}
                return 200, {"ref":"refs/"+ref, "object":{"type":"commit","sha":sha}}
            if path.startswith("git/commits/"):
                sha = path.split("/")[-1]
                return 200, {"sha":sha,"tree":{"sha":self.git("rev-parse", sha+"^{tree}")}}
            if path.startswith("git/blobs/"):
                sha = path.split("/")[-1]
                data = subprocess.check_output(["git", "--git-dir", str(self.git_dir), "cat-file", "blob", sha])
                return 200, {"sha":sha, "encoding":"base64", "content":base64.b64encode(data).decode()}
            if path.startswith("git/trees/"):
                sha = path.split("/")[-1]
                return 200, {"sha":sha, "truncated":False, "tree":self.entries(sha)}
            if path == "pulls":
                assert query["state"] == ["all"]
                return 200, [] if self.hide_pulls else [p for p in self.pulls if query["head"] == ["fixture:"+p["head"]["ref"]]
                                                         and query["base"] == [p["base"]["ref"]]]
        if method == "POST":
            self.writes.append(path)
            if path == "git/trees":
                change, = obj["tree"]
                assert change["type"] == "blob" and change["mode"] == "100644"
                with tempfile.TemporaryDirectory() as scratch:
                    env = dict(os.environ, GIT_INDEX_FILE=str(Path(scratch) / "index"))
                    def index(*args):
                        return subprocess.check_output(["git", "--git-dir", str(self.git_dir), *args], env=env).decode().strip()
                    index("read-tree", obj["base_tree"])
                    index("update-index", "--add", "--cacheinfo", "100644", self.blob(change["content"].encode()), change["path"])
                    return 201, {"sha":index("write-tree")}
            if path == "git/commits":
                parent, = obj["parents"]
                sha = self.git("commit-tree",obj["tree"],"-p",parent,"-m",obj["message"],date=obj["author"]["date"])
                return 201, {"sha":sha, "tree":{"sha":obj["tree"]}, "parents":[{"sha":parent}]}
            if path == "git/refs":
                try:
                    self.git("update-ref", obj["ref"], obj["sha"], "0"*40)
                except subprocess.CalledProcessError:
                    return 422, {}
                if self.drop_ref:
                    self.drop_ref = False
                    return 0, None
                return 201, {"ref":obj["ref"], "object":{"sha":obj["sha"]}}
            if path == "pulls":
                assert obj["draft"] is True and obj["base"] == "main" and obj["head"].startswith("factory/")
                assert obj["maintainer_can_modify"] is False
                number = len(self.pulls) + 20
                pr = {"number":number, "state":"open", "draft":True, "body":obj["body"], "title":obj["title"],
                      "html_url":f"{self.url}/{self.repo}/pull/{number}",
                      "head":{"ref":obj["head"], "repo":{"id":self.repo_id},"sha":self.git("rev-parse","refs/heads/"+obj["head"])},
                      "base":{"ref":obj["base"],"repo":{"id":self.repo_id}}}
                self._pulls.append(pr)
                if self.drop_pr:
                    self.drop_pr = False
                    return 0, None
                return 201, pr
        return 404, {}
