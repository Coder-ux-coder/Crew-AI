// Full-page views of the shared browser, your phone and your computer (on a phone, these open as pages;
// on a computer they usually sit in the side panel beside your work).

import { h, store } from '../ui.js';
import { mountLive } from '../devices.js';

function livePage(view, kind, title, hint) {
  store.setTop(title, []);
  const holder = h('div', { style: { display: 'flex', flexDirection: 'column', flex: '1', minHeight: '0' } });
  view.append(h('div', { class: 'page-live' }, h('div', { class: 'live-head' }, h('span', { class: 'muted small' }, hint)), holder));
  return mountLive(kind, holder, 'page');
}

export function browserPage(view) {
  return livePage(view, 'browser', 'Browser', 'You and Claude share this browser. Sign in to websites here once; Claude can then use them when you ask.');
}

export function phonePage(view) {
  return livePage(view, 'phone', 'Your phone', 'Click to tap, drag to swipe, type to write. Claude can use it too when you ask.');
}

export function computerPage(view) {
  return livePage(view, 'computer', 'Your computer', store.isLocal ? 'Most useful from your phone: see and use this computer from anywhere in the house.'
    : 'Tap to click, drag to move things, type to write. Push the mouse into the top-left corner to stop Claude.');
}
