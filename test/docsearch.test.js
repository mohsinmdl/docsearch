/* Tests for the docsearch model layer.  Run: node --test test/
 *
 * No test framework or package.json in this repo by design — node:test is
 * built in, and createRow only touches createElement/appendChild/setAttribute,
 * so a ~20 line fake document is enough to assert the real link semantics
 * without pulling in jsdom.
 */
'use strict';

const { test } = require('node:test');
const assert = require('node:assert');
const DS = require('../docsearch.js');

/* Local-time constructor keeps day boundaries stable regardless of the TZ the
   suite runs in. */
function at(y, m, d, hh, mm) {
  return Math.floor(new Date(y, m - 1, d, hh || 0, mm || 0, 0).getTime() / 1000);
}

function doc(path, rel, mtime) {
  return { path: path, rel: rel, mtime: mtime };
}

function fakeDocument() {
  function createElement(tag) {
    return {
      tagName: tag.toUpperCase(),
      className: '',
      textContent: '',
      children: [],
      attrs: Object.create(null),
      setAttribute: function (k, v) { this.attrs[k] = String(v); },
      getAttribute: function (k) { return k in this.attrs ? this.attrs[k] : null; },
      appendChild: function (c) { this.children.push(c); return c; }
    };
  }
  return { createElement: createElement };
}

function descendants(node) {
  return node.children.reduce(function (acc, c) {
    return acc.concat([c], descendants(c));
  }, []);
}

const RC = '/Users/dev/projects/rocketchat-push-gateway/docs/runbooks';
const PAI = '/Users/dev/projects/powerai/docs/runbooks';

// Server order: newest first, matching what /search returns.
const FIXTURE = [
  doc(RC + '/custom-rocketchat-image-runbook.html', 'projects/rocketchat-push-gateway/docs/runbooks/custom-rocketchat-image-runbook.html', at(2026, 7, 16, 9, 30)),
  doc(RC + '/apns-failure-diagnosis-and-panel-fix.html', 'projects/rocketchat-push-gateway/docs/runbooks/apns-failure-diagnosis-and-panel-fix.html', at(2026, 7, 16, 8, 0)),
  doc(RC + '/slack-mirror-rollout-runbook.html', 'projects/rocketchat-push-gateway/docs/runbooks/slack-mirror-rollout-runbook.html', at(2026, 7, 15, 18, 0)),
  doc(RC + '/deployment-runbook.html', 'projects/rocketchat-push-gateway/docs/runbooks/deployment-runbook.html', at(2026, 7, 15, 9, 0)),
  doc(PAI + '/linkedin-rag-courses.html', 'projects/powerai/docs/runbooks/linkedin-rag-courses.html', at(2026, 7, 14, 12, 0))
];

const BOTH = { folder: true, date: true };
const kindsOf = items => items.map(it => it.kind);
const headsOf = (items, kind) => items.filter(it => it.kind === kind);
const allExpanded = () => true;
const allCollapsed = () => false;

// ── grouping ──

test('folder+date: different folders get separate folder headings, alphabetical', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const folders = headsOf(items, 'folder-head');
  assert.deepEqual(folders.map(f => f.label), [
    '~/projects/powerai/docs/runbooks',
    '~/projects/rocketchat-push-gateway/docs/runbooks'
  ]);
  // Folder identity is the absolute dir, so same-named docs/runbooks don't merge.
  assert.deepEqual(folders.map(f => f.key).sort(), [PAI, RC]);
  assert.deepEqual(folders.map(f => f.count), [1, 4]);
});

test('folder+date: same folder, different days get separate date headings, newest first', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const rcStart = items.findIndex(it => it.kind === 'folder-head' && it.key === RC);
  const rcDates = items.slice(rcStart + 1).filter(it => it.kind === 'date-head');
  assert.deepEqual(rcDates.map(d => d.label), ['July 16, 2026', 'July 15, 2026']);
  assert.deepEqual(rcDates.map(d => d.key), ['2026-07-16', '2026-07-15']);
  assert.deepEqual(rcDates.map(d => d.count), [2, 2]);
});

