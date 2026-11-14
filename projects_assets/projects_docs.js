/* ═══════════════════════════════════════════════════════════════════════════
   Projects module — Budget, Files and Notes tabs.
   Served ONLY by the gated route /projects/assets/projects_docs.js (never
   static/). Built against window.PM exactly as documented at the top of
   projects.js; nothing else of that file is used.

   What lives here
     PM.registerTab('budget')   lines by category, sources, history, the
                                on-demand "From linked shows" card
     PM.registerTab('files')    list by folder, upload (button + drop),
                                versions, archive / restore / purge
     PM.registerTab('notes')    list + rich-text editor with autosave,
                                the 409 flow, versions, PDF / Word export
     the file preview DRAWER    shared by Files and the Budget's sources

   Rules kept throughout
   - DOM is built with createElement / textContent. The ONE exception is a
     note's body_html, which the server sanitizes on the way in AND out
     (project_docs.sanitize_note_html). What the editor sends is rebuilt
     from an allow-list here first (cleanInto) — the server stays the
     authority. A file's text (it may be "<script>…") is only ever
     textContent.
   - Nothing of a project goes into a URL path or the console: ids only.
   - No write control is drawn — and every write handler returns at once —
     unless PM.can allows it (view-only members, read-only accounts and
     archived projects have PM.can.edit = false; the Budget needs
     PM.can.budget too).
   - On PM 'state' a tab refetches its own data only while it is the visible
     tab and no dialog is open (PM.dialogOpen()); an open note's text is
     NEVER replaced while it has focus or unsaved changes (the 409 flow
     covers a real conflict).
   ═══════════════════════════════════════════════════════════════════════════ */
