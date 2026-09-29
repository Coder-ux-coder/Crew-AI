// Live screens: the app's own browser and the owner's Android phone, shown as
// moving pictures you can click, type into and record. Plus screen capture,
// screen recording, and the capture viewer with drawing tools.

import { $, h, icon, iconBtn, btn, api, stream, toast, fail, ask, confirmBox, duration, bus, store, download, isSmall } from './ui.js';

// ------------------------------------------------------------------ frames → canvas

class Screen {
  constructor(cls = '') {
    this.canvas = h('canvas', { class: ('screen ' + cls).trim(), tabindex: 0, width: 1280, height: 800, 'aria-label': 'Live screen' });
    this.ctx = this.canvas.getContext('2d');
    this.frames = 0;
    this.latest = null;
    this.busy = false;
  }

  draw(b64, mime = 'image/jpeg') {
    this.latest = { b64, mime };
    if (!this.busy) this.next();
  }

  async next() {
    const f = this.latest;
    if (!f) return;
    this.latest = null;
    this.busy = true;
    try {
      const img = new Image();
      img.src = `data:${f.mime};base64,${f.b64}`;
      await img.decode();
      if (this.canvas.width !== img.naturalWidth || this.canvas.height !== img.naturalHeight) {
        this.canvas.width = img.naturalWidth;
        this.canvas.height = img.naturalHeight;
      }
      this.ctx.drawImage(img, 0, 0);
      this.last = img;
      this.frames++;
      this.onframe && this.onframe();
    } catch (e) { /* a broken frame: skip it */ }
    this.busy = false;
    if (this.latest) this.next();
  }

  // Draw the last picture again: a recording needs a steady stream of frames even when nothing moves.
  repaint() {
    if (this.last) this.ctx.drawImage(this.last, 0, 0);
  }

  // A video stream of this screen, for recording.
  stream(fps) {
    const ms = this.canvas.captureStream(fps);
    const timer = setInterval(() => this.repaint(), Math.round(1000 / fps));
    ms.getVideoTracks().forEach((t) => t.addEventListener('ended', () => clearInterval(timer)));
    ms.stopRepaint = () => clearInterval(timer);
    return ms;
  }

  rel(e) {
    const r = this.canvas.getBoundingClientRect();
    return { x: Math.min(1, Math.max(0, (e.clientX - r.left) / r.width)), y: Math.min(1, Math.max(0, (e.clientY - r.top) / r.height)) };
  }
}

// ------------------------------------------------------------------ recording

function pickMime() {
  for (const m of ['video/webm;codecs=vp9', 'video/webm;codecs=vp8', 'video/webm', 'video/mp4']) {
    if (window.MediaRecorder && MediaRecorder.isTypeSupported(m)) return m;
  }
  return '';
}

export async function saveCapture(blob, ext, label) {
  const info = await api(`/api/captures?ext=${ext}&label=${encodeURIComponent(label)}`, { method: 'POST', raw: blob });
  bus.emit('captures');
  toast(ext === 'png' ? 'Screenshot saved in Captures.' : 'Recording saved in Captures.', {
    action: 'Open', onAction: () => openViewer(info),
  });
  return info;
}

// Records a MediaStream; shows a floating "recording" pill with a timer and a Stop button.
export class Recorder {
  constructor(mediaStream, label, { onstop = null, stopTracks = false } = {}) {
    this.stream = mediaStream;
    this.label = label;
    this.onstop = onstop;
    this.stopTracks = stopTracks;
    this.chunks = [];
    this.mime = pickMime();
  }

  start() {
    if (!this.mime) { toast('This browser cannot record video.', { bad: true }); return false; }
    this.mr = new MediaRecorder(this.stream, { mimeType: this.mime, videoBitsPerSecond: 4_000_000 });
    this.mr.ondataavailable = (e) => { if (e.data && e.data.size) this.chunks.push(e.data); };
    this.mr.onstop = () => this.finish();
    this.mr.start(1000);
    this.started = Date.now();
    const time = h('span', { class: 'rec-time' }, '0:00');
    this.pill = h('div', { class: 'recpill' }, h('span', { class: 'reddot' }), h('span', null, `Recording ${this.label}`), time,
      h('button', { class: 'btn sm danger', onclick: () => this.stop() }, icon('stop'), 'Stop'));
    $('#recbar').append(this.pill);
    this.timer = setInterval(() => { time.textContent = duration((Date.now() - this.started) / 1000); }, 500);
    for (const t of this.stream.getVideoTracks()) t.addEventListener('ended', () => this.stop());
    return true;
  }

  stop() {
    if (this.mr && this.mr.state !== 'inactive') this.mr.stop();
  }

  get recording() { return !!this.mr && this.mr.state === 'recording'; }

  async finish() {
    clearInterval(this.timer);
    this.pill && this.pill.remove();
    if (this.stream.stopRepaint) this.stream.stopRepaint();
    if (this.stopTracks) this.stream.getTracks().forEach((t) => t.stop());
    const blob = new Blob(this.chunks, { type: this.mime.split(';')[0] });
    this.onstop && this.onstop();
    if (!blob.size) return;
    try { await saveCapture(blob, this.mime.includes('mp4') ? 'mp4' : 'webm', this.label); } catch (e) { fail(e); }
  }
}