test('folder+date: runbooks within a date group are ordered newest first', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const docs = DS.docsOf(items);
  // rc folder sorts after powerai; July 16 has 09:30 then 08:00.
  assert.deepEqual(docs.map(d => DS.titleOf(d.path)), [
    'linkedin rag courses',
    'custom rocketchat image runbook',
    'apns failure diagnosis and panel fix',
    'slack mirror rollout runbook',
    'deployment runbook'
  ]);
});

test('folder only: folder headings, no date headings, newest first within folder', function () {
  const items = DS.buildList(FIXTURE, { folder: true, date: false });
  assert.equal(headsOf(items, 'date-head').length, 0);
  assert.deepEqual(kindsOf(items), ['folder-head', 'doc', 'folder-head', 'doc', 'doc', 'doc', 'doc']);
  const rcDocs = items.slice(3).map(it => it.doc.mtime);
  assert.deepEqual(rcDocs, rcDocs.slice().sort((a, b) => b - a));
});

test('date only: date headings across all folders, newest first', function () {
  const items = DS.buildList(FIXTURE, { folder: false, date: true });
  assert.equal(headsOf(items, 'folder-head').length, 0);
  const dates = headsOf(items, 'date-head');
  assert.deepEqual(dates.map(d => d.label), ['July 16, 2026', 'July 15, 2026', 'July 14, 2026']);
  assert.deepEqual(dates.map(d => d.count), [2, 2, 1]);
});

test('neither: no headings, server order untouched — the original flat list', function () {
  const items = DS.buildList(FIXTURE, { folder: false, date: false });
  assert.ok(items.every(it => it.kind === 'doc'));
  assert.deepEqual(DS.docsOf(items).map(d => d.path), FIXTURE.map(d => d.path));
});

test('filtered results leave no empty folder or date headings', function () {
  const filtered = FIXTURE.filter(d => /slack-mirror/.test(d.path));
  const items = DS.buildList(filtered, BOTH);
  // Every head is followed by content of the next level down; the list never
  // ends on a head.
  items.forEach(function (it, i) {
    const next = items[i + 1];
    if (it.kind === 'folder-head') assert.ok(next && next.kind === 'date-head', 'empty folder head: ' + it.label);
    if (it.kind === 'date-head') assert.ok(next && next.kind === 'doc', 'empty date head: ' + it.label);
  });
  assert.equal(headsOf(items, 'folder-head').length, 1);
  assert.deepEqual(DS.buildList([], BOTH), []);
});

test('buildList does not mutate its input', function () {
  const input = FIXTURE.slice();
  const snapshot = JSON.parse(JSON.stringify(input));
  DS.buildList(input, BOTH);
  DS.buildList(input, { folder: true, date: false });
  DS.buildList(input, { folder: false, date: true });
  assert.deepEqual(input, snapshot);
});

// ── collapse ──

test('collapsed by default: only heads visible, giving the first-glance index', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const vis = DS.visibleItems(items, allCollapsed);
  assert.ok(vis.every(it => it.kind === 'folder-head'), 'collapsed folders hide their date heads and docs');
  assert.equal(vis.length, 2);
  assert.deepEqual(DS.docsOf(vis), []);

  const dateOnly = DS.visibleItems(DS.buildList(FIXTURE, { folder: false, date: true }), allCollapsed);
  assert.ok(dateOnly.every(it => it.kind === 'date-head'));
  assert.equal(dateOnly.length, 3);
});

