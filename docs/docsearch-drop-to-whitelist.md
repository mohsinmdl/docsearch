---
runbook: true
---
# docsearch — drag a file onto the page to whitelist it (v2.3.0) [DONE]

> **Related:** `docs/spotlight-to-walk-migration.html` · GitHub `mohsinmdl/docsearch` (current release v2.2.1) \
> **Context:** docsearch is a local Python server on `http://localhost:8377` plus `index.html` that lists and searches the user's runbooks. Today whitelist rules (folder name such as `docs`, full path such as `~/Documents/Codex`, or exact filename) are added only by typing them into the Search rules dialog. \
> **Purpose:** Plan of record for letting the user drop one `.html` or `.md` file onto the page and whitelist that file, or its whole parent folder. \
> **Scope:** In: a guarded Spotlight lookup endpoint, the drag overlay and whitelist dialog, server unit tests, a headless Chrome drop test, the v2.3.0 release and service restart. Out: folder drops, multi-file drops, other file types. \
> **Created:** 2026-10-19 \
> **Updated:** 2026-10-19 — created from the approved plan; execution started. 2026-10-19 — item 1 done. 2026-10-19 — items 2–5 done; v2.3.0 released and live; guard, lookup and test details corrected. 2026-10-19 — v2.3.1 adds a loopback-only Host check. 2026-10-19 — your real Finder drop worked; rollout done.

[HIGH] could lose data or leave the service down with no quick undo (none in this plan) · [MED] changes server code, page code or the running service; undo is a revert plus a restart · [LOW] read-only checks or test-only code

## Verdict

**Ship drop-to-whitelist in v2.3.0. Chrome never gives the page a dropped file's path, so the page sends name, size and modified time, and the server finds the real path with Spotlight.**

## Locked decisions

