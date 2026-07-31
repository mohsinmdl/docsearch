"""Tests for the docsearch walk engine.  Run: python3 -m unittest discover -s test

No test framework or requirements file in this repo by design — unittest is
built in, and the server is stdlib-only. Each test builds a throwaway $HOME
under tmp and points the module's HOME at it, so nothing here touches the real
corpus. The module is loaded by path (the filename has a dash, so it isn't
importable normally) and never starts the HTTP server: everything under test
lives above the request handler.
"""
import importlib.util
import os
import shutil
import tempfile
import time
import unittest

SERVER = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                      "docsearch-server.py")


def load_server():
    spec = importlib.util.spec_from_file_location("docsearch_server", SERVER)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class WalkTestCase(unittest.TestCase):
    """Fresh module + fresh fake $HOME per test — the engine caches globally."""

    def setUp(self):
        self.ds = load_server()
        # realpath: on macOS tmp is a symlink, and view_allowed compares against
        # the real path.
        self.home = os.path.realpath(tempfile.mkdtemp(prefix="docsearch-test-"))
        self.addCleanup(shutil.rmtree, self.home, True)
        self.ds.HOME = self.home

    def write(self, rel, body="<html><body>hello</body></html>", mtime=None):
        path = os.path.join(self.home, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write(body)
        if mtime is not None:
            os.utime(path, (mtime, mtime))
        return path

    def rels(self, docs):
        return sorted(d["rel"] for d in docs)

    def scan(self):
        docs, _ = self.ds.scan_corpus()
        return docs

    def search(self, query):
        results, _ = self.ds.run_search(query)
        return results


# ── corpus: what the walk collects ──────────────────────────────────────────

class TestCorpus(WalkTestCase):

    def test_only_html_inside_a_docs_folder(self):
        self.write("docs/runbook.html")
        self.write("projects/app/docs/deploy.html")
        self.write("projects/app/docs/nested/deep.html")
        self.write("projects/app/README.html")          # not under docs/
        self.write("docs/notes.txt")                     # not html
        self.assertEqual(
            self.rels(self.scan()),
            ["docs/runbook.html", "projects/app/docs/deploy.html",
             "projects/app/docs/nested/deep.html"])

    def test_prunes_hidden_and_vendor_directories(self):
        self.write("docs/real.html")
        # The reason dot-dirs are pruned: worktrees and agent scratch dirs hold
        # stale duplicates of real runbooks, and Spotlight never indexed them.
        self.write(".claude/worktrees/wt1/docs/real.html")
        self.write("projects/app/.git-worktrees/x/docs/real.html")
        self.write("projects/app/node_modules/pkg/docs/vendor.html")
        self.write("Library/Caches/thing/docs/cache.html")
        self.write("projects/app/venv/lib/docs/venv.html")
        self.write("projects/app/__pycache__/docs/pyc.html")
        self.write("projects/app/.venv/lib/python3/site-packages/docs/dep.html")
        self.assertEqual(self.rels(self.scan()), ["docs/real.html"])

    def test_skips_hidden_files(self):
        self.write("docs/visible.html")
        self.write("docs/.hidden.html")
        self.assertEqual(self.rels(self.scan()), ["docs/visible.html"])

    def test_skips_htm_because_view_would_reject_it(self):
        # view_allowed() requires .html, so a .htm hit would 403 when clicked.
        self.write("docs/page.htm")
        self.assertEqual(self.scan(), [])
        self.assertFalse(self.ds.view_allowed(os.path.join(self.home, "docs/page.htm")))

    def test_newest_first(self):
        now = int(time.time())
        self.write("docs/old.html", mtime=now - 9000)
        self.write("docs/newest.html", mtime=now)
        self.write("docs/middle.html", mtime=now - 60)
        self.assertEqual([d["rel"] for d in self.scan()],
                         ["docs/newest.html", "docs/middle.html", "docs/old.html"])

    def test_unreadable_directories_are_reported_not_fatal(self):
        # This is how a TCC-blocked folder (~/Documents) surfaces: named in the
        # log, rather than quietly contributing no runbooks.
        self.write("docs/ok.html")
        blocked = os.path.join(self.home, "blocked")
        os.makedirs(os.path.join(blocked, "docs"))
        os.chmod(blocked, 0o000)
        self.addCleanup(os.chmod, blocked, 0o755)
        docs, unreadable = self.ds.scan_corpus()
        self.assertEqual(self.rels(docs), ["docs/ok.html"])
        self.assertEqual(unreadable, [blocked])

    def test_wire_shape_drops_internal_size_field(self):
        self.write("docs/a.html")
        self.assertIn("size", self.scan()[0])
        self.assertEqual(sorted(self.search("")[0]), ["mtime", "path", "rel"])


# ── query matching ──────────────────────────────────────────────────────────

class TestSearch(WalkTestCase):

    def test_empty_query_returns_whole_corpus(self):
        self.write("docs/a.html")
        self.write("docs/b.html")
        self.assertEqual(len(self.search("")), 2)

    def test_all_words_must_match(self):
        self.write("docs/mobile-app-upstream-upgrade.html")
        self.write("docs/mobile-app-nested-lists.html")
        self.assertEqual(len(self.search("mobile app")), 2)
        self.assertEqual(self.rels(self.search("mobile upstream")),
                         ["docs/mobile-app-upstream-upgrade.html"])
        self.assertEqual(self.search("mobile nonexistent"), [])

    def test_substring_matches_mid_token(self):
        # Build tags like "cc18" are why this is substring, not whole-word.
        self.write("docs/cc18-build-staging.html")
        self.assertEqual(len(self.search("cc18")), 1)
        self.assertEqual(len(self.search("staging")), 1)

    def test_case_insensitive(self):
        self.write("docs/Deploy.html", body="<p>Rollback Procedure</p>")
        self.assertEqual(len(self.search("DEPLOY")), 1)
        self.assertEqual(len(self.search("rollback procedure")), 1)

    def test_matches_body_text_not_just_filename(self):
        self.write("docs/a.html", body="<html><body><p>kickstart the daemon</p></body></html>")
        self.write("docs/b.html", body="<html><body><p>something else</p></body></html>")
        self.assertEqual(self.rels(self.search("kickstart")), ["docs/a.html"])

    def test_style_and_script_contents_are_not_searchable(self):
        # Runbooks embed their CSS, so "flex" would otherwise match every doc.
        self.write("docs/styled.html", body=(
            "<html><head><style>body { display: flex; grid-gap: 4px; }</style>"
            "<script>function alarm() { return 1; }</script></head>"
            "<body><p>deployment steps</p></body></html>"))
        self.assertEqual(self.search("flex"), [])
        self.assertEqual(self.search("alarm"), [])
        self.assertEqual(len(self.search("deployment")), 1)

    def test_entities_are_unescaped_before_matching(self):
        self.write("docs/a.html", body="<p>rock&amp;roll &lt;tag&gt;</p>")
        self.assertEqual(len(self.search("rock&roll")), 1)

    def test_tags_do_not_glue_words_together(self):
        self.write("docs/a.html", body="<p>alpha</p><p>beta</p>")
        self.assertEqual(self.search("alphabeta"), [])
        self.assertEqual(len(self.search("alpha beta")), 1)

    def test_path_words_match_but_home_prefix_does_not(self):
        # `rel` is matched, not the absolute path — otherwise the username and
        # "Users" would match every document.
        self.write("projects/rocketchat/docs/deploy.html")
        self.assertEqual(len(self.search("rocketchat")), 1)
        self.assertEqual(self.search(os.path.basename(self.home)), [])

    def test_results_keep_newest_first_order(self):
        now = int(time.time())
        self.write("docs/old-deploy.html", mtime=now - 9000)
        self.write("docs/new-deploy.html", mtime=now)
        self.assertEqual([d["rel"] for d in self.search("deploy")],
                         ["docs/new-deploy.html", "docs/old-deploy.html"])


# ── caching ─────────────────────────────────────────────────────────────────

class TestCaching(WalkTestCase):

    def test_corpus_is_reused_within_ttl(self):
        self.write("docs/a.html")
        scans = []
        real = self.ds.scan_corpus
        self.ds.scan_corpus = lambda: (scans.append(1), real())[1]
        self.search("")
        self.search("")
        self.assertEqual(len(scans), 1)

    def test_stale_corpus_triggers_a_refresh(self):
        self.write("docs/a.html")
        self.search("")
        self.ds.SCAN_TTL = 0
        self.write("docs/b.html")
        self.search("")            # serves stale, kicks off a background scan
        for _ in range(100):       # ... which lands shortly after
            if len(self.search("")) == 2:
                break
            time.sleep(0.02)
        self.assertEqual(len(self.search("")), 2)

    def test_text_cache_invalidated_when_file_changes(self):
        path = self.write("docs/a.html", body="<p>original</p>")
        self.assertEqual(len(self.search("original")), 1)
        stat = os.stat(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("<p>rewritten completely</p>")
        os.utime(path, (stat.st_mtime + 10, stat.st_mtime + 10))
        self.ds.SCAN_TTL = 0       # force the corpus to pick up the new mtime
        for _ in range(100):
            if self.search("rewritten"):
                break
            time.sleep(0.02)
        self.assertEqual(len(self.search("rewritten")), 1)
        self.assertEqual(self.search("original"), [])

    def test_text_cache_entries_dropped_for_files_that_disappear(self):
        path = self.write("docs/a.html", body="<p>doomed</p>")
        self.search("doomed")
        self.assertIn(path, self.ds._text_cache)
        os.remove(path)
        self.ds.SCAN_TTL = 0
        for _ in range(100):
            if not self.search(""):
                break
            time.sleep(0.02)
        self.assertEqual(self.search(""), [])
        self.assertNotIn(path, self.ds._text_cache)

    def test_file_deleted_after_scan_matches_nothing(self):
        path = self.write("docs/a.html", body="<p>ephemeral</p>")
        self.search("")            # corpus now holds a path that is about to go
        os.remove(path)
        self.assertEqual(self.search("ephemeral"), [])   # no traceback


# ── /view guard (unchanged by the engine swap, still worth pinning) ─────────

class TestViewAllowed(WalkTestCase):

    def test_accepts_a_real_corpus_file(self):
        path = self.write("docs/a.html")
        self.assertTrue(self.ds.view_allowed(path))

    def test_rejects_outside_home_and_outside_docs(self):
        self.assertFalse(self.ds.view_allowed("/etc/passwd"))
        self.assertFalse(self.ds.view_allowed(self.write("notes/a.html")))
        self.assertFalse(self.ds.view_allowed(
            self.write("projects/app/node_modules/docs/a.html")))

    def test_rejects_traversal_back_out_of_home(self):
        self.assertFalse(self.ds.view_allowed(
            os.path.join(self.home, "docs", "..", "..", "etc", "passwd")))


if __name__ == "__main__":
    unittest.main()
