// Crew app: start-up, navigation, the sidebar, the side panel (browser, phone, computer and file previews
// beside whatever you are doing, resizable like Claude's artifacts), app-wide notices and one-click updates.

import { $, $$, h, icon, btn, api, stream, store, bus, fail, toast, markdown, download, esc, pct, isSmall, clear } from './js/ui.js';
import { homePage, chatPage, chatsPage } from './js/pages/chat.js';
import { projectsPage, projectPage } from './js/pages/projects.js';
import { workflowsPage } from './js/pages/workflows.js';
import { connectionsPage } from './js/pages/connections.js';
import { usagePage } from './js/pages/usage.js';
import { skillsPage } from './js/pages/skills.js';
import { libraryPage } from './js/pages/library.js';
import { browserPage, phonePage, computerPage } from './js/pages/misc.js';
import { settingsPage, applyLook } from './js/pages/settings.js';
import { mountLive } from './js/devices.js';

const ROUTES = [
  [/^\/(?:new)?$/, homePage, 'new'],
  [/^\/chat\/([\w-]+)$/, chatPage, 'chat'],
  [/^\/chats$/, chatsPage, 'chats'],
  [/^\/projects$/, projectsPage, 'projects'],
  [/^\/projects\/([\w.-]+)$/, projectPage, 'projects'],
  [/^\/workflows$/, workflowsPage, 'workflows'],
  [/^\/library$/, libraryPage, 'library'],
  [/^\/skills$/, skillsPage, 'skills'],
  [/^\/connections$/, connectionsPage, 'connections'],
  [/^\/usage(?:\/(scorecard))?$/, usagePage, 'usage'],
  [/^\/settings(?:\/(\w+))?$/, settingsPage, 'settings'],
  [/^\/browser$/, browserPage, 'browser'],
  [/^\/phone$/, phonePage, 'phone'],
  [/^\/computer$/, computerPage, 'computer'],
];
const LEGACY = [[/^\/assistant\/new$/, '/new'], [/^\/assistant\/([\w-]+)$/, '/chat/$1'], [/^\/assistant$/, '/chats'], [/^\/captures$/, '/library'],
  [/^\/more$/, '/settings']];

let cleanup = null;
const app = $('#app');