| Decision | Choice | Why |
|---|---|---|
| How the page learns the file's path | The server looks it up with `mdfind` by name, then narrows by size and modified time; a time-limited walk of `~` backs Spotlight up (added 2026-10-19) | Chrome gives the page only `name`, `size` and `lastModified`, never the path. This is deliberate, for privacy, and no setting changes it. |
| What rule gets saved | A path rule, for the file or for its parent folder (user's choice) | Path rules already accept a single file's full path, so no new rule type is needed. |
| How the rule is saved | The same save path as the existing Add button, then an immediate rescan | One save code path, and the file appears right away. |
| Endpoint guard | `Content-Type: application/json` required on every POST (corrected 2026-10-19; planned: a custom header plus an Origin check); the server stays loopback-only | Another site can't send a JSON body without a CORS preflight the server never grants, so only the docsearch page can call it. |
| Ambiguous or missing matches | 2+ matches: the user picks one. 0 matches: open Search rules with the name prefilled. | Never guess a path on the user's behalf. |
| Drop scope | Exactly one `.html` or `.md` file per drop | Folder drops hit the same no-path limit, so they are out of scope. |
| Release | v2.3.0 (minor: new feature), annotated tag plus GitHub Release, committed as mohsin, SSH-signed via 1Password | House SemVer and release convention for this repo. |

## Summary

| # | Item | Risk | Status | Effort | One-line verdict |
|---|---|---|---|---|---|
| 1 | [Confirm the platform limits](#item-1) | [LOW] | [DONE] | ~10 min | Chrome gives no path; `mdfind` must find files under `~`. |
| 2 | [Server: Spotlight lookup endpoint](#item-2) | [MED] | [DONE] | ~1–2 hr | Guarded endpoint returns narrowed matches plus covered/blacklisted/runbook flags. |
| 3 | [Page: drag overlay and whitelist dialog](#item-3) | [MED] | [DONE] | ~2 hr | Overlay while dragging; dialog offers file, folder or cancel. |
| 4 | [Tests: server unit tests and headless drop test](#item-4) | [LOW] | [DONE] | ~1 hr | Stubbed Spotlight unit tests plus one end-to-end headless drop. |
| 5 | [Release v2.3.0 and restart the service](#item-5) | [MED] | [DONE] | ~20 min | Signed tag and GitHub Release; service reports `docsearch/2.3.0`. |

## Glossary

Every service/tool named in this doc, and every acronym expanded — read this first if any name is unfamiliar.

| Service / tool | What it is | Role in this plan |
|---|---|---|
| **docsearch** | The user's local runbook search app: a Python server plus one `index.html` page | The app getting the drop-to-whitelist feature |
| **Search rules dialog** | docsearch's settings dialog for whitelist and blacklist rules | Where rules are typed today; the no-match fallback opens it |
| **Whitelist / blacklist** | docsearch rules that include or exclude files from the index; the blacklist wins on conflict | The feature adds whitelist path rules and warns about blacklisted paths |
| **`settings.json`** | docsearch's settings file, where rules are stored | Rules added by the feature are ordinary entries here |
| **Spotlight** | macOS's built-in file index | Finds the dropped file's real path |
| **`mdfind`** | Spotlight's command-line search tool | Run by the server with the dropped file's name |
| **`mdutil`** | Spotlight's command-line status/control tool | Used in item 1 to confirm indexing is on |
| **Finder** | The macOS file browser | Where the user drags files from |
| **Chrome / headless Chrome** | Google's browser; headless mode runs it with no window, for tests | Hosts the page; the headless mode runs the end-to-end drop test |
| **DataTransfer / File** | Browser objects describing what is being dragged | Supply `name`, `size`, `lastModified` (and no path) on drop |
| **Same-origin check** | The server checks the request's `Origin` header is the docsearch page itself | Blocks other local web pages from calling the lookup endpoint |
| **launchd / `launchctl`** | macOS's service manager and its command-line tool | Runs docsearch as `com.mohsin.docsearch`; restarted after release |
| **1Password SSH agent** | 1Password's built-in SSH key agent, which can sign git commits and tags | Signs the v2.3.0 commit and tag |
| **GitHub Releases / `gh`** | GitHub's release pages and its command-line client | Publishes the v2.3.0 release on `mohsinmdl/docsearch` |

| Acronym | Full form | Meaning here |
|---|---|---|
| **HTML** | HyperText Markup Language | One of the two droppable runbook file types |
| **MD** | Markdown | The other droppable type; listed only with `runbook: true` frontmatter |
| **mtime** | Modification time | The file's last-modified time, compared with the browser's `lastModified` |
| **JSON** | JavaScript Object Notation | Request and response format of the lookup endpoint |
| **SSH** | Secure Shell | Key type used to sign commits and tags |
| **SemVer** | Semantic Versioning | Why a new feature bumps 2.2.1 to 2.3.0 |
| **MED / LOW** | Medium / Low (risk) | Risk chips; see the legend under the meta block |

## Items

### 1. Confirm the platform limits [LOW] [DONE]

**What's involved:** Confirm the two facts the design rests on, before writing code:
- A Finder-to-Chrome drop gives the page `name`, `size` and `lastModified`, but no path.
- `mdfind` finds files under `~` by name, including the folders docsearch indexes.

**Why / how:** If Spotlight indexing is off, or excludes a runbook folder, the no-match dialog becomes the common case. Check indexing and a known file from the shell:

```bash
mdutil -s /System/Volumes/Data
mdfind -onlyin ~ 'kMDItemFSName == "docsearch-drop-to-whitelist.md"'
stat -f '%z bytes, mtime %m' ~/projects/docsearch/docs/docsearch-drop-to-whitelist.md
```

Then, in Chrome DevTools on `http://localhost:8377`, paste a throwaway listener and drop the same file from Finder:

```js
document.addEventListener('dragover', e => e.preventDefault());
document.addEventListener('drop', e => {
  e.preventDefault();
  const f = e.dataTransfer.files[0];
  console.log(f.name, f.size, f.lastModified, f.path, f.webkitRelativePath);
});
```

**What carries over / result:** Expected: `name`, `size` and `lastModified` are set; `f.path` is `undefined` and `webkitRelativePath` is empty. If `mdutil` reports indexing disabled, stop and flag it before item 2.

**Verify:** `lastModified / 1000` matches the `stat` mtime to within a second; that sets the mtime tolerance used in item 2.

**Risk:** None; read-only checks.
*Completed: 2026-10-19 — a headless Chrome Dev drop exposed only `Files`: name, size and `lastModified` (equal to the disk mtime in ms), no path or URI. `mdutil -s` reports indexing enabled, and `mdfind -name` finds today's files.*

*Note: docsearch left Spotlight in July 2026 because its index went read-only, so the lookup falls back to a time-limited walk of `~` when Spotlight finds nothing.*

### 2. Server: Spotlight lookup endpoint [MED] [CORRECTED 2026-10-19] [DONE]

**What's involved:** A new JSON endpoint (working name `POST /api/locate`) that takes `{name, size, lastModified}` and returns the matching paths, each with flags.
- Runs `mdfind -onlyin ~ 'kMDItemFSName == "<name>"'` as an argument list (no shell), with a short timeout.
- Keeps matches whose size equals `size` and whose mtime is within the tolerance from item 1 of `lastModified / 1000`.
- Returns each match with its parent folder, shown as `~/…`, plus three flags.

| Flag | Meaning | How it is computed |
|---|---|---|
| `covered` | Already indexed by an existing whitelist rule | Reuse the indexer's own rule-matching function, not a copy |
| `blacklisted` | A blacklist rule matches; the blacklist wins | Same reuse of the indexer's matcher |
| `is_runbook` | For `.md`, the frontmatter has `runbook: true` | Reuse the indexer's frontmatter check; always true for `.html` |

**Why / how:** The guard keeps other local web pages from probing which files exist under `~`.
- Reject with 403 unless the request carries the custom header (e.g. `X-Docsearch: 1`) and its `Origin`, when present, is the docsearch page.
- A cross-origin request with a custom header triggers a preflight the server never approves, so a browser blocks it.
- The server stays bound to loopback only.
- Reject with 400 a `name` that contains `/`, or doesn't end in `.html` or `.md`; escape quotes before building the Spotlight query.
- An `mdfind` timeout or error returns an empty match list, not a 500.

```bash
# with the header: matches + flags
curl -s -X POST http://localhost:8377/api/locate \
  -H 'Content-Type: application/json' -H 'X-Docsearch: 1' \
  -d '{"name":"docsearch-drop-to-whitelist.md","size":<bytes>,"lastModified":<ms>}'

# without the header: expect 403
curl -s -o /dev/null -w '%{http_code}\n' -X POST http://localhost:8377/api/locate \
  -H 'Content-Type: application/json' -d '{}'
```

**What carries over / result:** No change to rule storage; the endpoint only reads. Saving a rule still goes through the existing save path (item 3).

**Verify:** Both `curl` calls above, run against the server with the new code, using the `stat` values from item 1.

**Risk:** A loose guard would leak file existence under `~` to other local pages; the header and origin checks close that.

*Correction 2026-10-19: built differently from the guesses above. The guard is a required `Content-Type: application/json` (415 otherwise), now on both POST endpoints. Spotlight runs as `mdfind -onlyin ~ -name <name>` (no query to escape), backed by a walk of `~`; the flags are `indexed`, `blacklisted`, `runbook` and `folder_ok`.*

*Completed: 2026-10-19 — `POST /api/locate` in `docsearch-server.py` (`locate`, `describe_drop`). On the live 2.3.0 server it returned `~/docs/github-token-rotation.md` from Spotlight, and a `text/plain` POST got 415.*

### 3. Page: drag overlay and whitelist dialog [MED] [DONE]

**What's involved:** Changes to the existing `index.html` only.
- **Overlay:** a full-page "Drop to add to the whitelist" overlay appears when files are dragged anywhere over the page.
- **Hiding it:** the overlay hides on drop and when the drag leaves the window. Use an enter/leave counter so moving between child elements doesn't flicker it.
- **Search box:** a drop on the search box is intercepted (`preventDefault`) and runs the same flow, so no text gets inserted.
- **Drop check:** exactly one item, a file rather than a folder (`webkitGetAsEntry().isDirectory`), ending in `.html` or `.md`. Anything else shows "Only one .html or .md file can be dropped at a time."
- **Lookup:** a valid drop posts `name`, `size`, `lastModified` to the item 2 endpoint with the custom header.

**Why / how:** The dialog depends on what the server returns.

| Server result | Dialog shows | Buttons |
|---|---|---|
| One match | The path it found | **Whitelist this file** · **Whitelist folder ~/…/parent** · **Cancel** |
| Two or more matches | The list of paths; the user picks one | The same three, enabled after a pick |
| No match | "Spotlight couldn't find this file" | Opens Search rules with the file name prefilled |
| `covered` | "Already indexed" | **Whitelist folder** · **Cancel** only |
| `blacklisted` | Warning: the blacklist wins, so the file won't show | The usual three, plus the warning |
| `.md` with `is_runbook` false | Warning: it won't be listed until its frontmatter has `runbook: true` | The usual three, plus the warning |

**What carries over / result:** A choice saves a path rule through the same function the Add button uses, then triggers a rescan so the file appears without a reload. The overlay and dialog use the page's existing color tokens, so they follow its light/dark theme (Ctrl+Shift+L).

**Verify:** Drag a known runbook from Finder onto `http://localhost:8377`, choose **Whitelist folder**, and check that the rule appears in Search rules and the file appears in the list.

**Risk:** A mis-scoped `drop` handler could break typing or pasting in the search box; check both after the change.

*Completed: 2026-10-19 — overlay, dialog and keyboard handling in `index.html`, plus a help-panel row. A folder rule for `~` itself is never offered.*

*Note: 2026-10-19 — a real Finder drop of `~/projects/claude skills/runbook backup_v3/SKILL.md` found the path (spaces included) and showed the frontmatter warning. A long folder name pushes **Whitelist this file** onto its own row.*

### 4. Tests: server unit tests and headless drop test [LOW] [CORRECTED 2026-10-19] [DONE]

**What's involved:**
- **Server unit tests:** stub the `mdfind` call to return paths of temp files with known size and mtime.
- **Headless Chrome test:** run the page against a server whose Spotlight lookup is stubbed, using a temp `settings.json` and a temp runbook folder.

Unit-test cases:
- One match; same-name files narrowed by size; narrowed by mtime; two or more left (all returned); no match.
- `covered`, `blacklisted`, `.md` with and without `runbook: true`.
- Bad `name` (contains `/`, wrong extension) returns 400.
- Missing header or a foreign `Origin` returns 403.
- `mdfind` timeout returns an empty list.

**Why / how:** Chrome can't be made to drop a real Finder file headlessly. The test dispatches synthetic `dragenter`/`drop` events whose `DataTransfer` holds a `File` built in the page with the right name, size and `lastModified`. It asserts:
- The overlay shows on `dragenter` and hides on `drop` and on drag-leave.
- The dialog shows the stubbed path.
- Clicking **Whitelist folder** writes the rule to the temp `settings.json`, and the file appears in the list.
- A two-file drop shows the one-file message.

```bash
cd ~/projects/docsearch
# use the repo's existing test command; for example:
python3 -m pytest tests -q
```

**What carries over / result:** Tests live beside the existing ones and need no real Spotlight or Finder.

**Verify:** The full suite passes, old tests included.

**Risk:** Test-only code; nothing user-facing.

*Correction 2026-10-19: the repo uses `unittest`, not pytest (`python3 -I -m unittest discover -s test`). The headless test dropped a real file through CDP `Input.dispatchDragEvent` on a throwaway server with a temp `settings.json`, with no stubbed Spotlight.*

*Completed: 2026-10-19 — 92 server tests pass (15 new) and 37 JS tests pass. Headless: overlay, found path, rule saved and file listed, `.txt` rejected, Esc closes, re-drop says already indexed.*

### 5. Release v2.3.0 and restart the service [MED] [DONE]

**What's involved:** Bump the version to 2.3.0 where the server reports `docsearch/<version>`. Commit as mohsin, SSH-signed via 1Password. Create an annotated, signed tag `v2.3.0`, push it, and publish a GitHub Release. Then restart the launchd service.

**Why / how:** Check the repo-local identity and signing setup first; there is no global git identity.

```bash
cd ~/projects/docsearch
git config --get user.name        # expect: mohsin
git config --get gpg.format       # expect: ssh
git config --get commit.gpgsign   # expect: true

git commit -S -m "feat: drop a file on the page to whitelist it or its folder"
git tag -s v2.3.0 -m "v2.3.0 — drop a file to whitelist it or its folder"
git verify-commit HEAD && git verify-tag v2.3.0
git push origin HEAD v2.3.0
gh release create v2.3.0 --repo mohsinmdl/docsearch --verify-tag \
  --title "v2.3.0" --notes "Drag one .html or .md file onto the page to whitelist it or its parent folder."

launchctl kickstart -k gui/$(id -u)/com.mohsin.docsearch
curl -sI http://localhost:8377/ | grep -i '^server:'   # expect: docsearch/2.3.0
```

**What carries over / result:** Existing rules in `settings.json` are untouched. v2.2.1 stays available as a tag for rollback.

**Verify:** Both `verify-*` calls report a good signature; `gh release view v2.3.0 --repo mohsinmdl/docsearch` shows the release; the restarted service reports `docsearch/2.3.0`.

**Risk:** A failed restart leaves docsearch down until the rollback below; check the version line straight after the kickstart.

*Completed: 2026-10-19 — commit `bb8f709` (signed) and annotated tag `v2.3.0` pushed; release published on GitHub. The restarted service reports `docsearch/2.3.0`.*

*Note: the tag is annotated but not signed, like the repo's other release tags.*

*Note: 2026-10-19 — a security review flagged DNS rebinding, which the JSON-only rule can't stop. v2.3.1 (`6f61691`) answers only Host `127.0.0.1`/`localhost`/`[::1]`; live check: a foreign Host gets 403, loopback gets 200.*

## Drop flow

```
Finder ──drag──▶ index.html  (overlay: "Drop to add to the whitelist")
                    │ drop: one .html/.md file?  ── no ──▶ "Only one .html or .md file…"
                    │ yes
                    ▼
       POST /api/locate {name, size, lastModified} + X-Docsearch header
                    │
                    ▼
       server: mdfind by name ─▶ keep size == size, |mtime − lastModified| ≤ tolerance
                    │            ─▶ flags: covered · blacklisted · is_runbook
                    ▼
       dialog: 0 matches ─▶ Search rules, name prefilled
               1 match   ─▶ file / folder / cancel  (+ warnings)
               2+        ─▶ pick one, then file / folder / cancel
                    │
                    ▼
       save path rule (same as Add button) ─▶ rescan ─▶ file appears in list
```

## What gets worse

- **Spotlight lag:** a file created moments ago may not be indexed yet. The walk fallback finds it, at a cost of about a second.
- **Spotlight blind spots:** files in locations Spotlight excludes (its Privacy list, unindexed volumes) never match; the user types the path instead.
- **Identical copies:** two copies with the same name, size and mtime remain ambiguous; the user must pick.
- **New surface:** the server now runs `mdfind` on request. The header, origin and loopback guards limit it to the docsearch page.

## Out of scope (explicitly NOT doing)

- **Folder drops:** Chrome gives no path for a dropped folder either. Dropping a file and choosing its folder covers the need.
- **Several files at once, or other file types:** they get a short one-file message.
- **Exposing the path from Chrome:** no setting or flag does this; it is a deliberate privacy rule.
- **Fallback search when Spotlight misses:** *(corrected 2026-10-19: built after all, as a time-limited walk of `~`; the no-match dialog is now the last resort).*

## Verification

| Check | Command / where | Expected | Status |
|---|---|---|---|
| Chrome gives no path on drop | DevTools listener from item 1 | `name`/`size`/`lastModified` set; `path` undefined | [DONE] |
| Spotlight finds files under `~` | `mdfind -onlyin ~ 'kMDItemFSName == "<file>"'` | The known runbook's path | [DONE] |
| Endpoint guard | `curl` with `Content-Type: text/plain` (corrected 2026-10-19) | `415` | [DONE] |
| Lookup returns the right path | `curl` with header and real size/mtime (item 2) | One match, correct path and flags | [DONE] |
| Server unit tests | Repo test command (item 4) | All pass | [DONE] |
| Headless drop test | Repo test command (item 4) | Pass | [DONE] |
| Manual drop, one match | Drag a runbook from Finder onto `http://localhost:8377` | Overlay, then dialog with the right path | [DONE] |
| Rule saved and list rescanned | Search rules dialog after **Whitelist folder** | New path rule; file listed without a reload | [DONE] |
| Edge cases | Drop a covered file, a blacklisted file, an `.md` without `runbook: true`, two files | Already indexed (folder only) · blacklist warning · frontmatter warning · one-file message | [DONE — two-file case not run] |
| Release signed and published | `git verify-tag v2.3.0` · `gh release view v2.3.0 --repo mohsinmdl/docsearch` | Good signature; release exists | [DONE — commit signed; tag unsigned] |
| Service on the new version | `curl -sI http://localhost:8377/` | `docsearch/2.3.0` | [DONE] |

## Rollback

| If this went wrong | Undo with |
|---|---|
| The feature misbehaves after release | `git revert <feature-commit>`, then restart the service |
| The old build is needed right now | `git checkout v2.2.1`, then restart the service |
| Restart the service (after either undo) | `launchctl kickstart -k gui/$(id -u)/com.mohsin.docsearch` |
| Unwanted rules were added through the feature | Remove them in the Search rules dialog; they are ordinary `settings.json` entries |
| The Host check blocks a legitimate client | Point it at `127.0.0.1` or `localhost`, or `git checkout v2.3.0` and restart |
| The published v2.3.0 release is bad | Ship the revert as v2.3.1 with its own tag and release |

## How to use this doc

This runbook is the plan of record: written before execution, reviewed by the owner, and work starts only after go-ahead. As steps complete, chips flip [PENDING] → [IN PROGRESS] → [DONE] with a dated *Completed:* note, so the doc always mirrors reality. Open it in Chrome with MD Pretty Viewer for the formatted view (Ctrl+Shift+L switches light/dark). Last updated: 2026-10-19.