test('expanding a folder reveals its date heads; expanding a date reveals its docs', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const rcId = 'f:' + RC;
  const julyId = 'd:' + RC + '|2026-07-16';

  const folderOpen = DS.visibleItems(items, id => id === rcId);
  assert.deepEqual(kindsOf(folderOpen), ['folder-head', 'folder-head', 'date-head', 'date-head']);

  const dateOpen = DS.visibleItems(items, id => id === rcId || id === julyId);
  assert.deepEqual(kindsOf(dateOpen), ['folder-head', 'folder-head', 'date-head', 'doc', 'doc', 'date-head']);
  assert.equal(DS.docsOf(dateOpen).length, 2);
});

test('head ids are unique and stable, so the same day under two folders collapses independently', function () {
  const twoFolders = [
    doc(RC + '/a.html', 'x/a.html', at(2026, 7, 16, 9, 0)),
    doc(PAI + '/b.html', 'y/b.html', at(2026, 7, 16, 8, 0))
  ];
  const items = DS.buildList(twoFolders, BOTH);
  const ids = items.filter(it => it.kind !== 'doc').map(it => it.id);
  assert.equal(new Set(ids).size, ids.length, 'duplicate head id: ' + ids.join(', '));
  // Same ids on a rebuild — expansion state keyed on them survives re-searching.
  assert.deepEqual(DS.buildList(twoFolders, BOTH).filter(it => it.kind !== 'doc').map(it => it.id), ids);
});

test('fully expanded shows everything in painted order', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const vis = DS.visibleItems(items, allExpanded);
  assert.deepEqual(vis, items);
  assert.equal(DS.docsOf(vis).length, FIXTURE.length);
});

// ── keyboard navigation across collapsed groups ──

test('docEntriesOf lists every doc in painted order, collapsed or not', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const entries = DS.docEntriesOf(items);
  assert.deepEqual(entries.map(e => e.doc), DS.docsOf(items));
  assert.equal(entries.length, FIXTURE.length);
});

test('docEntriesOf: flat mode has nothing to expand', function () {
  const entries = DS.docEntriesOf(DS.buildList(FIXTURE, { folder: false, date: false }));
  assert.ok(entries.every(e => e.heads.length === 0));
});

test('docEntriesOf: date mode names the date head hiding each doc', function () {
  const entries = DS.docEntriesOf(DS.buildList(FIXTURE, { folder: false, date: true }));
  assert.deepEqual(entries.map(e => e.heads), [
    ['d:|2026-07-16'], ['d:|2026-07-16'], ['d:|2026-07-15'], ['d:|2026-07-15'], ['d:|2026-07-14']
  ]);
});

test('docEntriesOf: folder+date names both levels, date scoped to its folder', function () {
  const entries = DS.docEntriesOf(DS.buildList(FIXTURE, BOTH));
  assert.deepEqual(entries[0].heads, ['f:' + PAI, 'd:' + PAI + '|2026-07-14']);
  assert.deepEqual(entries[1].heads, ['f:' + RC, 'd:' + RC + '|2026-07-16']);
  assert.deepEqual(entries[4].heads, ['f:' + RC, 'd:' + RC + '|2026-07-15']);
  // Folder mode alone: only the folder head stands between you and the doc.
  const folderOnly = DS.docEntriesOf(DS.buildList(FIXTURE, { folder: true, date: false }));
  assert.deepEqual(folderOnly.map(e => e.heads.length), [1, 1, 1, 1, 1]);
});

test('expanding an entry\'s heads is exactly what reveals its doc', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const entries = DS.docEntriesOf(items);
  entries.forEach(function (e) {
    const open = new Set(e.heads);
    const shown = DS.docsOf(DS.visibleItems(items, id => open.has(id)));
    assert.ok(shown.indexOf(e.doc) !== -1, 'not revealed: ' + e.doc.path);
  });
});

test('navTarget enters at the first doc when nothing is selected, either arrow', function () {
  const entries = DS.docEntriesOf(DS.buildList(FIXTURE, BOTH));
  assert.equal(DS.navTarget(entries, -1, 1), 0);
  assert.equal(DS.navTarget(entries, -1, -1), 0);
});

