// Voice: dictation (speech to text), reading aloud (text to speech) and a
// hands-free voice conversation with the assistant. Uses the browser's own
// speech services (Edge and Chrome), so nothing extra needs installing.

import { h, icon, toast, speakable, store } from './ui.js';

const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
export const canDictate = !!SR;
export const canSpeak = 'speechSynthesis' in window;

const app = () => (store.overview && store.overview.app) || {};

function micError(err) {
  if (err === 'not-allowed' || err === 'service-not-allowed') {
    if (!window.isSecureContext) {
      toast('The microphone only works on a secure address. On the phone, open Crew through the phone link (Settings → Phone).', { bad: true, ms: 9000 });
    } else {
      toast('Crew is not allowed to use the microphone. Allow it in the browser (the icon in the address bar), then try again.', { bad: true, ms: 9000 });
    }
  } else if (err === 'network') {
    toast('Speech recognition needs an internet connection.', { bad: true });
  } else if (err === 'audio-capture') {
    toast('No microphone was found.', { bad: true });
  } else if (err === 'language-not-supported') {
    toast('This browser cannot listen in the chosen language. Change it in Settings → Voice.', { bad: true });
  }
}

// ------------------------------------------------------------------ dictation

let active = null; // the one running dictation

export function stopDictation() {
  if (active) active.stop();
}

// Toggle dictation into a text box. The button gets the "on" look while listening.
export function dictate(textarea, button) {
  if (!canDictate) {
    toast('Voice typing needs Microsoft Edge or Google Chrome.', { bad: true });
    return;
  }
  if (active) {
    const same = active.textarea === textarea;
    active.stop();
    if (same) return;
  }
  const rec = new SR();
  rec.lang = app().dictation_lang || 'en-US';
  rec.continuous = true;
  rec.interimResults = true;
  const base = textarea.value && !/\s$/.test(textarea.value) ? textarea.value + ' ' : textarea.value;
  let finals = '';
  const session = {
    textarea, stopped: false,
    stop() { this.stopped = true; try { rec.stop(); } catch (e) { /* already stopped */ } },
  };
  rec.onresult = (e) => {
    let interim = '';
    for (let k = e.resultIndex; k < e.results.length; k++) {
      const r = e.results[k];
      if (r.isFinal) finals += r[0].transcript.trim() + ' ';
      else interim += r[0].transcript;
    }
    textarea.value = base + finals + interim;
    textarea.dispatchEvent(new Event('input'));
  };
  rec.onerror = (e) => { if (e.error !== 'no-speech' && e.error !== 'aborted') micError(e.error); };
  rec.onend = () => {
    button && button.classList.remove('on');
    if (active === session) active = null;
    textarea.value = (base + finals).replace(/\s+$/, finals ? ' ' : '');
    textarea.dispatchEvent(new Event('input'));
    textarea.focus();
  };
  try {
    rec.start();
  } catch (e) {
    toast('The microphone is busy. Try again in a moment.', { bad: true });
    return;
  }
  active = session;
  button && button.classList.add('on');
}

// ------------------------------------------------------------------ reading aloud

function pickVoice(text) {
  const voices = speechSynthesis.getVoices();
  const wanted = app().voice_name;
  if (/[؀-ۿ]/.test(text)) { // Arabic script: use a voice that can read it
    const ar = voices.find((v) => /^ar/i.test(v.lang));
    if (ar) return ar;
  }
  return voices.find((v) => v.name === wanted) || voices.find((v) => v.default) || null;
}

function chunks(text) {
  // Short pieces: some voices stop after ~15 seconds of continuous speech.
  const parts = text.match(/[^.!?؟\n]+[.!?؟]*[\s\n]*/g) || [text];
  const out = [];
  let cur = '';
  for (const p of parts) {
    if ((cur + p).length > 220 && cur) { out.push(cur); cur = ''; }
    cur += p;
  }
  if (cur.trim()) out.push(cur);
  return out.map((c) => c.trim()).filter(Boolean);
}

function utter(text, onend) {
  const u = new SpeechSynthesisUtterance(text);
  const v = pickVoice(text);
  if (v) { u.voice = v; u.lang = v.lang; }
  u.rate = Number(app().voice_rate) || 1;
  u.onend = onend;
  u.onerror = onend;
  speechSynthesis.speak(u);
}

export const speech = {
  current: null,
  speak(markdownText, { onend } = {}) {
    if (!canSpeak) { toast('Reading aloud needs Microsoft Edge or Google Chrome.', { bad: true }); return; }
    this.stop();
    const pieces = chunks(speakable(markdownText));
    if (!pieces.length) return;
    const token = {};
    this.current = token;
    let left = pieces.length;
    pieces.forEach((p) => utter(p, () => {
      if (--left === 0 && this.current === token) { this.current = null; onend && onend(); }
    }));
  },
  stop() {
    this.current = null;
    if (canSpeak) speechSynthesis.cancel();
  },
  get speaking() { return canSpeak && (speechSynthesis.speaking || speechSynthesis.pending); },
  voices() { return canSpeak ? speechSynthesis.getVoices() : []; },
};

if (canSpeak) speechSynthesis.onvoiceschanged = () => document.dispatchEvent(new Event('crew:voices'));