// ------------------------------------------------------------------ whole-screen capture (desktop browsers)

export const canCaptureScreen = !!(navigator.mediaDevices && navigator.mediaDevices.getDisplayMedia);

export async function captureScreen() {
  if (!canCaptureScreen) { toast('This browser cannot capture the screen.', { bad: true }); return; }
  let ms;
  try { ms = await navigator.mediaDevices.getDisplayMedia({ video: true, audio: false }); } catch (e) { return; }
  try {
    const video = h('video', { muted: true, playsInline: true });
    video.srcObject = ms;
    await video.play();
    await new Promise((r) => setTimeout(r, 350)); // let the first real frame arrive
    const c = h('canvas', { width: video.videoWidth, height: video.videoHeight });
    c.getContext('2d').drawImage(video, 0, 0);
    const blob = await new Promise((r) => c.toBlob(r, 'image/png'));
    await saveCapture(blob, 'png', 'screen');
  } catch (e) { fail(e); } finally { ms.getTracks().forEach((t) => t.stop()); }
}

export async function recordScreen() {
  if (!canCaptureScreen) { toast('This browser cannot record the screen.', { bad: true }); return; }
  let ms;
  try { ms = await navigator.mediaDevices.getDisplayMedia({ video: { frameRate: 30 }, audio: true }); } catch (e) { return; }
  new Recorder(ms, 'the screen', { stopTracks: true }).start();
}

// ------------------------------------------------------------------ the app's browser

export class LiveBrowser {
  constructor() {
    this.screen = new Screen();
    this.meta = {};
    this.queue = Promise.resolve();
    this.typed = '';
    this.es = null;
    this.recorder = null;
    this.build();
  }

