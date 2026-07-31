#!/usr/bin/env python3
"""docsearch — local web UI for the HTML runbooks scattered across $HOME.

Serves the search page (index.html, from this script's directory) at
http://127.0.0.1:8377. The corpus is every .html file under $HOME that sits
inside a folder named "docs", minus the exclude pattern; results are newest
first, and a query matches when every word appears in the file's home-relative
path or its stripped body text.

This used to shell out to Spotlight (`mdfind -onlyin ~ "kind:html <q>"`). In
July 2026 the Mac's Spotlight index went read-only, new files silently stopped
being indexed, and the tool started hiding fresh runbooks. It now walks the
filesystem itself and owns its own index, so a wedged Spotlight can't rot it
again. See docs/spotlight-to-walk-migration.html.

Runbook: ~/docs/doc-web-search.html
"""
import html
import json
import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs, unquote

HOST = "127.0.0.1"
PORT = 8377
HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
INDEX = os.path.join(BASE_DIR, "index.html")
SCRIPT = os.path.join(BASE_DIR, "docsearch.js")

INCLUDE = re.compile(r"/docs/")
EXCLUDE = re.compile(r"node_modules|/Library/|/\.git/|/\.cache/|/\.npm/|/\.Trash/|site-packages")

# Directory names the walk never descends into. Hidden directories (any name
# starting with ".") are pruned as well — not just for speed, but for parity:
# Spotlight never indexed them, and descending turns up ~50 stale duplicate
# runbooks living in git worktrees and agent scratch dirs. Pruning the dot-dirs
# takes the walk from ~118k dirs / 10.4s down to ~17k dirs / 1.8s.
PRUNE_DIRS = frozenset(("node_modules", "Library", "site-packages", "venv", "__pycache__"))

SCAN_TTL = 45  # seconds a scanned corpus is served before a refresh is kicked off

# <style>/<script> bodies are dropped whole before tags are stripped: these
# runbooks carry their CSS inline, so leaving it in would make "grid" or "flex"
# match every single document.
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")

_lock = threading.Lock()
_corpus = []       # list of docs, newest first
_scanned_at = 0.0  # time.time() of the last completed scan; 0 = never scanned
_scan_stamp = 0    # same, as int, for the X-Docsearch-Last-Scan header
_scanning = False  # a scan is in flight; don't start a second one
_text_cache = {}   # path -> ((mtime, size), lowercased body text)


def scan_corpus():
    """Walk $HOME for runbooks. Returns (docs newest-first, unreadable dirs).

    Docs carry `size` alongside the wire fields; it is half of the text-cache
    key and is stripped before the JSON response goes out.
    """
    unreadable = []
    docs = []
    for root, dirs, files in os.walk(HOME, onerror=lambda e: unreadable.append(e.filename)):
        dirs[:] = [d for d in dirs if not d.startswith(".") and d not in PRUNE_DIRS]
        for name in files:
            # .html only: view_allowed() below refuses anything else, so
            # collecting .htm would produce hits that 403 when clicked.
            if name.startswith(".") or not name.lower().endswith(".html"):
                continue
            path = os.path.join(root, name)
            # Pruning already covers most of this, but the regexes stay
            # authoritative — they're the same guard /view and /open apply.
            if not INCLUDE.search(path) or EXCLUDE.search(path):
                continue
            try:
                st = os.stat(path)
            except OSError:
                continue  # vanished or unreadable between walk and stat
            docs.append({
                "path": path,
                # `rel` lets the page label folder groups home-relative
                # (~/projects/foo/docs) instead of printing an absolute path.
                "rel": os.path.relpath(path, HOME),
                "mtime": int(st.st_mtime),
                "size": st.st_size,
            })
    docs.sort(key=lambda d: d["mtime"], reverse=True)
    return docs, unreadable


def _rescan():
    """Walk outside the lock, swap the result in under it."""
    global _corpus, _scanned_at, _scan_stamp, _scanning
    try:
        docs, unreadable = scan_corpus()
    except Exception as exc:  # never leave _scanning stuck on
        with _lock:
            _scanning = False
        sys.stderr.write("scan failed: %r\n" % (exc,))
        return
    now = time.time()
    with _lock:
        _corpus = docs
        _scanned_at = now
        _scan_stamp = int(now)
        live = set(d["path"] for d in docs)
        for gone in [p for p in _text_cache if p not in live]:
            del _text_cache[gone]
        _scanning = False
    # A folder macOS won't let this daemon read (TCC: ~/Documents, ~/Desktop)
    # is named here rather than silently going missing from the results.
    note = ""
    if unreadable:
        shown = sorted(set(os.path.relpath(p, HOME).split(os.sep)[0] for p in unreadable))
        note = ", unreadable: %s" % ", ".join(shown[:8])
    sys.stderr.write("scan: %d docs%s\n" % (len(docs), note))


