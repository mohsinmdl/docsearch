#!/usr/bin/env python3
"""docsearch — local web UI for the HTML runbooks scattered across $HOME.

Serves the search page (index.html, from this script's directory) at
http://127.0.0.1:8377. The corpus is every .html file that the whitelist/
blacklist rules admit (by default: anything inside a folder named "docs",
minus the usual vendor folders); results are newest first, and a query
matches when every word appears in the file's home-relative path or its
stripped body text.

Rules are managed from the page's settings modal and persisted to
settings.json next to this script (gitignored — machine state, not code).
When the file is absent the built-in defaults apply and nothing is written.

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
import tempfile
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
SETTINGS_PATH = os.path.join(BASE_DIR, "settings.json")

SCAN_TTL = 45  # seconds a scanned corpus is served before a refresh is kicked off

# <style>/<script> bodies are dropped whole before tags are stripped: these
# runbooks carry their CSS inline, so leaving it in would make "grid" or "flex"
# match every single document.
SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.S | re.I)
TAG_RE = re.compile(r"<[^>]+>")

# Reproduces the pre-settings behavior exactly: include /docs/ folders, skip
# the vendor/VCS folders the old EXCLUDE regex and prune list covered. The
# dot-named entries look redundant with the hardcoded hidden-dir rule below,
# but deleting THESE from the blacklist is what lets a whitelisted path inside
# one of them actually through.
DEFAULT_SETTINGS = {
    "version": 1,
    "whitelist": [{"type": "folder-name", "value": "docs"}],
    "blacklist": [{"type": "folder-name", "value": v} for v in (
        "node_modules", "Library", ".git", ".cache", ".npm", ".Trash",
        "site-packages", "venv", "__pycache__",
    )],
}


def normalize_entry(entry):
    """One raw rule (string or {type,value}) -> canonical {type,value}.

    Detection, after normalization: contains "/" -> path (absolute; a .html
    suffix makes it a single-file rule); ends in .html -> filename (exact
    basename); otherwise -> folder-name (exact path segment). Raises
    ValueError on anything unusable.
    """
    if isinstance(entry, dict):
        raw, typ = entry.get("value", ""), entry.get("type") or None
    else:
        raw, typ = entry, None
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("empty rule")
    raw = raw.strip()
    if any(c in raw for c in "*?[]"):
        raise ValueError("wildcards aren't supported — use a plain name or path: %s" % raw)
    value = raw.rstrip("/") or "/"
    # expand ~ against the module's HOME (not expanduser) so the tests'
    # patched HOME is honored; identical in production
    if value == "~":
        value = HOME
    elif value.startswith("~/"):
        value = HOME + value[1:]
    if typ is None:
        if "/" in value:
            typ = "path"
        elif value.lower().endswith(".html"):
            typ = "filename"
        else:
            typ = "folder-name"
    if typ == "path":
        if not os.path.isabs(value):
            value = os.path.join(HOME, value)  # "projects/app" means ~/projects/app
        value = os.path.normpath(value)
        if value == "/":
            raise ValueError("refusing a rule for the filesystem root")
    elif typ == "filename":
        if "/" in value or not value.lower().endswith(".html"):
            raise ValueError("a filename rule is a bare *.html name: %s" % raw)
    elif typ == "folder-name":
        if "/" in value:
            raise ValueError("a folder-name rule is a single name, not a path: %s" % raw)
    else:
        raise ValueError("unknown rule type: %s" % typ)
    return {"type": typ, "value": value}


def normalize_settings(doc):
    """Full settings doc -> (canonical doc, warnings). Raises ValueError."""
    if not isinstance(doc, dict):
        raise ValueError("settings must be an object")
    out = {"version": 1, "whitelist": [], "blacklist": []}
    warnings = []
    for side in ("whitelist", "blacklist"):
        entries = doc.get(side, [])
        if not isinstance(entries, list):
            raise ValueError("%s must be a list" % side)
        seen = set()
        for entry in entries:
            rule = normalize_entry(entry)
            key = (rule["type"], rule["value"].lower())
            if key in seen:
                continue
            seen.add(key)
            if rule["type"] == "path" and not os.path.exists(rule["value"]):
                warnings.append("%s doesn't exist right now (kept — it may be a volume that isn't mounted)" % rule["value"])
            out[side].append(rule)
    if not out["whitelist"]:
        warnings.append("whitelist is empty — no runbooks will be indexed")
    return out, warnings


class Rules(object):
    """A settings doc compiled for matching. Built once per swap, never
    mutated — scans and /view snapshot a reference and use it lock-free."""

    def __init__(self, doc):
        self.doc = doc
        self.wl_names, self.wl_files, self.wl_roots, self.wl_file_roots = self._side(doc["whitelist"])
        self.bl_names, self.bl_files, self.bl_roots, self.bl_file_roots = self._side(doc["blacklist"])

    @staticmethod
    def _side(entries):
        names, files, roots, file_roots = set(), set(), [], []
        for rule in entries:
            if rule["type"] == "folder-name":
                names.add(rule["value"].lower())
            elif rule["type"] == "filename":
                files.add(rule["value"].lower())
            else:
                # realpath at compile time: a symlinked rule path must match
                # the real paths the walk produces, or every click would 403.
                real = os.path.realpath(rule["value"])
                if rule["value"].lower().endswith(".html"):
                    file_roots.append(real)
                else:
                    roots.append(real)
        return names, files, roots, file_roots

    def allowed(self, real):
        """Would the scan collect this (real) path? One answer for the walk
        filter, /view and /open, in strict precedence order:
        blacklist > whitelisted path > hidden/outside-HOME > whitelist name."""
        base = os.path.basename(real)
        if base.startswith(".") or not base.lower().endswith(".html"):
            return False
        dir_segs = real.split(os.sep)[1:-1]
        # 1. blacklist always wins; the escape hatch is deleting the entry
        if base.lower() in self.bl_files:
            return False
        if any(s.lower() in self.bl_names for s in dir_segs):
            return False
        if real in self.bl_file_roots:
            return False
        if any(real.startswith(r + os.sep) for r in self.bl_roots):
            return False
        # 2. an explicitly whitelisted path overrides everything below,
        #    including the hidden-dir rule and HOME containment
        if real in self.wl_file_roots:
            return True
        if any(real.startswith(r + os.sep) for r in self.wl_roots):
            return True
        # 3. hardcoded: outside HOME, or behind a hidden directory. The
        #    hidden-dir rule is what keeps ~50 stale worktree/scratch
        #    duplicates out of the list, so it is not a managed setting.
        if not real.startswith(HOME + os.sep):
            return False
        home_segs = real[len(HOME) + 1:].split(os.sep)
        if any(s.startswith(".") for s in home_segs[:-1]):
            return False
        # 4. the whitelist proper: folder name or exact filename
        if any(s.lower() in self.wl_names for s in home_segs[:-1]):
            return True
        return base.lower() in self.wl_files

    def prune(self, name, path):
        """Should the walk skip this directory outright? Directory-level
        pruning is the 10s->1.8s invariant — never demote it to allowed()."""
        if name.startswith("."):
            return True
        if name.lower() in self.bl_names:
            return True
        return any(path == r or path.startswith(r + os.sep) for r in self.bl_roots)


_lock = threading.Lock()
_corpus = []       # list of docs, newest first
_scanned_at = 0.0  # time.time() of the last completed scan; 0 = never scanned
_scan_stamp = 0    # same, as int, for the X-Docsearch-Last-Scan header
_scanning = False  # a scan is in flight; don't start a second one
_text_cache = {}   # path -> ((mtime, size), lowercased body text)
_rules = None      # current Rules snapshot (set by load_settings/apply)
_rules_gen = 0     # bumped on every rules swap; stale scans check it


def load_settings():
    """Startup: settings.json if present and sane, else in-memory defaults.
    Never writes the file — only a POST does that."""
    global _rules
    doc = DEFAULT_SETTINGS
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            doc, _ = normalize_settings(json.load(f))
    except FileNotFoundError:
        pass
    except (OSError, ValueError) as exc:
        sys.stderr.write("settings.json unusable (%s) — using defaults\n" % exc)
        doc = DEFAULT_SETTINGS
    _rules = Rules(doc)


def apply_settings(doc):
    """Persist + swap in new rules. Returns the compiled Rules.

    The gen bump is what keeps an in-flight stale-while-revalidate scan
    (walking under the OLD rules) from committing after our rescan and
    resurrecting the old corpus for a TTL."""
    global _rules, _rules_gen
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(SETTINGS_PATH) or ".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(doc, f, indent=2)
            f.write("\n")
        os.replace(tmp, SETTINGS_PATH)
    except OSError:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    with _lock:
        _rules = Rules(doc)
        _rules_gen += 1
        return _rules


def scan_corpus(rules):
    """Walk $HOME plus any whitelisted path roots. Returns (docs newest-first,
    unreadable dirs).

    Roots are collected into a dict keyed on path: a whitelisted root that
    also sits inside HOME's walk would otherwise appear twice. Docs carry
    `size` alongside the wire fields; it is half of the text-cache key and is
    stripped before the JSON response goes out.
    """
    unreadable = []
    found = {}

    def collect(path, st):
        found[path] = {
            "path": path,
            # `rel` labels folder groups home-relative (~/projects/foo/docs);
            # for an opt-in root outside HOME the absolute path is the honest
            # label — never a ../ chain, which the page would render as "~/..".
            "rel": os.path.relpath(path, HOME) if path.startswith(HOME + os.sep) else path,
            "mtime": int(st.st_mtime),
            "size": st.st_size,
        }

    def walk(top):
        for root, dirs, files in os.walk(top, onerror=lambda e: unreadable.append(e.filename)):
            dirs[:] = [d for d in dirs if not rules.prune(d, os.path.join(root, d))]
            for name in files:
                path = os.path.join(root, name)
                if path in found or not rules.allowed(path):
                    continue
                try:
                    st = os.stat(path)
                except OSError:
                    continue  # vanished or unreadable between walk and stat
                collect(path, st)

    walk(HOME)
    for top in rules.wl_roots:
        # Always walked in their own pass: the HOME walk may not have reached
        # this root at all (hidden parent, blacklisted parent, outside HOME),
        # and when it did, found{} dedupes. Roots are small; the cost is noise.
        walk(top)
    for path in rules.wl_file_roots:
        if path in found or not rules.allowed(path):
            continue
        try:
            st = os.stat(path)
        except OSError:
            continue
        collect(path, st)

    docs = sorted(found.values(), key=lambda d: d["mtime"], reverse=True)
    return docs, unreadable


def _rescan():
    """Walk outside the lock, swap the result in under it — unless the rules
    changed mid-walk, in which case this result describes a corpus nobody
    asked for anymore and is discarded."""
    global _corpus, _scanned_at, _scan_stamp, _scanning
    with _lock:
        rules, gen = _rules, _rules_gen
    try:
        docs, unreadable = scan_corpus(rules)
    except Exception as exc:  # never leave _scanning stuck on
        with _lock:
            _scanning = False
        sys.stderr.write("scan failed: %r\n" % (exc,))
        return
    now = time.time()
    with _lock:
        if gen != _rules_gen:
            _scanning = False
            sys.stderr.write("scan: discarded (settings changed mid-walk)\n")
            return
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
        shown = sorted(set(os.path.relpath(p, HOME).split(os.sep)[0]
                           if p and p.startswith(HOME + os.sep) else str(p)
                           for p in unreadable))
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
        cold = not _corpus and not _scanned_at
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
    """The /view and /open guard: exactly the files the scan would collect."""
    with _lock:
        rules = _rules
    return rules.allowed(os.path.realpath(path))


class Handler(BaseHTTPRequestHandler):
    server_version = "docsearch/2.1"

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

    def _send_json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

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

        elif url.path == "/api/settings":
            with _lock:
                doc = _rules.doc
            self._send_json(200, doc)

        elif url.path == "/open":
            # Opens the doc via macOS `open` so the browser lands on the plain
            # file:// URL — file:// links straight from this http page would be
            # blocked by the browser. Same guard set as /view.
            p = params.get("p", [""])[0]
            if not p or not view_allowed(p):
                self._send(403, "excluded by current settings")
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
                self._send(403, "excluded by current settings")
                return
            try:
                with open(os.path.realpath(p), "rb") as f:
                    self._send(200, f.read(), "text/html; charset=utf-8")
            except OSError as exc:
                self._send(404, "not found: %s" % exc)

        else:
            self._send(404, "not found")

    def do_POST(self):
        url = urlparse(self.path)
        if url.path != "/api/settings":
            self._send(404, "not found")
            return
        try:
            length = int(self.headers.get("Content-Length", 0))
        except ValueError:
            length = 0
        if not 0 < length <= 65536:
            self._send_json(400, {"ok": False, "error": "body required (max 64KB)"})
            return
        try:
            doc, warnings = normalize_settings(json.loads(self.rfile.read(length)))
        except (ValueError, UnicodeDecodeError) as exc:
            self._send_json(400, {"ok": False, "error": str(exc)})
            return
        try:
            apply_settings(doc)
        except OSError as exc:
            self._send_json(500, {"ok": False, "error": "could not save settings: %s" % exc})
            return
        # Synchronous rescan: the modal shows "rescanning…" and repaints the
        # result list from this response, so answer only when the new corpus
        # is really in place. Our gen is current, so this commit always wins.
        _rescan()
        with _lock:
            count = len(_corpus)
        self._send_json(200, {"ok": True, "settings": doc, "docs": count, "warnings": warnings})

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.address_string(), fmt % args))


def enable_dataless_reads():
    """iCloud-evicted files (e.g. under a Documents synced to iCloud) keep
    their stat metadata but hold no local data; for a launchd daemon, open()
    on one fails with EDEADLK ("Resource deadlock avoided") instead of
    triggering a download. Opt in to on-demand materialization so whitelisted
    iCloud folders actually open and full-text search. Constants from xnu
    sys/resource.h: IOPOL_TYPE_VFS_MATERIALIZE_DATALESS_FILES=3,
    IOPOL_SCOPE_PROCESS=0, IOPOL_MATERIALIZE_DATALESS_FILES_ON=2."""
    try:
        import ctypes
        ctypes.CDLL(None).setiopolicy_np(3, 0, 2)
    except Exception as exc:
        sys.stderr.write("dataless-file opt-in failed (%r) — iCloud-evicted files will 404\n" % (exc,))


load_settings()

if __name__ == "__main__":
    enable_dataless_reads()
    # Scan before accepting connections so the first page load is instant.
    _rescan()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("docsearch serving on http://%s:%d\n" % (HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