test('navTarget steps one doc at a time and stops at both ends', function () {
  const entries = DS.docEntriesOf(DS.buildList(FIXTURE, BOTH));
  const last = entries.length - 1;
  assert.equal(DS.navTarget(entries, 0, 1), 1);
  assert.equal(DS.navTarget(entries, 2, -1), 1);
  assert.equal(DS.navTarget(entries, last, 1), -1, 'past the last runbook: no move');
  assert.equal(DS.navTarget(entries, 0, -1), -1, 'above the first runbook: no move');
});

test('navTarget on an empty list has nowhere to go', function () {
  assert.equal(DS.navTarget([], 1, 1), -1);
  assert.equal(DS.navTarget([], -1, 1), -1);
  assert.equal(DS.navTarget(undefined, -1, 1), -1);
});

// ── group-level navigation (⌘↑/⌘↓, ←/→) ──

test('headParentsOf: a date head belongs to the folder above it', function () {
  const parents = DS.headParentsOf(DS.buildList(FIXTURE, BOTH));
  assert.equal(parents['d:' + RC + '|2026-07-16'], 'f:' + RC);
  assert.equal(parents['d:' + PAI + '|2026-07-14'], 'f:' + PAI);
  assert.equal(parents['f:' + RC], null, 'folder heads are the top of the ladder');
});

test('headParentsOf: without folder grouping there is nothing to step out to', function () {
  const parents = DS.headParentsOf(DS.buildList(FIXTURE, { folder: false, date: true }));
  assert.deepEqual(Object.keys(parents).sort(), ['d:|2026-07-14', 'd:|2026-07-15', 'd:|2026-07-16']);
  assert.ok(Object.keys(parents).every(id => parents[id] === null));
});

test('headAbove finds the group a doc sits in, and a head is its own', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const vis = DS.visibleItems(items, allExpanded);
  // ['folder-head', 'date-head', 'doc', 'folder-head', 'date-head', 'doc', 'doc', ...]
  assert.equal(DS.headAbove(vis, 2), 1, 'a doc resolves to its innermost (date) head');
  assert.equal(vis[DS.headAbove(vis, 2)].kind, 'date-head');
  assert.equal(DS.headAbove(vis, 1), 1, 'a head is its own answer');
  assert.equal(DS.headAbove(vis, 0), 0);
  // Flat mode has no heads at all — ← has nothing to close.
  assert.equal(DS.headAbove(DS.visibleItems(DS.buildList(FIXTURE, { folder: false, date: false }), allCollapsed), 3), -1);
});

test('nextHead walks group to group and stops at the ends', function () {
  const vis = DS.visibleItems(DS.buildList(FIXTURE, { folder: false, date: true }), allExpanded);
  const heads = vis.map((it, i) => (it.kind !== 'doc' ? i : -1)).filter(i => i >= 0);
  assert.equal(DS.nextHead(vis, -1, 1), heads[0], 'no selection enters at the first head');
  assert.equal(DS.nextHead(vis, -1, -1), heads[0]);
  assert.equal(DS.nextHead(vis, heads[0], 1), heads[1]);
  assert.equal(DS.nextHead(vis, heads[1], -1), heads[0]);
  assert.equal(DS.nextHead(vis, heads[heads.length - 1], 1), -1, 'past the last group: stay put');
  assert.equal(DS.nextHead(vis, heads[0], -1), -1);
  // From inside a group, ⌘↑ lands on that group's own head — it is the nearest above.
  assert.equal(DS.nextHead(vis, heads[1] + 1, -1), heads[1]);
});

