#!/usr/bin/env python3
"""docsearch — local web UI over Spotlight for HTML runbooks.

Serves the search page (index.html, from this script's directory) at
http://127.0.0.1:8377 and runs the same query pipeline as the `docsearch`
zsh function: mdfind "kind:html <q>" scoped to $HOME, kept to paths
containing /docs/, minus the exclude pattern, newest first.

Runbook: ~/docs/doc-web-search.html
NOTE: INCLUDE/EXCLUDE below are duplicated in the docsearch() function in
~/.zshrc — if you tweak one, tweak the other.
"""
import json
import os
import re
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HOST = "127.0.0.1"
PORT = 8377
HOME = os.path.expanduser("~")
BASE_DIR = os.path.dirname(os.path.realpath(__file__))
INDEX = os.path.join(BASE_DIR, "index.html")

INCLUDE = re.compile(r"/docs/")
EXCLUDE = re.compile(r"node_modules|/Library/|/\.git/|/\.cache/|/\.npm/|/\.Trash/|site-packages")


def run_mdfind(query):
    if query:
        args = ["mdfind", "-onlyin", HOME, "kind:html " + query]
    else:
        args = ["mdfind", "-onlyin", HOME, 'kMDItemContentType == "public.html"']
    out = subprocess.run(args, capture_output=True, text=True, timeout=15).stdout
    results = []
    for path in out.splitlines():
        if not INCLUDE.search(path) or EXCLUDE.search(path):
            continue
        try:
            mtime = os.stat(path).st_mtime
        except OSError:
            continue
        results.append({"path": path, "mtime": int(mtime)})
    results.sort(key=lambda r: r["mtime"], reverse=True)
    return results


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
    server_version = "docsearch/1.0"

    def _send(self, code, body, ctype="text/plain; charset=utf-8"):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
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
            try:
                results = run_mdfind(q)
            except subprocess.TimeoutExpired:
                self._send(504, "mdfind timed out")
                return
            self._send(200, json.dumps(results), "application/json; charset=utf-8")

        elif url.path == "/view":
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
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    sys.stderr.write("docsearch serving on http://%s:%d\n" % (HOST, PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
