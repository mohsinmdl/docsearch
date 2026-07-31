/* docsearch — pure model layer for the runbook list.
 *
 * Titles, folder/date grouping, hrefs and row construction. Deliberately free of
 * page state: index.html owns the DOM and the fetching, this file owns the
 * shaping, so the grouping rules can be tested without a browser (test/).
 *
 * Loaded as a plain script by index.html (window.docsearch) and via require()
 * by node --test. ES5 style, matching index.html.
 */
(function (root, factory) {
  var api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.docsearch = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  var MONTHS_LONG = ['January', 'February', 'March', 'April', 'May', 'June',
    'July', 'August', 'September', 'October', 'November', 'December'];
  var MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

  var UNKNOWN_DAY = 'unknown';
  var UNKNOWN_FOLDER = 'unknown folder';

  function pad(n) { return ('0' + n).slice(-2); }

  function dirOf(p) {
    var i = p.lastIndexOf('/');
    return i > 0 ? p.slice(0, i) : (i === 0 ? '/' : '');
  }

  function baseOf(p) { return p.split('/').pop(); }

  /* "custom-rocketchat-image-runbook.html" -> "custom rocketchat image runbook".
     Unchanged from the original flat list — titles are not part of this change. */
  function titleOf(path) {
    if (typeof path !== 'string' || !path) return 'Untitled';
    var base = baseOf(path).replace(/\.html?$/i, '');
    return base.replace(/[-_]+/g, ' ').trim() || 'Untitled';
  }

  function fileOf(path) {
    if (typeof path !== 'string' || !path) return '';
    return baseOf(path);
  }

  /* mtime is unix seconds. Anything non-finite/non-positive is treated as absent
     rather than thrown — mdfind can hand back rows we can't stat cleanly. */
  function dateOf(mtime) {
    var n = typeof mtime === 'number' ? mtime : Number(mtime);
    if (!isFinite(n) || n <= 0) return null;
    var d = new Date(n * 1000);
    return isNaN(d.getTime()) ? null : d;
  }

  /* Local calendar day. Lexical sort on this key is also chronological. */
  function dayKeyOf(mtime) {
    var d = dateOf(mtime);
    if (!d) return UNKNOWN_DAY;
    return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate());
  }

  function dayLabelOf(mtime) {
    var d = dateOf(mtime);
    if (!d) return 'Unknown date';
    return MONTHS_LONG[d.getMonth()] + ' ' + d.getDate() + ', ' + d.getFullYear();
  }

  /* Short stamp for the row chip — the original list's format, kept as-is. */
  function stampOf(mtime) {
    var d = dateOf(mtime);
    if (!d) return '—';
    return MONTHS[d.getMonth()] + ' ' + d.getDate() + ', ' + pad(d.getHours()) + ':' + pad(d.getMinutes());
  }

  function isRecent(mtime, now) {
    var d = dateOf(mtime);
    if (!d) return false;
    var ref = typeof now === 'number' ? now : Date.now();
    return (ref - d.getTime()) < 7 * 24 * 3600 * 1000;
  }

  /* Group identity is the absolute parent dir: two repos both ending in
     docs/runbooks are different folders and must not merge. */
  function folderKeyOf(d) {
    var dir = dirOf(d.path);
    return dir || UNKNOWN_FOLDER;
  }

  /* Display label is home-relative ("~/projects/powerai/docs/runbooks"): keeps
     folders distinct without printing an absolute machine path. `rel` comes from
     the server; if it's missing we fall back to the absolute dir rather than
     guessing at a home prefix. For docs outside $HOME (whitelisted roots on
     another volume) the server sends `rel` as the absolute path — label it as
     such rather than gluing '~/' onto it. */
  function folderLabelOf(d) {
    if (typeof d.rel === 'string' && d.rel) {
      if (d.rel.charAt(0) === '/') return dirOf(d.rel) || '/';
      var dir = dirOf(d.rel);
      return dir ? '~/' + dir : '~';
    }
    return dirOf(d.path) || UNKNOWN_FOLDER;
  }

  /* Scaffolding segments that carry no identity of their own in a heading.
     Volumes covers absolute labels from whitelisted roots on other disks. */
  var STRUCTURAL = { projects: true, docs: true, Volumes: true };

  /* '~/projects/powerai/docs/runbooks' -> parts for two-tone rendering:
     [{text:'~/projects/'},{text:'powerai',strong:true},{text:'/docs/'},{text:'runbooks',strong:true}]

     The informative words in these labels are the project name — the first
     segment that isn't ~/projects/docs scaffolding — and everything after the
     last 'docs', which is what tells two folders of the same project apart
     (docs vs docs/runbooks). Everything else dims. Long noise between the two
     (a cloned course folder, a dated backup dir) stays visible but quiet.
     If nothing qualifies (label is just '~/docs'), the last segment carries
     the identity. Every character of the label is preserved. */
  function folderLabelParts(label) {
    if (typeof label !== 'string' || !label) {
      return [{ text: String(label == null ? '' : label), strong: false }];
    }
    var segs = label.split('/');
    var n = segs.length;
    var strong = new Array(n);
    var lastDocs = -1;
    var seenPrimary = false;
    var i;
    for (i = 0; i < n; i++) if (segs[i] === 'docs') lastDocs = i;
    for (i = 0; i < n; i++) {
      if (segs[i] === '~' || segs[i] === '') continue;
      if (!seenPrimary && !STRUCTURAL[segs[i]]) { strong[i] = true; seenPrimary = true; }
      if (lastDocs !== -1 && i > lastDocs) strong[i] = true;
    }
    var any = false;
    for (i = 0; i < n; i++) if (strong[i]) any = true;
    if (!any) strong[n - 1] = true;

    var parts = [];
    function push(text, st) {
      if (!text) return;
      var last = parts[parts.length - 1];
      if (last && last.strong === st) last.text += text;
      else parts.push({ text: text, strong: st });
    }
    for (i = 0; i < n; i++) {
      push(segs[i], !!strong[i]);
      if (i < n - 1) push('/', false);
    }
    return parts;
  }

  function usable(d) {
    return !!d && typeof d.path === 'string' && d.path !== '';
  }

  function byDayDesc(a, b) {
    if (a.key === UNKNOWN_DAY) return 1;   // undated sinks to the bottom
    if (b.key === UNKNOWN_DAY) return -1;
    return a.key < b.key ? 1 : (a.key > b.key ? -1 : 0);
  }

  function byMtimeDesc(a, b) {
    var x = Number(a.mtime), y = Number(b.mtime);
    if (!isFinite(x)) x = -Infinity;
    if (!isFinite(y)) y = -Infinity;
    return y - x;
  }

  function folderGroupsOf(list) {
    var byFolder = Object.create(null);
    var order = [];
    list.forEach(function (d) {
      var fk = folderKeyOf(d);
      if (!byFolder[fk]) {
        byFolder[fk] = { key: fk, label: folderLabelOf(d), items: [] };
        order.push(fk);
      }
      byFolder[fk].items.push(d);
    });
    return order
      .map(function (k) { return byFolder[k]; })
      .sort(function (a, b) { return a.label.localeCompare(b.label); });
  }

  function dateGroupsOf(list) {
    var byDay = Object.create(null);
    var order = [];
    list.forEach(function (d) {
      var dk = dayKeyOf(d.mtime);
      if (!byDay[dk]) {
        byDay[dk] = { key: dk, label: dayLabelOf(d.mtime), items: [] };
        order.push(dk);
      }
      byDay[dk].items.push(d);
    });
    return order
      .map(function (k) { return byDay[k]; })
      .sort(byDayDesc)
      .map(function (g) {
        return { key: g.key, label: g.label, items: g.items.slice().sort(byMtimeDesc) };
      });
  }

  /* docs + independent {folder, date} flags -> the ordered list the page paints:
     [{kind:'folder-head'|'date-head', key, label} | {kind:'doc', doc}]

     folder+date  folder heads (alphabetical) > date heads (newest first) > docs (newest first)
     folder only  folder heads (alphabetical) > docs (newest first)
     date only    date heads (newest first) > docs (newest first)
     neither      docs in the order given — the server's newest-first, untouched

     Input is never mutated. Heads are only emitted above docs that exist in
     `docs`, so a filtered (searched) list can't yield an empty heading. */
  function buildList(docs, by) {
    var folder = !!(by && by.folder);
    var date = !!(by && by.date);
    var list = Array.isArray(docs) ? docs.filter(usable) : [];
    var out = [];

    function pushDocs(items) {
      items.forEach(function (d) { out.push({ kind: 'doc', doc: d }); });
    }
    /* Head ids are stable across renders (they're what the expand/collapse
       state is keyed on) and scoped so the same calendar day under two
       different folders is two independently collapsible heads. */
    function pushDated(items, scope) {
      dateGroupsOf(items).forEach(function (dg) {
        out.push({ kind: 'date-head', key: dg.key, label: dg.label, id: 'd:' + scope + '|' + dg.key, count: dg.items.length });
        pushDocs(dg.items);
      });
    }

    if (folder) {
      folderGroupsOf(list).forEach(function (g) {
        out.push({ kind: 'folder-head', key: g.key, label: g.label, id: 'f:' + g.key, count: g.items.length });
        if (date) pushDated(g.items, g.key);
        else pushDocs(g.items.slice().sort(byMtimeDesc));
      });
    } else if (date) {
      pushDated(list, '');
    } else {
      pushDocs(list);
    }
    return out;
  }

  /* Collapse filter: heads under a collapsed folder disappear, docs under any
     collapsed head disappear, folder heads always show. isExpanded is a
     predicate over head ids — everything starts collapsed, so grouped mode
     opens as a plain index of folder names / dates. */
  function visibleItems(items, isExpanded) {
    var out = [];
    var inCollapsedFolder = false;
    var inCollapsedDate = false;
    (items || []).forEach(function (it) {
      if (!it) return;
      if (it.kind === 'folder-head') {
        inCollapsedFolder = !isExpanded(it.id);
        inCollapsedDate = false;
        out.push(it);
      } else if (it.kind === 'date-head') {
        if (inCollapsedFolder) return;
        inCollapsedDate = !isExpanded(it.id);
        out.push(it);
      } else {
        if (inCollapsedFolder || inCollapsedDate) return;
        out.push(it);
      }
    });
    return out;
  }

  /* Docs in the order the list paints them — the index space for the
     keyboard selection. */
  function docsOf(items) {
    var out = [];
    (items || []).forEach(function (it) {
      if (it && it.kind === 'doc') out.push(it.doc);
    });
    return out;
  }

  /* Every doc in painted order paired with the head ids that hide it while
     collapsed — [] flat, [dateId] under Date, [folderId, dateId] under both.
     Same scoping visibleItems applies (a folder head opens a fresh date scope),
     so expanding an entry's heads is exactly the inverse of that collapse
     filter: it is what lets the arrow keys step into a group that isn't
     showing yet. Parallel to docsOf(items). */
  function docEntriesOf(items) {
    var out = [];
    var folderId = null;
    var dateId = null;
    (items || []).forEach(function (it) {
      if (!it) return;
      if (it.kind === 'folder-head') { folderId = it.id; dateId = null; }
      else if (it.kind === 'date-head') { dateId = it.id; }
      else {
        var heads = [];
        if (folderId) heads.push(folderId);
        if (dateId) heads.push(dateId);
        out.push({ doc: it.doc, heads: heads });
      }
    });
    return out;
  }

  /* Where an arrow key lands, as an index into docEntriesOf's full order.
     `cur` is the currently selected doc's index there, or -1 when nothing is
     selected at all (every head collapsed, so no row is showing); `delta` is
     ±1. Returns -1 when the move would run off either end — the caller leaves
     the selection alone. Entering from nothing lands on the first doc whichever
     key was pressed: with no cursor on screen there is no "previous" to go
     back to. */
  function navTarget(entries, cur, delta) {
    var n = (entries || []).length;
    if (!n) return -1;
    if (cur < 0) return 0;
    var next = cur + delta;
    return next >= 0 && next < n ? next : -1;
  }

  /* The head the cursor belongs to: index of the innermost head at or above
     `idx` in painted order (a head is its own answer), -1 above the first one.
     Works on `visible` rather than `items` because what ← should collapse is
     the group you can see yourself inside. */
  function headAbove(visible, idx) {
    var list = visible || [];
    for (var i = Math.min(idx, list.length - 1); i >= 0; i--) {
      if (list[i] && list[i].kind !== 'doc') return i;
    }
    return -1;
  }

  /* Group-to-group motion: index of the next (dir > 0) or previous head in
     painted order, -1 when there is none that way. From no selection at all
     (idx < 0) both directions enter at the first head. Deliberately blind to
     expansion state — this moves the cursor, it doesn't open anything. */
  function nextHead(visible, idx, dir) {
    var list = visible || [];
    var i;
    if (idx < 0) {
      for (i = 0; i < list.length; i++) if (list[i] && list[i].kind !== 'doc') return i;
      return -1;
    }
    if (dir > 0) {
      for (i = idx + 1; i < list.length; i++) if (list[i] && list[i].kind !== 'doc') return i;
    } else {
      for (i = Math.min(idx, list.length) - 1; i >= 0; i--) if (list[i] && list[i].kind !== 'doc') return i;
    }
    return -1;
  }

  /* First doc under a head, as an index into docEntriesOf's full order (-1 if
     the head has none — buildList never emits such a head, but callers get a
     safe answer anyway). Docs under a head are contiguous, so this one number
     anchors ↑/↓ when the cursor is sitting on a head: step forward from
     firstDocUnder - 1 to enter the group, back from firstDocUnder to leave it. */
  function firstDocUnder(entries, headId) {
    var list = entries || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i] && list[i].heads.indexOf(headId) !== -1) return i;
    }
    return -1;
  }

  /* Path-form /view URL, so relative links inside a runbook
     (href="sibling.html") resolve against the doc's own directory. */
  function hrefFor(path) {
    if (typeof path !== 'string' || !path) return null;
    return '/view' + path.split('/').map(encodeURIComponent).join('/');
  }

  /* A real anchor: same-tab on plain click, new tab on Cmd/Ctrl-click or
     middle-click, focusable for keyboard nav — all from the browser, with no
     click handler and no target="_blank".
     opts.pathText overrides the sub-line (flat mode shows the full path there;
     grouped mode defaults to the bare filename, the folder being the heading). */
  function createRow(doc, d, opts) {
    var selected = !!(opts && opts.selected);
    var now = opts && typeof opts.now === 'number' ? opts.now : undefined;
    var pathText = opts && typeof opts.pathText === 'string' ? opts.pathText : '';

    var a = doc.createElement('a');
    a.className = 'row' + (selected ? ' sel' : '');
    var href = hrefFor(d.path);
    if (href) a.setAttribute('href', href);

    var txt = doc.createElement('div');
    txt.className = 'txt';
    var title = doc.createElement('div');
    title.className = 'title';
    title.textContent = titleOf(d.path);
    var file = doc.createElement('div');
    file.className = 'path';
    file.textContent = pathText || fileOf(d.path);
    txt.appendChild(title);
    txt.appendChild(file);
    a.appendChild(txt);

    if (selected) {
      var hint = doc.createElement('span');
      hint.className = 'hint';
      hint.textContent = '↩ open';
      a.appendChild(hint);
    }

    var chip = doc.createElement('span');
    chip.className = 'chip ' + (isRecent(d.mtime, now) ? 'green' : 'amber');
    chip.textContent = stampOf(d.mtime);
    a.appendChild(chip);

    return a;
  }

  return {
    titleOf: titleOf,
    fileOf: fileOf,
    dayKeyOf: dayKeyOf,
    dayLabelOf: dayLabelOf,
    stampOf: stampOf,
    isRecent: isRecent,
    folderKeyOf: folderKeyOf,
    folderLabelOf: folderLabelOf,
    folderLabelParts: folderLabelParts,
    buildList: buildList,
    visibleItems: visibleItems,
    docsOf: docsOf,
    docEntriesOf: docEntriesOf,
    navTarget: navTarget,
    headAbove: headAbove,
    nextHead: nextHead,
    firstDocUnder: firstDocUnder,
    hrefFor: hrefFor,
    createRow: createRow
  };
});