function currentPath() {
  return (location.hash.replace(/^#/, '') || '/').split('?')[0] || '/';
}

// Pages set the title and the buttons in the top bar.
export function setTop(title, actions = []) {
  const box = $('#topTitle');
  clear(box, ...(title == null ? [] : [title instanceof Node ? title : h('span', { class: 'ellipsis', style: { padding: '0 6px' } }, title)]));
  clear($('#topActions'), ...actions.filter(Boolean));
}
store.setTop = setTop;

function route() {
  let path = currentPath();
  for (const [rx, to] of LEGACY) {
    if (rx.test(path)) { location.replace('#' + path.replace(rx, to)); return; }
  }
  const found = ROUTES.find(([rx]) => rx.test(path));
  if (!found) { location.hash = '#/'; return; }
  const [rx, render, nav] = found;
  if (cleanup) { try { cleanup(); } catch (e) { console.error(e); } cleanup = null; }
  const view = $('#view');
  clear(view);
  view.scrollTop = 0;
  view.className = 'view';
  setTop(null);
  $$('[data-nav]').forEach((a) => a.classList.toggle('on', a.dataset.nav === nav));
  document.title = 'Crew';
  app.classList.remove('drawer');
  if (['browser', 'phone', 'computer'].includes(nav) && panel.isOpen && panel.tab === nav) panel.close();
  try {
    cleanup = render(view, path.match(rx).slice(1)) || null;
  } catch (e) {
    console.error(e);
    view.append(h('div', { class: 'page' }, h('div', { class: 'empty' }, 'This screen could not open. ' + e.message)));
  }
  markRecent();
  view.focus({ preventScroll: true });
}

// ------------------------------------------------------------------ sidebar

let recentItems = [];

async function refreshRecent() {
  try {
    const ov = await api('/api/overview');
    store.overview = ov;
    const runs = ov.runs.map((r) => ({ href: '#/projects/' + r.id, title: r.title, t: r.started, ic: 'layers', live: r.running, pinned: false }));
    const chats = ov.chats.filter((c) => c.kind !== 'workflow').map((c) => ({
      href: '#/chat/' + c.id, title: c.title, t: c.updated, ic: c.engine === 'codex' ? 'gpt' : 'chat', pinned: !!c.pinned,
    }));
    recentItems = [...runs, ...chats].sort((a, b) => (b.pinned - a.pinned) || (b.t - a.t)).slice(0, 40);
    const live = ov.runs.filter((r) => r.running).length;
    $('#liveCount').textContent = live ? String(live) : '';
    drawRecent();
    const name = (ov.app && ov.app.owner_name) || '';
    $('#meName').textContent = name || 'Settings';
    $('#meInitial').textContent = (name || 'C').trim().charAt(0).toUpperCase();
  } catch (e) {
    if (e.status === 401) location.reload();
  }
}

function drawRecent() {
  const box = $('#recents');
  clear(box, ...(recentItems.length ? recentItems.map((it) => h('a', { href: it.href, title: it.title },
    it.live ? h('span', { class: 'live', title: 'Working now' }) : icon(it.pinned ? 'pin' : it.ic), h('span', { class: 't' }, it.title)))
    : [h('div', { class: 'empty-note' }, 'Your chats and projects will appear here.')]));
  markRecent();
}

function markRecent() {
  const here = '#' + currentPath();
  $$('#recents a').forEach((a) => a.classList.toggle('on', a.getAttribute('href') === here));
}

async function refreshMeters() {
  try {
    const u = await api('/api/usage');
    store.usage = u;
    const lines = u.accounts.filter((a) => a.limits && (a.limits.five_util != null || a.limits.week_util != null)).slice(0, 4).map((a) => {
      const five = Number(a.limits.five_util || 0), week = Number(a.limits.week_util || 0);
      const worst = Math.max(five, week);
      const cls = worst >= 0.9 ? 'bad' : worst >= 0.7 ? 'warn' : 'accent';
      return h('div', { class: 'meter-line', title: `${a.name}: ${pct(five)} of the 5-hour limit, ${pct(week)} of the weekly limit` },
        h('b', null, a.name), h('div', { class: 'bar' }, h('i', { class: cls, style: { width: Math.round(five * 100) + '%' } })), h('span', null, pct(five)));
    });
    clear($('#meters'), ...lines);
    bus.emit('usage', u);
  } catch (e) { /* offline: keep the last numbers */ }
}

function setSidebar(open) {
  if (isSmall()) { app.classList.toggle('drawer', open); return; }
  if (open) delete document.documentElement.dataset.sidebar; else document.documentElement.dataset.sidebar = 'closed';
  try { localStorage.setItem('crew.sidebar', open ? 'open' : 'closed'); } catch (e) { /* private mode */ }
}
$('#sbClose').addEventListener('click', () => setSidebar(false));
$('#sbOpen').addEventListener('click', () => setSidebar(true));
$('#scrim').addEventListener('click', () => app.classList.remove('drawer'));
$('#newChat').addEventListener('click', () => { if (currentPath() === '/' || currentPath() === '/new') bus.emit('home:reset'); });
$$('.sb-devices button').forEach((b) => b.addEventListener('click', () => {
  const kind = b.dataset.open;
  if (isSmall()) { location.hash = '#/' + kind; return; }
  if (panel.isOpen && panel.tab === kind) panel.close(); else panel.open(kind);
}));
document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'b' && !e.shiftKey) { e.preventDefault(); setSidebar(document.documentElement.dataset.sidebar === 'closed'); }
  if ((e.ctrlKey || e.metaKey) && e.shiftKey && e.key.toLowerCase() === 'o') { e.preventDefault(); location.hash = '#/new'; }
});

// ------------------------------------------------------------------ side panel (like Claude's artifacts)

const TABS = { browser: ['globe', 'Browser'], phone: ['phone', 'Phone'], computer: ['monitor', 'Computer'], preview: ['doc', 'Preview'], files: ['folder', 'Files'] };

