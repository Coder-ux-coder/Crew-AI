// Library: everything made for you in one place — files from your chats, results of team projects, and the
// screenshots and recordings you took (draw on them, copy them, or ask about them).

import { h, icon, btn, api, fail, store, bus, ago, bytes, clear } from '../ui.js';
import { openViewer, captureScreen, recordScreen, canCaptureScreen, saveCapture } from '../live.js';

const FILE_ICONS = { web: 'globe', doc: 'doc', image: 'image', pdf: 'doc', table: 'table', file: 'doc' };

export function libraryPage(view) {
  let tab = 'files';
  let caps = [];
  const body = h('div', { class: 'stack', style: { gap: '16px' } });
  const seg = h('div', { class: 'seg' }, [['files', 'Files made for you'], ['captures', 'Captures']].map(([k, l]) => h('button', {
    type: 'button', class: k === tab ? 'on' : '', onclick: (e) => { tab = k; [...seg.children].forEach((b) => b.classList.toggle('on', b === e.currentTarget)); draw(); },
  }, l)));
  store.setTop(null, []);
  view.append(h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Library'), h('p', null, 'Pages, documents, spreadsheets and pictures made for you, and your captures.'))),
    seg, body));

  async function draw() {
    if (tab === 'files') return drawFiles();
    return drawCaptures();
  }

  async function drawFiles() {
    clear(body, h('div', { class: 'muted' }, 'Loading…'));
    try {
      const r = await api('/api/library');
      if (tab !== 'files') return;  // the owner moved to the other tab meanwhile: it is not drawn over
      clear(body, ...(r.items.length ? [h('div', { class: 'list' }, r.items.map((f) => h('div', { class: 'li', style: { cursor: 'pointer' }, onclick: () => bus.emit('panel:preview', f) },
        h('span', { class: 'li-ico' }, icon(FILE_ICONS[f.kind] || 'doc')),
        h('span', { class: 'li-main' }, h('b', null, f.name.split('/').pop()), h('small', null, `${f.where} · ${ago(f.modified)} · ${bytes(f.size)}`)),
        h('a', { class: 'btn sm ghost', href: f.origin, onclick: (e) => e.stopPropagation() }, 'Open the chat'))))]
        : [h('div', { class: 'empty' }, icon('folder'), 'Nothing yet. Ask Claude to make a page, a document or a spreadsheet, and it appears here.')]));
    } catch (e) { fail(e); }
  }

  async function drawCaptures() {
    const gallery = h('div', { class: 'gallery' });
    const upload = h('input', {
      type: 'file', accept: 'image/png,image/jpeg,video/webm,video/mp4', multiple: true, class: 'hidden', onchange: async (e) => {
        for (const f of e.target.files) {
          const ext = (f.name.split('.').pop() || 'png').toLowerCase();
          try { await saveCapture(f, ext === 'jpeg' ? 'jpg' : ext, f.name.replace(/\.[^.]+$/, '')); } catch (x) { fail(x); }
        }
        e.target.value = '';
      },
    });
    clear(body, h('div', { class: 'row wrap' },
      canCaptureScreen ? btn('Capture my screen', () => captureScreen(), { cls: 'primary sm', ic: 'camera' }) : null,
      canCaptureScreen ? btn('Record my screen', () => recordScreen(), { cls: 'sm rec', ic: 'record' }) : null,
      btn('Browser picture', async () => { try { const info = await api('/api/browser/screenshot', { method: 'POST', body: { full: false } }); bus.emit('captures'); openViewer(info); } catch (e) { fail(e); } }, { cls: 'sm', ic: 'globe' }),
      btn('Phone picture', async () => { try { const info = await api('/api/phone/screenshot', { method: 'POST', body: {} }); bus.emit('captures'); openViewer(info); } catch (e) { fail(e); } }, { cls: 'sm', ic: 'phone' }),
      btn('Add from this device', () => upload.click(), { cls: 'sm', ic: 'plus' }), upload), gallery);
    try { caps = (await api('/api/captures')).captures; } catch (e) { fail(e); }
    if (tab !== 'captures') return;
    clear(gallery, ...(caps.length ? caps.map((c) => h('div', { class: 'shot', role: 'button', tabindex: 0, onclick: () => openViewer(c, { onChange: draw }) },
      c.kind === 'video' ? h('video', { src: c.url + '#t=0.5', preload: 'metadata', muted: true }) : h('img', { src: c.url, loading: 'lazy', alt: c.name }),
      c.kind === 'video' ? h('span', { class: 'kind' }, 'Recording') : null,
      h('div', null, h('span', null, ago(c.created)), h('span', null, bytes(c.size)))))
      : [h('div', { class: 'empty', style: { gridColumn: '1/-1' } }, icon('camera'), 'Screenshots and recordings you make appear here.')]));
  }

  draw();
  return bus.on('captures', () => { if (tab === 'captures') draw(); });
}