  build() {
    const s = this.screen;
    this.url = h('input', {
      type: 'text', placeholder: 'Search or type a web address', 'aria-label': 'Web address', spellcheck: false,
      onfocus: (e) => e.target.select(),
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); this.go(this.url.value); this.url.blur(); } },
    });
    this.devBtns = {
      desktop: iconBtn('monitor', 'Show as a computer', () => this.device('desktop')),
      phone: iconBtn('phone', 'Show as a phone', () => this.device('phone')),
    };
    this.recBtn = btn('', () => this.toggleRecord(), { cls: 'icon ghost rec', ic: 'record', title: 'Record the browser' });
    this.kbBtn = iconBtn('keyboard', 'Type into the page', () => this.toggleTypebar());
    const bar = h('div', { class: 'screen-bar' },
      iconBtn('left', 'Back', () => this.act('back')),
      iconBtn('right', 'Forward', () => this.act('forward')),
      iconBtn('reload', 'Reload', () => this.act('reload')),
      h('div', { class: 'url' }, this.url),
      this.devBtns.desktop, this.devBtns.phone,
      h('span', { class: 'sep' }),
      iconBtn('camera', 'Screenshot', () => this.shot(false)),
      iconBtn('page', 'Screenshot of the whole page', () => this.shot(true)),
      this.recBtn, this.kbBtn,
      iconBtn('external', 'Open in my usual browser', () => this.meta.url && window.open(this.meta.url, '_blank', 'noopener')));
    this.banner = h('div', { class: 'ai-banner hidden' }, h('span', { class: 'pill live' }, 'The assistant is using the browser'));
    this.note = h('div', { class: 'placeholder' }, h('div', { class: 'typing' }, h('i'), h('i'), h('i')), h('div', null, 'Starting the browser…'));
    s.canvas.classList.add('hidden');
    this.wrap = h('div', { class: 'screen-wrap' }, this.banner, this.note, s.canvas);
    this.typeInput = h('input', {
      type: 'text', placeholder: 'Type here, then press Enter to put it into the page', 'aria-label': 'Text for the page',
      onkeydown: (e) => {
        if (e.key === 'Enter') {
          e.preventDefault();
          const v = this.typeInput.value;
          this.typeInput.value = '';
          this.send('type', { text: v, submit: e.shiftKey });
        }
      },
    });
    this.typebar = h('div', { class: 'typebar hidden' }, this.typeInput,
      btn('Send', () => { const v = this.typeInput.value; this.typeInput.value = ''; this.send('type', { text: v }); }, { cls: 'sm' }),
      btn('Enter', () => this.send('press', { key: 'enter' }), { cls: 'sm ghost' }));
    this.root = h('div', { class: 'livebox' }, bar, this.wrap, this.typebar);
    if (isSmall()) this.typebar.classList.remove('hidden');
    this.bindInput();
    s.onframe = () => {
      if (s.frames === 1) { this.note.classList.add('hidden'); s.canvas.classList.remove('hidden'); }
    };
  }

  bindInput() {
    const c = this.screen.canvas;
    let down = null;
    c.addEventListener('pointerdown', (e) => { down = { ...this.screen.rel(e), px: e.clientX, py: e.clientY }; c.focus({ preventScroll: true }); });
    c.addEventListener('pointerup', (e) => {
      if (!down) return;
      const dy = e.clientY - down.py, dx = e.clientX - down.px;
      if (Math.hypot(dx, dy) < 8) this.send('click', { x: down.x, y: down.y });
      else if (Math.abs(dy) > Math.abs(dx)) {
        const r = c.getBoundingClientRect();
        this.send('scroll', { dy: -dy / r.height * (this.meta.device === 'phone' ? 915 : 800) });
      }
      down = null;
    });
    let wheel = 0, wt = null;
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      wheel += e.deltaY * (e.deltaMode === 1 ? 32 : 1);
      clearTimeout(wt);
      wt = setTimeout(() => { const dy = wheel; wheel = 0; this.send('scroll', { dy }); }, 90);
    }, { passive: false });
    const SPECIAL = new Set(['Enter', 'Backspace', 'Tab', 'Escape', 'Delete', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight',
      'PageUp', 'PageDown', 'Home', 'End']);
    let tt = null;
    const flush = () => { if (this.typed) { const t = this.typed; this.typed = ''; this.send('type', { text: t }); } };
    c.addEventListener('keydown', (e) => {
      if ((e.ctrlKey || e.metaKey) && e.key.toLowerCase() === 'v') {
        e.preventDefault();
        navigator.clipboard.readText().then((t) => t && this.send('type', { text: t })).catch(() => toast('Paste into the typing box instead.'));
        return;
      }
      if (e.ctrlKey || e.metaKey || e.altKey) {
        if (e.key.length === 1) { e.preventDefault(); flush(); this.send('press', { key: (e.ctrlKey || e.metaKey ? 'Control+' : 'Alt+') + e.key }); }
        return;
      }
      if (e.key.length === 1) {
        e.preventDefault();
        this.typed += e.key;
        clearTimeout(tt);
        tt = setTimeout(flush, 70);
      } else if (SPECIAL.has(e.key)) {
        e.preventDefault();
        clearTimeout(tt);
        flush();
        this.send('press', { key: e.key });
      }
    });
  }

  // Actions go one after another, in order.
  send(action, body = {}) {
    this.queue = this.queue.then(() => api('/api/browser/' + action, { method: 'POST', body })).catch(fail);
    return this.queue;
  }

  act(a) { this.send(a); }

  go(url) { if (url.trim()) this.send('navigate', { url: url.trim() }); }

  device(d) { this.send('device', { device: d }).then(() => this.setMeta({ ...this.meta, device: d })); }

  async shot(full) {
    try {
      const info = await api('/api/browser/screenshot', { method: 'POST', body: { full } });
      bus.emit('captures');
      toast('Screenshot saved in Captures.', { action: 'Open', onAction: () => openViewer(info) });
    } catch (e) { fail(e); }
  }

  toggleRecord() {
    if (this.recorder && this.recorder.recording) { this.recorder.stop(); return; }
    const ms = this.screen.stream(20);
    this.recorder = new Recorder(ms, 'the browser', { onstop: () => this.recBtn.classList.remove('on') });
    if (this.recorder.start()) this.recBtn.classList.add('on');
  }

  toggleTypebar() {
    this.typebar.classList.toggle('hidden');
    this.kbBtn.classList.toggle('on', !this.typebar.classList.contains('hidden'));
    if (!this.typebar.classList.contains('hidden')) this.typeInput.focus();
  }

  setMeta(m) {
    this.meta = { ...this.meta, ...m };
    if (document.activeElement !== this.url && this.meta.url !== undefined) this.url.value = /^(data:|about:)/.test(this.meta.url || '') ? '' : this.meta.url;
    for (const [k, b] of Object.entries(this.devBtns)) b.classList.toggle('on', this.meta.device === k);
    if (this.meta.driver === 'assistant') {
      this.banner.classList.remove('hidden');
      clearTimeout(this.bannerT);
      this.bannerT = setTimeout(() => this.banner.classList.add('hidden'), 5000);
    }
    this.screen.canvas.classList.toggle('phone-frame', this.meta.device === 'phone');
  }

  showError(msg) {
    this.note.replaceChildren(icon('info'), h('div', null, msg), btn('Try again', () => { this.disconnect(); this.connect(); }, { cls: 'sm' }));
    this.note.classList.remove('hidden');
    this.screen.canvas.classList.add('hidden');
    this.screen.frames = 0;
  }

  connect() {
    if (this.es) return;
    this.es = stream('/api/browser/events', {
      frame: (d) => this.screen.draw(d.data),
      meta: (d) => { this.setMeta(d); if (d.error && !d.running) this.showError(d.error); },
      error: (d) => { if (d && d.message) this.showError(d.message); },
    });
  }

  disconnect() {
    if (this.es) { this.es.close(); this.es = null; }
  }

  mount(container) {
    container.append(this.root);
    this.connect();
  }

  unmount() {
    this.root.remove();
    if (!(this.recorder && this.recorder.recording)) this.disconnect();
  }
}

// ------------------------------------------------------------------ the owner's Android phone

export class LivePhone {
  constructor() {
    this.screen = new Screen('phone-frame');
    this.queue = Promise.resolve();
    this.es = null;
    this.status = null;
    this.recorder = null;
    this.root = h('div', { class: 'livebox' });
    this.bindInput();
  }

  send(action, body = {}) {
    this.queue = this.queue.then(() => api('/api/phone/' + action, { method: 'POST', body })).catch(fail);
    return this.queue;
  }