export const panel = {
  el: $('#panel'),
  body: $('#panelBody'),
  tab: null,
  item: null,
  files: null,  // {title, load: async () => [files]} from the page that owns them
  release: null,
  get isOpen() { return app.classList.contains('with-panel'); },

  open(tab, item = null) {
    if (item) this.item = item;
    app.classList.add('with-panel');
    this.drawTabs();
    if (tab !== this.tab || tab === 'preview' || tab === 'files') this.show(tab);
    $$('.sb-devices button').forEach((b) => b.classList.toggle('on', b.dataset.open === this.tab));
  },

  close() {
    app.classList.remove('with-panel');
    this.el.classList.remove('max');
    if (this.release) { this.release(); this.release = null; }
    clear(this.body);
    this.tab = null;
    clear($('#panelMax'), icon('expand'));
    $$('.sb-devices button').forEach((b) => b.classList.remove('on'));
    bus.emit('panel:closed');
  },

  drawTabs() {
    const tabs = ['browser', 'phone', 'computer'];
    if (this.item) tabs.push('preview');
    if (this.files) tabs.push('files');
    clear($('#panelTabs'), ...tabs.map((t) => h('button', { type: 'button', class: t === this.tab ? 'on' : '', onclick: () => this.open(t) },
      icon(TABS[t][0]), TABS[t][1])));
  },

  show(tab) {
    if (this.release) { this.release(); this.release = null; }
    this.tab = tab;
    clear(this.body);
    this.drawTabs();
    if (tab === 'browser' || tab === 'phone' || tab === 'computer') {
      const holder = h('div', { style: { display: 'flex', flexDirection: 'column', minHeight: '0' } });
      this.body.append(holder);
      this.release = mountLive(tab, holder, 'panel');
    } else if (tab === 'files') {
      this.body.append(filesView(this.files));
    } else {
      this.body.append(preview(this.item));
    }
  },
};
store.panel = panel;

bus.on('live:claim', ({ kind, owner }) => {
  if (owner !== 'panel' && panel.isOpen && panel.tab === kind) panel.close();
});
bus.on('panel:open', (tab) => { if (!isSmall() || tab === 'preview' || tab === 'files') panel.open(tab); else location.hash = '#/' + tab; });
bus.on('panel:preview', (item) => panel.open('preview', item));
bus.on('panel:files', (files) => { panel.files = files; if (files && files.show) panel.open('files'); else if (panel.isOpen) panel.drawTabs(); });
$('#panelClose').addEventListener('click', () => panel.close());
$('#panelMax').addEventListener('click', () => {
  const max = panel.el.classList.toggle('max');
  clear($('#panelMax'), icon(max ? 'shrink' : 'expand'));
});

// Drag the divider to resize the panel; the width is remembered.
(function resizable() {
  let saved = 0;
  try { saved = Number(localStorage.getItem('crew.panelW') || 0); } catch (e) { /* the browser keeps no site data */ }
  if (saved > 280) document.documentElement.style.setProperty('--panel-w', saved + 'px');
  const bar = $('#resizer');
  bar.addEventListener('pointerdown', (e) => {
    e.preventDefault();
    bar.setPointerCapture(e.pointerId);
    app.classList.add('resizing');
    const move = (ev) => {
      const w = Math.max(320, Math.min(window.innerWidth - 420, window.innerWidth - ev.clientX));
      document.documentElement.style.setProperty('--panel-w', w + 'px');
    };
    const up = () => {
      app.classList.remove('resizing');
      bar.removeEventListener('pointermove', move);
      bar.removeEventListener('pointerup', up);
      const w = parseInt(getComputedStyle(document.documentElement).getPropertyValue('--panel-w'), 10);
      try { if (w) localStorage.setItem('crew.panelW', String(w)); } catch (x) { /* private mode */ }
    };
    bar.addEventListener('pointermove', move);
    bar.addEventListener('pointerup', up);
  });
  bar.addEventListener('dblclick', () => {
    document.documentElement.style.removeProperty('--panel-w');
    try { localStorage.removeItem('crew.panelW'); } catch (e) { /* the browser keeps no site data */ }
  });
})();

// ------------------------------------------------------------------ previews and the files list

const FILE_ICONS = { web: 'globe', doc: 'doc', image: 'image', pdf: 'doc', table: 'table', file: 'doc' };

export function kindOf(name) {
  const ext = String(name).split('.').pop().toLowerCase();
  return { html: 'web', htm: 'web', md: 'doc', txt: 'doc', png: 'image', jpg: 'image', jpeg: 'image', gif: 'image', svg: 'image', webp: 'image',
    pdf: 'pdf', csv: 'table' }[ext] || 'file';
}