def get_corpus():
    """Corpus + last-scan stamp, refreshed in the background when stale.

    Stale-while-revalidate: once there is a corpus to serve, a stale one goes
    out immediately and the ~2s walk runs on a worker thread, so no request
    ever waits on it. Only a cold start scans inline, and startup does that
    before the server accepts connections.
    """
    global _scanning
    with _lock:
        cold = not _corpus
        mine = (time.time() - _scanned_at) >= SCAN_TTL and not _scanning
        if mine:
            _scanning = True
        corpus, stamp = _corpus, _scan_stamp

    if mine:
        if cold:
            _rescan()  # nothing to serve yet — take the hit
            with _lock:
                return _corpus, _scan_stamp
        threading.Thread(target=_rescan, daemon=True).start()
    return corpus, stamp


def text_of(doc):
    """Lowercased body text, cached until the file's (mtime, size) changes."""
    path = doc["path"]
    key = (doc["mtime"], doc["size"])
    with _lock:
        hit = _text_cache.get(path)
    if hit and hit[0] == key:
        return hit[1]
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            raw = f.read()
    except OSError:
        with _lock:
            _text_cache.pop(path, None)
        return ""  # deleted since the scan — matches nothing
    body = html.unescape(TAG_RE.sub(" ", SCRIPT_STYLE_RE.sub(" ", raw))).lower()
    with _lock:
        _text_cache[path] = (key, body)
    return body


def run_search(query):
    """Docs matching every word of `query`, newest first, plus the scan stamp.

    Matching is case-insensitive substring against the home-relative path and
    the body text. Substring rather than whole-word because half these
    runbooks are named for build tags like "cc18"; `rel` rather than the
    absolute path so "Users" or the username don't match everything.
    """
    corpus, stamp = get_corpus()
    words = query.lower().split()
    if not words:
        return [wire(d) for d in corpus], stamp
    hits = []
    for d in corpus:
        rel = d["rel"].lower()
        # Filename-fragment queries are the common case, and answering them
        # from `rel` alone means never opening the file.
        if not all(w in rel for w in words):
            haystack = rel + " " + text_of(d)
            if not all(w in haystack for w in words):
                continue
        hits.append(wire(d))
    return hits, stamp


def wire(doc):
    """Response shape. The page reads only these three fields."""
    return {"path": doc["path"], "rel": doc["rel"], "mtime": doc["mtime"]}


def view_allowed(path):
    """The /view guard: only files the search itself could return."""
    real = os.path.realpath(path)
    return (
        real.startswith(HOME + os.sep)
        and real.endswith(".html")
        and INCLUDE.search(real)
        and not EXCLUDE.search(real)
    )


class Handler(BaseHTTPRequestHandler):
    server_version = "docsearch/2.0"

    def _send(self, code, body, ctype="text/plain; charset=utf-8", headers=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        url = urlparse(self.path)
        params = parse_qs(url.query)

        if url.path == "/":
            try:
                with open(INDEX, "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(500, "index.html missing next to server script")

        elif url.path == "/search":
            q = params.get("q", [""])[0].strip()
            results, stamp = run_search(q)
            # The page requires a bare array, so anything worth knowing about
            # the engine goes in headers where curl -i and devtools can see it.
            self._send(200, json.dumps(results), "application/json; charset=utf-8",
                       {"X-Docsearch-Engine": "walk", "X-Docsearch-Last-Scan": str(stamp)})

        elif url.path == "/open":
            # Opens the doc via macOS `open` so the browser lands on the plain
            # file:// URL — file:// links straight from this http page would be
            # blocked by the browser. Same guard set as /view.
            p = params.get("p", [""])[0]
            if not p or not view_allowed(p):
                self._send(403, "forbidden")
                return
            try:
                subprocess.run(["open", os.path.realpath(p)], timeout=10, check=True)
            except (subprocess.SubprocessError, OSError):
                self._send(500, "open failed")
                return
            self._send(200, "ok")

        elif url.path == "/docsearch.js":
            try:
                with open(SCRIPT, "rb") as f:
                    self._send(200, f.read(), "application/javascript; charset=utf-8")
            except OSError:
                self._send(500, "docsearch.js missing next to server script")

        elif url.path == "/view" or url.path.startswith("/view/"):
            # Two forms. Path form (/view/Users/me/docs/x.html) is what the list
            # links to: a relative href inside the doc then resolves against the
            # doc's own directory instead of the server root. Query form
            # (/view?p=...) is kept for anything already pointing at it.
            if url.path.startswith("/view/"):
                p = unquote(url.path[len("/view"):])
            else:
                p = params.get("p", [""])[0]
            if not p or not view_allowed(p):
                self._send(403, "forbidden")
                return
            try:
                with open(os.path.realpath(p), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError:
                self._send(404, "not found")

        else:
            self._send(404, "not found")

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


if __name__ == "__main__":
    # Scan before accepting connections so the first page load is instant.
    _rescan()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("docsearch serving on http://%s:%d\n" % (HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