  bindInput() {
    const c = this.screen.canvas;
    let down = null;
    c.addEventListener('pointerdown', (e) => { down = { ...this.screen.rel(e), t: Date.now() }; c.focus({ preventScroll: true }); });
    c.addEventListener('pointerup', (e) => {
      if (!down) return;
      const up = this.screen.rel(e);
      if (Math.hypot(up.x - down.x, up.y - down.y) < 0.02) this.send('tap', { x: down.x, y: down.y });
      else this.send('swipe', { x1: down.x, y1: down.y, x2: up.x, y2: up.y, ms: Math.min(1200, Math.max(120, Date.now() - down.t)) });
      down = null;
    });
    let wt = null, wheel = 0;
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      wheel += e.deltaY;
      clearTimeout(wt);
      wt = setTimeout(() => { const d = wheel; wheel = 0; this.send('swipe', { direction: d > 0 ? 'up' : 'down' }); }, 140);
    }, { passive: false });
    let typed = '', tt = null;
    const flush = () => { if (typed) { const t = typed; typed = ''; this.send('type', { text: t }); } };
    c.addEventListener('keydown', (e) => {
      if (e.ctrlKey || e.metaKey || e.altKey) return;
      const keys = { Enter: 'enter', Backspace: 'delete', Escape: 'back', Tab: 'tab' };
      if (e.key.length === 1) { e.preventDefault(); typed += e.key; clearTimeout(tt); tt = setTimeout(flush, 120); }
      else if (keys[e.key]) { e.preventDefault(); clearTimeout(tt); flush(); this.send('key', { key: keys[e.key] }); }
    });
  }

  async refresh() {
    try { this.status = await api('/api/phone/status'); } catch (e) { this.status = { available: false, connected: false, reason: e.message }; }
    this.render();
  }

  render() {
    const st = this.status || {};
    this.disconnect();
    if (!st.available) {
      this.root.replaceChildren(h('div', { class: 'screen-wrap' }, h('div', { class: 'placeholder' }, icon('phone'),
        h('h3', null, 'The phone connector is not installed yet'),
        h('div', null, st.reason || 'Run the Crew installer on this computer; it adds Android’s phone connector.'),
        btn('Check again', () => this.refresh(), { cls: 'sm' }))));
      return;
    }
    if (!st.connected) { this.root.replaceChildren(this.wizard(st)); return; }
    this.showLive(st);
  }

  showLive(st) {
    this.recBtn = btn('', () => this.toggleRecord(), { cls: 'icon ghost rec', ic: 'record', title: 'Record the phone screen' });
    this.typeInput = h('input', {
      type: 'text', placeholder: 'Type English text for the phone, then Enter', 'aria-label': 'Text for the phone',
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); const v = this.typeInput.value; this.typeInput.value = ''; if (v) this.send('type', { text: v }); } },
    });
    this.typebar = h('div', { class: 'typebar' + (isSmall() ? '' : ' hidden') }, this.typeInput,
      btn('Enter', () => this.send('key', { key: 'enter' }), { cls: 'sm ghost' }));
    const bar = h('div', { class: 'screen-bar' },
      h('span', { class: 'pill ok' }, h('span', { class: 'dot ok' }), st.device || 'Phone connected'),
      h('span', { class: 'grow' }),
      iconBtn('apps', 'Open an app', async () => {
        const v = await ask('Open an app', [{ name: 'name', label: 'App name', placeholder: 'WhatsApp, Chrome, Settings…', required: true }], { ok: 'Open' });
        if (v) this.send('open_app', { name: v.name });
      }),
      iconBtn('keyboard', 'Type on the phone', () => { this.typebar.classList.toggle('hidden'); this.typeInput.focus(); }),
      iconBtn('camera', 'Screenshot', async () => {
        try { const info = await api('/api/phone/screenshot', { method: 'POST', body: {} }); bus.emit('captures'); toast('Screenshot saved in Captures.', { action: 'Open', onAction: () => openViewer(info) }); } catch (e) { fail(e); }
      }),
      this.recBtn);
    this.banner = h('div', { class: 'ai-banner hidden' }, h('span', { class: 'pill live' }, 'The assistant is using your phone'));
    this.note = h('div', { class: 'placeholder' }, h('div', { class: 'typing' }, h('i'), h('i'), h('i')), h('div', null, 'Showing your phone’s screen…'));
    this.screen.canvas.classList.add('hidden');
    this.screen.frames = 0;
    this.screen.onframe = () => { if (this.screen.frames === 1) { this.note.classList.add('hidden'); this.screen.canvas.classList.remove('hidden'); } };
    const side = h('div', { class: 'phone-side' },
      btn('Back', () => this.send('key', { key: 'back' }), { cls: 'sm', ic: 'triangle' }),
      btn('Home', () => this.send('key', { key: 'home' }), { cls: 'sm', ic: 'circle' }),
      btn('Apps', () => this.send('key', { key: 'recents' }), { cls: 'sm', ic: 'square' }),
      btn('Scroll up', () => this.send('swipe', { direction: 'down' }), { cls: 'sm ghost' }),
      btn('Scroll down', () => this.send('swipe', { direction: 'up' }), { cls: 'sm ghost' }),
      btn('Wake', () => this.send('key', { key: 'wake' }), { cls: 'sm ghost' }));
    this.root.replaceChildren(bar, h('div', { class: 'screen-wrap' }, this.banner, this.note, this.screen.canvas), this.typebar, side);
    this.connect();
  }

  wizard(st) {
    const addrPair = h('input', { type: 'text', placeholder: '192.168.1.20:37123', inputmode: 'decimal', autocomplete: 'off' });
    const code = h('input', { type: 'text', placeholder: '6-digit code', inputmode: 'numeric', maxlength: 6, autocomplete: 'off' });
    const addrConn = h('input', { type: 'text', placeholder: '192.168.1.20:41555', inputmode: 'decimal', autocomplete: 'off' });
    const pairBtn = btn('Pair', async () => {
      pairBtn.disabled = true;
      try { const r = await api('/api/phone/pair', { method: 'POST', body: { address: addrPair.value.trim(), code: code.value.trim() } }); toast(r.message || 'Paired.'); addrConn.focus(); } catch (e) { fail(e); }
      pairBtn.disabled = false;
    }, { cls: 'primary sm' });
    const connBtn = btn('Connect', async () => {
      connBtn.disabled = true;
      try { await api('/api/phone/connect', { method: 'POST', body: { address: addrConn.value.trim() } }); toast('Your phone is connected.'); await this.refresh(); } catch (e) { fail(e); }
      connBtn.disabled = false;
    }, { cls: 'primary sm' });
    const step = (n, title, ...body) => h('div', { class: 'step-box' }, h('h3', null, h('span', { class: 'num' }, n), title), ...body);
    return h('div', { class: 'connect' },
      h('div', { class: 'stack' }, h('h2', null, 'Connect your Samsung phone'),
        h('p', { class: 'muted', style: { margin: 0 } }, 'Once connected, you can see and control your phone from here, and the assistant can use it for you when you ask. Your phone and this computer need to be on the same Wi-Fi.')),
      st.needs_approval ? h('div', { class: 'card flat', style: { borderColor: 'var(--warn)' } }, h('b', null, 'Look at your phone: '), 'tap “Allow” to let this computer connect, then press Check again.') : null,
      step(1, 'Turn on Developer options (one time)', h('div', { class: 'muted' }, 'On the phone: Settings → About phone → Software information → tap “Build number” seven times. Enter your PIN if asked.')),
      step(2, 'Turn on Wireless debugging', h('div', { class: 'muted' }, 'Settings → Developer options → switch on “Wireless debugging”, then tap its name to open it.')),
      step(3, 'Pair (one time)', h('div', { class: 'muted' }, 'Tap “Pair device with pairing code”. Type the IP address & port and the code the phone shows:'),
        h('div', { class: 'row wrap' }, h('div', { style: { flex: '2 1 180px' } }, addrPair), h('div', { style: { flex: '1 1 110px' } }, code), pairBtn)),
      step(4, 'Connect', h('div', { class: 'muted' }, 'Back on the Wireless debugging screen, type the “IP address & port” shown at the top:'),
        h('div', { class: 'row wrap' }, h('div', { style: { flex: '2 1 180px' } }, addrConn), connBtn)),
      h('div', { class: 'row wrap between' },
        h('span', { class: 'muted small' }, 'Using a USB cable instead? Plug it in, allow USB debugging on the phone, then:'),
        btn('Check again', () => this.refresh(), { cls: 'sm', ic: 'reload' })));
  }

  toggleRecord() {
    if (this.recorder && this.recorder.recording) { this.recorder.stop(); return; }
    this.recorder = new Recorder(this.screen.stream(12), 'the phone', { onstop: () => this.recBtn.classList.remove('on') });
    if (this.recorder.start()) this.recBtn.classList.add('on');
  }

  connect() {
    if (this.es) return;
    this.es = stream('/api/phone/events', {
      frame: (d) => this.screen.draw(d.data, d.mime),
      status: (d) => {
        if (d.connected === false) {
          this.note.replaceChildren(icon('info'), h('div', null, d.reason || 'The phone stopped answering.'), btn('Check again', () => this.refresh(), { cls: 'sm' }));
          this.note.classList.remove('hidden');
        }
      },
    });
    clearInterval(this.poll);
    this.poll = setInterval(async () => { // who is driving (you or the assistant)
      try { const s = await api('/api/phone/status'); this.banner && this.banner.classList.toggle('hidden', s.driver !== 'assistant'); } catch (e) { /* ignore */ }
    }, 2500);
  }

  disconnect() {
    if (this.es) { this.es.close(); this.es = null; }
    clearInterval(this.poll);
  }

  mount(container) {
    container.append(this.root);
    this.refresh();
  }

  unmount() {
    this.root.remove();
    if (!(this.recorder && this.recorder.recording)) this.disconnect();
  }
}