function filesView(files) {
  const box = h('div', { class: 'files-tree' }, h('div', { class: 'muted small', style: { padding: '8px' } }, 'Loading…'));
  const wrap = h('div', { class: 'livebox' }, h('div', { class: 'screen-bar' }, h('b', { class: 'grow', style: { padding: '0 6px' } }, files ? files.title || 'Files' : 'Files'),
    btn('', () => panel.show('files'), { cls: 'icon ghost', ic: 'reload', title: 'Refresh' })), box);
  if (!files) { clear(box, h('div', { class: 'muted small', style: { padding: '8px' } }, 'No files here.')); return wrap; }
  files.load().then((list) => {
    clear(box, ...(list.length ? list.map((f) => h('button', { type: 'button', onclick: () => panel.open('preview', { name: f.name, url: f.url, kind: f.kind || kindOf(f.name) }) },
      icon(FILE_ICONS[f.kind || kindOf(f.name)] || 'doc'), h('span', null, f.name), f.size != null ? h('small', { class: 'muted' }, bytesShort(f.size)) : null))
      : [h('div', { class: 'muted small', style: { padding: '8px' } }, 'Nothing here yet. Files made in this conversation appear here.')]));
  }).catch(fail);
  return wrap;
}

const bytesShort = (n) => (n < 1024 ? `${n} B` : n < 1048576 ? `${Math.round(n / 1024)} KB` : `${(n / 1048576).toFixed(1)} MB`);

function preview(item) {
  if (!item) {
    return h('div', { class: 'screen-wrap' }, h('div', { class: 'placeholder' }, icon('doc'),
      h('div', null, 'Pages, documents and pictures made for you open here.')));
  }
  const name = String(item.name || '').split('/').pop() || 'Preview';
  const bar = h('div', { class: 'screen-bar' }, h('b', { class: 'grow ellipsis', style: { padding: '0 6px' } }, name),
    btn('', () => { const f = box.querySelector('iframe'); if (f) f.setAttribute('src', f.src); else panel.show('preview'); }, { cls: 'icon ghost', ic: 'reload', title: 'Reload' }),
    btn('', () => window.open(item.url, '_blank', 'noopener'), { cls: 'icon ghost', ic: 'external', title: 'Open in a new window' }),
    btn('', () => download(item.url, name), { cls: 'icon ghost', ic: 'download', title: 'Download' }));
  const box = h('div', { class: 'livebox' }, bar);
  const kind = item.kind || kindOf(name);
  if (kind === 'web') {
    box.append(h('iframe', { class: 'preview-frame', src: item.url, sandbox: 'allow-scripts allow-forms allow-popups allow-modals', title: name }));
  } else if (kind === 'pdf') {
    box.append(h('iframe', { class: 'preview-frame', src: item.url, title: name }));
  } else if (kind === 'image') {
    box.append(h('div', { class: 'preview-img' }, h('img', { src: item.url, alt: name })));
  } else if (kind === 'doc' || kind === 'table') {
    const doc = h('div', { class: 'preview-doc md' }, h('p', { class: 'muted' }, 'Opening…'));
    box.append(doc);
    fetch(item.url, { credentials: 'same-origin' }).then((r) => r.text()).then((text) => {
      if (kind === 'table' || /\.csv$/i.test(name)) doc.innerHTML = csvTable(text);
      else if (/\.(md|markdown)$/i.test(name)) doc.innerHTML = markdown(text);
      else clear(doc, h('pre', { style: { whiteSpace: 'pre-wrap', fontFamily: 'var(--mono)', fontSize: '13px' } }, text));
    }).catch(fail);
  } else {
    box.append(h('div', { class: 'screen-wrap' }, h('div', { class: 'placeholder' }, icon('doc'), h('div', null, 'This file cannot be shown here.'),
      btn('Download it', () => download(item.url, name), { cls: 'primary', ic: 'download' }))));
  }
  return box;
}

function csvTable(text) {
  const rows = [];
  let row = [], cell = '', q = false;
  for (let i = 0; i < text.length; i++) {
    const c = text[i];
    if (q) {
      if (c === '"' && text[i + 1] === '"') { cell += '"'; i++; } else if (c === '"') q = false; else cell += c;
    } else if (c === '"') q = true;
    else if (c === ',') { row.push(cell); cell = ''; } else if (c === '\n' || c === '\r') {
      if (c === '\r' && text[i + 1] === '\n') i++;
      row.push(cell); rows.push(row); row = []; cell = '';
    } else cell += c;
  }
  if (cell || row.length) { row.push(cell); rows.push(row); }
  if (!rows.length) return '<p class="muted">Empty file.</p>';
  const [head, ...rest] = rows.slice(0, 1001);
  return '<table><thead><tr>' + head.map((c) => `<th>${esc(c)}</th>`).join('') + '</tr></thead><tbody>' +
    rest.map((r) => '<tr>' + head.map((_, k) => `<td>${esc(r[k] || '')}</td>`).join('') + '</tr>').join('') + '</tbody></table>' +
    (rows.length > 1001 ? `<p class="muted">Showing the first 1,000 of ${rows.length - 1} rows.</p>` : '');
}