test('firstDocUnder anchors the arrows when the cursor is on a head', function () {
  const items = DS.buildList(FIXTURE, BOTH);
  const entries = DS.docEntriesOf(items);
  const rcJuly16 = 'd:' + RC + '|2026-07-16';
  const at = DS.firstDocUnder(entries, rcJuly16);
  assert.equal(DS.titleOf(entries[at].doc.path), 'custom rocketchat image runbook');
  assert.equal(DS.firstDocUnder(entries, 'f:' + PAI), 0, 'the first folder starts the order');
  assert.equal(DS.firstDocUnder(entries, 'f:nope'), -1);
  // ↓ from the head enters the group; ↑ from it leaves upwards.
  assert.equal(DS.navTarget(entries, at - 1, 1), at);
  assert.equal(DS.navTarget(entries, at, -1), at - 1);
});

// ── folder label emphasis ──

test('folder labels emphasize the identifying words, dim the scaffolding', function () {
  const strongOf = label => DS.folderLabelParts(label).filter(p => p.strong).map(p => p.text);

  assert.deepEqual(strongOf('~/docs'), ['docs']);
  assert.deepEqual(strongOf('~/docs/runbook-skill-backup-2026-07-14'), ['runbook-skill-backup-2026-07-14']);
  // Long noise between project and docs stays dim — 'cursor' is the identity.
  assert.deepEqual(strongOf('~/projects/cursor/react-in-action-from-setup-to-deployment-5972102-main/docs'), ['cursor']);
  assert.deepEqual(strongOf('~/projects/docsearch/docs'), ['docsearch']);
  assert.deepEqual(strongOf('~/projects/powerai/docs'), ['powerai']);
  // The sub-area after docs is what separates sibling folders of one project.
  assert.deepEqual(strongOf('~/projects/powerai/docs/runbooks'), ['powerai', 'runbooks']);
  assert.deepEqual(strongOf('~/projects/rocketchat-push-gateway/docs'), ['rocketchat-push-gateway']);
  assert.deepEqual(strongOf('~/projects/rocketchat-push-gateway/docs/runbooks'), ['rocketchat-push-gateway', 'runbooks']);
});

test('outside-HOME docs (absolute rel from a whitelisted root) label sanely', function () {
  // The server sends `rel` as the absolute path for docs outside $HOME —
  // the label must not become '~//Volumes/…' or bolt '~/' onto it.
  assert.equal(DS.folderLabelOf({ path: '/Volumes/x/docs/a.html', rel: '/Volumes/x/docs/a.html' }),
    '/Volumes/x/docs');
  // 'Volumes' is scaffolding; the volume name is the identity.
  const strongOf = label => DS.folderLabelParts(label).filter(p => p.strong).map(p => p.text);
  assert.deepEqual(strongOf('/Volumes/backup/docs'), ['backup']);
});

test('folderLabelParts preserves every character and never crashes on junk', function () {
  ['~/docs', '~/projects/powerai/docs/runbooks', '~', 'unknown folder', '/Users/dev/docs'].forEach(function (label) {
    const joined = DS.folderLabelParts(label).map(p => p.text).join('');
    assert.equal(joined, label);
  });
  assert.deepEqual(DS.folderLabelParts(''), [{ text: '', strong: false }]);
  assert.deepEqual(DS.folderLabelParts(undefined), [{ text: '', strong: false }]);
  // Adjacent same-tone segments merge — no span-per-character noise.
  DS.folderLabelParts('~/projects/powerai/docs/runbooks').reduce(function (prev, p) {
    assert.notEqual(prev, p.strong, 'adjacent parts share a tone');
    return p.strong;
  }, null);
});

// ── rows / links ──

test('a row is a standard anchor with an href and no target="_blank"', function () {
  const row = DS.createRow(fakeDocument(), FIXTURE[0], { selected: false });

  assert.equal(row.tagName, 'A');
  assert.equal(row.getAttribute('href'), '/view/Users/dev/projects/rocketchat-push-gateway/docs/runbooks/custom-rocketchat-image-runbook.html');
  // No target: plain click stays in-tab, Cmd/Ctrl-click opens a new tab natively.
  assert.equal(row.getAttribute('target'), null);
  assert.equal(row.getAttribute('rel'), null);
  descendants(row).forEach(function (el) {
    assert.equal(el.getAttribute('target'), null);
  });
});