// ------------------------------------------------------------------ the owner's Windows computer

const COMPUTER_KEYS = { Enter: 'enter', Backspace: 'backspace', Tab: 'tab', Escape: 'esc', Delete: 'delete', ArrowUp: 'up',
  ArrowDown: 'down', ArrowLeft: 'left', ArrowRight: 'right', Home: 'home', End: 'end', PageUp: 'pageup', PageDown: 'pagedown' };

export class LiveComputer {
  constructor() {
    this.screen = new Screen();
    this.queue = Promise.resolve();
    this.es = null;
    this.recorder = null;
    this.rightNext = false;
    this.root = h('div', { class: 'livebox' });
    this.bindInput();
  }

  send(action, body = {}) {
    this.queue = this.queue.then(() => api('/api/computer/' + action, { method: 'POST', body })).catch(fail);
    return this.queue;
  }

  bindInput() {
    const c = this.screen.canvas;
    let down = null;
    c.addEventListener('contextmenu', (e) => e.preventDefault());
    c.addEventListener('pointerdown', (e) => { down = { ...this.screen.rel(e), button: e.button }; c.focus({ preventScroll: true }); });
    c.addEventListener('pointerup', (e) => {
      if (!down) return;
      const up = this.screen.rel(e);
      const right = down.button === 2 || this.rightNext;
      if (Math.hypot(up.x - down.x, up.y - down.y) < 0.01) {
        this.send('click', { xr: down.x, yr: down.y, button: right ? 'right' : 'left' });
      } else {
        this.send('drag', { xr1: down.x, yr1: down.y, xr2: up.x, yr2: up.y });
      }
      if (this.rightNext) this.setRightNext(false);
      down = null;
    });
    // No extra double-click: each click above reaches the computer at once, and two quick ones at one place are a
    // double-click there, as on the computer itself (sending one more made four clicks: a file opened twice).
    let wt = null, wheel = 0;
    c.addEventListener('wheel', (e) => {
      e.preventDefault();
      wheel += e.deltaY;
      clearTimeout(wt);
      wt = setTimeout(() => { const d = wheel; wheel = 0; this.send('scroll', { direction: d > 0 ? 'down' : 'up', amount: Math.min(15, Math.max(1, Math.round(Math.abs(d) / 100))) }); }, 120);
    }, { passive: false });
    let typed = '', tt = null;
    const flush = () => { if (typed) { const t = typed; typed = ''; this.send('type', { text: t }); } };
    c.addEventListener('keydown', (e) => {
      if (e.ctrlKey || e.altKey || e.metaKey) {
        if (e.key.length === 1 || COMPUTER_KEYS[e.key]) {
          e.preventDefault();
          flush();
          const mods = [e.ctrlKey && 'ctrl', e.altKey && 'alt', e.shiftKey && 'shift', e.metaKey && 'win'].filter(Boolean);
          this.send('key', { keys: [...mods, COMPUTER_KEYS[e.key] || e.key.toLowerCase()].join('+') });
        }
        return;
      }
      if (e.key.length === 1) { e.preventDefault(); typed += e.key; clearTimeout(tt); tt = setTimeout(flush, 90); }
      else if (COMPUTER_KEYS[e.key]) { e.preventDefault(); clearTimeout(tt); flush(); this.send('key', { keys: COMPUTER_KEYS[e.key] }); }
    });
  }

