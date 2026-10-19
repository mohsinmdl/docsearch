"""Tests for the docsearch walk engine.  Run: python3 -m unittest discover -s test

No test framework or requirements file in this repo by design — unittest is
built in, and the server is stdlib-only. Each test builds a throwaway $HOME
under tmp and points the module's HOME at it, so nothing here touches the real
corpus. The module is loaded by path (the filename has a dash, so it isn't
importable normally) and never starts the HTTP server: everything under test
lives above the request handler.
"""
import importlib.util
import json
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
        # Hermetic settings: point the file into the fake home (absent =
        # defaults) and recompile the rules AFTER the HOME patch.
        self.ds.SETTINGS_PATH = os.path.join(self.home, "settings.json")
        self.ds.load_settings()

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
        docs, _ = self.ds.scan_corpus(self.ds._rules)
        return docs

    def search(self, query, fresh=False):
        results, _, _ = self.ds.run_search(query, fresh=fresh)
        return results

    def scanning(self, query=""):
        """The 'a background walk is still running' flag from a search."""
        return self.ds.run_search(query)[2]

    def use_settings(self, whitelist=None, blacklist=None):
        """Apply a rules doc (raw entries — server-side detection applies)."""
        doc, warnings = self.ds.normalize_settings({
            "whitelist": whitelist if whitelist is not None else ["docs"],
            "blacklist": blacklist if blacklist is not None else [],
        })
        self.ds.apply_settings(doc)
        return warnings


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
        docs, unreadable = self.ds.scan_corpus(self.ds._rules)
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
        self.ds.scan_corpus = lambda rules: (scans.append(1), real(rules))[1]
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

    def test_forced_rescan_sees_a_file_added_inside_the_ttl(self):
        # The Re-sync link. Without it a file copied in during the TTL is
        # invisible: the cached corpus is served and no walk is even started.
        self.write("docs/a.html")
        self.assertEqual(len(self.search("")), 1)
        self.write("docs/b.html")
        self.assertEqual(len(self.search("")), 1, "still inside the TTL — cached corpus")
        self.assertEqual(len(self.search("", fresh=True)), 2)
        self.assertEqual(len(self.search("")), 2, "and that fresh corpus is now what's cached")

    def test_scanning_flag_marks_a_stale_response_as_provisional(self):
        # What the page's auto catch-up listens to: "this body is the old
        # corpus, a newer one is one request away".
        self.write("docs/a.html")
        self.search("")
        self.assertFalse(self.scanning(), "corpus is fresh — nothing in flight")
        self.ds.SCAN_TTL = 0
        self.assertTrue(self.scanning(), "stale response kicked off a background walk")
        self.ds.SCAN_TTL = 45
        for _ in range(100):
            if not self.scanning():
                break
            time.sleep(0.02)
        self.assertFalse(self.scanning(), "flag clears once that walk lands")

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


# ── Markdown runbooks (runbook skill 2.0+, rendered by MD Pretty Viewer) ────

MD_RUNBOOK = """---
runbook: true
---
# Roll out runbook template v2 [AWAITING REVIEW]

> **Related:** `~/docs/other.html` \\
> **Context:** The template grew too big. \\
> **Purpose:** Plan of record for the rollout. \\
> **Scope:** Template only. \\
> **Created:** 2026-10-01 \\
> **Updated:** 2026-10-02 — items 1–2 done. 2026-10-05 — item 3 blocked on DNS.

[HIGH] can lose work · [LOW] easily redone

## Summary

| # | Item | Risk | Status |
|---|---|---|---|
| 1 | [Backups](#item-1) | [HIGH] | [DONE] |

## Items

### 1. Backups [HIGH] [DONE]

**What's involved:** copy things.

*Completed: 2026-10-02 — copied.*

### 2. Write the template [CORRECTED 2026-10-03] [MED] [DONE]

```
### 9. Not an item [DONE]
```

### 3. Point DNS [LOW] [BLOCKED — waiting on DNS]

### 4. Verify `[DONE]` stays in code [LOW] [PENDING]

## Rollback

### Not an item either [DONE]
"""