// ------------------------------------------------------------------ theme button

$('#themeBtn').addEventListener('click', async () => {
  const look = (store.overview && store.overview.app) || {};
  const dark = document.documentElement.dataset.theme === 'dark' ||
    (!document.documentElement.dataset.theme && window.matchMedia('(prefers-color-scheme: dark)').matches);
  look.theme = dark ? 'light' : 'dark';
  applyLook(look);
  try { await api('/api/settings', { method: 'PUT', body: { app: { theme: look.theme } } }); } catch (e) { fail(e); }
});

// ------------------------------------------------------------------ updates (keep everything; one click)

function showUpdate(info) {
  const bannerEl = $('#updateBanner');
  if (!info || !info.available || !store.isLocal) { bannerEl.classList.add('hidden'); return; }
  const notes = (info.notes || []).slice(0, 3);
  clear(bannerEl, icon('cloud'),
    h('div', { class: 'grow' }, h('b', null, `A new version of Crew is ready (${info.latest}). `),
      h('span', { class: 'muted' }, notes.length ? notes.join(' · ') : 'Improvements and fixes.'),
      h('div', { class: 'small muted' }, 'Your chats, projects, sign-ins, API keys and settings stay exactly as they are.')),
    btn('Update now', () => installUpdate(info), { cls: 'accent sm', ic: 'download' }),
    btn('Later', () => bannerEl.classList.add('hidden'), { cls: 'ghost sm' }));
  bannerEl.classList.remove('hidden');
}

function updatingCover(info) {
  return h('div', { class: 'overlay-full' }, h('div', { class: 'stack', style: { justifyItems: 'center' } },
    h('span', { class: 'logo-mark', style: { width: '48px', height: '48px' } }, icon('spark')),
    h('h1', null, 'Updating Crew…'),
    h('p', { class: 'muted' }, `Installing version ${info && info.latest ? info.latest : 'the newest version'}. Crew reopens by itself — this can take a minute or two.`),
    h('p', { class: 'muted small' }, 'Your chats, projects, sign-ins, API keys and settings are kept.'),
    h('span', { class: 'spinner', style: { width: '26px', height: '26px' } })));
}

// Where is Crew now? Here, or — after a restart — on one of its other ports (Windows can keep a port for a few
// minutes after a program closes). Another port cannot be read from this page, but it can be reached.
async function findCrew(expected) {
  const here = Number(location.port || (location.protocol === 'https:' ? 443 : 80));
  try {
    const r = await fetch('/api/ping', { cache: 'no-store' });
    if (r.ok) {
      const d = await r.json();
      if (!expected || d.version === expected) return here;
    }
  } catch (e) { /* not here (yet) */ }
  for (let p = 8765; p < 8775; p++) {
    if (p === here) continue;
    try {
      await fetch(`${location.protocol}//${location.hostname}:${p}/api/ping`, { mode: 'no-cors', cache: 'no-store' });
      return p;
    } catch (e) { /* nothing there */ }
  }
  return null;
}

function goTo(port) {
  const here = Number(location.port || (location.protocol === 'https:' ? 443 : 80));
  if (port === here) location.reload();
  else location.href = `${location.protocol}//${location.hostname}:${port}/${location.hash}`;
}

// Crew restarts after an update (by the owner's click, or automatically at a quiet moment): wait for it and
// reconnect, wherever it comes back.
function waitForRestart(cover, expected) {
  const started = Date.now();
  const tick = async () => {
    await new Promise((r) => setTimeout(r, 2000));
    if (!cover.isConnected) return;  // the update did not go ahead (see update_failed): nothing to wait for
    if (Date.now() - started > 5000) {
      const port = await findCrew(expected);
      if (port) { goTo(port); return; }
    }
    if (Date.now() - started < 6 * 60000) tick();
    else {
      cover.remove();
      toast('Crew has not come back yet. Close this window and click the Crew icon: it finds Crew wherever it is.', { bad: true, ms: 30000 });
    }
  };
  tick();
}

export async function installUpdate(info) {
  const cover = updatingCover(info);
  document.body.append(cover);
  try {
    await api('/api/update', { method: 'POST', body: {} });
  } catch (e) {
    cover.remove();
    fail(e);
    return;
  }
  waitForRestart(cover, info && info.latest);
}