  setRightNext(on) {
    this.rightNext = on;
    this.rightBtn && this.rightBtn.classList.toggle('on', on);
  }

  async refresh() {
    let st;
    try { st = await api('/api/computer/status'); } catch (e) { st = { available: false, reason: e.message }; }
    this.disconnect();
    if (!st.available) {
      this.root.replaceChildren(h('div', { class: 'screen-wrap' }, h('div', { class: 'placeholder' }, icon('monitor'),
        h('h3', null, 'Computer control is not available here'), h('div', null, st.reason || ''),
        btn('Check again', () => this.refresh(), { cls: 'sm' }))));
      return;
    }
    this.showLive(st);
  }

  showLive(st) {
    this.recBtn = btn('', () => this.toggleRecord(), { cls: 'icon ghost rec', ic: 'record', title: 'Record the screen' });
    this.rightBtn = btn('Right-click', () => this.setRightNext(!this.rightNext), { cls: 'sm ghost', title: 'Make the next tap a right-click' });
    this.typeInput = h('input', {
      type: 'text', placeholder: 'Type here (any language), then Enter to send it to the computer', 'aria-label': 'Text for the computer',
      onkeydown: (e) => { if (e.key === 'Enter') { e.preventDefault(); const v = this.typeInput.value; this.typeInput.value = ''; if (v) this.send('type', { text: v }); } },
    });
    this.typebar = h('div', { class: 'typebar' + (isSmall() ? '' : ' hidden') }, this.typeInput,
      btn('Enter', () => this.send('key', { keys: 'enter' }), { cls: 'sm ghost' }));
    const bar = h('div', { class: 'screen-bar' },
      h('span', { class: 'pill ok' }, h('span', { class: 'dot ok' }), `Your computer · ${st.width}×${st.height}`),
      h('span', { class: 'grow' }),
      iconBtn('keyboard', 'Type on the computer', () => { this.typebar.classList.toggle('hidden'); this.typeInput.focus(); }),
      iconBtn('camera', 'Screenshot', async () => {
        try { const info = await api('/api/computer/screenshot', { method: 'POST', body: {} }); bus.emit('captures'); toast('Screenshot saved in Captures.', { action: 'Open', onAction: () => openViewer(info) }); } catch (e) { fail(e); }
      }),
      this.recBtn);
    this.banner = h('div', { class: 'ai-banner hidden' }, h('span', { class: 'pill live' }, 'The assistant is using your computer — push the mouse into the top-left corner to stop it'));
    this.note = h('div', { class: 'placeholder' }, h('div', { class: 'typing' }, h('i'), h('i'), h('i')), h('div', null, 'Showing your screen…'));
    this.screen.canvas.classList.add('hidden');
    this.screen.frames = 0;
    this.screen.onframe = () => { if (this.screen.frames === 1) { this.note.classList.add('hidden'); this.screen.canvas.classList.remove('hidden'); } };
    const keys = h('div', { class: 'phone-side' },
      btn('Esc', () => this.send('key', { keys: 'esc' }), { cls: 'sm' }),
      btn('Tab', () => this.send('key', { keys: 'tab' }), { cls: 'sm' }),
      btn('Start', () => this.send('key', { keys: 'win' }), { cls: 'sm' }),
      btn('Switch app', () => this.send('key', { keys: 'alt+tab' }), { cls: 'sm' }),
      btn('Undo', () => this.send('key', { keys: 'ctrl+z' }), { cls: 'sm ghost' }),
      btn('Scroll up', () => this.send('scroll', { direction: 'up', amount: 5 }), { cls: 'sm ghost' }),
      btn('Scroll down', () => this.send('scroll', { direction: 'down', amount: 5 }), { cls: 'sm ghost' }),
      this.rightBtn);
    this.root.replaceChildren(bar, h('div', { class: 'screen-wrap' }, this.banner, this.note, this.screen.canvas), this.typebar, keys);
    this.connect();
  }