test('hrefFor escapes segments but keeps the path structure', function () {
  assert.equal(DS.hrefFor('/Users/dev/docs/a b.html'), '/view/Users/dev/docs/a%20b.html');
  assert.equal(DS.hrefFor('/Users/dev/docs/r&d.html'), '/view/Users/dev/docs/r%26d.html');
  assert.equal(DS.hrefFor(''), null);
  assert.equal(DS.hrefFor(undefined), null);
});

test('row sub-line is the filename by default, the full path when pathText is given', function () {
  const groupedRow = DS.createRow(fakeDocument(), FIXTURE[0], {});
  const groupedPath = descendants(groupedRow).find(el => el.className === 'path');
  assert.equal(groupedPath.textContent, 'custom-rocketchat-image-runbook.html');

  const flatRow = DS.createRow(fakeDocument(), FIXTURE[0], { pathText: FIXTURE[0].path });
  const flatPath = descendants(flatRow).find(el => el.className === 'path');
  assert.equal(flatPath.textContent, RC + '/custom-rocketchat-image-runbook.html');

  // Both are still identical anchors — layout mode never changes link semantics.
  assert.equal(flatRow.tagName, 'A');
  assert.equal(flatRow.getAttribute('href'), groupedRow.getAttribute('href'));
  assert.equal(flatRow.getAttribute('target'), null);
});

test('the selected row is marked and still a plain anchor', function () {
  const row = DS.createRow(fakeDocument(), FIXTURE[0], { selected: true });
  assert.match(row.className, /\bsel\b/);
  assert.equal(row.tagName, 'A');
  assert.ok(row.getAttribute('href'));
});

// ── resilience ──

test('missing or invalid metadata does not crash', function () {
  const messy = [
    doc('/Users/dev/docs/ok.html', 'docs/ok.html', at(2026, 7, 16, 10, 0)),
    doc('/Users/dev/docs/no-mtime.html', 'docs/no-mtime.html', undefined),
    doc('/Users/dev/docs/nan-mtime.html', 'docs/nan-mtime.html', NaN),
    doc('/Users/dev/docs/junk-mtime.html', 'docs/junk-mtime.html', 'not-a-number'),
    doc('/Users/dev/docs/negative.html', 'docs/negative.html', -1),
    doc('/Users/dev/docs/no-rel.html', undefined, at(2026, 7, 16, 11, 0)),
    { path: '/Users/dev/docs/bare.html' },
    { rel: 'docs/no-path.html', mtime: at(2026, 7, 16, 12, 0) }, // dropped: no path
    null,
    undefined
  ];

  let items;
  assert.doesNotThrow(function () { items = DS.buildList(messy, BOTH); });
  assert.equal(DS.docsOf(items).length, 7, 'rows without a usable path are dropped, the rest survive');

  // Undated rows collect under one "Unknown date" heading, sorted last.
  const dates = headsOf(items, 'date-head');
  assert.equal(dates[dates.length - 1].label, 'Unknown date');
  assert.equal(dates[dates.length - 1].count, 5);

  assert.equal(DS.folderLabelOf({ path: '/Users/dev/docs/no-rel.html' }), '/Users/dev/docs');
  assert.equal(DS.stampOf(undefined), '—');
  assert.equal(DS.dayLabelOf(NaN), 'Unknown date');
  assert.equal(DS.titleOf(undefined), 'Untitled');

  const d = fakeDocument();
  assert.doesNotThrow(function () { DS.createRow(d, { path: '/Users/dev/docs/bare.html' }, {}); });
  assert.deepEqual(DS.buildList(null, BOTH), []);
  assert.deepEqual(DS.docsOf(undefined), []);
  assert.deepEqual(DS.visibleItems(undefined, allExpanded), []);
});