function showUpdating(info) {
  if (document.querySelector('.overlay-full')) return;
  const cover = updatingCover(info);
  document.body.append(cover);
  waitForRestart(cover, info && info.latest);
}

// After an update: say so once.
function sayUpdated(info) {
  const u = info && info.updated;
  if (!u || u.to !== info.current) return;
  let seen = '';
  try { seen = localStorage.getItem('crew-updated-seen') || ''; } catch (e) { /* private window */ }
  if (seen === u.to) return;
  try { localStorage.setItem('crew-updated-seen', u.to); } catch (e) { /* private window */ }
  toast(`Crew ${u.auto ? 'updated itself' : 'is updated'} to version ${u.to}.` + ((info.notes || []).length ? ` New: ${info.notes.slice(0, 2).join(' · ')}` : ''), { ms: 12000 });
}
store.installUpdate = installUpdate;

// A settings file Crew could not read was set aside and the last good settings are in use: say so, once.
function tellSettingsProblem(p) {
  if (!p || !p.at) return;
  let seen = '';
  try { seen = localStorage.getItem('crew.settingsProblemSeen') || ''; } catch (e) { /* private window */ }
  if (seen === String(p.at)) return;
  try { localStorage.setItem('crew.settingsProblemSeen', String(p.at)); } catch (e) { /* private window */ }
  toast(`Crew could not read your settings file, so it is using ${p.restored}. The file is kept as ${p.kept} in Crew’s folder.`,
    { bad: true, ms: 20000, action: 'Details', onAction: () => { location.hash = '#/settings'; } });
}

// A database Crew could not read was kept under another name and a new one started: say so, once for each.
function tellDataProblems(list) {
  let seen = [];
  try { seen = JSON.parse(localStorage.getItem('crew.dataProblemsSeen') || '[]'); } catch (e) { /* private window */ }
  const fresh = (list || []).filter((p) => p && p.kept && !seen.includes(p.kept));
  if (!fresh.length) return;
  try { localStorage.setItem('crew.dataProblemsSeen', JSON.stringify([...seen, ...fresh.map((p) => p.kept)].slice(-40))); } catch (e) { /* private window */ }
  for (const p of fresh) {
    toast(`Crew could not read ${p.what} (the file was damaged), so it started a new one. The old file is kept, untouched, as ${p.kept} in Crew’s folder.`,
      { bad: true, ms: 20000 });
  }
}

// ------------------------------------------------------------------ start

async function boot() {
  try {
    store.overview = await api('/api/overview');
  } catch (e) {
    if (e.status === 401) { location.reload(); return; }
    $('#view').append(h('div', { class: 'page' }, h('div', { class: 'empty' }, e.message, btn('Try again', () => location.reload(), { cls: 'primary' }))));
    return;
  }
  store.isLocal = !!store.overview.local;
  applyLook(store.overview.app || {});
  tellSettingsProblem(store.overview.settings_problem);
  tellDataProblems(store.overview.data_problems);
  window.addEventListener('hashchange', route);
  route();
  refreshRecent();
  refreshMeters();
  bus.on('chats', refreshRecent);
  bus.on('runs', refreshRecent);
  bus.on('rate', () => { clearTimeout(boot.mt); boot.mt = setTimeout(refreshMeters, 800); });
  setInterval(refreshRecent, 20000);
  setInterval(refreshMeters, 60000);
  stream('/api/events', {
    update: (info) => showUpdate(info),
    updating: (info) => showUpdating(info),
    update_failed: (d) => {
      document.querySelectorAll('.overlay-full').forEach((el) => el.remove());
      toast(d.message || 'The update did not install. Crew carries on as it was.', { bad: true, ms: 15000 });
    },
    notice: (n) => {
      toast(n.text || 'Done.', { bad: n.status === 'failed', ms: 7000, action: n.kind === 'workflow' ? 'Open' : null, onAction: () => { location.hash = '#/workflows'; } });
      if (n.kind === 'workflow') { bus.emit('workflows'); refreshRecent(); }
    },
  });
  api('/api/update').then((info) => { showUpdate(info); sayUpdated(info); }).catch(() => {});
  if ('serviceWorker' in navigator && window.isSecureContext) {
    navigator.serviceWorker.register('/sw.js').catch(() => { /* offline support is optional */ });
  }
  window.addEventListener('offline', () => toast('You are offline. Crew will reconnect by itself.', { bad: true }));
}

boot();