  toggleRecord() {
    if (this.recorder && this.recorder.recording) { this.recorder.stop(); return; }
    this.recorder = new Recorder(this.screen.stream(8), 'the computer', { onstop: () => this.recBtn.classList.remove('on') });
    if (this.recorder.start()) this.recBtn.classList.add('on');
  }

  connect() {
    if (this.es) return;
    this.es = stream('/api/computer/events', {
      frame: (d) => this.screen.draw(d.data, d.mime),
      status: (d) => {
        if (d.available === false) {
          this.note.replaceChildren(icon('info'), h('div', null, d.reason || 'The screen is not available.'), btn('Check again', () => this.refresh(), { cls: 'sm' }));
          this.note.classList.remove('hidden');
        }
      },
    });
    clearInterval(this.poll);
    this.poll = setInterval(async () => {
      try { const s = await api('/api/computer/status'); this.banner && this.banner.classList.toggle('hidden', s.driver !== 'assistant'); } catch (e) { /* ignore */ }
    }, 2500);
  }

  disconnect() {
    if (this.es) { this.es.close(); this.es = null; }
    clearInterval(this.poll);
  }

  mount(container) {
    container.append(this.root);
    this.refresh();
  }

  unmount() {
    this.root.remove();
    if (!(this.recorder && this.recorder.recording)) this.disconnect();
  }
}

// ------------------------------------------------------------------ capture viewer with drawing tools

const COLORS = ['#E5484D', '#FFB224', '#30A46C', '#3E63DD', '#FFFFFF', '#111111'];