(function () {
  'use strict';

  const PM = window.PM;
  if (!PM || typeof PM.registerTab !== 'function' || !document.getElementById('pm-pane-files')) return;

  /* ── Small helpers (this file's own; core's are private to it) ─────────── */

  // h('div', {class, text, onclick, 'aria-label', …}, ...children): strings
  // become TEXT nodes — there is no HTML path.
  function h(tag, props) {
    const n = document.createElement(tag);
    if (props) {
      for (const k of Object.keys(props)) {
        const v = props[k];
        if (v == null || v === false) continue;
        if (k === 'class') n.className = v;
        else if (k === 'text') n.textContent = v;
        else if (k === 'data') Object.keys(v).forEach(d => { n.dataset[d] = String(v[d]); });
        else if (k.slice(0, 2) === 'on') n.addEventListener(k.slice(2), v);
        else if (k === 'value' || k === 'checked' || k === 'disabled' || k === 'hidden' || k === 'readOnly' ||
                 k === 'type' || k === 'href' || k === 'id' || k === 'title' || k === 'tabIndex' ||
                 k === 'placeholder' || k === 'htmlFor' || k === 'selected' || k === 'rows' || k === 'maxLength' ||
                 k === 'name' || k === 'target' || k === 'rel' || k === 'multiple' || k === 'colSpan' || k === 'src' ||
                 k === 'alt') n[k] = v;
        else n.setAttribute(k, v === true ? '' : String(v));
      }
    }
    for (let i = 2; i < arguments.length; i++) add(n, arguments[i]);
    return n;
  }
  function add(n, c) {
    if (c == null || c === false) return;
    if (Array.isArray(c)) { c.forEach(x => add(n, x)); return; }
    n.appendChild(typeof c === 'object' ? c : document.createTextNode(String(c)));
  }
  const clear = PM.clear;
  const plural = (n, one, many) => n + ' ' + (n === 1 ? one : (many || one + 's'));
  const say = e => PM.toast((e && e.message) || 'Something went wrong.', true);
  const isPhone = () => document.documentElement.classList.contains('mobile-view');
  const canBudgetEdit = () => !!(PM.can.edit && PM.can.budget);

  function busy(btn, promise) {
    if (btn) btn.disabled = true;
    const done = () => { if (btn && btn.isConnected) btn.disabled = false; };
    promise.then(done, done);
    return promise;
  }
  let fieldSeq = 0;
  function field(label, control, opts) {
    opts = opts || {};
    if (!control.id) control.id = 'pm-df-' + (++fieldSeq);
    return h('div', {class: 'field-group' + (opts.required ? ' required' : '')},
             h('label', {class: 'field-label', htmlFor: control.id, text: label}), control,
             opts.hint ? h('p', {class: 'pm-note', text: opts.hint}) : null);
  }
  function select(options, value, props) {
    const s = h('select', Object.assign({class: 'field-input'}, props || {}));
    options.forEach(o => {
      if (o.group) {
        const g = h('optgroup', {label: o.group});
        o.items.forEach(i => g.appendChild(h('option', {value: String(i.value), text: i.label})));
        s.appendChild(g);
      } else s.appendChild(h('option', {value: String(o.value), text: o.label}));
    });
    s.value = value == null ? '' : String(value);
    return s;
  }
  // A server timestamp → "Nov 14, 3:05 PM" in the browser's zone.
  function fmtWhen(ts) {
    const d = typeof parseServerTime === 'function' ? parseServerTime(ts) : null;
    if (!d) return '';
    let hr = d.getHours();
    const ap = hr >= 12 ? 'PM' : 'AM';
    hr = hr % 12 || 12;
    return PM.fmtDate(window.localIsoDate(d)) + ', ' + hr + ':' + String(d.getMinutes()).padStart(2, '0') + ' ' + ap;
  }
  function fmtSize(n) {
    n = +n || 0;
    if (n < 1024) return n + ' B';
    if (n < 1024 * 1024) return (n / 1024).toFixed(n < 10240 ? 1 : 0) + ' KB';
    return (n / 1048576).toFixed(n < 10485760 ? 1 : 0) + ' MB';
  }
  const byWhen = (name, ts) => [name, fmtWhen(ts)].filter(Boolean).join(' · ');

  // Re-render `box` with `fn`, keeping keyboard focus on the "same" control
  // (matched by data-fk): a poll must not throw a keyboard user to <body>.
  function keepFocus(box, fn) {
    const a = document.activeElement;
    const key = (a && box.contains(a) && a.dataset) ? a.dataset.fk : null;
    fn();
    if (key) {
      const again = Array.from(box.querySelectorAll('[data-fk]')).find(n => n.dataset.fk === key);
      if (again) again.focus({preventScroll: true});
    }
  }

  // The app's action menu (window.ToolsMenu), built as DOM. Items come from
  // the provider registered under `provider` when the menu OPENS.
  function menu(provider, data, title) {
    title = title || 'Actions';
    return h('div', {class: 'tools-menu', data: Object.assign({tools: provider}, data || {})},
      h('button', {type: 'button', class: 'btn btn-sm btn-ghost tools-menu-btn pm-d-more', title, 'aria-label': title,
                   'aria-haspopup': 'menu', 'aria-expanded': 'false', data: {fk: provider + ':' + JSON.stringify(data || {})}},
        h('span', {class: 'tools-menu-label', text: '⋯'}), h('span', {class: 'tools-menu-busy', hidden: true})),
      h('div', {class: 'tools-menu-panel', role: 'menu', 'aria-label': title, hidden: true}));
  }
  const regMenu = (name, fn) => { if (window.ToolsMenu) window.ToolsMenu.register(name, fn); };

  // After one of MY writes. `counts` = the write changed what the tab strip
  // counts (a file / note added or removed): take the whole bundle again.
  // Otherwise adopt the rev so the poll does not refetch for my own change.
  let ownWrite = false;
  function wrote(resp, counts) {
    if (counts) return PM.reload().catch(() => {});
    if (resp && typeof resp.rev === 'number') {
      ownWrite = true;
      try { PM.applyWrite({rev: resp.rev}); } finally { ownWrite = false; }
    }
    return Promise.resolve();
  }

  // A tab's answer to PM 'state'. Nothing is redrawn under an open dialog or
  // drawer (the button that opened it must still be there to take the focus
  // back) or under an open action menu: `fn` runs now, or once — however
  // many 'state' events arrived — as soon as they are closed.
  // fn(own): own = the event came from this file's own write (no refetch).
  function onState(fn) {
    let waiting = false;
    const blocked = () => !!((window.ToolsMenu && window.ToolsMenu.isOpen()) || PM.dialogOpen());
    const tick = () => {
      if (blocked()) { setTimeout(tick, 700); return; }
      waiting = false;
      fn(false);
    };
    PM.on('state', () => {
      if (waiting) return;
      if (!blocked()) { fn(ownWrite); return; }
      waiting = true;
      setTimeout(tick, 700);
    });
  }

  const fileUrl = (fid, vid, what) => '/projects/' + PM.pid + '/files/' + (+fid) + '/v/' + (+vid) + '/' + what;
  const httpUrl = v => (typeof v === 'string' && /^https?:\/\/[^\s<>"'\\`]+$/i.test(v)) ? v : '';

  const KINDS = {
    pdf:    {tile: 'PDF',  label: 'PDF document'},
    image:  {tile: 'IMG',  label: 'Image'},
    office: {tile: 'DOC',  label: 'Office document'},
    email:  {tile: 'MAIL', label: 'E-mail'},
    text:   {tile: 'TXT',  label: 'Text file'},
    other:  {tile: 'FILE', label: 'File'},
  };
  function kindTile(kind) {
    const k = KINDS[kind] ? kind : 'other';
    return h('span', {class: 'pm-f-kind pm-f-kind-' + k, role: 'img', 'aria-label': KINDS[k].label, title: KINDS[k].label,
                      text: KINDS[k].tile});
  }

  // Folder names seen in the Files list — the datalist of "Rename / move".
  let knownFolders = [];
  let filesChanged = null;        // set by the Files tab: refresh its list
  let budgetChanged = null;       // set by the Budget tab: refresh its lines

  /* ═══ The file preview drawer (Files tab + Budget sources) ══════════════ */

  // Budget line descriptions for "Used by budget line …" (people with budget
  // rights only; fetched when a drawer needs them).
  function lineNames(ids) {
    if (!PM.can.budget || !ids || !ids.length) return Promise.resolve({});
    return PM.api('GET', '/budget').then(j => {
      const m = {};
      (j.lines || []).forEach(l => { m[l.id] = l.description || l.category || 'Budget line'; });
      return m;
    }, () => ({}));
  }

  // pdf.js into `box`: one placeholder per page, each rendered when it
  // scrolls into view. Returns a function that stops everything.
  // `src` is {url} (a stored PDF) or {data} (a converted preview's bytes).
  function showPdf(box, src, scroller, onFail) {
    let dead = false, task = null, io = null;
    const note = h('p', {class: 'pm-fd-wait', role: 'status', text: 'Loading the pages…'});
    box.appendChild(note);
    (async function () {
      const lib = await window._ensurePdfJs();
      if (dead) return;
      // isEvalSupported:false closes CVE-2024-4367 in the bundled pdf.js 3.x.
      task = lib.getDocument(Object.assign({isEvalSupported: false}, src));
      const pdf = await task.promise;
      if (dead) return;
      const first = await pdf.getPage(1);
      if (dead) return;
      const base = first.getViewport({scale: 1});
      const width = Math.max(240, Math.min(box.clientWidth || 600, 1100));
      const scale = width / base.width;
      note.remove();
      const pages = h('div', {class: 'pm-fd-pages'});
      box.appendChild(pages);
      const done = new Set();
      async function paint(holder) {
        const n = +holder.dataset.page;
        if (done.has(n) || dead) return;
        done.add(n);
        try {
          const page = n === 1 ? first : await pdf.getPage(n);
          if (dead) return;
          const vp = page.getViewport({scale});
          const ratio = Math.min(window.devicePixelRatio || 1, 2);
          const canvas = h('canvas', {'aria-hidden': 'true'});
          canvas.width = Math.floor(vp.width * ratio);
          canvas.height = Math.floor(vp.height * ratio);
          holder.style.height = Math.floor(vp.height) + 'px';
          holder.textContent = '';
          holder.appendChild(canvas);
          await page.render({canvasContext: canvas.getContext('2d'), viewport: vp,
                             transform: ratio !== 1 ? [ratio, 0, 0, ratio, 0, 0] : null}).promise;
        } catch (e) {
          if (!dead) holder.textContent = 'This page could not be drawn.';
        }
      }
      io = new IntersectionObserver(entries => {
        entries.forEach(en => { if (en.isIntersecting) { io.unobserve(en.target); paint(en.target); } });
      }, {root: scroller || null, rootMargin: '600px 0px'});
      for (let n = 1; n <= pdf.numPages; n++) {
        const holder = h('div', {class: 'pm-fd-page', data: {page: n}, role: 'img', 'aria-label': 'Page ' + n + ' of ' + pdf.numPages,
                                 text: 'Page ' + n});
        holder.style.width = Math.floor(base.width * scale) + 'px';
        holder.style.height = Math.floor(base.height * scale) + 'px';
        pages.appendChild(holder);
        io.observe(holder);
      }
      if (pdf.numPages > 1) pages.appendChild(h('p', {class: 'pm-note', text: plural(pdf.numPages, 'page')}));
    })().catch(() => {
      if (dead) return;
      note.remove();
      if (onFail) onFail();
    });
    return function stop() {
      dead = true;
      if (io) io.disconnect();
      if (task && task.destroy) { try { task.destroy(); } catch (e) { /* already gone */ } }
    };
  }

  function renameDialog(file, after) {
    if (!PM.can.edit) return;
    const d = PM.dialog({title: 'Rename / move'});
    const title = h('input', {type: 'text', class: 'field-input', maxLength: 200, autocomplete: 'off', value: file.title || ''});
    const listId = 'pm-folders-' + (++fieldSeq);
    const folder = h('input', {type: 'text', class: 'field-input', maxLength: 120, autocomplete: 'off', value: file.folder || '',
                               placeholder: 'No folder', list: listId});
    const dl = h('datalist', {id: listId}, knownFolders.map(f => h('option', {value: f})));
    const bo = h('input', {type: 'checkbox', checked: !!file.budget_only});
    d.body.append(field('Name', title, {required: true}),
                  field('Folder', folder, {hint: 'Type a new name to make a folder; leave it empty for none.'}), dl);
    if (PM.can.budget) {
      d.body.append(h('label', {class: 'pm-check'}, bo, 'Budget only'),
                    h('p', {class: 'pm-note', text: 'A budget-only file is visible only to people who can see the budget.'}));
    }
    const save = h('button', {type: 'button', class: 'btn btn-primary', text: 'Save'});
    const go = () => {
      const ch = {};
      const t = title.value.trim(), f = folder.value.trim();
      if (!t) { PM.toast('Give the file a name.', true); title.focus(); return; }
      if (t !== (file.title || '')) ch.title = t;
      if (f !== (file.folder || '')) ch.folder = f;
      if (PM.can.budget && bo.checked !== !!file.budget_only) ch.budget_only = bo.checked;
      if (!Object.keys(ch).length) { d.close(); return; }
      busy(save, PM.api('PUT', '/files/' + file.id, ch)).then(resp => {
        d.close();
        PM.toast('✓ Saved');
        wrote(resp);
        if (after) after(resp.file);
      }, say);
    };
    save.addEventListener('click', go);
    [title, folder].forEach(i => i.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } }));
    d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => d.ask()}), save);
  }

  function archiveFile(file) {
    if (!PM.can.edit) return Promise.resolve(false);
    return PM.confirm('Archive “' + file.title + '”? It leaves the list; an editor can restore it from Archived.', 'Archive').then(ok => {
      if (!ok) return false;
      return PM.api('DELETE', '/files/' + file.id).then(resp => { PM.toast('✓ Archived'); return wrote(resp, true).then(() => true); },
                                                       e => { say(e); return false; });
    });
  }
  function restoreFile(file) {
    if (!PM.can.edit) return Promise.resolve(false);
    return PM.api('POST', '/files/' + file.id + '/restore').then(resp => { PM.toast('✓ Restored'); return wrote(resp, true).then(() => true); },
                                                                 e => { say(e); return false; });
  }
  function purgeFile(file) {
    if (!PM.can.manage) return Promise.resolve(false);
    return PM.confirm('Delete “' + file.title + '” and all its versions for good? It cannot be restored.', 'Delete forever').then(ok => {
      if (!ok) return false;
      return PM.api('POST', '/files/' + file.id + '/purge').then(resp => { PM.toast('✓ Deleted for good'); return wrote(resp, true).then(() => true); },
                                                                 e => { say(e); return false; });
    });
  }
  // One upload with its message spelled out (the server's size-limit text;
  // a proxy's bare 413 has no JSON body).
  function uploadOne(fd, onProgress) {
    return PM.upload('/files', fd, onProgress).catch(e => {
      if (e && e.status === 0 && e.http === 413) e.message = 'File too large for this server.';
      else if (e && e.status === -1) e.message = 'The upload did not finish — check your connection (or the file is too large).';
      throw e;
    });
  }

  // An open drawer is never rebuilt on 'state' (rule b) — but its "Already a
  // contact" follows the contact that was just added from it.
  const drawers = new Set();
  PM.on('state', () => {
    drawers.forEach(fn => { try { fn(); } catch (e) { /* the drawer still works */ } });
  });

  // opts: {version?: id (a budget source's pinned version)}
  function openFile(fid, opts) {
    opts = opts || {};
    fid = +fid;
    let alive = true, det = null, seq = 0, autoTried = false, timer = null, stopPdf = null, changed = false;
    let contactSlot = null;
    const d = PM.dialog({title: 'File', drawer: true, noDirtyGuard: true, onClose: () => {
      alive = false;
      clearTimeout(timer);
      if (stopPdf) stopPdf();
      drawers.delete(syncContact);
      if (changed && filesChanged) filesChanged();
      if (changed && budgetChanged) budgetChanged();
    }});
    d.el.classList.add('pm-fd');
    const head = h('div', {class: 'pm-fd-head'});
    const notices = h('div', {class: 'pm-fd-notices'});
    const view = h('div', {class: 'pm-fd-view'});
    const extra = h('div', {class: 'pm-fd-extra'});
    d.body.append(head, notices, view, extra);
    view.appendChild(h('p', {class: 'pm-fd-wait', role: 'status', text: 'Loading…'}));
    const picker = h('input', {type: 'file', hidden: true, 'aria-hidden': 'true', tabIndex: -1});
    d.body.appendChild(picker);
    picker.addEventListener('change', () => {
      const f = picker.files && picker.files[0];
      picker.value = '';
      if (f) newVersion(f);
    });
    drawers.add(syncContact);

    function syncContact() {
      if (!alive || !contactSlot || !det || !det.email) return;
      drawContact();
    }

    async function load(vid) {
      const my = ++seq;
      clearTimeout(timer);
      let j;
      try {
        j = await PM.api('GET', '/files/' + fid + (vid ? '?version=' + (+vid) : ''));
      } catch (e) {
        if (!alive || my !== seq) return;
        if (!det) {
          d.close();
          PM.toast(e.status === 404 ? 'That item no longer exists.' : e.message, true);
        } else say(e);
        return;
      }
      if (!alive || my !== seq) return;
      det = j;
      draw();
    }

    const cur = () => det.versions.find(v => v.id === det.version_id) || det.versions[0];

    function draw() {
      const f = det.file, v = cur();
      d.setTitle(f.title);
      const isCurrent = det.version_id === f.current_version_id;
      const editable = PM.can.edit && !f.archived;

      clear(head);
      head.appendChild(h('div', {class: 'pm-fd-meta'}, kindTile(v.kind),
        h('div', {class: 'pm-fd-meta-main'},
          h('div', {class: 'pm-fd-name', text: v.filename}),
          h('div', {class: 'pm-row-sub', text: [fmtSize(v.size), 'version ' + v.version, byWhen(v.uploaded_by, v.at)].filter(Boolean).join(' · ')})),
        f.budget_only ? h('span', {class: 'pm-badge pm-badge-warn', text: 'Budget only', title: 'Visible only to people who can see the budget'}) : null,
        f.archived ? h('span', {class: 'pm-badge pm-badge-muted', text: 'Archived'}) : null));
      if (det.versions.length > 1) {
        const sel = select(det.versions.map(x => ({value: x.id,
          label: 'Version ' + x.version + (x.id === f.current_version_id ? ' (current)' : '') + ' — ' + byWhen(x.uploaded_by, x.at)})),
          det.version_id, {'aria-label': 'Version shown', class: 'field-input pm-fd-vsel'});
        sel.addEventListener('change', () => { autoTried = false; load(+sel.value); });
        head.appendChild(sel);
      }

      clear(notices);
      if (!isCurrent) {
        notices.appendChild(h('div', {class: 'pm-banner pm-banner-warn', role: 'status'},
          h('span', null, h('strong', {text: 'A newer version exists.'}), ' You are looking at version ' + v.version + '.'),
          h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Open the current version',
                       onclick: () => { autoTried = false; load(f.current_version_id); }})));
      }
      const used = f.used_by_lines || [];
      if (used.length) {
        const row = h('div', {class: 'pm-fd-used'}, h('span', {class: 'pm-section-label', text: 'Used by budget line'}));
        const chips = h('span', {class: 'pm-chips'});
        used.forEach(id => chips.appendChild(h('button', {type: 'button', class: 'pm-chip', data: {line: id}, text: 'Line…',
          onclick: () => { d.close(); PM.setTab('budget', {line: id}); }})));
        row.appendChild(chips);
        notices.appendChild(row);
        lineNames(used).then(m => {
          if (!alive) return;
          Array.from(chips.children).forEach(b => { b.textContent = m[+b.dataset.line] || 'Budget line'; });
        });
      }

      drawView(v);

      clear(extra);
      if (det.versions.length > 1) {
        const list = h('div', {class: 'pm-list'});
        det.versions.forEach(x => {
          list.appendChild(h('div', {class: 'pm-row pm-fd-vrow'},
            h('span', {class: 'pm-row-main'},
              h('span', {class: 'pm-row-title', text: 'Version ' + x.version + (x.id === f.current_version_id ? ' (current)' : '')}),
              h('span', {class: 'pm-row-sub', style: 'display:block', text: [x.filename, fmtSize(x.size), byWhen(x.uploaded_by, x.at)].filter(Boolean).join(' · ')})),
            x.id === det.version_id ? h('span', {class: 'pm-badge', text: 'Shown'})
              : h('button', {type: 'button', class: 'btn btn-xs btn-ghost', text: 'View', onclick: () => { autoTried = false; load(x.id); }}),
            h('a', {class: 'btn btn-xs btn-ghost no-prefetch', href: fileUrl(fid, x.id, 'download'), text: 'Download',
                    'aria-label': 'Download version ' + x.version})));
        });
        extra.append(h('span', {class: 'pm-section-label', text: 'Versions (' + det.versions.length + ')'}), list);
      }

      clear(d.footer);
      const left = h('div', {class: 'pm-foot-left'});
      if (f.archived) {
        if (PM.can.edit) left.appendChild(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Restore',
          onclick: e => busy(e.currentTarget, restoreFile(f)).then(ok => { if (ok) { changed = true; load(); } })}));
        if (PM.can.manage) left.appendChild(h('button', {type: 'button', class: 'btn btn-danger-ghost', text: 'Delete forever',
          onclick: () => purgeFile(f).then(ok => { if (ok) { changed = true; d.close(); } })}));
      } else if (editable) {
        left.appendChild(h('button', {type: 'button', class: 'btn btn-danger-ghost', text: 'Archive',
          onclick: () => archiveFile(f).then(ok => { if (ok) { changed = true; d.close(); } })}));
      }
      if (left.firstChild) d.footer.appendChild(left);
      if (editable) {
        d.footer.append(
          h('button', {type: 'button', class: 'btn btn-ghost', text: 'Rename / move…',
                       onclick: () => renameDialog(f, () => { changed = true; load(det.version_id); })}),
          h('button', {type: 'button', class: 'btn btn-ghost', text: 'Upload new version', onclick: () => picker.click()}));
      }
      d.footer.append(
        h('a', {class: 'btn btn-ghost no-prefetch', href: fileUrl(fid, v.id, 'download'), text: 'Download'}),
        h('button', {type: 'button', class: 'btn btn-primary', text: 'Close', onclick: () => d.close()}));
    }

    function newVersion(file) {
      if (!PM.can.edit) return;
      const fd = new FormData();
      fd.append('file', file);
      fd.append('file_id', String(fid));
      const bar = h('div', {class: 'pm-banner', role: 'status'}, h('span', {text: 'Uploading the new version…'}));
      notices.prepend(bar);
      uploadOne(fd, p => { bar.firstChild.textContent = 'Uploading the new version… ' + Math.round(p * 100) + '%'; }).then(resp => {
        changed = true;
        autoTried = false;
        PM.toast('✓ Version ' + ((resp.file.current && resp.file.current.version) || '') + ' uploaded');
        wrote(resp, true);
        if (alive) load();
      }, e => { bar.remove(); say(e); });
    }

    // "Reading the file…" → one automatic look after 3 s → "Try again".
    // `quiet` = the preview does not depend on the text (a PDF, an office
    // file, an image): only this status line is refreshed, the pages stay.
    function reading(v, box, quiet) {
      const st = det.extract_status;
      if (st !== '' && st !== 'error') return false;
      const row = h('div', {class: quiet ? 'pm-fd-status' : 'pm-fd-wait', role: 'status'});
      const again = () => {
        if (!alive) return;
        if (!quiet) { load(det.version_id); return; }
        const my = seq;
        PM.api('GET', '/files/' + fid + '?version=' + (+v.id)).then(j => {
          if (!alive || my !== seq || !row.isConnected) return;
          det.extract_status = j.extract_status;
          det.text = j.text;
          det.text_more = j.text_more;
          const next = document.createDocumentFragment();
          reading(v, next, true);                 // the next line, or nothing once it is read
          row.replaceWith(next);
        }, () => {});
      };
      if (st === '' && !autoTried) {
        row.appendChild(h('span', {text: quiet ? 'Reading the file’s text for search…' : 'Reading the file…'}));
        autoTried = true;
        timer = setTimeout(again, 3000);
      } else {
        row.appendChild(h('span', {text: st === 'error'
          ? (quiet ? 'The text of this file could not be read, so search will not find it.' : 'The text of this file could not be read.')
          : (quiet ? 'Still reading this file’s text.' : 'Still reading this file.')}));
        row.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Try again', onclick: e => {
          const btn = e.currentTarget;
          // Only editors may re-queue; everyone may look again.
          const ask = (PM.can.edit && !det.file.archived)
            ? PM.api('POST', '/files/' + fid + '/v/' + v.id + '/reextract').then(r => { wrote(r); return r; })
            : Promise.resolve(null);
          busy(btn, ask).then(() => {
            if (!alive) return;
            clear(row).appendChild(h('span', {text: quiet ? 'Reading the file’s text for search…' : 'Reading the file…'}));
            timer = setTimeout(again, 3000);
          }, say);
        }}));
      }
      box.appendChild(row);
      return true;
    }

    function noPreview(box, v, text) {
      box.appendChild(h('div', {class: 'pm-fd-none'},
        h('p', {text: text || 'No preview for this kind of file.'}),
        h('a', {class: 'btn btn-primary btn-sm no-prefetch', href: fileUrl(fid, v.id, 'download'), text: 'Download'})));
    }
    function textBlock(box, label) {
      if (!det.text) return;
      if (label) box.appendChild(h('span', {class: 'pm-section-label', text: label}));
      // textContent ONLY: a file's text may hold markup characters.
      box.appendChild(h('pre', {class: 'pm-fd-text', tabIndex: 0, text: det.text}));
      if (det.text_more) box.appendChild(h('p', {class: 'pm-note', text: 'This is the first part — download the file for the rest.'}));
    }

    function drawView(v) {
      if (stopPdf) { stopPdf(); stopPdf = null; }
      contactSlot = null;
      clear(view);
      const kind = v.kind;
      if (det.email) { drawMail(v); return; }
      if (kind === 'email') {
        if (reading(v, view)) return;
        noPreview(view, v, 'This message can’t be previewed here — the server could not read this Outlook file. Download it to open it in your mail program.');
        return;
      }
      if (kind === 'pdf') {
        stopPdf = showPdf(view, {url: fileUrl(fid, v.id, 'download') + '?inline=1'}, d.body,
                          () => noPreview(view, v, 'This PDF could not be shown here.'));
        reading(v, view, true);
        return;
      }
      if (kind === 'office') {
        const wait = h('p', {class: 'pm-fd-wait', role: 'status', text: 'Preparing preview… this can take a little while the first time.'});
        view.appendChild(wait);
        const my = seq;
        // Ask first (a JSON 404 = no preview) so pdf.js only ever gets a PDF.
        fetch(fileUrl(fid, v.id, 'preview.pdf'), {method: 'GET', credentials: 'same-origin', cache: 'no-store',
                                                   headers: {'X-Requested-With': 'XMLHttpRequest'}})
          .then(r => (r.ok && (r.headers.get('Content-Type') || '').indexOf('application/pdf') === 0) ? r.arrayBuffer() : null, () => null)
          .then(buf => {
            if (!alive || my !== seq) return;
            wait.remove();
            if (!buf) {
              noPreview(view, v, 'No preview is available for this file.');
              textBlock(view, 'Text in this file');
              return;
            }
            stopPdf = showPdf(view, {data: new Uint8Array(buf)}, d.body,
                              () => noPreview(view, v, 'No preview is available for this file.'));
          });
        reading(v, view, true);
        return;
      }
      if (kind === 'image') {
        const img = h('img', {class: 'pm-fd-img', alt: det.file.title, src: fileUrl(fid, v.id, 'download') + '?inline=1'});
        img.addEventListener('error', () => { img.remove(); noPreview(view, v, 'This image can’t be shown here.'); });
        view.appendChild(img);
        return;
      }
      if (kind === 'text') {
        if (det.text) { textBlock(view); return; }
        if (reading(v, view)) return;
        noPreview(view, v, 'This file has no text to show.');
        return;
      }
      if (det.text) { textBlock(view, 'Text in this file'); noPreview(view, v, 'Download the file to open it.'); return; }
      if (reading(v, view)) return;
      noPreview(view, v);
    }

    function drawContact() {
      const m = det.email;
      clear(contactSlot);
      const addr = (m.from_email || '').trim().toLowerCase();
      if (!addr) return;
      const known = PM.state.contacts.some(c => (c.email || '').trim().toLowerCase() === addr);
      if (known) contactSlot.appendChild(h('span', {class: 'pm-badge', text: 'Already a contact'}));
      else if (PM.can.edit) {
        contactSlot.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Add sender as contact',
          onclick: () => PM.newContact({name: m.from_name || m.from_email, email: m.from_email})}));
      }
    }

    function drawMail(v) {
      const m = det.email;
      const dl = h('dl', {class: 'pm-mail-head'});
      const when = m.date ? (fmtWhen(m.date) || m.date) : '';
      [['From', m.from], ['To', m.to], ['Cc', m.cc], ['Date', when], ['Subject', m.subject]].forEach(p => {
        if (!p[1]) return;
        dl.append(h('dt', {text: p[0]}), h('dd', {text: p[1]}));
      });
      contactSlot = h('div', {class: 'pm-mail-contact'});
      view.append(dl, contactSlot);
      drawContact();
      if (det.text) {
        view.appendChild(h('pre', {class: 'pm-fd-text pm-mail-body', tabIndex: 0, text: det.text}));
        if (det.text_more) view.appendChild(h('p', {class: 'pm-note', text: 'This is the first part of the message — download the file for the rest.'}));
      } else {
        view.appendChild(h('p', {class: 'pm-note', text: 'This message has no text.'}));
      }
      const atts = m.attachments || [];
      if (atts.length) {
        const list = h('div', {class: 'pm-list pm-mail-atts'});
        atts.forEach(a => {
          const act = h('span', {class: 'pm-mail-att-act'});
          if (PM.can.edit && !det.file.archived) {
            act.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Save to project', onclick: e => {
              busy(e.currentTarget, PM.api('POST', '/files/' + fid + '/v/' + v.id + '/attachments/' + (+a.index) + '/save')).then(resp => {
                changed = true;
                wrote(resp, true);
                PM.toast('✓ Saved to Files');
                clear(act).append(h('span', {class: 'pm-badge', text: 'Saved'}),
                  h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Open',
                               onclick: () => { d.close(); openFile(resp.file.id); }}));
              }, say);
            }}));
          }
          list.appendChild(h('div', {class: 'pm-row'},
            h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: a.name || 'Attachment'}),
              h('span', {class: 'pm-row-sub', style: 'display:block', text: fmtSize(a.size)})), act));
        });
        view.append(h('span', {class: 'pm-section-label', text: 'Attachments (' + atts.length + ')'}), list);
      }
    }

    load(opts.version || null);
    return d;
  }

  /* ═══ Tab: Budget ═════════════════════════════════════════════════════ */
  PM.registerTab('budget', (function () {
    let pane = null, statsBox = null, tableBox = null, addBtn = null, showsBox = null;
    let data = null, seq = 0, latest = null, target = null, targetTimer = null;
    let shows = null, showsBusy = false, showsErr = '';

    // estimate − actual, as the summary PDF words it. null = nothing to say.
    function variance(est, act) {
      if (est == null || act == null) return null;
      const v = Math.round((est - act) * 100) / 100;
      if (v === 0) return {text: 'On estimate', over: false};
      return v < 0 ? {text: 'Over by ' + PM.money(-v), over: true} : {text: PM.money(v) + ' under', over: false};
    }
    // Every cell carries its column name (phones stack the cells) and an
    // explicit role (restyling a table must not cost it its semantics).
    function varianceCell(v) {
      if (!v) return h('td', {class: 'pm-b-num pm-b-var', role: 'cell', data: {label: 'Variance'}, text: '—'});
      // Never colour alone: the cell SAYS "Over by $X".
      return h('td', {class: 'pm-b-num pm-b-var' + (v.over ? ' is-over' : ''), role: 'cell', data: {label: 'Variance'}},
               v.over ? h('span', {class: 'pm-badge pm-badge-late', text: v.text}) : v.text);
    }
    const num = (v, label) => h('td', {class: 'pm-b-num', role: 'cell', data: label ? {label} : null, text: PM.money(v)});
    const amounts = o => [num(o.estimate, 'Estimate'), num(o.committed, 'Committed'), num(o.actual, 'Actual')];
    const lineById = id => (data && data.lines.find(l => l.id === +id)) || null;

    function mount(el) {
      pane = el;
      statsBox = h('div', {class: 'pm-b-stats'});
      addBtn = h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: '+ Add line', data: {fk: 'add'}, onclick: () => lineDialog(null)});
      tableBox = h('div');
      showsBox = h('div', {class: 'form-section-body'});
      pane.append(statsBox,
        h('div', {class: 'form-section'},
          h('div', {class: 'form-section-header'}, h('span', {text: 'Budget lines'}), h('span', {class: 'pm-h-actions'}, addBtn)),
          tableBox),
        h('div', {class: 'form-section'},
          h('div', {class: 'form-section-header'}, h('span', {text: 'From linked shows'})), showsBox));
      clear(tableBox).appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'}));
      drawShows();
    }

    // A call that a newer one overtook resolves when the NEWEST has drawn.
    function load() {
      if (!pane || !PM.can.budget) return Promise.resolve();
      const my = ++seq;
      latest = (async () => {
        try {
          const j = await PM.api('GET', '/budget');
          if (my !== seq) return latest;
          data = j;
          draw();
        } catch (e) {
          if (my !== seq) return latest;
          if (data) return;                      // keep what is on screen; the next refresh retries
          clear(tableBox).appendChild(h('div', {class: 'pm-empty', role: 'alert'}, e.message,
            h('div', null, h('button', {type: 'button', class: 'btn btn-ghost', text: 'Retry', onclick: load}))));
        }
      })();
      return latest;
    }

    function stat(label, value, sub, over) {
      return h('div', {class: 'pm-b-stat' + (over ? ' is-over' : '')},
        h('span', {class: 'l', text: label}), h('span', {class: 'v', text: value}),
        sub ? h('span', {class: 's', text: sub}) : null);
    }

    function sourceChip(s) {
      const editable = canBudgetEdit();
      const rm = editable ? h('button', {type: 'button', class: 'pm-chip-x', title: 'Remove this source', 'aria-label': 'Remove source', text: '×',
        data: {fk: 'sx' + s.id}, onclick: e => { e.preventDefault(); e.stopPropagation(); removeSource(s); }}) : null;
      const glyph = (ch, label) => h('span', {class: 'pm-b-src-g', role: 'img', 'aria-label': label, title: label, text: ch});
      if (s.kind === 'link') {
        const url = httpUrl(s.url);
        const text = s.label || s.url;
        const inner = [glyph('🔗', 'Link'), h('span', {class: 'pm-chip-text', text})];
        // Only an http(s) address is ever written into an href.
        const a = url ? h('a', {class: 'pm-chip pm-b-src no-prefetch', href: url, target: '_blank', rel: 'noopener noreferrer',
                                title: s.url, data: {fk: 's' + s.id}}, inner)
                      : h('span', {class: 'pm-chip pm-b-src', title: s.url}, inner);
        return rm ? h('span', {class: 'pm-b-src-wrap'}, a, rm) : a;
      }
      if (s.kind === 'note') {
        const b = h('button', {type: 'button', class: 'pm-chip pm-b-src', data: {fk: 's' + s.id}, onclick: () => noteSource(s)},
          glyph('📝', 'Note'), h('span', {class: 'pm-chip-text', text: s.label || s.body.slice(0, 40) || 'Note'}));
        return rm ? h('span', {class: 'pm-b-src-wrap'}, b, rm) : b;
      }
      const mail = s.file_kind === 'email';
      const name = s.file_missing ? (s.file_title ? s.file_title + ' (file removed)' : 'File removed') : (s.file_title || 'File');
      const b = h('button', {type: 'button', class: 'pm-chip pm-b-src' + (s.file_missing ? ' is-missing' : ''), data: {fk: 's' + s.id},
        title: s.file_missing ? 'This file is no longer in the project' : (s.newer_version ? 'Opens the version this number came from — a newer version exists' : 'Open the file'),
        onclick: () => {
          if (s.file_missing) { PM.toast('That file is no longer in the project.', true); return; }
          openFile(s.file_id, {version: s.version_id});
        }},
        glyph(mail ? '✉' : '📄', mail ? 'E-mail' : 'File'), h('span', {class: 'pm-chip-text', text: name}),
        s.newer_version ? h('span', {class: 'pm-badge pm-badge-warn', text: 'Newer version exists'}) : null);
      return rm ? h('span', {class: 'pm-b-src-wrap'}, b, rm) : b;
    }

    function lineRow(l) {
      const editable = canBudgetEdit();
      const task = l.task_id ? PM.taskById(l.task_id) : null;
      const contact = l.contact_id ? PM.contactName(l.contact_id) : '';
      const sub = h('div', {class: 'pm-b-sub'});
      if (contact) sub.appendChild(h('span', {text: contact}));
      if (task) sub.appendChild(h('button', {type: 'button', class: 'pm-link-btn', text: 'Task: ' + task.title, data: {fk: 'lt' + l.id},
                                             onclick: () => PM.openTask(task.id)}));
      if (l.notes) sub.appendChild(h('span', {class: 'pm-b-notes', text: l.notes}));
      const srcs = h('div', {class: 'pm-chips pm-b-srcs'});
      (l.sources || []).forEach(s => srcs.appendChild(sourceChip(s)));
      if (editable) {
        srcs.appendChild(h('button', {type: 'button', class: 'pm-chip pm-b-addsrc', text: '+ Source', data: {fk: 'as' + l.id},
                                      title: 'Say where this number came from', onclick: () => addSource(l)}));
      } else if (!(l.sources || []).length) {
        srcs.appendChild(h('span', {class: 'pm-note', text: 'No sources'}));
      }
      return h('tr', {class: 'pm-b-line' + (target === l.id ? ' is-target' : ''), id: 'pm-b-line-' + l.id, role: 'row'},
        h('td', {class: 'pm-b-main', role: 'cell'},
          h('button', {type: 'button', class: 'pm-b-desc', text: l.description, data: {fk: 'l' + l.id}, onclick: () => lineDialog(l)}),
          sub.firstChild ? sub : null, srcs),
        amounts(l), varianceCell(variance(l.estimate, l.actual)),
        h('td', {class: 'pm-b-act', role: 'cell'}, menu('pm-budget-line', {line: l.id}, 'Line actions')));
    }

    function draw() {
      if (!pane || !data) return;
      addBtn.hidden = !canBudgetEdit();
      const lines = data.lines, t = data.totals;
      const anyActual = lines.some(l => l.actual != null), anyEst = lines.some(l => l.estimate != null);
      const tv = (anyActual && anyEst) ? variance(t.estimate, t.actual) : null;
      keepFocus(pane, () => {
        clear(statsBox).append(
          stat('Estimate', PM.money(t.estimate)), stat('Committed', PM.money(t.committed)), stat('Actual', PM.money(t.actual)),
          stat('Variance', tv ? tv.text : '—', tv ? null : 'Needs an estimate and an actual', tv && tv.over));
        clear(tableBox);
        if (!lines.length) {
          tableBox.appendChild(h('div', {class: 'pm-empty'}, 'No budget lines yet.',
            h('span', {class: 'pm-empty-sub', text: 'A line is one cost: what you expect, what is committed and what was actually paid.'}),
            canBudgetEdit() ? h('div', null, h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Add the first line', data: {fk: 'add1'},
                                                         onclick: () => lineDialog(null)})) : null));
          return;
        }
        const col = (text, cls) => h('th', {scope: 'col', role: 'columnheader', class: cls || null, text});
        const table = h('table', {class: 'data-table pm-b-table pm-b-lines', role: 'table', 'aria-label': 'Budget lines'},
          h('thead', {role: 'rowgroup'}, h('tr', {role: 'row'},
            col('Line'), col('Estimate', 'pm-b-num'), col('Committed', 'pm-b-num'), col('Actual', 'pm-b-num'), col('Variance', 'pm-b-num'),
            h('th', {scope: 'col', role: 'columnheader', class: 'pm-b-act', 'aria-label': 'Actions'}))));
        // Categories in the order their first line appears (the server's order).
        const cats = [], at = {};
        lines.forEach(l => {
          const k = l.category || '';
          if (!(k in at)) { at[k] = cats.length; cats.push({name: k, lines: []}); }
          cats[at[k]].lines.push(l);
        });
        cats.forEach(c => {
          const sum = (data.by_category || []).find(x => (x.category || '') === c.name) || {estimate: 0, committed: 0, actual: 0};
          const cv = (c.lines.some(l => l.actual != null) && c.lines.some(l => l.estimate != null)) ? variance(sum.estimate, sum.actual) : null;
          const body = h('tbody', {role: 'rowgroup'});
          body.appendChild(h('tr', {class: 'pm-b-cat', role: 'row'},
            h('th', {scope: 'rowgroup', role: 'rowheader'}, c.name || 'Uncategorized', h('span', {class: 'pm-count', text: String(c.lines.length)})),
            amounts(sum), varianceCell(cv), h('td', {class: 'pm-b-act', role: 'cell'})));
          c.lines.forEach(l => body.appendChild(lineRow(l)));
          table.appendChild(body);
        });
        table.appendChild(h('tfoot', {role: 'rowgroup'}, h('tr', {class: 'pm-b-total', role: 'row'},
          h('th', {scope: 'row', role: 'rowheader', text: 'Total'}), amounts(t), varianceCell(tv), h('td', {class: 'pm-b-act', role: 'cell'}))));
        // Its own sideways scroll box: the card clips, the table is wide.
        tableBox.appendChild(h('div', {class: 'pm-scroll-x pm-b-scroll', tabIndex: -1}, table));
      });
    }

    function focusTarget() {
      if (!target || !data) return;
      const row = document.getElementById('pm-b-line-' + target);
      if (!row) { PM.toast('That item no longer exists.', true); target = null; return; }
      row.scrollIntoView({block: 'center', behavior: 'auto'});
      clearTimeout(targetTimer);
      const id = target;
      targetTimer = setTimeout(() => {          // the highlight is a pointer, not a state
        if (target !== id) return;
        target = null;
        const r = document.getElementById('pm-b-line-' + id);
        if (r) r.classList.remove('is-target');
      }, 6000);
    }

    regMenu('pm-budget-line', wrap => {
      const l = lineById(wrap.dataset.line);
      if (!l) return [];
      const items = [{label: canBudgetEdit() ? 'Edit…' : 'Details…', onSelect: () => lineDialog(l)}];
      if (canBudgetEdit()) items.push({label: 'Add source…', onSelect: () => addSource(l)});
      items.push({label: 'History…', onSelect: () => historyDialog(l)});
      if (canBudgetEdit()) items.push({separator: true}, {label: 'Delete…', danger: true, onSelect: () => deleteLine(l)});
      return items;
    });

    /* ── Line dialog (add / edit / read) ─────────────────────────────────── */
    function parseMoney(input, label) {
      const raw = input.value.replace(/[$,\s]/g, '').replace(/^−/, '-');
      if (raw === '') return null;
      if (!/^-?(\d+(\.\d{0,2})?|\.\d{1,2})$/.test(raw)) {
        const err = new Error(label + ' is not a valid amount.');
        err.input = input;
        throw err;
      }
      return Math.round(parseFloat(raw) * 100) / 100;
    }
    const moneyIn = v => (v == null ? '' : (+v).toFixed(2));

    function lineDialog(line) {
      const isNew = !line;
      const editable = canBudgetEdit();
      if (isNew && !editable) return;
      const d = PM.dialog({title: isNew ? 'New budget line' : 'Budget line', wide: true, noDirtyGuard: !editable});
      const listId = 'pm-cats-' + (++fieldSeq);
      const cats = Array.from(new Set((data ? data.lines : []).map(l => l.category).filter(Boolean)));
      const mk = (val, max, extra) => h('input', Object.assign({type: 'text', class: 'field-input', maxLength: max, autocomplete: 'off',
                                                                readOnly: !editable, value: val || ''}, extra || {}));
      const f = {
        category: mk(line && line.category, 120, {list: listId, placeholder: 'e.g. Production'}),
        description: mk(line && line.description, 10000),
        estimate: mk(moneyIn(line && line.estimate), 20, {inputmode: 'decimal', placeholder: '0.00', class: 'field-input pm-b-money'}),
        committed: mk(moneyIn(line && line.committed), 20, {inputmode: 'decimal', placeholder: '0.00', class: 'field-input pm-b-money'}),
        actual: mk(moneyIn(line && line.actual), 20, {inputmode: 'decimal', placeholder: '0.00', class: 'field-input pm-b-money'}),
        contact: select([{value: '', label: 'None'}].concat(PM.state.contacts.map(c => ({value: c.id, label: c.name + (c.company ? ' — ' + c.company : '')}))),
                        line && line.contact_id, {disabled: !editable}),
        task: select([{value: '', label: 'None'}].concat(PM.taskGroups().filter(g => g.tasks.length).map(g => ({
                        group: g.name || 'No group', items: g.tasks.map(t => ({value: t.id, label: t.title}))}))),
                     line && line.task_id, {disabled: !editable}),
        notes: h('textarea', {class: 'field-input field-textarea', rows: 3, maxLength: 10000, readOnly: !editable}),
        why: mk('', 4000, {placeholder: 'e.g. Revised quote from the vendor'}),
      };
      f.notes.value = (line && line.notes) || '';
      d.body.append(
        field('Description', f.description, {required: true}),
        h('div', {class: 'form-row cols-2'}, field('Category', f.category), field('Contact', f.contact)),
        h('datalist', {id: listId}, cats.map(c => h('option', {value: c}))),
        h('div', {class: 'form-row cols-3'},
          field('Estimate', f.estimate, {hint: 'What you expect it to cost'}),
          field('Committed', f.committed, {hint: 'Signed or ordered'}),
          field('Actual', f.actual, {hint: 'Paid so far'})),
        field('Task', f.task),
        field('Notes', f.notes));
      if (!isNew && editable) {
        d.body.appendChild(field('Why are you changing it? (optional)', f.why, {hint: 'Kept with the change in this line’s history.'}));
      }
      if (!isNew) {
        const v = variance(line.estimate, line.actual);
        if (v && v.over) d.body.prepend(h('div', {class: 'pm-banner pm-banner-warn', role: 'status'},
          h('span', null, h('strong', {text: v.text + '.'}), ' The actual is above the estimate.')));
      }

      const save = h('button', {type: 'button', class: 'btn btn-primary', text: isNew ? 'Add line' : 'Save'});
      const go = () => {
        if (!canBudgetEdit()) return;
        let v;
        try {
          v = {category: f.category.value.trim(), description: f.description.value.trim(),
               estimate: parseMoney(f.estimate, 'Estimate'), committed: parseMoney(f.committed, 'Committed'),
               actual: parseMoney(f.actual, 'Actual'),
               contact_id: f.contact.value ? +f.contact.value : null, task_id: f.task.value ? +f.task.value : null,
               notes: f.notes.value.trim()};
        } catch (e) { PM.toast(e.message, true); if (e.input) e.input.focus(); return; }
        if (!v.description) { PM.toast('Give the line a description.', true); f.description.focus(); return; }
        let call;
        if (isNew) call = PM.api('POST', '/budget/lines', v);
        else {
          const ch = {};
          Object.keys(v).forEach(k => {
            const old = (k === 'contact_id' || k === 'task_id' || k === 'estimate' || k === 'committed' || k === 'actual')
              ? (line[k] == null ? null : line[k]) : (line[k] || '');
            if (v[k] !== old) ch[k] = v[k];
          });
          if (!Object.keys(ch).length) { d.close(); return; }
          if (f.why.value.trim()) ch.note = f.why.value.trim();
          call = PM.api('PUT', '/budget/lines/' + line.id, ch);
        }
        busy(save, call).then(resp => {
          d.close();
          PM.toast(isNew ? '✓ Line added' : '✓ Saved');
          wrote(resp, isNew);
          load();
        }, say);
      };
      save.addEventListener('click', go);
      ['category', 'description', 'estimate', 'committed', 'actual', 'why'].forEach(k =>
        f[k].addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } }));
      if (!isNew) {
        const left = h('div', {class: 'pm-foot-left'},
          h('button', {type: 'button', class: 'btn btn-ghost', text: 'History', onclick: () => historyDialog(line)}));
        if (editable) left.appendChild(h('button', {type: 'button', class: 'btn btn-danger-ghost', text: 'Delete',
          onclick: () => deleteLine(line).then(ok => { if (ok) d.close(); })}));
        d.footer.appendChild(left);
      }
      d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-ghost', text: editable ? 'Cancel' : 'Close', onclick: () => d.ask()}));
      if (editable) d.footer.appendChild(save);
    }

    function deleteLine(line) {
      if (!canBudgetEdit()) return Promise.resolve(false);
      return PM.confirm('Delete the budget line “' + line.description + '”? Its sources and history go with it. It cannot be restored.', 'Delete').then(ok => {
        if (!ok) return false;
        return PM.api('DELETE', '/budget/lines/' + line.id).then(resp => {
          PM.toast('✓ Line deleted');
          wrote(resp, true);
          load();
          return true;
        }, e => { say(e); return false; });
      });
    }

    /* ── History ─────────────────────────────────────────────────────────── */
    const FIELD_LABEL = {estimate: 'Estimate', committed: 'Committed', actual: 'Actual', category: 'Category',
                         description: 'Description', notes: 'Notes'};
    function historyDialog(line) {
      const d = PM.dialog({title: 'History — ' + line.description, wide: true, noDirtyGuard: true});
      const box = h('div', {class: 'pm-b-hist'}, h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'}));
      d.body.appendChild(box);
      d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-primary', text: 'Close', onclick: () => d.close()}));
      const val = (field_, v) => {
        if (v == null || v === '') return '(empty)';
        if (field_ === 'estimate' || field_ === 'committed' || field_ === 'actual') return PM.money(parseFloat(v));
        return '“' + v + '”';
      };
      PM.api('GET', '/budget/lines/' + line.id + '/history').then(j => {
        clear(box);
        const rows = j.history || [];
        if (!rows.length) {
          box.appendChild(h('p', {class: 'pm-empty', text: 'Nothing has changed since this line was added.'}));
          return;
        }
        rows.forEach(r => {
          box.appendChild(h('div', {class: 'pm-b-hrow'},
            h('div', {class: 'pm-b-hhead'}, h('strong', {text: r.actor}), h('time', {text: fmtWhen(r.at)})),
            h('div', {class: 'pm-b-hchange'}, h('span', {class: 'pm-section-label', text: FIELD_LABEL[r.field] || r.field}),
              h('span', {class: 'old', text: val(r.field, r.old_value)}), h('span', {'aria-label': 'changed to', text: '→'}),
              h('span', {class: 'new', text: val(r.field, r.new_value)})),
            r.note ? h('div', {class: 'pm-b-hwhy'}, h('span', {class: 'pm-section-label', text: 'Why'}), ' ', r.note) : null));
        });
      }, e => { clear(box).appendChild(h('p', {class: 'pm-empty', role: 'alert', text: e.message})); });
    }

    /* ── Sources ─────────────────────────────────────────────────────────── */
    function removeSource(s) {
      if (!canBudgetEdit()) return Promise.resolve(false);
      return PM.confirm(s.kind === 'file' ? 'Remove this source from the line? The file itself stays in Files.' : 'Remove this source from the line?', 'Remove').then(ok => {
        if (!ok) return false;
        return PM.api('DELETE', '/budget/sources/' + s.id).then(resp => { PM.toast('✓ Source removed'); wrote(resp); load(); return true; },
                                                                e => { say(e); return false; });
      });
    }
    function noteSource(s) {
      const d = PM.dialog({title: s.label || 'Source note', noDirtyGuard: true});
      d.body.append(h('div', {class: 'pm-ro-val pm-b-notebody', text: s.body || '(no text)'}),
                    h('p', {class: 'pm-note', text: 'Added by ' + byWhen(s.creator || 'someone', s.at)}));
      if (canBudgetEdit()) {
        d.footer.appendChild(h('div', {class: 'pm-foot-left'}, h('button', {type: 'button', class: 'btn btn-danger-ghost', text: 'Remove',
          onclick: () => removeSource(s).then(ok => { if (ok) d.close(); })})));
      }
      d.footer.appendChild(h('button', {type: 'button', class: 'btn btn-primary', text: 'Close', onclick: () => d.close()}));
    }

    function addSource(line) {
      if (!canBudgetEdit()) return;
      const d = PM.dialog({title: 'Add a source — ' + line.description, wide: true});
      const MODES = [['upload', 'Upload a file'], ['file', 'A project file'], ['note', 'A note'], ['link', 'A link']];
      let mode = 'upload', picked = null, files = null;
      const seg = h('div', {class: 'pm-seg pm-b-modes', role: 'group', 'aria-label': 'Kind of source'});
      const body = h('div', {class: 'pm-b-srcform'});
      const save = h('button', {type: 'button', class: 'btn btn-primary', text: 'Add source'});
      const fileIn = h('input', {type: 'file', class: 'field-input'});
      const prog = h('p', {class: 'pm-note', role: 'status'});
      const label = h('input', {type: 'text', class: 'field-input', maxLength: 120, autocomplete: 'off'});
      const text = h('textarea', {class: 'field-input field-textarea', rows: 5, maxLength: 4000});
      const url = h('input', {type: 'url', class: 'field-input', maxLength: 500, autocomplete: 'off', placeholder: 'https://', inputmode: 'url'});
      const filter = h('input', {type: 'search', class: 'field-input', placeholder: 'Filter files…', 'aria-label': 'Filter files', autocomplete: 'off'});
      const pickList = h('div', {class: 'pm-list pm-b-pick', role: 'listbox', 'aria-label': 'Project files'});

      function drawPick() {
        clear(pickList);
        if (!files) { pickList.appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'})); return; }
        const needle = filter.value.trim().toLowerCase();
        const list = files.filter(x => !needle || (x.title + ' ' + (x.current ? x.current.filename : '') + ' ' + x.folder).toLowerCase().indexOf(needle) >= 0);
        if (!list.length) { pickList.appendChild(h('p', {class: 'pm-empty', text: files.length ? 'No files match.' : 'This project has no files yet.'})); return; }
        list.forEach(x => {
          const on = picked === x.id;
          pickList.appendChild(h('button', {type: 'button', class: 'pm-row' + (on ? ' is-picked' : ''), role: 'option', 'aria-selected': on ? 'true' : 'false',
            onclick: () => { picked = x.id; drawPick(); }},
            kindTile(x.current ? x.current.kind : 'other'),
            h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: x.title}),
              h('span', {class: 'pm-row-sub', style: 'display:block', text: [x.folder, x.current ? 'version ' + x.current.version : ''].filter(Boolean).join(' · ')})),
            x.budget_only ? h('span', {class: 'pm-badge pm-badge-warn', text: 'Budget only'}) : null,
            on ? h('span', {class: 'pm-badge pm-badge-accent', text: 'Picked'}) : null));
        });
      }
      filter.addEventListener('input', drawPick);

      function drawMode() {
        Array.from(seg.children).forEach(b => b.setAttribute('aria-pressed', b.dataset.mode === mode ? 'true' : 'false'));
        clear(body);
        if (mode === 'upload') {
          body.append(field('File', fileIn),
            h('div', {class: 'pm-banner', role: 'note'}, h('span', {text: 'The file will be visible only to people who can see the budget. It is added to Files as “Budget only”.'})),
            prog);
        } else if (mode === 'file') {
          body.append(filter, pickList, h('p', {class: 'pm-note', text: 'The line keeps the version that is current now, even if the file gets a newer one later.'}));
          if (!files) PM.api('GET', '/files').then(j => { files = j.files || []; drawPick(); }, e => { files = []; drawPick(); say(e); });
          drawPick();
        } else if (mode === 'note') {
          body.append(field('Label', label, {hint: 'e.g. Phone quote, 12 Nov'}), field('Note', text));
        } else {
          body.append(field('Label', label, {hint: 'Shown on the chip instead of the address'}), field('Link', url, {required: true}));
        }
        d.focusFirst();
      }
      MODES.forEach(m => seg.appendChild(h('button', {type: 'button', class: 'pm-seg-btn', data: {mode: m[0]}, text: m[1],
        onclick: () => { mode = m[0]; drawMode(); }})));
      d.body.append(seg, body);

      const done = resp => {
        d.close();
        PM.toast('✓ Source added');
        wrote(resp, mode === 'upload');
        load();
      };
      save.addEventListener('click', () => {
        if (!canBudgetEdit()) return;
        if (mode === 'upload') {
          const f0 = fileIn.files && fileIn.files[0];
          if (!f0) { PM.toast('Choose a file to upload.', true); fileIn.focus(); return; }
          const fd = new FormData();
          fd.append('file', f0);
          fd.append('line_id', String(line.id));
          prog.textContent = 'Uploading…';
          busy(save, uploadOne(fd, p => { prog.textContent = 'Uploading… ' + Math.round(p * 100) + '%'; })).then(done, e => { prog.textContent = e.message; say(e); });
        } else if (mode === 'file') {
          if (!picked) { PM.toast('Pick a file.', true); return; }
          busy(save, PM.api('POST', '/budget/lines/' + line.id + '/sources', {kind: 'file', file_id: picked})).then(done, say);
        } else if (mode === 'note') {
          if (!text.value.trim() && !label.value.trim()) { PM.toast('Write the note.', true); text.focus(); return; }
          busy(save, PM.api('POST', '/budget/lines/' + line.id + '/sources', {kind: 'note', label: label.value.trim(), body: text.value.trim()})).then(done, say);
        } else {
          const u = url.value.trim();
          if (!httpUrl(u)) { PM.toast('Links must start with http:// or https://.', true); url.focus(); return; }
          busy(save, PM.api('POST', '/budget/lines/' + line.id + '/sources', {kind: 'link', label: label.value.trim(), url: u})).then(done, say);
        }
      });
      d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => d.ask()}), save);
      drawMode();
    }

    /* ── From linked shows (on demand, never stored) ─────────────────────── */
    const SHOW_COLS = [['assets', 'Assets', 'assets'], ['external', 'External rentals', 'assets'], ['piano', 'Piano', 'piano'],
                       ['labor_estimate', 'Labor estimate', 'labor'], ['labor_actual', 'Labor actual', 'labor']];
    function loadShows() {
      if (showsBusy || !PM.can.budget) return;
      showsBusy = true; showsErr = '';
      drawShows();
      PM.api('GET', '/budget/shows').then(j => { shows = j; }, e => { showsErr = e.message; }).then(() => { showsBusy = false; drawShows(); });
    }
    function drawShows() {
      if (!showsBox) return;
      keepFocus(showsBox, () => {
        clear(showsBox);
        if (showsBusy) { showsBox.appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading the figures…'})); return; }
        if (!shows) {
          const n = PM.state.shows.length;
          add(showsBox, [
            h('p', {class: 'pm-note', text: n ? 'What the linked shows cost, straight from their own pages: assets, external rentals, piano and labor. Nothing is copied into this budget.'
                                              : 'No shows are linked to this project. Link one on the Overview tab to see its costs here.'}),
            showsErr ? h('p', {class: 'pm-note pm-b-err', role: 'alert', text: showsErr}) : null,
            n ? h('div', null, h('button', {type: 'button', class: 'btn btn-ghost btn-sm', data: {fk: 'ls'},
                                            text: 'Show costs from ' + plural(n, 'linked show'), onclick: loadShows})) : null]);
          return;
        }
        const can = shows.can || {};
        const cols = SHOW_COLS.filter(c => can[c[2]]);
        const hidden = ['assets', 'labor', 'piano'].filter(k => !can[k]);
        if (!cols.length) {
          // Everything is hidden from this person: say why, show no numbers.
          showsBox.appendChild(h('p', {class: 'pm-note', text: 'Show costs appear for people with Reports access to assets, labor or piano.'}));
          return;
        }
        if (!shows.shows.length) {
          showsBox.appendChild(h('p', {class: 'pm-note', text: 'No linked shows that you can open.'}));
        } else {
          const cell = (s, c) => {
            const v = s[c[0]];
            if (v == null) {
              const why = c[0] === 'labor_actual' && !s.settled ? 'Not settled' : 'Not available';
              return h('td', {class: 'pm-b-num pm-b-na', text: why});
            }
            return num(v);
          };
          const table = h('table', {class: 'data-table pm-b-table pm-b-shows'},
            h('thead', null, h('tr', null, h('th', {scope: 'col', text: 'Show'}), cols.map(c => h('th', {scope: 'col', class: 'pm-b-num', text: c[1]})))),
            h('tbody', null, shows.shows.map(s => h('tr', null,
              h('th', {scope: 'row'}, h('a', {href: '/shows/' + (+s.show_id), target: '_blank', rel: 'noopener', text: s.name})),
              cols.map(c => cell(s, c))))),
            h('tfoot', null, h('tr', {class: 'pm-b-total'}, h('th', {scope: 'row', text: 'Total'}),
              cols.map(c => num(shows.totals ? shows.totals[c[0]] : null)))));
          showsBox.appendChild(h('div', {class: 'pm-scroll-x pm-b-scroll'}, table));
        }
        if (hidden.length) {
          const names = {assets: 'assets and external rentals', labor: 'labor', piano: 'piano'};
          showsBox.appendChild(h('p', {class: 'pm-note', text: 'Not shown: ' + hidden.map(k => names[k]).join(', ') +
            ' — those figures need Reports access you don’t have.'}));
        }
        showsBox.appendChild(h('div', {class: 'pm-b-showsfoot'},
          h('span', {class: 'pm-note', text: 'Labor actual appears once a show is settled. These figures are not part of the totals above.'}),
          h('button', {type: 'button', class: 'btn btn-ghost btn-xs', text: 'Refresh', data: {fk: 'lsr'}, onclick: loadShows})));
      });
    }

    budgetChanged = () => { if (pane && PM.activeTab === 'budget') load(); };
    onState(own => {
      if (!pane || PM.activeTab !== 'budget' || !PM.can.budget) return;
      draw();                                   // names (contacts, tasks) and my rights may have changed
      if (!shows) drawShows();
      if (!own) load();
    });
    return {
      mount,
      show(params) {
        target = (params && params.line) || null;
        load().then(focusTarget);
      },
      hide() { target = null; },
    };
  })());

  /* ═══ Tab: Files ══════════════════════════════════════════════════════ */
  PM.registerTab('files', (function () {
    let pane = null, listBox = null, queueBox = null, q = null, archBtn = null, upBtn = null, picker = null, verPicker = null;
    let files = null, seq = 0, latest = null, archived = false, verFor = null, dragDepth = 0;
    const queue = [];                 // {name, size, state: wait|up|done|err, pct, msg, file, folder, fileId}
    let pumping = false;

    const fileById = id => (files || []).find(f => f.id === +id) || null;

    function mount(el) {
      pane = el;
      q = h('input', {type: 'search', class: 'field-input', placeholder: 'Filter files…', 'aria-label': 'Filter files', autocomplete: 'off'});
      q.addEventListener('input', draw);
      archBtn = h('button', {type: 'button', class: 'pm-toggle', 'aria-pressed': 'false', text: 'Archived', data: {fk: 'arch'},
                             title: 'Show the files that were archived', onclick: () => { archived = !archived; files = null; draw(); load(); }});
      picker = h('input', {type: 'file', multiple: true, hidden: true, 'aria-hidden': 'true', tabIndex: -1});
      picker.addEventListener('change', () => { enqueue(picker.files, ''); picker.value = ''; });
      verPicker = h('input', {type: 'file', hidden: true, 'aria-hidden': 'true', tabIndex: -1});
      verPicker.addEventListener('change', () => {
        const f = verPicker.files && verPicker.files[0];
        verPicker.value = '';
        if (f && verFor) { queue.push({name: f.name, size: f.size, state: 'wait', pct: 0, msg: '', file: f, folder: '', fileId: verFor}); drawQueue(); pump(); }
        verFor = null;
      });
      upBtn = h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Upload files', data: {fk: 'up'}, onclick: () => { if (PM.can.edit) picker.click(); }});
      queueBox = h('div', {class: 'pm-up-queue', hidden: true, 'aria-live': 'polite'});
      listBox = h('div', {class: 'pm-f-list'});
      pane.append(h('div', {class: 'pm-toolbar'}, q, archBtn, h('div', {class: 'pm-toolbar-right'}, upBtn)), picker, verPicker, queueBox, listBox);
      listBox.appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'}));

      // Drag-and-drop onto the list. Only a drag that carries FILES counts
      // (not a dragged link or text), and only for people who can edit.
      const hasFiles = e => !!(e.dataTransfer && Array.from(e.dataTransfer.types || []).indexOf('Files') >= 0);
      const dropOk = () => PM.can.edit && !archived;
      pane.addEventListener('dragenter', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        dragDepth++;
        if (dropOk()) pane.classList.add('is-drop');
      });
      pane.addEventListener('dragover', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();                       // never let the browser open the dropped file
        e.dataTransfer.dropEffect = dropOk() ? 'copy' : 'none';
        const g = e.target.closest ? e.target.closest('.pm-f-group') : null;
        Array.from(listBox.querySelectorAll('.pm-f-group.is-drop')).forEach(n => { if (n !== g) n.classList.remove('is-drop'); });
        if (g && dropOk()) g.classList.add('is-drop');
      });
      const endDrag = () => {
        dragDepth = 0;
        pane.classList.remove('is-drop');
        Array.from(listBox.querySelectorAll('.pm-f-group.is-drop')).forEach(n => n.classList.remove('is-drop'));
      };
      pane.addEventListener('dragleave', e => { if (hasFiles(e) && --dragDepth <= 0) endDrag(); });
      pane.addEventListener('drop', e => {
        if (!hasFiles(e)) return;
        e.preventDefault();
        const g = e.target.closest ? e.target.closest('.pm-f-group') : null;
        endDrag();
        if (!dropOk()) { PM.toast(archived ? 'Leave the Archived view to upload.' : 'You have view-only access to this project.', true); return; }
        enqueue(e.dataTransfer.files, g ? (g.dataset.folder || '') : '');
      });
    }

    /* ── Upload queue: one at a time, each with its own progress ─────────── */
    function enqueue(list, folder) {
      if (!PM.can.edit) return;
      Array.from(list || []).forEach(f => queue.push({name: f.name, size: f.size, state: 'wait', pct: 0, msg: '', file: f, folder: folder || '', fileId: null}));
      drawQueue();
      pump();
    }
    function drawQueue() {
      queueBox.hidden = !queue.length;
      clear(queueBox);
      if (!queue.length) return;
      const active = queue.some(x => x.state === 'wait' || x.state === 'up');
      queueBox.appendChild(h('div', {class: 'pm-up-head'},
        h('span', {class: 'pm-section-label', text: active ? 'Uploading' : 'Uploads'}),
        active ? null : h('button', {type: 'button', class: 'btn btn-xs btn-ghost', text: 'Clear', onclick: () => { queue.length = 0; drawQueue(); }})));
      queue.forEach(x => {
        const bar = h('span', {class: 'pm-up-bar', role: 'progressbar', 'aria-valuemin': '0', 'aria-valuemax': '100',
                               'aria-valuenow': String(Math.round(x.pct * 100)), 'aria-label': 'Upload progress'}, h('span'));
        bar.firstChild.style.width = Math.round(x.pct * 100) + '%';
        const status = x.state === 'wait' ? 'Waiting…' : x.state === 'up' ? 'Uploading ' + Math.round(x.pct * 100) + '%'
                     : x.state === 'done' ? '✓ Uploaded' : x.msg;
        queueBox.appendChild(h('div', {class: 'pm-up-row is-' + x.state},
          h('span', {class: 'pm-up-name', text: x.name}), h('span', {class: 'pm-up-size', text: fmtSize(x.size)}),
          x.state === 'up' || x.state === 'wait' ? bar : null,
          h('span', {class: 'pm-up-status', role: x.state === 'err' ? 'alert' : null, text: status})));
      });
    }
    async function pump() {
      if (pumping) return;
      pumping = true;
      let any = false;
      for (;;) {
        const x = queue.find(i => i.state === 'wait');
        if (!x) break;
        x.state = 'up';
        drawQueue();
        const fd = new FormData();
        fd.append('file', x.file);
        if (x.fileId) fd.append('file_id', String(x.fileId));
        else if (x.folder) fd.append('folder', x.folder);
        try {
          await uploadOne(fd, p => { x.pct = p; drawQueue(); });
          x.state = 'done'; x.pct = 1;
          any = true;
        } catch (e) {
          x.state = 'err';
          x.msg = e.message;                      // the server's own words ("File too large (max 20 MB).")
        }
        x.file = null;
        drawQueue();
      }
      pumping = false;
      if (any) { await wrote(null, true); load(); }
    }

    // A call that a newer one overtook resolves when the NEWEST has drawn.
    function load() {
      if (!pane) return Promise.resolve();
      const my = ++seq, wantArchived = archived;
      latest = (async () => {
        try {
          const j = await PM.api('GET', '/files' + (wantArchived ? '?archived=1' : ''));
          if (my !== seq) return latest;
          files = j.files || [];
          if (!wantArchived) knownFolders = Array.from(new Set(files.map(f => f.folder).filter(Boolean))).sort((a, b) => a.toLowerCase().localeCompare(b.toLowerCase()));
          draw();
        } catch (e) {
          if (my !== seq) return latest;
          if (wantArchived && e.status === 403) { archived = false; return load(); }   // no longer an editor
          if (files) return;
          clear(listBox).appendChild(h('div', {class: 'pm-empty', role: 'alert'}, e.message,
            h('div', null, h('button', {type: 'button', class: 'btn btn-ghost', text: 'Retry', onclick: load}))));
        }
      })();
      return latest;
    }

    function row(f) {
      const c = f.current || {};
      const opener = h('button', {type: 'button', class: 'pm-f-title', text: f.title, data: {fk: 'f' + f.id}, onclick: () => openFile(f.id)});
      const tags = h('span', {class: 'pm-f-tags'});
      if (f.budget_only) tags.appendChild(h('span', {class: 'pm-badge pm-badge-warn', text: 'Budget only', title: 'Visible only to people who can see the budget'}));
      if (f.versions > 1) tags.appendChild(h('span', {class: 'pm-badge', text: f.versions + ' versions'}));
      if ((f.used_by_lines || []).length) tags.appendChild(h('span', {class: 'pm-badge pm-badge-accent', text: 'Budget source',
        title: 'A budget line cites this file'}));
      if (c.extract_status === 'error') tags.appendChild(h('span', {class: 'pm-badge pm-badge-muted', text: 'Text not read', title: 'Search will not find this file’s text — open it to try again'}));
      const acts = h('span', {class: 'pm-f-acts'});
      if (archived) {
        acts.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Restore', data: {fk: 'fr' + f.id},
          onclick: e => busy(e.currentTarget, restoreFile(f)).then(ok => { if (ok) load(); })}));
        if (PM.can.manage) acts.appendChild(h('button', {type: 'button', class: 'btn btn-sm btn-danger-ghost', text: 'Delete forever', data: {fk: 'fp' + f.id},
          onclick: () => purgeFile(f).then(ok => { if (ok) load(); })}));
      } else {
        acts.appendChild(menu('pm-file-row', {file: f.id}, 'File actions'));
      }
      return h('div', {class: 'pm-f-row', data: {file: f.id}},
        kindTile(c.kind),
        h('div', {class: 'pm-f-main'}, opener,
          (c.filename && c.filename !== f.title) ? h('span', {class: 'pm-row-sub pm-f-fn', text: c.filename}) : null, tags.firstChild ? tags : null),
        h('span', {class: 'pm-f-size', text: fmtSize(c.size)}),
        h('span', {class: 'pm-f-who', text: byWhen(c.uploaded_by, c.at)}),
        acts);
    }

    function draw() {
      if (!pane) return;
      upBtn.hidden = !PM.can.edit || archived;
      archBtn.hidden = !PM.can.edit && !archived;
      archBtn.setAttribute('aria-pressed', archived ? 'true' : 'false');
      if (!files) { clear(listBox).appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'})); return; }
      const needle = q.value.trim().toLowerCase();
      const list = needle ? files.filter(f => (f.title + ' ' + (f.current ? f.current.filename : '') + ' ' + f.folder).toLowerCase().indexOf(needle) >= 0) : files;
      keepFocus(listBox, () => {
        clear(listBox);
        if (archived) {
          listBox.appendChild(h('p', {class: 'rv-note'}, h('span', {class: 'pm-badge pm-badge-muted', text: 'Archived'}),
            ' Files that were archived. Restore one to bring it back' + (PM.can.manage ? ', or delete it for good.' : '; only the owner can delete one for good.')));
        }
        if (!files.length) {
          listBox.appendChild(h('div', {class: 'form-section'}, h('div', {class: 'pm-empty'},
            archived ? 'Nothing is archived.' : 'No files yet.',
            (!archived && PM.can.edit) ? h('span', {class: 'pm-empty-sub', text: 'Upload quotes, drawings, contracts or e-mails (.eml / .msg) — or drop them anywhere on this tab.'}) : null,
            (!archived && PM.can.edit) ? h('div', null, h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Upload files', data: {fk: 'up1'},
                                                                    onclick: () => picker.click()})) : null)));
          return;
        }
        if (!list.length) { listBox.appendChild(h('p', {class: 'pm-empty', text: 'No files match that filter.'})); return; }
        // By folder: no folder first, then folders A→Z (the server's order).
        const groups = [], at = {};
        list.forEach(f => {
          const k = f.folder || '';
          if (!(k in at)) { at[k] = groups.length; groups.push({name: k, files: []}); }
          groups[at[k]].files.push(f);
        });
        groups.sort((a, b) => (a.name === '' ? -1 : b.name === '' ? 1 : a.name.toLowerCase().localeCompare(b.name.toLowerCase())));
        groups.forEach(g => {
          listBox.appendChild(h('div', {class: 'form-section pm-f-group', data: {folder: g.name}},
            h('div', {class: 'form-section-header'},
              h('span', null, g.name || 'No folder', ' ', h('span', {class: 'pm-count', text: String(g.files.length)})),
              h('span', {class: 'pm-f-drophint', 'aria-hidden': 'true', text: g.name ? 'Drop to upload into this folder' : 'Drop to upload'})),
            h('div', {class: 'pm-f-rows'}, g.files.map(row))));
        });
      });
    }

    regMenu('pm-file-row', wrap => {
      const f = fileById(wrap.dataset.file);
      if (!f || !f.current) return [];
      const items = [
        {label: 'Open', onSelect: () => openFile(f.id)},
        {label: 'Download', onSelect: () => { window.location.href = fileUrl(f.id, f.current.id, 'download'); }}];
      if (PM.can.edit) {
        items.push({separator: true},
          {label: 'Upload new version…', onSelect: () => { verFor = f.id; verPicker.click(); }},
          {label: 'Rename / move…', onSelect: () => renameDialog(f, () => load())});
        if (PM.can.budget) {
          items.push({label: f.budget_only ? 'Show to everyone on the project' : 'Make budget only',
            title: f.budget_only ? 'Everyone on the project will see this file' : 'Only people who can see the budget will see this file',
            onSelect: () => PM.api('PUT', '/files/' + f.id, {budget_only: !f.budget_only}).then(resp => {
              PM.toast(f.budget_only ? '✓ Visible to everyone on the project' : '✓ Budget only');
              wrote(resp);
              load();
            }, say)});
        }
        items.push({separator: true}, {label: 'Archive…', danger: true, onSelect: () => archiveFile(f).then(ok => { if (ok) load(); })});
      }
      return items;
    });

    filesChanged = () => { if (pane && PM.activeTab === 'files') load(); };
    onState(own => {
      if (!pane || PM.activeTab !== 'files') return;
      if (archived && !PM.can.edit) archived = false;
      draw();                                   // my rights may have changed
      if (!own) load();
    });
    return {
      mount,
      show(params) {
        const want = (params && params.file) || null;
        load().then(async () => {
          if (!want) return;
          // Look in the lists first: a link to a file that is gone (or that
          // I may not see) should say so, not ask the server for it.
          let there = !files || !!fileById(want);
          if (!there && (archived || PM.can.edit)) {
            try {
              const j = await PM.api('GET', '/files' + (archived ? '' : '?archived=1'));
              there = (j.files || []).some(f => f.id === want);
            } catch (e) { there = false; }
          }
          if (there) openFile(want); else PM.toast('That item no longer exists.', true);
        });
      },
    };
  })());

  /* ═══ Note HTML: rebuilt from the server's allow-list ══════════════════ */
  // The same list project_docs.sanitize_note_html writes from. What the
  // editor sends, and everything pasted or dropped into it, is REBUILT here
  // with createElement — no attribute survives except a link's own three,
  // and nothing is copied as markup. The server sanitizes again and is the
  // authority; this keeps what the person sees equal to what gets stored.
  const N_ALLOWED = new Set(['p', 'br', 'h2', 'h3', 'h4', 'strong', 'b', 'em', 'i', 'u', 's', 'ul', 'ol', 'li', 'blockquote',
                             'code', 'pre', 'hr', 'a', 'table', 'thead', 'tbody', 'tr', 'th', 'td']);
  const N_MAP = {h1: 'h2', h5: 'h4', h6: 'h4', strike: 's', del: 's', tfoot: 'tbody'};
  // Dropped WITH their content (scripts, styles, frames, widgets, pictures).
  const N_DROP = new Set(['script', 'style', 'iframe', 'object', 'embed', 'applet', 'svg', 'math', 'noscript', 'noembed', 'noframes',
                          'template', 'title', 'xmp', 'plaintext', 'audio', 'video', 'canvas', 'frameset', 'select', 'datalist',
                          'img', 'picture', 'source', 'track', 'map', 'area', 'meta', 'link', 'base', 'head', 'input', 'button',
                          'textarea', 'option', 'optgroup']);
  const N_BLOCKISH = new Set(['div', 'section', 'article', 'header', 'footer', 'main', 'nav', 'aside', 'dl', 'dt', 'dd', 'figure',
                              'figcaption', 'address', 'center', 'fieldset', 'form', 'details', 'summary', 'caption', 'legend']);
  const N_BLOCK_SEL = 'p,h1,h2,h3,h4,h5,h6,ul,ol,li,blockquote,pre,hr,table,div,section,article,header,footer,main,nav,aside,dl,figure,address,center,fieldset,form,details';
  const N_INLINE = new Set(['strong', 'b', 'em', 'i', 'u', 's', 'code', 'a', 'br']);
  const XHTML = 'http://www.w3.org/1999/xhtml';
  const HREF_STRIP = /[\u0000-\u0020\u007f-\u00a0\u1680\u180e\u2000-\u200f\u2028-\u202f\u205f-\u206f\u3000\ufeff]/g;

  // http / https / mailto with something after it — or ''. (The server's rule.)
  function safeHref(v) {
    const s = String(v || '').replace(HREF_STRIP, '');
    if (!s || s.length > 2000) return '';
    const m = /^(https?:\/\/|mailto:)([\s\S]+)$/i.exec(s);
    if (!m) return '';
    if (m[1].toLowerCase() !== 'mailto:' && /^[\/\\?#]/.test(m[2])) return '';
    return m[1].toLowerCase() + m[2];
  }
  // What a person typed in the link box → a link we accept, or ''.
  function typedUrl(v) {
    let s = String(v || '').trim();
    if (!s) return '';
    if (!/^[a-z][a-z0-9+.-]*:/i.test(s)) {
      if (/^[^\s@\/]+@[^\s@\/]+\.[^\s@\/]+$/.test(s)) s = 'mailto:' + s;
      else if (/^[\w-]+(\.[\w-]+)+([\/?#]\S*)?$/.test(s)) s = 'https://' + s;
    }
    return /\s/.test(s) ? '' : safeHref(s);
  }
  const holdsP = out => out.nodeType === 11 || /^(DIV|BLOCKQUOTE|TD|TH|LI)$/.test(out.tagName);
  const isInlineNode = n => n.nodeType === 3 || (n.nodeType === 1 && N_INLINE.has(n.localName));

  function cleanInto(src, out, st) {
    for (let n = src.firstChild; n; n = n.nextSibling) {
      if (++st.count > 30000) { st.over = true; return; }
      if (n.nodeType === 3) {
        const t = n.nodeValue.replace(/\u0000/g, '');
        if (t) out.appendChild(document.createTextNode(t));
        continue;
      }
      if (n.nodeType !== 1 || n.namespaceURI !== XHTML) continue;      // comments, foreign content
      let tag = n.localName.toLowerCase();
      if (N_DROP.has(tag)) continue;
      if (N_MAP[tag]) tag = N_MAP[tag];
      // Google Docs wraps a whole clipboard in <b style="font-weight:normal">.
      if ((tag === 'b' || tag === 'strong') && /font-weight\s*:\s*(normal|[1-4]00)/i.test(n.getAttribute('style') || '')) tag = 'span';
      if (N_ALLOWED.has(tag) && st.depth < 60) {
        if (tag === 'br' || tag === 'hr') { out.appendChild(document.createElement(tag)); continue; }
        if (tag === 'a') {
          const href = safeHref(n.getAttribute('href'));
          if (!href || st.inA) { cleanInto(n, out, st); continue; }   // no safe target: the text is kept
          const a = document.createElement('a');
          a.setAttribute('href', href);
          a.setAttribute('rel', 'noopener noreferrer');
          a.setAttribute('target', '_blank');
          st.inA++; st.depth++;
          cleanInto(n, a, st);
          st.depth--; st.inA--;
          if (a.firstChild) out.appendChild(a);
          continue;
        }
        const el = document.createElement(tag);                         // no attribute is ever copied
        st.depth++;
        cleanInto(n, el, st);
        st.depth--;
        out.appendChild(el);
        continue;
      }
      if (N_BLOCKISH.has(tag)) {
        // A browser's "one <div> per line" becomes a paragraph.
        if (holdsP(out) && !n.querySelector(N_BLOCK_SEL)) {
          const p = document.createElement('p');
          st.depth++;
          cleanInto(n, p, st);
          st.depth--;
          if (p.firstChild) out.appendChild(p);
        } else {
          cleanInto(n, out, st);
          if (n.nextSibling && out.lastChild && isInlineNode(out.lastChild) && out.lastChild.localName !== 'br') {
            out.appendChild(document.createElement('br'));              // keep the lines apart
          }
        }
        continue;
      }
      cleanInto(n, out, st);                                            // span, font, o:p …: text kept
    }
  }
  // Loose text at the top level goes into paragraphs (a whole note only).
  function wrapTop(root) {
    let run = null;
    Array.from(root.childNodes).forEach(n => {
      if (isInlineNode(n)) {
        if (n.nodeType === 3 && !n.nodeValue.trim() && !run) { n.remove(); return; }
        if (!run) { run = document.createElement('p'); root.insertBefore(run, n); }
        run.appendChild(n);
      } else run = null;
    });
  }
  // A browser's list / quote command leaves the new block INSIDE the
  // paragraph it was made from (<p><ul>…</ul></p>) — markup a parser reads
  // differently the next time. Split the paragraph around the block (nodes
  // are moved, never re-created, so the caret stays where it is).
  const N_IN_P = ['ul', 'ol', 'blockquote', 'table', 'h2', 'h3', 'h4', 'pre', 'hr', 'p'].map(t => 'p > ' + t).join(',');
  function liftBlocks(root) {
    const filled = p => !!(p.textContent.trim() || p.querySelector('br,hr,table,ul,ol,blockquote,pre,h2,h3,h4,p,a'));
    for (let guard = 0; guard < 2000; guard++) {
      const el = root.querySelector(N_IN_P);
      if (!el) return;
      const p = el.parentNode;
      const rest = document.createElement('p');
      while (el.nextSibling) rest.appendChild(el.nextSibling);
      p.after(el);
      if (filled(rest)) el.after(rest);
      if (!filled(p)) p.remove();
    }
  }
  // The editor's content as allow-listed HTML ('' for an empty note).
  function noteHtml(body) {
    const tmp = document.createElement('div');
    cleanInto(body, tmp, {count: 0, depth: 0, inA: 0});
    wrapTop(tmp);
    liftBlocks(tmp);
    if (!tmp.textContent.trim() && !tmp.querySelector('hr,table')) return '';
    return tmp.innerHTML;                      // serializing nodes this file built — nothing is parsed
  }
  // Clipboard / dropped content → allow-listed HTML (or null: use the text).
  function clipHtml(html) {
    if (!html) return null;
    let doc;
    try { doc = new DOMParser().parseFromString(html, 'text/html'); } catch (e) { return null; }   // inert: runs nothing, loads nothing
    const tmp = document.createElement('div');
    const st = {count: 0, depth: 0, inA: 0};
    cleanInto(doc.body, tmp, st);
    if (st.over || !tmp.firstChild) return null;
    return tmp.innerHTML;
  }
  function textAsHtml(text) {
    const tmp = document.createElement('div');
    String(text || '').replace(/\r\n?/g, '\n').split('\n').forEach(line => {
      const p = document.createElement('p');
      if (line) p.textContent = line; else p.appendChild(document.createElement('br'));
      tmp.appendChild(p);
    });
    return tmp.innerHTML;
  }
  // The LIVE editor after a paste / a browser command: drop every attribute
  // and element the allow-list does not have, in place (the caret keeps its
  // text nodes).
  function scrubLive(body) {
    Array.from(body.querySelectorAll('*')).forEach(el => {
      if (!el.isConnected) return;
      const tag = el.localName;
      if (el.namespaceURI !== XHTML || N_DROP.has(tag)) { el.remove(); return; }
      const want = N_MAP[tag] || tag;
      if (!N_ALLOWED.has(want) || want !== tag) {
        let into = null;
        if (N_ALLOWED.has(want)) into = document.createElement(want);
        else if (N_BLOCKISH.has(tag) && holdsP(el.parentNode) && !el.querySelector(N_BLOCK_SEL)) into = document.createElement('p');
        if (into) { while (el.firstChild) into.appendChild(el.firstChild); el.replaceWith(into); }
        else el.replaceWith(...Array.from(el.childNodes));
        return;
      }
      const href = tag === 'a' ? safeHref(el.getAttribute('href')) : '';
      if (tag === 'a' && !href) { el.replaceWith(...Array.from(el.childNodes)); return; }
      Array.from(el.attributes).forEach(a => el.removeAttribute(a.name));
      if (tag === 'a') {
        el.setAttribute('href', href);
        el.setAttribute('rel', 'noopener noreferrer');
        el.setAttribute('target', '_blank');
      }
    });
    liftBlocks(body);
  }

  /* ═══ Tab: Notes ═══════════════════════════════════════════════════════ */
  PM.registerTab('notes', (function () {
    let pane = null, wrap = null, listBox = null, edBox = null, newBtn = null;
    let notes = null, listSeq = 0, listLatest = null, openSeq = 0;
    // The open note: {id, rev, baseTitle, baseHtml, editable, dirty, saving,
    //   again, timer, conflict, gone, error, tooLong, behind, updated_name, updated_at}
    let cur = null;
    let titleIn = null, body = null, statusEl = null, metaEl = null, bannerBox = null, toolBox = null, tableTools = null;
    let savedRange = null;
    const toolBtns = {};

    function mount(el) {
      pane = el;
      newBtn = h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: '+ New note', data: {fk: 'new'}, onclick: newNote});
      listBox = h('div', {class: 'pm-n-items'});
      edBox = h('div', {class: 'form-section pm-n-editor'});
      wrap = h('div', {class: 'pm-notes'},
        h('div', {class: 'form-section pm-n-list'},
          h('div', {class: 'form-section-header'}, h('span', {text: 'Notes'}), h('span', {class: 'pm-h-actions'}, newBtn)),
          listBox),
        edBox);
      pane.appendChild(wrap);
      listBox.appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'}));
      drawEmptyEditor();
    }

    /* ── The list ────────────────────────────────────────────────────────── */
    // A call that a newer one overtook resolves when the NEWEST has drawn.
    function loadList() {
      if (!pane) return Promise.resolve();
      const my = ++listSeq;
      listLatest = loadListNow(my);
      return listLatest;
    }
    async function loadListNow(my) {
      let j;
      try {
        j = await PM.api('GET', '/notes');
      } catch (e) {
        if (my !== listSeq) return listLatest;
        if (notes) return;
        clear(listBox).appendChild(h('div', {class: 'pm-empty', role: 'alert'}, e.message,
          h('div', null, h('button', {type: 'button', class: 'btn btn-ghost', text: 'Retry', onclick: loadList}))));
        return;
      }
      if (my !== listSeq) return listLatest;
      notes = j.notes || [];
      drawList();
      const n = cur;
      if (!n) { if (!body) drawEmptyEditor(); return; }
      const row = notes.find(x => x.id === n.id);
      if (!row) {
        // Someone deleted the open note.
        if (stillDirty()) { n.gone = true; setStatus(); drawBanner(); }
        else { cur = null; drawEmptyEditor(); drawList(); PM.toast('That note was deleted.', true); }
        return;
      }
      if (row.rev > n.rev && !n.saving && !n.conflict) {
        // Someone else saved it. Never under the person's hands: only when
        // the note has no unsaved change AND no focus; otherwise it waits
        // (a save from here would get the 409 choice).
        if (!stillDirty() && !hasFocus()) refreshOpen();
        else if (!n.behind) { n.behind = row.updated_name || 'Someone'; setMeta(); }
      }
    }
    function drawList() {
      if (!notes) return;
      newBtn.hidden = !PM.can.edit;
      keepFocus(listBox, () => {
        clear(listBox);
        if (!notes.length) {
          listBox.appendChild(h('div', {class: 'pm-empty'}, 'No notes yet.',
            h('span', {class: 'pm-empty-sub', text: 'Meeting notes, site visits, decisions — written here, kept with the project.'}),
            PM.can.edit ? h('div', null, h('button', {type: 'button', class: 'btn btn-primary btn-sm', text: 'Write the first note', data: {fk: 'new1'}, onclick: newNote})) : null));
          return;
        }
        notes.forEach(n => {
          const on = !!(cur && cur.id === n.id);
          listBox.appendChild(h('button', {type: 'button', class: 'pm-n-item' + (on ? ' is-on' : ''), 'aria-current': on ? 'true' : null,
                                           data: {fk: 'n' + n.id, note: n.id}, onclick: () => openNote(n.id)},
            h('span', {class: 't', text: n.title || 'Untitled'}),
            h('span', {class: 's', text: byWhen(n.updated_name, n.updated_at)})));
        });
      });
    }
    function patchList(head) {
      if (!notes) return;
      const row = notes.find(x => x.id === head.id);
      if (row) Object.assign(row, head); else notes.unshift(head);
      drawList();
    }

    /* ── Editor shell ────────────────────────────────────────────────────── */
    function drawEmptyEditor() {
      wrap.classList.remove('is-open');
      titleIn = body = statusEl = metaEl = bannerBox = toolBox = tableTools = null;
      clear(edBox).appendChild(h('div', {class: 'pm-empty pm-n-none'},
        !notes ? '' : notes.length ? 'Pick a note from the list.' : 'A note you write opens here.'));
    }
    const hasFocus = () => !!(cur && edBox.contains(document.activeElement) &&
                              (document.activeElement === body || document.activeElement === titleIn));
    function stillDirty() {
      const n = cur;
      if (!n || !n.editable || !body || !titleIn) return false;
      return (titleIn.value.trim() || 'Untitled') !== n.baseTitle || noteHtml(body) !== n.baseHtml;
    }
    function setStatus() {
      const n = cur;
      if (!n || !statusEl) return;
      let text = 'Saved', cls = '';
      if (!n.editable) text = '';
      else if (n.conflict) { text = 'Not saved — changed by someone else'; cls = 'is-bad'; }
      else if (n.gone) { text = 'Not saved — the note was deleted'; cls = 'is-bad'; }
      else if (n.saving) { text = 'Saving…'; cls = 'is-busy'; }
      else if (n.error) { text = 'Not saved'; cls = 'is-bad'; }
      else if (n.dirty) { text = 'Not saved yet'; cls = 'is-busy'; }
      statusEl.textContent = text;
      statusEl.className = 'pm-n-status ' + cls;
    }
    function setMeta() {
      const n = cur;
      if (!n || !metaEl) return;
      clear(metaEl);
      metaEl.appendChild(h('span', {text: 'Last edited by ' + byWhen(n.updated_name || 'someone', n.updated_at)}));
      if (n.behind) metaEl.appendChild(h('span', {class: 'pm-n-behind', role: 'status',
        text: n.behind + ' has saved a newer text — it loads when you click away from the note.'}));
    }
    function drawBanner() {
      const n = cur;
      if (!n || !bannerBox) return;
      clear(bannerBox);
      const keep = () => h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Keep mine as a copy', onclick: e => busy(e.currentTarget, keepCopy())});
      if (n.conflict) {
        bannerBox.appendChild(h('div', {class: 'pm-banner pm-banner-warn', role: 'alert'},
          h('span', null, h('strong', {text: (n.conflict.updated_name || 'Someone else') + ' changed this note'}),
            ' ' + (fmtWhen(n.conflict.updated_at) ? 'at ' + fmtWhen(n.conflict.updated_at) : '') + '. Your changes are not saved — choose which text to keep.'),
          h('button', {type: 'button', class: 'btn btn-sm btn-primary', text: 'Load theirs', onclick: e => busy(e.currentTarget, loadTheirs())}),
          keep()));
      } else if (n.gone) {
        bannerBox.appendChild(h('div', {class: 'pm-banner pm-banner-warn', role: 'alert'},
          h('span', null, h('strong', {text: 'This note was deleted.'}), ' Your text is still here.'),
          keep(),
          h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Close it', onclick: () => { cur = null; drawEmptyEditor(); drawList(); }})));
      } else if (n.error) {
        bannerBox.appendChild(h('div', {class: 'pm-banner pm-banner-warn', role: 'alert'},
          h('span', null, h('strong', {text: n.tooLong ? 'This note is too long.' : 'Not saved.'}),
            n.tooLong ? ' Shorten it, or move part of it into another note — your text is still here and will be saved once it fits.' : ' ' + n.error),
          n.tooLong ? null : h('button', {type: 'button', class: 'btn btn-sm btn-ghost', text: 'Try again', onclick: () => save()})));
      }
    }

    /* ── Autosave ────────────────────────────────────────────────────────── */
    function changed() {
      const n = cur;
      if (!n || !n.editable) return;
      if (body) body.classList.toggle('is-empty', !body.textContent.trim() && !body.querySelector('hr,table,li'));
      n.dirty = true;
      setStatus();
      clearTimeout(n.timer);
      if (!n.conflict && !n.gone) n.timer = setTimeout(save, 1500);
    }
    function pending(n) {
      const title = titleIn.value.trim() || 'Untitled', html = noteHtml(body), ch = {};
      if (title !== n.baseTitle) ch.title = title;
      if (html !== n.baseHtml) ch.body_html = html;
      return {ch, title, html};
    }
    function save() {
      const n = cur;
      if (!n || !n.editable || n.conflict || n.gone || !PM.can.edit) return Promise.resolve();
      clearTimeout(n.timer);
      n.timer = null;
      if (n.saving) { n.again = true; return n.saving; }
      const p = pending(n);
      if (!Object.keys(p.ch).length) {
        n.dirty = false; n.error = ''; n.tooLong = false;
        setStatus(); drawBanner();
        return Promise.resolve();
      }
      p.ch.base_rev = n.rev;
      n.saving = PM.api('PUT', '/notes/' + n.id, p.ch).then(resp => {
        // Adopt the rev and remember what was SENT. The editor is never
        // replaced by its own save.
        n.rev = resp.note.rev;
        n.baseTitle = p.title;
        n.baseHtml = p.html;
        n.updated_name = resp.note.updated_name;
        n.updated_at = resp.note.updated_at;
        n.error = ''; n.tooLong = false; n.retries = 0;
        patchList(resp.note);
        if (typeof resp.rev === 'number') wrote(resp);          // a rename moved the project rev
      }, e => {
        if (e.status === 409 && e.data && e.data.current) n.conflict = e.data.current;
        else if (e.status === 404) n.gone = true;
        else {
          n.error = e.message;
          n.tooLong = e.status === 413;
          // Offline or a server hiccup: try again by itself, a few times.
          if ((e.status === -1 || e.status >= 500) && (n.retries = (n.retries || 0) + 1) <= 5) n.again = true;
        }
      }).then(() => {
        n.saving = null;
        if (cur !== n) return;
        n.dirty = stillDirty();
        const again = n.again;
        n.again = false;
        if (n.dirty && !n.conflict && !n.gone && (again || !n.error)) n.timer = setTimeout(save, n.error ? 5000 : 800);
        setStatus(); setMeta(); drawBanner();
      });
      setStatus();
      return n.saving;
    }
    // Save now (blur, leaving the tab, before an export or the versions list).
    function flush() {
      const n = cur;
      if (!n || !n.editable) return Promise.resolve();
      if (n.saving) return n.saving.then(() => (cur === n && stillDirty() && !n.error && !n.conflict && !n.gone) ? save() : null);
      if (!stillDirty()) return Promise.resolve();
      return save();
    }
    // Leaving the page: one last attempt that survives the unload.
    window.addEventListener('pagehide', () => {
      const n = cur;
      if (!n || !n.editable || n.conflict || n.gone || n.saving || !body || !PM.can.edit) return;
      const p = pending(n);
      if (!Object.keys(p.ch).length) return;
      p.ch.base_rev = n.rev;
      try {
        fetch('/api/projects/' + PM.pid + '/notes/' + n.id, {method: 'PUT', keepalive: true, credentials: 'same-origin',
          headers: {'Content-Type': 'application/json', 'Accept': 'application/json', 'X-Requested-With': 'XMLHttpRequest'},
          body: JSON.stringify(p.ch)}).catch(() => {});
      } catch (e) { /* too large for a keepalive request: nothing more can be done here */ }
    });
    window.addEventListener('beforeunload', e => {
      const n = cur;
      // Only when the text could NOT be saved (a conflict, too long, an error).
      if (n && n.editable && (n.conflict || n.gone || n.error) && stillDirty()) { e.preventDefault(); e.returnValue = ''; }
    });

    function applyNote(note) {
      // `note` is the server's GET shape. body_html was sanitized there on the
      // way in and again on the way out — the one HTML this module inserts.
      const n = cur;
      n.rev = note.rev;
      n.updated_name = note.updated_name;
      n.updated_at = note.updated_at;
      n.conflict = null; n.gone = false; n.error = ''; n.tooLong = false; n.behind = null; n.dirty = false;
      clearTimeout(n.timer);
      body.innerHTML = note.body_html || '';
      if (titleIn.tagName === 'INPUT') titleIn.value = note.title === 'Untitled' ? '' : note.title;
      else titleIn.textContent = note.title;
      n.title = note.title;
      n.baseTitle = note.title || 'Untitled';
      n.baseHtml = n.editable ? noteHtml(body) : '';
      body.classList.toggle('is-empty', !body.textContent.trim() && !body.querySelector('hr,table,li'));
      setStatus(); setMeta(); drawBanner();
      patchList({id: n.id, title: note.title, rev: note.rev, updated_name: note.updated_name, updated_at: note.updated_at});
    }
    async function refreshOpen() {
      const n = cur;
      if (!n) return;
      let j;
      try { j = await PM.api('GET', '/notes/' + n.id); } catch (e) { return; }
      if (cur !== n || n.saving || n.conflict || j.note.rev <= n.rev) return;
      if (stillDirty() || hasFocus()) return;              // the person got there first: leave their text alone
      applyNote(j.note);
    }
    async function loadTheirs() {
      const n = cur;
      if (!n) return;
      let note = null;
      try { note = (await PM.api('GET', '/notes/' + n.id)).note; }
      catch (e) {
        if (e.status === 404) { cur = null; drawEmptyEditor(); loadList(); PM.toast('That note was deleted.', true); return; }
        if (n.conflict) note = Object.assign({id: n.id}, n.conflict);
      }
      if (cur !== n || !note) return;
      applyNote(note);
    }
    async function keepCopy() {
      const n = cur;
      if (!n || !PM.can.edit) return;
      const p = pending(n);
      const title = (p.title + ' (my copy)').slice(0, 200);
      try {
        const made = await PM.api('POST', '/notes', {title});
        try {
          await PM.api('PUT', '/notes/' + made.note.id, {body_html: p.html, base_rev: made.note.rev});
        } catch (e) {
          // The text did not go in (too long, offline): no empty "copy" is left behind.
          await PM.api('DELETE', '/notes/' + made.note.id).catch(() => {});
          throw e;
        }
        PM.toast('✓ Your text was saved as “' + title + '”');
        if (cur !== n) return;
        if (n.gone) { cur = null; drawEmptyEditor(); }
        else await loadTheirs();
        await wrote(made, true);
        loadList();
      } catch (e) { say(e); }
    }

    /* ── Opening, creating, deleting ─────────────────────────────────────── */
    async function leave() {
      const n = cur;
      if (!n) return true;
      if (n.editable && stillDirty()) await flush();
      if (cur !== n) return true;
      if (n.editable && stillDirty()) {
        const ok = await PM.confirm('This note has changes that could not be saved. Leave it and lose them?', 'Discard changes');
        if (!ok) return false;
      }
      clearTimeout(n.timer);
      return true;
    }
    async function openNote(id, force) {
      id = +id;
      if (cur && cur.id === id && !force) { wrap.classList.add('is-open'); return; }
      if (!(await leave())) return;
      const my = ++openSeq;
      let j;
      try { j = await PM.api('GET', '/notes/' + id); }
      catch (e) {
        if (my !== openSeq) return;
        PM.toast(e.status === 404 ? 'That item no longer exists.' : e.message, true);
        if (cur && cur.id === id) { cur = null; drawEmptyEditor(); }
        return;
      }
      if (my !== openSeq) return;
      buildEditor(j.note);
    }
    function newNote() {
      if (!PM.can.edit) return;
      leave().then(ok => {
        if (!ok) return;
        const my = ++openSeq;
        busy(newBtn, PM.api('POST', '/notes', {})).then(resp => {
          wrote(resp, true);
          if (my !== openSeq) return;
          buildEditor(resp.note);
          loadList();
          if (titleIn && titleIn.focus) titleIn.focus();
        }, say);
      });
    }
    function deleteNote() {
      const n = cur;
      if (!n || !PM.can.edit) return;
      PM.confirm('Delete the note “' + (n.title || 'Untitled') + '”? It cannot be restored.', 'Delete').then(ok => {
        if (!ok || cur !== n) return;
        clearTimeout(n.timer);
        PM.api('DELETE', '/notes/' + n.id).then(resp => {
          if (cur === n) { cur = null; drawEmptyEditor(); }
          PM.toast('✓ Note deleted');
          wrote(resp, true);
          loadList();
        }, say);
      });
    }

    /* ── Formatting ──────────────────────────────────────────────────────── */
    const exec = (cmd, val) => { try { return document.execCommand(cmd, false, val == null ? null : val); } catch (e) { return false; } };
    function blockOf(tags) {
      const sel = window.getSelection();
      let n = sel && sel.rangeCount ? sel.getRangeAt(0).startContainer : null;
      while (n && n !== body) {
        if (n.nodeType === 1 && tags.indexOf(n.tagName) >= 0) return n;
        n = n.parentNode;
      }
      return null;
    }
    function inBody(node) { return !!(body && node && (node === body || body.contains(node))); }
    function restoreSel() {
      body.focus({preventScroll: true});
      const sel = window.getSelection();
      if (savedRange && inBody(savedRange.startContainer)) {
        sel.removeAllRanges();
        sel.addRange(savedRange);
      } else if (!sel.rangeCount || !inBody(sel.anchorNode)) {
        const r = document.createRange();
        r.selectNodeContents(body);
        r.collapse(false);
        sel.removeAllRanges();
        sel.addRange(r);
      }
    }
    function ensureParagraph() {
      if (!body.firstChild) {
        const p = document.createElement('p');
        p.appendChild(document.createElement('br'));
        body.appendChild(p);
        caretIn(p);
      }
    }
    function insertHtml(html) {
      // `html` is always the serialization of nodes cleanInto() built (or a
      // fixed string below) — going through insertHTML keeps the browser's
      // undo history; the fallback inserts the same nodes directly.
      if (exec('insertHTML', html)) return;
      const sel = window.getSelection();
      if (!sel.rangeCount) return;
      const r = sel.getRangeAt(0);
      r.deleteContents();
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const frag = document.createDocumentFragment();
      cleanInto(doc.body, frag, {count: 0, depth: 0, inA: 0});
      r.insertNode(frag);
      r.collapse(false);
    }
    function emptyCell(tag) { const c = document.createElement(tag); c.appendChild(document.createElement('br')); return c; }
    function caretIn(node) {
      const r = document.createRange();
      r.setStart(node, 0);
      r.collapse(true);
      const sel = window.getSelection();
      sel.removeAllRanges();
      sel.addRange(r);
    }
    // A 3 × 3 table (a header row + two rows) AFTER the block the caret is
    // in — as nodes, so no browser has to guess how a table fits inside a
    // paragraph or a list item.
    function insertTable() {
      const sel = window.getSelection();
      let at = sel.rangeCount ? sel.getRangeAt(0).startContainer : null;
      while (at && at !== body && at.parentNode !== body) at = at.parentNode;
      const table = document.createElement('table');
      const head = document.createElement('thead'), tb = document.createElement('tbody');
      const row = tag => { const tr = document.createElement('tr'); for (let i = 0; i < 3; i++) tr.appendChild(emptyCell(tag)); return tr; };
      head.appendChild(row('th'));
      tb.append(row('td'), row('td'));
      table.append(head, tb);
      const after = document.createElement('p');
      after.appendChild(document.createElement('br'));     // somewhere to go on typing below the table
      if (!at || at === body) body.append(table, after);
      else if (at.tagName === 'P' && !at.textContent.trim() && !at.querySelector('a,hr')) at.replaceWith(table, after);
      else at.after(table, after);
      caretIn(head.rows[0].cells[0]);
    }

    function linkDialog() {
      const range = savedRange ? savedRange.cloneRange() : null;
      const old = blockOf(['A']);
      const d = PM.dialog({title: old ? 'Edit link' : 'Add a link', noDirtyGuard: true});
      const url = h('input', {type: 'text', class: 'field-input', maxLength: 2000, autocomplete: 'off', placeholder: 'https://', inputmode: 'url',
                              value: old ? (old.getAttribute('href') || '') : ''});
      const msg = h('p', {class: 'pm-note', text: 'A web address (http:// or https://) or an e-mail address.'});
      d.body.append(field('Address', url), msg);
      const back = () => { savedRange = range; restoreSel(); };
      const go = () => {
        const v = typedUrl(url.value);
        if (!v) { msg.textContent = 'That is not a link this note can hold — use http://, https:// or an e-mail address.'; msg.classList.add('pm-b-err'); url.focus(); return; }
        d.close();
        back();
        if (old && old.isConnected) old.setAttribute('href', v);
        else if (window.getSelection().isCollapsed) {
          const tmp = document.createElement('div');
          const a = document.createElement('a');
          a.setAttribute('href', v);
          a.textContent = v.replace(/^mailto:/, '');
          tmp.appendChild(a);
          insertHtml(tmp.innerHTML);
        } else exec('createLink', v);
        scrubLive(body);
        changed();
      };
      url.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); go(); } });
      if (old) {
        d.footer.appendChild(h('div', {class: 'pm-foot-left'}, h('button', {type: 'button', class: 'btn btn-danger-ghost', text: 'Remove link', onclick: () => {
          d.close();
          back();
          if (old.isConnected) old.replaceWith(...Array.from(old.childNodes));
          changed();
        }})));
      }
      d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Cancel', onclick: () => { d.close(); back(); }}),
                      h('button', {type: 'button', class: 'btn btn-primary', text: old ? 'Save' : 'Add link', onclick: go}));
    }

    const COMMANDS = {
      h2() { exec('formatBlock', blockOf(['H2']) ? 'p' : 'h2'); },
      h3() { exec('formatBlock', blockOf(['H3']) ? 'p' : 'h3'); },
      bold() { exec('bold'); },
      italic() { exec('italic'); },
      underline() { exec('underline'); },
      ul() { exec('insertUnorderedList'); },
      ol() { exec('insertOrderedList'); },
      quote() {
        const q = blockOf(['BLOCKQUOTE']);
        if (!q) { exec('formatBlock', 'blockquote'); return; }
        // Leave the quote: its lines become ordinary paragraphs again.
        if (q.querySelector(N_BLOCK_SEL)) q.replaceWith(...Array.from(q.childNodes));
        else {
          const p = document.createElement('p');
          while (q.firstChild) p.appendChild(q.firstChild);
          q.replaceWith(p);
        }
      },
      link() { linkDialog(); return false; },
      table() { if (!blockOf(['TABLE'])) insertTable(); },
      undo() { exec('undo'); },
      redo() { exec('redo'); },
      row() {
        const cell = blockOf(['TD', 'TH']);
        if (!cell) return;
        const tr = cell.parentNode, table = tr.closest('table');
        const fresh = document.createElement('tr');
        for (let i = 0; i < tr.cells.length; i++) fresh.appendChild(emptyCell('td'));
        if (tr.parentNode.tagName === 'THEAD') {
          let tb = table.tBodies[0];
          if (!tb) { tb = document.createElement('tbody'); table.appendChild(tb); }
          tb.insertBefore(fresh, tb.firstChild);
        } else tr.after(fresh);
      },
      col() {
        const cell = blockOf(['TD', 'TH']);
        if (!cell) return;
        const at = cell.cellIndex, table = cell.closest('table');
        if (table.rows[0] && table.rows[0].cells.length >= 12) { PM.toast('A table here can have at most 12 columns.', true); return; }
        Array.from(table.rows).forEach(r => {
          const c = emptyCell(r.parentNode.tagName === 'THEAD' ? 'th' : 'td');
          if (r.cells[at]) r.cells[at].after(c); else r.appendChild(c);
        });
      },
      deltable() {
        const t = blockOf(['TABLE']);
        if (t) t.remove();
        ensureParagraph();
      },
    };
    function run(name) {
      const n = cur;
      if (!n || !n.editable || !body || !COMMANDS[name]) return;
      restoreSel();
      ensureParagraph();
      if (COMMANDS[name]() === false) return;      // a dialog took over
      scrubLive(body);
      changed();
      syncTools();
    }
    function syncTools() {
      if (!toolBox || !body) return;
      const sel = window.getSelection();
      const inside = !!(sel && sel.rangeCount && inBody(sel.anchorNode));
      const state = name => { try { return document.queryCommandState(name); } catch (e) { return false; } };
      const on = {bold: inside && state('bold'), italic: inside && state('italic'), underline: inside && state('underline'),
                  h2: inside && !!blockOf(['H2']), h3: inside && !!blockOf(['H3']), quote: inside && !!blockOf(['BLOCKQUOTE']),
                  ul: inside && !!blockOf(['UL']) && !blockOf(['OL']), ol: inside && !!blockOf(['OL']),
                  link: inside && !!blockOf(['A'])};
      Object.keys(on).forEach(k => { if (toolBtns[k]) toolBtns[k].setAttribute('aria-pressed', on[k] ? 'true' : 'false'); });
      if (tableTools) tableTools.hidden = !(inside && blockOf(['TD', 'TH']));
    }
    document.addEventListener('selectionchange', () => {
      if (!body || !cur || !cur.editable || PM.activeTab !== 'notes') return;
      const sel = window.getSelection();
      if (sel && sel.rangeCount && inBody(sel.anchorNode)) savedRange = sel.getRangeAt(0).cloneRange();
      syncTools();
    });

    function onPaste(e) {
      const n = cur;
      if (!n || !n.editable) return;
      e.preventDefault();                          // nothing reaches the note un-rebuilt
      const cd = e.clipboardData;
      if (!cd) return;
      putClip(cd.getData('text/html'), cd.getData('text/plain'));
    }
    function putClip(html, text) {
      const clean = clipHtml(html);
      if (clean != null) insertHtml(clean);
      else if (text) {
        if (/[\r\n]/.test(text)) insertHtml(textAsHtml(text));
        else if (!exec('insertText', text)) insertHtml(textAsHtml(text));
      }
      scrubLive(body);
      changed();
    }
    function onDrop(e) {
      const n = cur;
      e.preventDefault();                          // never a dropped file, image or raw HTML
      e.stopPropagation();
      if (!n || !n.editable || !e.dataTransfer) return;
      const html = e.dataTransfer.getData('text/html'), text = e.dataTransfer.getData('text/plain');
      if (!html && !text) return;
      const r = document.caretRangeFromPoint ? document.caretRangeFromPoint(e.clientX, e.clientY) : null;
      body.focus({preventScroll: true});
      if (r && inBody(r.startContainer)) {
        const sel = window.getSelection();
        sel.removeAllRanges();
        sel.addRange(r);
      }
      putClip(html, text);
    }

    /* ── Building the editor for a note ──────────────────────────────────── */
    const TOOLS = [
      ['h2', 'H2', 'Heading'], ['h3', 'H3', 'Subheading'], null,
      ['bold', 'B', 'Bold'], ['italic', 'I', 'Italic'], ['underline', 'U', 'Underline'], null,
      ['ul', '• List', 'Bulleted list'], ['ol', '1. List', 'Numbered list'], ['quote', '❝ Quote', 'Quote'], null,
      ['link', 'Link', 'Link'], ['table', 'Table', 'Insert a table'], null,
      ['undo', '↶', 'Undo'], ['redo', '↷', 'Redo']];
    function toolButton(t) {
      const b = h('button', {type: 'button', class: 'pm-n-tool pm-n-tool-' + t[0], title: t[2], 'aria-label': t[2], text: t[1]});
      // mousedown: keep the selection in the note; click: also the keyboard.
      b.addEventListener('mousedown', e => e.preventDefault());
      b.addEventListener('click', () => run(t[0]));
      return b;
    }

    function exportTo(e, kind) {
      const n = cur;
      if (!n) { e.preventDefault(); return; }
      if (!n.editable || !stillDirty()) return;               // the link works by itself
      e.preventDefault();
      const href = e.currentTarget.href;
      const tab = kind === 'pdf' ? window.open('', '_blank') : null;   // opened in the click itself (pop-up blockers)
      flush().then(() => {                                     // export what is on screen
        if (tab) { tab.opener = null; tab.location.href = href; }
        else window.location.href = href;
      });
    }

    function buildEditor(note) {
      const editable = !!PM.can.edit;
      cur = {id: note.id, rev: note.rev, title: note.title, editable, dirty: false, saving: null, again: false, timer: null,
             conflict: null, gone: false, error: '', tooLong: false, behind: null, retries: 0};
      savedRange = null;
      wrap.classList.add('is-open');
      clear(edBox);
      Object.keys(toolBtns).forEach(k => delete toolBtns[k]);

      const backBtn = h('button', {type: 'button', class: 'btn btn-ghost btn-sm pm-n-back', text: '← Notes',
        onclick: () => leave().then(ok => { if (ok) { cur = null; drawEmptyEditor(); drawList(); } })});
      statusEl = h('span', {class: 'pm-n-status', role: 'status', 'aria-live': 'polite'});
      if (editable) {
        titleIn = h('input', {type: 'text', class: 'field-input pm-n-title', maxLength: 200, autocomplete: 'off', placeholder: 'Untitled', 'aria-label': 'Note title'});
        titleIn.addEventListener('input', changed);
        titleIn.addEventListener('blur', () => { flush(); });
        titleIn.addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); body.focus(); } });
      } else {
        titleIn = h('h2', {class: 'pm-n-title-ro'});
      }
      const base = '/projects/' + PM.pid + '/notes/' + (+note.id);
      const acts = h('div', {class: 'pm-n-acts'},
        h('button', {type: 'button', class: 'btn btn-ghost btn-sm', text: 'Versions', onclick: versionsDialog}),
        // Generated documents: never hover-prefetched.
        h('a', {class: 'btn btn-ghost btn-sm no-prefetch', href: base + '/note.pdf', target: '_blank', rel: 'noopener', text: 'PDF',
                title: 'Export this note as a PDF', onclick: e => exportTo(e, 'pdf')}),
        h('a', {class: 'btn btn-ghost btn-sm no-prefetch', href: base + '/download/docx', text: 'Word',
                title: 'Export this note as a Word document', onclick: e => exportTo(e, 'docx')}),
        editable ? h('button', {type: 'button', class: 'btn btn-danger-ghost btn-sm', text: 'Delete', onclick: deleteNote}) : null);
      edBox.appendChild(h('div', {class: 'pm-n-top'}, backBtn, titleIn, statusEl, acts));

      bannerBox = h('div', {class: 'pm-n-banner'});
      toolBox = tableTools = null;
      if (editable) {
        toolBox = h('div', {class: 'pm-n-tools', role: 'toolbar', 'aria-label': 'Formatting'});
        let group = h('span', {class: 'pm-n-tgroup'});
        TOOLS.forEach(t => {
          if (!t) { toolBox.appendChild(group); group = h('span', {class: 'pm-n-tgroup'}); return; }
          toolBtns[t[0]] = toolButton(t);
          group.appendChild(toolBtns[t[0]]);
        });
        toolBox.appendChild(group);
        tableTools = h('span', {class: 'pm-n-tgroup pm-n-ttable', hidden: true},
          toolButton(['row', '+ Row', 'Add a row below']), toolButton(['col', '+ Column', 'Add a column to the right']),
          toolButton(['deltable', 'Delete table', 'Delete this table']));
        toolBox.appendChild(tableTools);
        edBox.appendChild(toolBox);
        stick();
      }
      edBox.appendChild(bannerBox);

      if (editable) {
        body = h('div', {class: 'pm-n-body', contenteditable: 'true', role: 'textbox', 'aria-multiline': 'true', 'aria-label': 'Note text',
                         spellcheck: 'true', data: {placeholder: 'Write here…'}});
        body.addEventListener('input', changed);
        body.addEventListener('paste', onPaste);
        body.addEventListener('drop', onDrop);
        body.addEventListener('dragover', e => e.preventDefault());
        body.addEventListener('focus', () => {
          exec('defaultParagraphSeparator', 'p');
          exec('styleWithCSS', false);
          ensureParagraph();
        });
        body.addEventListener('blur', () => {
          const n = cur;
          // Out of the note: tidy what the browser left behind, save now,
          // and take a newer text that was waiting for exactly this moment.
          setTimeout(() => {
            if (cur !== n || !body || document.activeElement === body) return;
            if (toolBox && toolBox.contains(document.activeElement)) return;      // using the toolbar by keyboard
            if (PM.dialogOpen()) return;                                           // the link box
            scrubLive(body);
            flush().then(() => { if (cur === n && n.behind && !stillDirty()) refreshOpen(); });
          }, 0);
        });
      } else {
        body = h('div', {class: 'pm-n-body is-readonly'});
      }
      edBox.appendChild(body);
      metaEl = h('div', {class: 'pm-n-meta'});
      edBox.appendChild(metaEl);
      applyNote(note);
      drawList();
    }

    // The toolbar stays in reach in a long note: it sticks just under the
    // project header (whose height this file can only measure).
    function stick() {
      const head = document.getElementById('pm-head');
      if (!head || !edBox) return;
      const top = parseFloat(window.getComputedStyle(head).top) || 0;
      edBox.style.setProperty('--pm-n-stick', Math.max(0, Math.round(top + head.offsetHeight)) + 'px');
    }
    window.addEventListener('resize', stick);

    /* ── Versions ────────────────────────────────────────────────────────── */
    function versionsDialog() {
      const n = cur;
      if (!n) return;
      flush().then(() => {
        if (cur !== n) return;
        const d = PM.dialog({title: 'Versions — ' + (n.title || 'Untitled'), wide: true, noDirtyGuard: true});
        d.el.classList.add('pm-n-vdlg');
        const list = h('div', {class: 'pm-list pm-n-vlist'}, h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'}));
        const view = h('div', {class: 'pm-n-vview'}, h('p', {class: 'pm-note', text: 'Pick a version to read it.'}));
        d.body.append(h('div', {class: 'pm-n-vers'}, list, view));
        let picked = null, seq = 0;
        const restore = h('button', {type: 'button', class: 'btn btn-primary', text: 'Restore this version', disabled: true});
        restore.addEventListener('click', () => {
          if (!picked || !PM.can.edit) return;
          PM.confirm('Restore this version? The text the note has now is kept as a version too, so you can go back.', 'Restore').then(ok => {
            if (!ok) return;
            busy(restore, PM.api('POST', '/notes/' + n.id + '/versions/' + picked + '/restore')).then(resp => {
              d.close();
              PM.toast('✓ Version restored');
              if (cur === n) applyNote(resp.note);
              wrote(resp);
              loadList();
            }, e => {
              if (e.status === 409) PM.toast('The note changed while you were looking — close this and try again.', true); else say(e);
            });
          });
        });
        function show(v, btn) {
          const my = ++seq;
          Array.from(list.children).forEach(b => b.classList.toggle('is-picked', b === btn));
          clear(view).appendChild(h('p', {class: 'pm-loading', role: 'status', text: 'Loading…'}));
          PM.api('GET', '/notes/' + n.id + '/versions/' + v.id).then(j => {
            if (my !== seq) return;
            picked = v.id;
            restore.disabled = !PM.can.edit;
            const text = h('div', {class: 'pm-n-body is-readonly'});
            text.innerHTML = j.version.body_html || '';          // server-sanitized, like the note itself
            clear(view).append(h('h3', {class: 'pm-n-vtitle', text: j.version.title || 'Untitled'}),
              h('p', {class: 'pm-note', text: 'As ' + (j.version.saver_name || 'someone') + ' left it — replaced ' + fmtWhen(j.version.at)}),
              text.firstChild ? text : h('p', {class: 'pm-note', text: '(This version was empty.)'}));
          }, e => { if (my === seq) clear(view).appendChild(h('p', {class: 'pm-empty', role: 'alert', text: e.message})); });
        }
        PM.api('GET', '/notes/' + n.id + '/versions').then(j => {
          clear(list);
          const vs = j.versions || [];
          if (!vs.length) {
            list.appendChild(h('p', {class: 'pm-empty', text: 'No earlier versions yet.'}));
            clear(view).appendChild(h('p', {class: 'pm-note', text: 'While a note is being edited its earlier text is kept every ten minutes, and whenever someone else takes over. The newest 50 are kept.'}));
            return;
          }
          vs.forEach(v => {
            const b = h('button', {type: 'button', class: 'pm-row'},
              h('span', {class: 'pm-row-main'}, h('span', {class: 'pm-row-title', text: fmtWhen(v.at)}),
                h('span', {class: 'pm-row-sub', style: 'display:block', text: [v.saver_name, v.title].filter(Boolean).join(' · ')})));
            b.addEventListener('click', () => show(v, b));
            list.appendChild(b);
          });
        }, e => { clear(list).appendChild(h('p', {class: 'pm-empty', role: 'alert', text: e.message})); });
        d.footer.append(h('button', {type: 'button', class: 'btn btn-ghost', text: 'Close', onclick: () => d.close()}));
        if (PM.can.edit) d.footer.appendChild(restore);
      });
    }

    /* ── State, visibility, tab ──────────────────────────────────────────── */
    function syncRights() {
      const n = cur;
      if (!n || n.editable === !!PM.can.edit) return;
      // My rights changed (role, archive). Rebuild only when nothing would be lost.
      if (!stillDirty()) openNote(n.id, true);
      else { n.error = 'You can no longer edit this project — your last changes were not saved.'; n.tooLong = false; setStatus(); drawBanner(); }
    }
    onState(own => {
      if (!pane || PM.activeTab !== 'notes') return;
      stick();
      syncRights();
      drawList();
      if (!own) loadList();
    });
    document.addEventListener('visibilitychange', () => {
      if (!document.hidden && pane && PM.activeTab === 'notes' && !PM.dialogOpen()) loadList();
    });
    return {
      mount,
      show(params) {
        const want = (params && params.note) || null;
        loadList().then(() => {
          if (want) {
            // The list knows whether it still exists: no need to ask for a note that is gone.
            if (!notes || notes.some(n => n.id === want)) openNote(want);
            else PM.toast('That item no longer exists.', true);
          } else if (!cur && notes && notes.length && !isPhone()) openNote(notes[0].id);   // desktop: show the newest
        });
      },
      hide() { flush(); },
    };
  })());
})();
