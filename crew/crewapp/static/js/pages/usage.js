// Usage: how much of each subscription is used — Claude's 5-hour and weekly limits, tokens per day — and the
// Claude Code version on this computer. The second tab is the model scorecard.

import { h, icon, btn, api, toast, fail, store, bus, tokens, pct, inTime, whenAt, clear } from '../ui.js';
import { scorecardView } from './scorecard.js';

function tabs(on) {
  return h('div', { class: 'seg', role: 'tablist', style: { marginBottom: '16px' } },
    [['usage', 'Limits', '#/usage'], ['scorecard', 'Model scorecard', '#/usage/scorecard']].map(([k, label, href]) => h('button', {
      type: 'button', role: 'tab', class: k === on ? 'on' : '', 'aria-selected': String(k === on), onclick: () => { location.hash = href; },
    }, label)));
}

export function usagePage(view, params = []) {
  if (params[0] === 'scorecard') {
    const box = h('div');
    store.setTop(null, [btn('Refresh', () => scorecardView(box), { cls: 'sm ghost', ic: 'reload' })]);
    view.append(h('div', { class: 'page' },
      h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Model scorecard'))), tabs('scorecard'), box));
    scorecardView(box);
    return null;
  }
  const cards = h('div', { class: 'grid-2' });
  const cli = h('div', { class: 'card' });
  store.setTop(null, [btn('Refresh', () => load(), { cls: 'sm ghost', ic: 'reload' })]);
  view.append(h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Usage'),
      h('p', null, 'How much of each subscription is used. Crew spreads work across your subscriptions and moves to another when one reaches its limit.'))),
    tabs('usage'), cards, cli));

  async function load() {
    try {
      const [u, st] = await Promise.all([api('/api/usage'), api('/api/accounts/status').catch(() => ({ accounts: [] }))]);
      const signed = Object.fromEntries((st.accounts || []).map((a) => [a.name, a]));
      clear(cards, ...(u.accounts.length ? u.accounts.map((a) => card(a, signed[a.name])) : [h('div', { class: 'empty' }, 'No subscriptions yet. Add one in Settings.')]));
    } catch (e) { fail(e); }
    try {
      const c = await api('/api/claude');
      const info = c.info || {};
      clear(cli, h('div', { class: 'card-head' }, icon('terminal'), h('h2', null, 'Claude Code on this computer'),
        store.isLocal ? btn('Update now', async (e) => {
          const b = e.currentTarget;
          b.disabled = true;
          b.lastChild.textContent = 'Updating…';
          try { const r = await api('/api/claude/update', { method: 'POST', body: {} }); toast(r.message, { bad: !r.ok, ms: 8000 }); load(); } catch (x) { fail(x); }
          b.disabled = false;
        }, { cls: 'sm', ic: 'download' }) : null),
      h('div', { class: 'stack tight small' },
        h('div', null, h('b', null, 'Version: '), c.version || 'not found', c.update && c.update.last_update ? h('span', { class: 'muted' }, ` · checked ${new Date(c.update.last_update * 1000).toLocaleDateString()}`) : null),
        h('div', { class: 'muted' }, 'Crew keeps it up to date by itself once a day; new models sometimes need the newest version.'),
        info.skills ? h('div', { class: 'muted' }, `${info.skills.length} skills and ${(info.slash_commands || []).length} commands available${info.plugins && info.plugins.length ? `, ${info.plugins.length} plug-ins` : ''}.`) : null));
    } catch (e) { /* offline */ }
  }

  function limitBar(label, util, reset) {
    const u = Number(util || 0);
    const cls = u >= 0.9 ? 'bad' : u >= 0.7 ? 'warn' : 'accent';
    return h('div', { class: 'limit' },
      h('div', { class: 'l-top' }, h('b', null, label), h('span', { class: 'muted' }, util == null ? 'shows after its first use' : `${pct(u)} used${reset ? ` · resets ${inTime(reset)} (${whenAt(reset)})` : ''}`)),
      h('div', { class: 'bar' }, h('i', { class: cls, style: { width: Math.round(u * 100) + '%' } })));
  }

  function card(a, signed) {
    const lim = a.limits || {};
    const tok = a.tokens || { today: 0, week: 0, days: {} };
    const days = [];
    for (let i = 6; i >= 0; i--) {
      const d = new Date(Date.now() - i * 86400000);
      const key = d.toISOString().slice(0, 10);
      const local = `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(d.getDate()).padStart(2, '0')}`;
      const v = (tok.days[local] || tok.days[key] || {}).total || 0;
      days.push([d.toLocaleDateString([], { weekday: 'narrow' }), v]);
    }
    const max = Math.max(1, ...days.map((x) => x[1]));
    const product = a.vendor === 'codex' ? 'codex' : 'claude';
    return h('div', { class: 'card usage-card' },
      h('div', { class: 'u-head' }, h('span', { class: 'u-logo ' + product }, icon(product === 'codex' ? 'gpt' : 'spark')),
        h('div', { class: 'grow' }, h('b', null, a.name), h('div', { class: 'muted small' }, product === 'codex' ? 'ChatGPT (through Codex)' : 'Claude')),
        signed ? h('span', { class: 'pill ' + (signed.signed_in ? 'ok' : signed.signed_in === false ? 'bad' : '') }, signed.signed_in ? 'Signed in' : signed.signed_in === false ? 'Not signed in' : 'Unknown') : null,
        lim.status === 'rejected' ? h('span', { class: 'pill bad' }, 'At its limit') : null),
      limitBar('5-hour limit', lim.five_util, lim.five_reset),
      limitBar('Weekly limit', lim.week_util, lim.week_reset),
      h('div', { class: 'row', style: { alignItems: 'flex-end', gap: '18px' } },
        h('div', { class: 'stack tight' }, h('small', { class: 'muted' }, 'Tokens today'), h('span', { class: 'big-num' }, tokens(tok.today))),
        h('div', { class: 'stack tight' }, h('small', { class: 'muted' }, 'Last 7 days'), h('span', { class: 'big-num' }, tokens(tok.week))),
        h('div', { class: 'spark-bars grow', title: 'Tokens per day' }, days.map(([l, v]) => h('div', null, h('i', { style: { height: Math.max(2, Math.round((v / max) * 54)) + 'px' }, title: tokens(v) }), h('small', null, l))))),
      lim.updated ? h('div', { class: 'muted small' }, `Limits last reported ${new Date(lim.updated * 1000).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })}. They update whenever this subscription is used.`) : null);
  }

  load();
  const off = bus.on('usage', () => {});
  const t = setInterval(load, 60000);
  return () => { clearInterval(t); off(); };
}