export function openViewer(cap, { onChange = null } = {}) {
  const isVideo = cap.kind === 'video';
  const close = () => { document.removeEventListener('keydown', onKey); box.remove(); };
  const onKey = (e) => { if (e.key === 'Escape' && !document.querySelector('.dialog')) close(); };

  const vbar = h('div', { class: 'vbar' }, h('span', { class: 'title' }, cap.name));
  const stage = h('div', { class: 'stage' });
  const box = h('div', { class: 'viewer', role: 'dialog', 'aria-label': 'Capture' }, vbar, stage);
  document.body.append(box);
  document.addEventListener('keydown', onKey);

  const askAssistant = async () => {
    try {
      const chat = await api('/api/chats', { method: 'POST', body: {} });
      const att = await api(`/api/chats/${chat.id}/attach-capture`, { method: 'POST', body: { name: cap.name } });
      store.pendingSend = { id: chat.id, text: isVideo ? 'What happens in this recording?' : 'What do you see in this screenshot?',
        attachments: [{ ...att, name: cap.name }], settings: { product: 'claude', model: chat.model, effort: chat.effort || 'auto', mode: 'auto' } };
      close();
      location.hash = '#/chat/' + chat.id;
    } catch (e) { fail(e); }
  };
  const del = async () => {
    if (!(await confirmBox('Delete this capture?', 'It will be removed from this computer.', { ok: 'Delete', danger: true }))) return;
    try { await api('/api/captures/' + encodeURIComponent(cap.name), { method: 'DELETE' }); bus.emit('captures'); onChange && onChange(); close(); } catch (e) { fail(e); }
  };
  const common = [
    btn('Ask Claude about it', askAssistant, { cls: 'sm', ic: 'chat' }),
    btn('Download', () => download(cap.url, cap.name), { cls: 'sm', ic: 'download' }),
    btn('Delete', del, { cls: 'sm', ic: 'trash' }),
    btn('Close', () => close(), { cls: 'sm', ic: 'x' }),
  ];

  if (isVideo) {
    stage.append(h('video', { src: cap.url, controls: true, autoplay: true, playsInline: true }));
    vbar.append(...common);
    return;
  }

  // Image: draw on it.
  const canvas = h('canvas');
  const ctx = canvas.getContext('2d');
  const img = new Image();
  let tool = 'pen', color = COLORS[0];
  const marks = [];
  let cur = null;
  img.onload = () => { canvas.width = img.naturalWidth; canvas.height = img.naturalHeight; redraw(); };
  img.src = cap.url;
  stage.append(canvas);

  const lw = () => Math.max(3, canvas.width / 320);
  function paint(m) {
    ctx.save();
    ctx.strokeStyle = ctx.fillStyle = m.color;
    ctx.lineCap = ctx.lineJoin = 'round';
    ctx.lineWidth = m.tool === 'marker' ? lw() * 5 : lw();
    if (m.tool === 'marker') ctx.globalAlpha = 0.35;
    if (m.tool === 'pen' || m.tool === 'marker') {
      ctx.beginPath();
      m.pts.forEach((p, k) => (k ? ctx.lineTo(p.x, p.y) : ctx.moveTo(p.x, p.y)));
      ctx.stroke();
    } else if (m.tool === 'box') {
      ctx.strokeRect(m.a.x, m.a.y, m.b.x - m.a.x, m.b.y - m.a.y);
    } else if (m.tool === 'arrow') {
      const ang = Math.atan2(m.b.y - m.a.y, m.b.x - m.a.x), head = lw() * 5;
      ctx.beginPath(); ctx.moveTo(m.a.x, m.a.y); ctx.lineTo(m.b.x, m.b.y); ctx.stroke();
      ctx.beginPath(); ctx.moveTo(m.b.x, m.b.y);
      ctx.lineTo(m.b.x - head * Math.cos(ang - 0.45), m.b.y - head * Math.sin(ang - 0.45));
      ctx.lineTo(m.b.x - head * Math.cos(ang + 0.45), m.b.y - head * Math.sin(ang + 0.45));
      ctx.closePath(); ctx.fill();
    } else if (m.tool === 'text') {
      const size = Math.max(18, canvas.width / 45);
      ctx.font = `600 ${size}px Segoe UI, system-ui, sans-serif`;
      ctx.lineWidth = size / 6;
      ctx.strokeStyle = m.color === '#111111' ? '#FFFFFF' : '#111111';
      ctx.strokeText(m.text, m.a.x, m.a.y);
      ctx.fillText(m.text, m.a.x, m.a.y);
    }
    ctx.restore();
  }
  function redraw() {
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    ctx.drawImage(img, 0, 0);
    marks.forEach(paint);
    if (cur) paint(cur);
  }
  const pos = (e) => {
    const r = canvas.getBoundingClientRect();
    return { x: (e.clientX - r.left) * canvas.width / r.width, y: (e.clientY - r.top) * canvas.height / r.height };
  };
  canvas.addEventListener('pointerdown', async (e) => {
    const p = pos(e);
    if (tool === 'text') {
      const v = await ask('Add a label', [{ name: 'text', label: 'Text', required: true }], { ok: 'Add' });
      if (v) { marks.push({ tool, color, a: p, text: v.text }); redraw(); }
      return;
    }
    canvas.setPointerCapture(e.pointerId);
    cur = tool === 'pen' || tool === 'marker' ? { tool, color: tool === 'marker' && color === COLORS[0] ? COLORS[1] : color, pts: [p] } : { tool, color, a: p, b: p };
  });
  canvas.addEventListener('pointermove', (e) => {
    if (!cur) return;
    const p = pos(e);
    if (cur.pts) cur.pts.push(p); else cur.b = p;
    redraw();
  });
  canvas.addEventListener('pointerup', () => { if (cur) { marks.push(cur); cur = null; redraw(); } });

  const toolBtns = {};
  const pickTool = (t) => { tool = t; for (const [k, b] of Object.entries(toolBtns)) b.classList.toggle('on', k === t); };
  for (const [t, ic, label] of [['pen', 'pen', 'Pen'], ['marker', 'marker', 'Highlighter'], ['box', 'box', 'Box'], ['arrow', 'arrow', 'Arrow'], ['text', 'text', 'Label']]) {
    toolBtns[t] = btn('', () => pickTool(t), { cls: 'sm icon', ic, title: label });
  }
  pickTool('pen');
  const swatches = COLORS.map((c) => h('button', {
    class: 'swatch' + (c === color ? ' on' : ''), style: { background: c }, title: 'Colour', 'aria-label': 'Colour',
    onclick: (e) => { color = c; swatches.forEach((s) => s.classList.toggle('on', s === e.currentTarget)); },
  }));
  const blob = () => new Promise((r) => canvas.toBlob(r, 'image/png'));
  vbar.append(
    h('div', { class: 'grp' }, Object.values(toolBtns), btn('', () => { marks.pop(); redraw(); }, { cls: 'sm icon', ic: 'undo', title: 'Undo' })),
    h('div', { class: 'grp' }, swatches),
    h('div', { class: 'grp' },
      btn('Save copy', async () => {
        if (!marks.length) { toast('Draw on the picture first.'); return; }
        try { await saveCapture(await blob(), 'png', 'marked-up'); onChange && onChange(); } catch (e) { fail(e); }
      }, { cls: 'sm', ic: 'check' }),
      btn('Copy', async () => {
        try { await navigator.clipboard.write([new ClipboardItem({ 'image/png': await blob() })]); toast('Picture copied. Paste it anywhere.'); } catch (e) { toast('Copying pictures is not allowed here. Use Download.', { bad: true }); }
      }, { cls: 'sm', ic: 'copy' })),
    ...common);
}