class TestMarkdownRunbooks(WalkTestCase):

    def meta(self, text):
        return self.ds.md_runbook_meta(text)

    def test_md_runbooks_are_collected_next_to_html(self):
        self.write("docs/old.html")
        self.write("projects/app/docs/runbooks/new.md", body=MD_RUNBOOK)
        self.assertEqual(self.rels(self.scan()),
                         ["docs/old.html", "projects/app/docs/runbooks/new.md"])

    def test_plain_md_without_frontmatter_is_ignored(self):
        # READMEs, notes and skill references live in docs/ folders too.
        plain = self.write("docs/README.md", body="# Readme [DONE]\n\nrunbook: true\n")
        self.write("docs/other.md", body="---\ntitle: notes\n---\n# Notes\n")
        self.write("docs/off.md", body="---\nrunbook: false\n---\n# Off\n")
        self.assertEqual(self.scan(), [])
        self.assertFalse(self.ds.view_allowed(plain))

    def test_frontmatter_matches_the_viewers_rules(self):
        for head in ("---\nrunbook: true\n---\n", "﻿---\nrunbook: true\n---\n",
                     "---\r\nRunbook: \"yes\"\r\n---\r\n", "---\nowner: me\nrunbook: TRUE\n---\n"):
            self.assertIsNotNone(self.meta(head + "# T\n"), repr(head))
        for head in ("\n---\nrunbook: true\n---\n",   # must be the very first line
                     "---\nrunbook: true\n",           # never closed
                     "---\nrunbooks: true\n---\n"):
            self.assertIsNone(self.meta(head + "# T\n"), repr(head))

    def test_title_status_and_dates(self):
        m = self.meta(MD_RUNBOOK)
        self.assertEqual(m["title"], "Roll out runbook template v2")
        self.assertEqual(m["status"], "AWAITING REVIEW")
        self.assertEqual(m["state"], "awaiting")
        self.assertEqual(m["created"], "2026-10-01")
        self.assertEqual(m["updated"], "2026-10-05")
        self.assertEqual(m["updated_note"], "item 3 blocked on DNS.")

    def test_item_progress_follows_the_viewers_chip_rules(self):
        # 1 DONE; 2 DONE behind a [CORRECTED …] note chip; 3 BLOCKED; 4 PENDING
        # (a chip in backticks is code). Headings in fences and after the
        # next ## are not items.
        self.assertEqual(self.meta(MD_RUNBOOK)["progress"], {"done": 2, "total": 4})

    def test_item_without_a_heading_chip_takes_one_from_its_body(self):
        text = ("---\nrunbook: true\n---\n# T [IN PROGRESS]\n\n## Items\n\n"
                "### 1. First\n\n| Check | Status |\n|---|---|\n| a | [DONE] |\n\n"
                "### 2. Second [HIGH]\n\nnothing yet\n")
        self.assertEqual(self.meta(text)["progress"], {"done": 1, "total": 2})

    def test_missing_pieces_degrade_to_none(self):
        m = self.meta("---\nrunbook: true\n---\nno heading, no meta, no items\n")
        self.assertEqual((m["title"], m["status"], m["state"], m["created"],
                          m["updated"], m["progress"]),
                         (None, None, None, None, None, None))
        m = self.meta("---\nrunbook: true\n---\n# Fresh [PENDING]\n\n"
                      "> **Created:** 2026-10-18 \\\n> **Updated:** —\n")
        self.assertEqual((m["created"], m["updated"], m["updated_note"]),
                         ("2026-10-18", None, None))

    def test_forced_style_chip_sets_the_status(self):
        m = self.meta("---\nrunbook: true\n---\n# Ship it [done: SHIPPED 2026-10-20]\n")
        self.assertEqual((m["title"], m["status"], m["state"]),
                         ("Ship it", "SHIPPED 2026-10-20", "done"))

    def test_neutral_h1_chip_is_the_status_without_a_state(self):
        # Real runbooks end as [REFERENCE] or [READY]; the viewer shows that
        # chip, so the list does too, in the neutral style.
        m = self.meta("---\nrunbook: true\n---\n# Mods explained [HIGH] [REFERENCE]\n")
        self.assertEqual((m["title"], m["status"], m["state"]),
                         ("Mods explained", "REFERENCE", None))
        m = self.meta("---\nrunbook: true\n---\n# Mixed [REFERENCE] [DONE]\n")
        self.assertEqual((m["status"], m["state"]), ("DONE", "done"), "a lifecycle chip wins")

    def test_only_the_first_items_section_counts(self):
        text = ("---\nrunbook: true\n---\n# T\n\n## Items\n\n### 1. a [DONE]\n\n"
                "## Items (archive)\n\n### 2. b [DONE]\n")
        self.assertEqual(self.meta(text)["progress"], {"done": 1, "total": 1})

    def test_unknown_forced_style_is_not_a_chip(self):
        m = self.meta("---\nrunbook: true\n---\n# T [shipped: TODAY]\n")
        self.assertEqual(m["status"], None)

    # ext-v1.10 rules: what real runbooks write, without viewer-only syntax

    def test_a_chip_may_carry_a_lowercase_reason_after_a_dash(self):
        m = self.meta("---\nrunbook: true\n---\n# T [BLOCKED — waiting on DNS]\n")
        self.assertEqual((m["title"], m["status"], m["state"]),
                         ("T", "BLOCKED — waiting on DNS", "blocked"))
        m = self.meta("---\nrunbook: true\n---\n# T [DONE 2026-07-05 – logged in as x]\n")
        self.assertEqual(m["state"], "done", "en dash splits too")
        for h1 in ("# T [Done — x]", "# T [BLOCKED - plain hyphen]"):
            self.assertIsNone(self.meta("---\nrunbook: true\n---\n" + h1 + "\n")["status"], h1)

    def test_extra_status_words_have_a_state_and_ambiguous_ones_stay_neutral(self):
        cases = {"VERIFIED": "done", "PASS": "done", "DEPLOYED": "done", "COMPLETED": "done",
                 "ON HOLD": "blocked", "FAIL": "blocked", "STANDBY": "awaiting",
                 "INVESTIGATING": "in-progress", "TO DO": "pending",
                 "READY": None, "REFERENCE": None, "SKIPPED": None, "PASSWORD RESET": None}
        for word, state in cases.items():
            m = self.meta("---\nrunbook: true\n---\n# T [%s]\n" % word)
            self.assertEqual((m["status"], m["state"]), (word, state), word)

    def test_steps_and_phases_sections_hold_items_but_topics_does_not(self):
        for name, total in (("Steps", 2), ("Phases", 2), ("Item details", 2), ("Topics", None)):
            text = ("---\nrunbook: true\n---\n# T\n\n## %s\n\n"
                    "### 1. a [DONE]\n\n### 2. b [PENDING]\n" % name)
            p = self.meta(text)["progress"]
            self.assertEqual(p and p["total"], total, name)

    def test_unnumbered_h3_is_a_group_label_not_an_item(self):
        text = ("---\nrunbook: true\n---\n# T\n\n## Items\n\n"
                "### Phase 0 — Pre-flight\n\n### 1. a [LOW] [DONE]\n\n"
                "### Phase 1 — Delete\n\n| check | [DONE] |\n\n"   # outside any item
                "### 2. b [LOW]\n\n### A3. lettered [LOW] [PENDING]\n")
        # 1 done; 2 has no status chip and the [DONE] above it belongs to no
        # item, so it stays pending; A3. is an item id too.
        self.assertEqual(self.meta(text)["progress"], {"done": 1, "total": 3})

    def test_wire_carries_md_metadata_and_leaves_html_alone(self):
        self.write("docs/a.html")
        self.write("docs/b.md", body=MD_RUNBOOK)
        by = {d["rel"]: d for d in self.search("")}
        self.assertEqual(sorted(by["docs/a.html"]), ["mtime", "path", "rel"])
        md = by["docs/b.md"]
        self.assertEqual(md["kind"], "md")
        self.assertEqual(md["title"], "Roll out runbook template v2")
        self.assertEqual(md["status"], "AWAITING REVIEW")
        self.assertEqual(md["progress"], {"done": 2, "total": 4})
        self.assertNotIn("size", md)

    def test_md_body_text_is_searchable(self):
        self.write("docs/b.md", body=MD_RUNBOOK)
        self.write("docs/a.html", body="<p>nothing relevant</p>")
        self.assertEqual(self.rels(self.search("template grew")), ["docs/b.md"])
        self.assertEqual(self.rels(self.search("blocked dns")), ["docs/b.md"])
        self.assertEqual(self.search("runbook: true"), [], "frontmatter is not body text")

    def test_metadata_is_reparsed_when_the_file_changes(self):
        path = self.write("docs/b.md", body=MD_RUNBOOK)
        self.assertEqual(self.scan()[0]["md"]["status"], "AWAITING REVIEW")
        stat = os.stat(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write(MD_RUNBOOK.replace("[AWAITING REVIEW]", "[IN PROGRESS]"))
        os.utime(path, (stat.st_mtime + 10, stat.st_mtime + 10))
        self.assertEqual(self.scan()[0]["md"]["status"], "IN PROGRESS")

    def test_a_file_that_drops_its_frontmatter_leaves_the_list(self):
        path = self.write("docs/b.md", body=MD_RUNBOOK)
        self.assertEqual(len(self.scan()), 1)
        stat = os.stat(path)
        with open(path, "w", encoding="utf-8") as f:
            f.write("# just notes now\n")
        os.utime(path, (stat.st_mtime + 10, stat.st_mtime + 10))
        self.assertEqual(self.scan(), [])
        self.assertFalse(self.ds.view_allowed(path))

    def test_filename_rules_accept_md(self):
        self.assertEqual(self.ds.normalize_entry("plan.md"),
                         {"type": "filename", "value": "plan.md"})
        self.write("random/plan.md", body=MD_RUNBOOK)
        self.write("random/other.md", body=MD_RUNBOOK)
        self.use_settings(whitelist=["docs", "plan.md"])
        self.assertEqual(self.rels(self.scan()), ["random/plan.md"])


# ── whitelist/blacklist rules ───────────────────────────────────────────────

class TestRuleNormalization(WalkTestCase):

    def norm(self, raw):
        return self.ds.normalize_entry(raw)

    def test_auto_detection(self):
        self.assertEqual(self.norm("docs"), {"type": "folder-name", "value": "docs"})
        self.assertEqual(self.norm("docs/"),  # trailing slash is not a path
                         {"type": "folder-name", "value": "docs"})
        self.assertEqual(self.norm("report.html"), {"type": "filename", "value": "report.html"})
        self.assertEqual(self.norm("~/Documents/Codex"),
                         {"type": "path", "value": os.path.join(self.home, "Documents/Codex")})
        self.assertEqual(self.norm("projects/app"),  # bare-relative means HOME-relative
                         {"type": "path", "value": os.path.join(self.home, "projects/app")})
        self.assertEqual(self.norm("/Volumes/x/docs"), {"type": "path", "value": "/Volumes/x/docs"})

    def test_explicit_type_is_validated(self):
        self.assertEqual(self.norm({"type": "filename", "value": "a.html"}),
                         {"type": "filename", "value": "a.html"})
        for bad in ({"type": "filename", "value": "notes.txt"},
                    {"type": "folder-name", "value": "a/b"},
                    {"type": "nonsense", "value": "x"}):
            with self.assertRaises(ValueError):
                self.norm(bad)

    def test_rejects_unusable_input(self):
        for bad in ("", "   ", "*.html", "docs?", "/", None, 42):
            with self.assertRaises(ValueError):
                self.norm(bad if bad is not None else {"value": None})

    def test_settings_dedupe_is_case_insensitive(self):
        doc, _ = self.ds.normalize_settings(
            {"whitelist": ["docs", "Docs", "DOCS/"], "blacklist": []})
        self.assertEqual(len(doc["whitelist"]), 1)

    def test_empty_whitelist_warns(self):
        doc, warnings = self.ds.normalize_settings({"whitelist": [], "blacklist": []})
        self.assertEqual(doc["whitelist"], [])
        self.assertTrue(any("whitelist is empty" in w for w in warnings))

    def test_missing_path_warns_but_is_kept(self):
        doc, warnings = self.ds.normalize_settings(
            {"whitelist": ["docs", "~/no/such/place"], "blacklist": []})
        self.assertEqual(len(doc["whitelist"]), 2)
        self.assertTrue(any("doesn't exist" in w for w in warnings))


class TestRuleMatching(WalkTestCase):

    def test_blacklist_filename_beats_whitelist(self):
        self.write("docs/keep.html")
        kill = self.write("docs/kill.html")
        self.use_settings(blacklist=["kill.html"])
        self.assertEqual(self.rels(self.scan()), ["docs/keep.html"])
        self.assertFalse(self.ds.view_allowed(kill))

    def test_blacklist_path_beats_whitelist(self):
        self.write("docs/keep.html")
        self.write("old/docs/gone.html")
        self.use_settings(blacklist=["~/old"])
        self.assertEqual(self.rels(self.scan()), ["docs/keep.html"])

    def test_folder_name_matching_is_case_insensitive(self):
        self.write("stuff/NODE_MODULES/docs/dep.html")
        self.write("docs/mine.html")
        self.use_settings(blacklist=["node_modules"])
        self.assertEqual(self.rels(self.scan()), ["docs/mine.html"])

    def test_whitelisted_path_wins_over_hidden_dir_rule(self):
        # Hidden dirs are hardcoded-off (worktree duplicates), but an explicit
        # path rule is the user saying "this one, I mean it".
        hidden = self.write(".stash/docs/secret.html")
        self.write("docs/normal.html")
        self.assertEqual(self.rels(self.scan()), ["docs/normal.html"])
        self.use_settings(whitelist=["docs", "~/.stash/docs"])
        self.assertEqual(self.rels(self.scan()),
                         [".stash/docs/secret.html", "docs/normal.html"])
        self.assertTrue(self.ds.view_allowed(hidden))

    def test_whitelisted_folder_without_docs_in_path(self):
        self.write("Documents/New project/page.html")
        self.use_settings(whitelist=["docs", "~/Documents/New project"])
        self.assertIn("Documents/New project/page.html", self.rels(self.scan()))

    def test_whitelisted_root_inside_home_does_not_duplicate(self):
        self.write("projects/app/docs/a.html")
        self.use_settings(whitelist=["docs", "~/projects/app/docs"])
        self.assertEqual(self.rels(self.scan()), ["projects/app/docs/a.html"])

    def test_whitelisted_root_outside_home_gets_absolute_rel(self):
        outside = os.path.realpath(tempfile.mkdtemp(prefix="docsearch-vol-"))
        self.addCleanup(shutil.rmtree, outside, True)
        with open(os.path.join(outside, "guide.html"), "w") as f:
            f.write("<p>external</p>")
        self.use_settings(whitelist=["docs", outside])
        docs = self.scan()
        hit = [d for d in docs if d["path"].startswith(outside)]
        self.assertEqual(len(hit), 1)
        self.assertEqual(hit[0]["rel"], hit[0]["path"])  # absolute, never ../
        self.assertTrue(self.ds.view_allowed(hit[0]["path"]))

    def test_whitelisted_single_file_rule(self):
        target = self.write("notes/standalone.html")
        self.write("notes/other.html")
        self.use_settings(whitelist=["docs", "~/notes/standalone.html"])
        self.assertEqual(self.rels(self.scan()), ["notes/standalone.html"])
        self.assertTrue(self.ds.view_allowed(target))

    def test_whitelist_filename_matches_anywhere_under_home(self):
        self.write("random/deep/special.html")
        self.write("random/deep/plain.html")
        self.use_settings(whitelist=["docs", "special.html"])
        self.assertEqual(self.rels(self.scan()), ["random/deep/special.html"])

    def test_deleting_the_docs_rule_empties_the_default_corpus(self):
        self.write("docs/a.html")
        self.use_settings(whitelist=[])
        self.assertEqual(self.scan(), [])


class TestSettingsPersistence(WalkTestCase):

    def test_apply_then_load_roundtrip(self):
        self.use_settings(whitelist=["docs", "special.html"], blacklist=["node_modules"])
        saved = self.ds._rules.doc
        self.ds.load_settings()  # re-read from disk
        self.assertEqual(self.ds._rules.doc, saved)
        self.assertTrue(os.path.exists(self.ds.SETTINGS_PATH))

    def test_defaults_used_when_file_absent_and_never_written(self):
        self.assertFalse(os.path.exists(self.ds.SETTINGS_PATH))
        self.assertEqual(self.ds._rules.doc, self.ds.DEFAULT_SETTINGS)

    def test_corrupt_file_falls_back_to_defaults(self):
        with open(self.ds.SETTINGS_PATH, "w") as f:
            f.write("{not json")
        self.ds.load_settings()
        self.assertEqual(self.ds._rules.doc, self.ds.DEFAULT_SETTINGS)

    def test_stale_scan_is_discarded_after_a_rules_swap(self):
        # A stale-while-revalidate walk that started under the OLD rules must
        # not commit over the corpus a settings change just rebuilt.
        import threading as th
        ds = self.ds
        self.write("docs/a.html")
        real_scan = ds.scan_corpus
        started, release = th.Event(), th.Event()

        def slow_scan(rules):
            started.set()
            release.wait(5)
            return real_scan(rules)

        ds.scan_corpus = slow_scan
        worker = th.Thread(target=ds._rescan)
        worker.start()
        self.assertTrue(started.wait(5))
        ds.scan_corpus = real_scan
        self.use_settings(whitelist=[])   # bumps the generation
        ds._rescan()                       # commits: empty corpus
        release.set()
        worker.join(5)
        self.assertEqual(ds._corpus, [])   # stale 1-doc result was discarded

    def test_a_late_finishing_scan_does_not_overwrite_a_newer_one(self):
        # The Re-sync race: a background walk that started before the new file
        # existed can finish after the forced walk. Without the start-order
        # ticket it would overwrite the fresh corpus *and* reset _scanned_at,
        # resurrecting for another TTL exactly the staleness Re-sync exists to
        # end. No rules change here — the ordering alone has to catch it.
        import threading as th
        ds = self.ds
        self.write("docs/a.html")
        real_scan = ds.scan_corpus
        started, release = th.Event(), th.Event()

        def slow_scan(rules):
            out = real_scan(rules)   # walk NOW, while only a.html exists...
            started.set()
            release.wait(5)          # ...but don't get to commit until later
            return out

        ds.scan_corpus = slow_scan
        worker = th.Thread(target=ds._rescan)   # ticket 1: one doc
        worker.start()
        self.assertTrue(started.wait(5))
        ds.scan_corpus = real_scan
        self.write("docs/b.html")
        ds._rescan()                            # ticket 2: two docs, commits
        self.assertEqual(len(ds._corpus), 2)
        release.set()
        worker.join(5)
        self.assertEqual(len(ds._corpus), 2, "the older walk's result was discarded")


class TestLocate(WalkTestCase):
    """Drop to whitelist: finding a dropped file from its name, size and
    lastModified (the browser never sends the path)."""

    def setUp(self):
        super().setUp()
        self.spotlight = []   # what the stubbed mdfind "returns"
        self.ds._spotlight_named = lambda name: [p for p in self.spotlight
                                                 if os.path.basename(p) == name]

    def drop(self, path, size=None, mtime_ms=None):
        st = os.stat(path)
        return self.ds.locate(os.path.basename(path),
                              st.st_size if size is None else size,
                              int(st.st_mtime * 1000) if mtime_ms is None else mtime_ms)

    def test_spotlight_hit_is_narrowed_by_size_and_mtime(self):
        a = self.write("notes/plan.html", mtime=1_700_000_000)
        b = self.write("other/plan.html", body="<html>different length</html>", mtime=1_700_000_000)
        self.spotlight = [a, b]
        got = self.drop(a)
        self.assertEqual(got["source"], "spotlight")
        self.assertEqual([m["path"] for m in got["matches"]], [a])

    def test_walk_backs_up_an_empty_spotlight(self):
        a = self.write("notes/deep/plan.html")
        got = self.drop(a)
        self.assertEqual(got["source"], "walk")
        self.assertEqual([m["path"] for m in got["matches"]], [a])

    def test_walk_backs_up_a_stale_spotlight_hit(self):
        # Spotlight knows the name but the size/mtime don't match (a copy, or
        # an index that lags edits); the walk still finds the real file.
        a = self.write("notes/plan.html", mtime=1_700_000_000)
        old = self.write("old/plan.html", body="<html>older, longer copy</html>")
        self.spotlight = [old]
        got = self.drop(a)
        self.assertEqual(got["source"], "walk")
        self.assertEqual([m["path"] for m in got["matches"]], [a])

    def test_walk_skips_hidden_and_vendor_folders(self):
        self.write(".cache/plan.html", mtime=1_700_000_000)
        hidden = os.path.join(self.home, ".cache/plan.html")
        self.assertEqual(self.drop(hidden)["matches"], [])

    def test_identical_copies_all_come_back(self):
        a = self.write("one/plan.html", mtime=1_700_000_000)
        b = self.write("two/plan.html", mtime=1_700_000_000)
        self.spotlight = [a, b]
        self.assertEqual(sorted(m["path"] for m in self.drop(a)["matches"]), [a, b])

    def test_nothing_found(self):
        a = self.write("notes/plan.html")
        self.assertEqual(self.drop(a, size=999999)["matches"], [])

    def test_flags_for_a_file_outside_the_whitelist(self):
        a = self.write("notes/plan.html")
        self.spotlight = [a]
        m = self.drop(a)["matches"][0]
        self.assertFalse(m["indexed"])
        self.assertFalse(m["blacklisted"])
        self.assertIsNone(m["runbook"])
        self.assertEqual(m["label"], "~/notes/plan.html")
        self.assertEqual(m["folder"], os.path.join(self.home, "notes"))
        self.assertEqual(m["folder_label"], "~/notes")
        self.assertTrue(m["folder_ok"])

    def test_flags_for_an_already_indexed_file(self):
        a = self.write("docs/plan.html")
        self.spotlight = [a]
        self.assertTrue(self.drop(a)["matches"][0]["indexed"])

    def test_flags_for_a_blacklisted_file(self):
        self.use_settings(whitelist=["docs"], blacklist=["archive"])
        a = self.write("archive/plan.html")
        self.spotlight = [a]
        m = self.drop(a)["matches"][0]
        self.assertTrue(m["blacklisted"])
        self.assertFalse(m["indexed"])

    def test_flags_for_markdown(self):
        rb = self.write("notes/rb.md", body=MD_RUNBOOK)
        plain = self.write("notes/README.md", body="# just notes\n")
        self.spotlight = [rb, plain]
        self.assertTrue(self.drop(rb)["matches"][0]["runbook"])
        m = self.drop(plain)["matches"][0]
        self.assertFalse(m["runbook"])
        self.assertFalse(m["indexed"])

    def test_no_folder_rule_for_the_home_folder_itself(self):
        a = self.write("plan.html")
        self.spotlight = [a]
        self.assertFalse(self.drop(a)["matches"][0]["folder_ok"])

    def test_a_dropped_file_rule_indexes_it(self):
        a = self.write("notes/plan.html")
        self.use_settings(whitelist=["docs", a])
        self.assertEqual(self.rels(self.scan()), ["notes/plan.html"])

    def test_an_exact_path_rule_lists_a_plain_md(self):
        # Dropping a non-runbook .md and choosing "Whitelist this file" must
        # actually list it: the user named that exact file.
        a = self.write("skills/SKILL.md", body="---\nname: x\n---\n# My skill\n\nbody\n")
        self.use_settings(whitelist=["docs", a])
        docs = self.scan()
        self.assertEqual(self.rels(docs), ["skills/SKILL.md"])
        self.assertEqual(self.ds.wire(docs[0])["kind"], "md")
        self.assertEqual(self.ds.wire(docs[0])["title"], "My skill")
        self.assertTrue(self.ds.view_allowed(a))
        self.spotlight = [a]
        self.assertTrue(self.drop(a)["matches"][0]["indexed"])

    def test_folder_and_filename_rules_still_need_runbook_frontmatter(self):
        a = self.write("skills/SKILL.md", body="# My skill\n")
        self.use_settings(whitelist=["docs", os.path.dirname(a), "SKILL.md"])
        self.assertEqual(self.scan(), [])

    def test_blacklist_still_beats_an_exact_md_rule(self):
        a = self.write("archive/SKILL.md", body="# My skill\n")
        self.use_settings(whitelist=["docs", a], blacklist=["archive"])
        self.assertEqual(self.scan(), [])

    def test_plain_md_without_an_h1_falls_back_to_the_filename(self):
        a = self.write("skills/notes.md", body="just text\n")
        self.use_settings(whitelist=["docs", a])
        self.assertNotIn("title", self.ds.wire(self.scan()[0]))

    def test_rejects_anything_but_one_doc_name(self):
        for name in ("", "a/b.html", ".hidden.html", "notes.txt", None):
            with self.assertRaises(ValueError):
                self.ds.locate(name, 1, 1)
        with self.assertRaises(ValueError):
            self.ds.locate("plan.html", "big", 1)



class TestFolderAllMarkdown(WalkTestCase):
    """A folder rule with "md": "all" (what a dropped folder gets) lists every
    .md under it, runbook frontmatter or not."""

    def rule(self, folder, **extra):
        return dict({"type": "path", "value": os.path.join(self.home, folder)}, **extra)

    def test_all_md_folder_lists_plain_markdown_recursively(self):
        self.write("notes/README.md", body="# Read me\n")
        self.write("notes/sub/idea.md", body="no heading\n")
        self.write("notes/rb.md", body=MD_RUNBOOK)
        self.write("other/README.md", body="# elsewhere\n")
        self.use_settings(whitelist=["docs", self.rule("notes", md="all")])
        docs = self.scan()
        self.assertEqual(self.rels(docs), ["notes/README.md", "notes/rb.md", "notes/sub/idea.md"])
        by = dict((d["rel"], self.ds.wire(d)) for d in docs)
        self.assertEqual(by["notes/README.md"]["title"], "Read me")
        self.assertNotIn("title", by["notes/sub/idea.md"])

    def test_plain_folder_rule_still_needs_frontmatter(self):
        self.write("notes/README.md", body="# Read me\n")
        self.write("notes/rb.md", body=MD_RUNBOOK)
        self.use_settings(whitelist=["docs", self.rule("notes")])
        self.assertEqual(self.rels(self.scan()), ["notes/rb.md"])

    def test_blacklist_beats_all_md(self):
        self.write("notes/hidden-stuff/secret.md", body="# s\n")
        self.write("notes/ok.md", body="# ok\n")
        self.use_settings(whitelist=["docs", self.rule("notes", md="all")], blacklist=["hidden-stuff"])
        self.assertEqual(self.rels(self.scan()), ["notes/ok.md"])

    def test_flag_survives_normalization_only_on_folder_paths(self):
        doc, _ = self.ds.normalize_settings({"whitelist": [
            self.rule("notes", md="all"),
            dict(self.rule("notes/a.md"), md="all"),
            {"value": "docs", "md": "all"},
            dict(self.rule("other"), md="yes"),
        ], "blacklist": []})
        self.assertEqual([r.get("md") for r in doc["whitelist"]], ["all", None, None, None])

    def test_drop_dialog_sees_a_plain_md_under_an_all_md_folder_as_indexed(self):
        a = self.write("notes/README.md", body="# Read me\n")
        self.use_settings(whitelist=["docs", self.rule("notes", md="all")])
        self.ds._spotlight_named = lambda name: [a]
        st = os.stat(a)
        m = self.ds.locate("README.md", st.st_size, int(st.st_mtime * 1000))["matches"][0]
        self.assertTrue(m["indexed"])
        self.assertFalse(m["runbook"])

class TestSettingsHttp(WalkTestCase):
    """One live-server pass over the new endpoints; everything else is unit."""

    def setUp(self):
        super().setUp()
        import http.client
        from http.server import ThreadingHTTPServer
        self.http = http.client
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.ds.Handler)
        import threading as th
        th.Thread(target=self.server.serve_forever, daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.port = self.server.server_address[1]

    def request_full(self, method, path, body=None, extra=None):
        conn = self.http.HTTPConnection("127.0.0.1", self.port, timeout=10)
        headers = {"Content-Type": "application/json"} if body else {}
        headers.update(extra or {})
        conn.request(method, path, body, headers)
        resp = conn.getresponse()
        data = resp.read()
        out = dict(resp.getheaders())
        conn.close()
        return resp.status, data, out

    def request(self, method, path, body=None):
        status, data, _ = self.request_full(method, path, body)
        return status, data

    def test_search_headers_report_index_freshness(self):
        self.write("docs/a.html")
        status, data, headers = self.request_full("GET", "/search?q=")
        self.assertEqual(status, 200)
        self.assertEqual(len(json.loads(data)), 1)
        self.assertEqual(headers["X-Docsearch-Rescanning"], "0")
        self.assertGreater(int(headers["X-Docsearch-Last-Scan"]), 0)

    def test_fresh_param_forces_a_rescan_over_the_wire(self):
        self.write("docs/a.html")
        self.request("GET", "/search?q=")
        self.write("docs/b.html")
        _, data = self.request("GET", "/search?q=")
        self.assertEqual(len(json.loads(data)), 1, "inside the TTL, the cached corpus")
        _, data, headers = self.request_full("GET", "/search?q=&fresh=1",
                                             extra={"X-Docsearch-Resync": "1"})
        self.assertEqual(len(json.loads(data)), 2)
        self.assertEqual(headers["X-Docsearch-Rescanning"], "0",
                         "a forced scan is already done — nothing left to catch up on")

    def test_fresh_param_without_the_header_does_not_rescan(self):
        # A cross-site page can GET this URL but can't add the custom header
        # without a CORS preflight, so the bare param must be inert.
        self.write("docs/a.html")
        self.request("GET", "/search?q=")
        self.write("docs/b.html")
        _, data = self.request("GET", "/search?q=&fresh=1")
        self.assertEqual(len(json.loads(data)), 1, "no header, no forced walk")

    def test_concurrent_resyncs_coalesce_into_one_walk(self):
        import threading as th
        ds = self.ds
        self.write("docs/a.html")
        ds.run_search("")
        walks = []
        real = ds.scan_corpus
        gate = th.Event()

        def slow_scan(rules):
            walks.append(1)
            gate.wait(5)
            return real(rules)
        ds.scan_corpus = slow_scan
        try:
            first = th.Thread(target=ds._forced_rescan)
            first.start()
            while not walks:          # first walk is in flight
                th.Event().wait(0.01)
            queued = [th.Thread(target=ds._forced_rescan) for _ in range(5)]
            for t in queued:
                t.start()
            gate.set()
            for t in [first] + queued:
                t.join(10)
        finally:
            ds.scan_corpus = real
        self.assertLessEqual(len(walks), 2, "one in flight + at most one queued")

    def test_view_serves_md_runbooks_as_markdown(self):
        # text/markdown + a .md URL is what MD Pretty Viewer takes over.
        self.write("docs/b.md", body=MD_RUNBOOK)
        self.write("docs/README.md", body="# plain\n")
        conn = self.http.HTTPConnection("127.0.0.1", self.port, timeout=10)
        conn.request("GET", "/view" + self.home + "/docs/b.md")
        resp = conn.getresponse()
        data = resp.read()
        conn.close()
        self.assertEqual(resp.status, 200)
        self.assertEqual(resp.getheader("Content-Type"), "text/markdown; charset=utf-8")
        self.assertEqual(data.decode("utf-8"), MD_RUNBOOK)
        status, _ = self.request("GET", "/view" + self.home + "/docs/README.md")
        self.assertEqual(status, 403)

    def test_get_returns_current_rules(self):
        status, data = self.request("GET", "/api/settings")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(data), self.ds.DEFAULT_SETTINGS)

    def test_post_applies_rules_and_rescans(self):
        self.write("docs/a.html")
        self.write("extra/b.html")
        status, data = self.request("POST", "/api/settings", json.dumps(
            {"whitelist": [{"value": "docs"}, {"value": "~/extra"}], "blacklist": []}))
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertTrue(body["ok"])
        self.assertEqual(body["docs"], 2)
        self.assertEqual(body["settings"]["whitelist"][1]["type"], "path")
        status, data = self.request("GET", "/api/settings")
        self.assertEqual(json.loads(data), body["settings"])

    def test_post_garbage_is_rejected_and_rules_unchanged(self):
        for payload in ("not json", json.dumps({"whitelist": "nope"}),
                        json.dumps({"whitelist": [{"value": "*.html"}]})):
            status, data = self.request("POST", "/api/settings", payload)
            self.assertEqual(status, 400)
            self.assertFalse(json.loads(data)["ok"])
        self.assertEqual(self.ds._rules.doc, self.ds.DEFAULT_SETTINGS)
        self.assertFalse(os.path.exists(self.ds.SETTINGS_PATH))

    def test_post_empty_body_rejected(self):
        status, _, _ = self.request_full("POST", "/api/settings",
                                         extra={"Content-Type": "application/json"})
        self.assertEqual(status, 400)

    def test_posts_need_a_json_content_type(self):
        # A cross-site form can POST text/plain to a loopback URL without a
        # preflight; application/json can't, so it's the only type accepted.
        body = json.dumps({"whitelist": [{"value": "docs"}, {"value": "~/x"}], "blacklist": []})
        for path in ("/api/settings", "/api/locate"):
            status, _, _ = self.request_full("POST", path, body, extra={"Content-Type": "text/plain"})
            self.assertEqual(status, 415, path)
        self.assertFalse(os.path.exists(self.ds.SETTINGS_PATH), "rules unchanged")

    def test_only_loopback_host_names_are_answered(self):
        # DNS rebinding: a hostile name resolving to 127.0.0.1 is same-origin
        # to the browser, but its Host header gives it away.
        self.write("docs/a.html")
        for host in ("127.0.0.1:%d" % self.port, "localhost:%d" % self.port, "LOCALHOST", "[::1]:8377"):
            status, _, _ = self.request_full("GET", "/search?q=", extra={"Host": host})
            self.assertEqual(status, 200, host)
        for host in ("evil.example:%d" % self.port, "127.0.0.1.evil.example", ""):
            for method, path, body in (("GET", "/search?q=", None), ("GET", "/api/settings", None),
                                       ("POST", "/api/locate", json.dumps({"name": "a.html", "size": 1, "mtime": 1}))):
                status, _, _ = self.request_full(method, path, body, extra={"Host": host})
                self.assertEqual(status, 403, (host, path))

    def test_locate_over_the_wire(self):
        path = self.write("notes/plan.html")
        st = os.stat(path)
        self.ds._spotlight_named = lambda name: [path]
        status, data = self.request("POST", "/api/locate", json.dumps(
            {"name": "plan.html", "size": st.st_size, "mtime": int(st.st_mtime * 1000)}))
        self.assertEqual(status, 200)
        body = json.loads(data)
        self.assertTrue(body["ok"])
        self.assertEqual([m["path"] for m in body["matches"]], [path])
        status, data = self.request("POST", "/api/locate", json.dumps(
            {"name": "../../etc/passwd", "size": 1, "mtime": 1}))
        self.assertEqual(status, 400)


if __name__ == "__main__":
    unittest.main()