// Speaks a reply sentence by sentence while it is still being written.
export class SentenceSpeaker {
  constructor(onIdle) {
    this.buf = '';
    this.pending = 0;
    this.flushed = false;
    this.onIdle = onIdle;
    this.stopped = false;
    this.inCode = false;
  }
  push(delta) {
    if (this.stopped) return;
    this.buf += delta;
    let m;
    while ((m = this.buf.match(/^([\s\S]*?[.!?؟:](?=\s)|[\s\S]*?\n)/))) {
      const piece = m[0];
      this.buf = this.buf.slice(piece.length);
      this.say(piece);
    }
  }
  say(piece) {
    if (/```/.test(piece)) { this.inCode = !this.inCode; return; }
    if (this.inCode) return;
    const text = speakable(piece);
    if (!text || !/[\p{L}\p{N}]/u.test(text)) return;
    this.pending++;
    utter(text, () => { this.pending--; this.check(); });
  }
  flush() {
    if (this.buf.trim()) this.say(this.buf);
    this.buf = '';
    this.flushed = true;
    this.check();
  }
  check() {
    if (!this.stopped && this.flushed && this.pending <= 0) { this.stopped = true; this.onIdle && this.onIdle(); }
  }
  stop() {
    this.stopped = true;
    this.buf = '';
    if (canSpeak) speechSynthesis.cancel();
  }
}

// ------------------------------------------------------------------ voice conversation

// Hands-free talk with the assistant: listen → answer out loud → listen again.
// `send(text, {onDelta, onDone, onError})` delivers the words to the conversation.
export function conversation({ send, title = 'Talking with the assistant' }) {
  if (!canDictate || !canSpeak) {
    toast('Voice conversation needs Microsoft Edge or Google Chrome.', { bad: true });
    return;
  }
  stopDictation();
  speech.stop();
  let state = 'idle', rec = null, speaker = null, closed = false, misses = 0;
  const orb = h('button', { class: 'orb', 'aria-label': 'Talk', title: 'Tap to talk, or to interrupt', onclick: () => tapOrb() });
  const status = h('div', { class: 'eyebrow' });
  const caption = h('div', { class: 'caption' });
  const hint = h('p', { class: 'muted small', style: { margin: 0 } }, 'Tap the circle to interrupt or to pause. Press Esc to end.');
  const end = h('button', { class: 'btn', onclick: () => close() }, icon('x'), 'End conversation');
  const box = h('div', { class: 'voice', role: 'dialog', 'aria-label': title },
    h('div', { class: 'inner' }, h('h2', null, title), orb, status, caption, hint, end));
  document.body.append(box);
  const onKey = (e) => { if (e.key === 'Escape') close(); };
  document.addEventListener('keydown', onKey);

  function set(s, words = '') {
    state = s;
    orb.className = 'orb ' + s;
    status.textContent = { listening: 'Listening…', thinking: 'Thinking…', speaking: 'Speaking', paused: 'Paused — tap to talk', idle: '' }[s] || '';
    if (words !== null) caption.textContent = words;
  }

  function listen() {
    if (closed) return;
    set('listening', '');
    rec = new SR();
    rec.lang = app().dictation_lang || 'en-US';
    rec.continuous = false;
    rec.interimResults = true;
    let heard = '';
    rec.onresult = (e) => {
      let interim = '';
      for (let k = e.resultIndex; k < e.results.length; k++) {
        if (e.results[k].isFinal) heard += e.results[k][0].transcript;
        else interim += e.results[k][0].transcript;
      }
      caption.textContent = (heard + ' ' + interim).trim();
    };
    rec.onerror = (e) => {
      if (e.error === 'not-allowed' || e.error === 'service-not-allowed' || e.error === 'audio-capture') { micError(e.error); close(); }
    };
    rec.onend = () => {
      rec = null;
      if (closed || state !== 'listening') return;
      const text = heard.trim();
      if (text) { misses = 0; ask(text); return; }
      if (++misses >= 3) { set('paused', 'I did not hear anything.'); return; }
      listen();
    };
    try { rec.start(); } catch (e) { set('paused', 'The microphone is busy. Tap to try again.'); }
  }

  function ask(text) {
    set('thinking', text);
    let started = false, said = '';
    speaker = new SentenceSpeaker(() => { if (!closed && state === 'speaking') listen(); });
    send(text, {
      onDelta: (d) => {
        if (closed) return;
        if (!started) { started = true; set('speaking', ''); }
        said += d;
        caption.textContent = speakable(said).slice(-280);
        speaker.push(d);
      },
      onDone: (full) => {
        if (closed) return;
        if (!started) { set('speaking', speakable(full).slice(-280)); speaker.push(full); }
        speaker.flush();
      },
      onError: (msg) => { if (!closed) { set('paused', msg || 'Something went wrong.'); } },
    });
  }

  function tapOrb() {
    if (state === 'speaking' || state === 'thinking') {
      speaker && speaker.stop();
      listen();
    } else if (state === 'listening') {
      try { rec && rec.abort(); } catch (e) { /* ignore */ }
      set('paused', null);
    } else {
      misses = 0;
      listen();
    }
  }

  function close() {
    if (closed) return;
    closed = true;
    try { rec && rec.abort(); } catch (e) { /* ignore */ }
    speaker && speaker.stop();
    speech.stop();
    document.removeEventListener('keydown', onKey);
    box.remove();
  }

  listen();
  return { close };
}
