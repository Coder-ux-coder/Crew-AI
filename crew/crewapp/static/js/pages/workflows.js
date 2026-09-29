// Workflows: saved jobs you run with one click or on a schedule — every morning, every Monday, every two hours.
// Each run is an ordinary chat (Claude or ChatGPT) or a team project, kept in the history.

import { h, icon, btn, api, toast, fail, confirmBox, dialog, store, bus, ago, whenAt, productBadge, PRODUCTS, clear } from '../ui.js';
import { efforts } from './chat.js';

const DAYS = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];

export function workflowsPage(view) {
  const list = h('div', { class: 'stack' });
  const templates = h('div', { class: 'grid-2' });
  const startup = h('div');
  store.setTop(null, [btn('New workflow', () => edit(null), { cls: 'sm accent', ic: 'plus' })]);
  view.append(h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Workflows'),
      h('p', null, 'Jobs you repeat: a morning news briefing, a weekly investor round-up, a letter in your house style. Run one with a click, or let it run on a schedule.'))),
    startup, list,
    h('div', { class: 'section-title' }, icon('sparkles'), 'Start from a template'), templates));

  let data = { workflows: [], templates: [] };
  async function load() {
    try {
      data = await api('/api/workflows');
      draw();
    } catch (e) { fail(e); }
  }

  function draw() {
    const ws = data.workflows;
    clear(list, ...(ws.length ? ws.map(card) : [h('div', { class: 'empty' }, icon('flow'), 'No workflows yet. Make one, or start from a template below.',
      btn('New workflow', () => edit(null), { cls: 'accent sm', ic: 'plus' }))]));
    clear(templates, ...data.templates.map((t) => h('button', { class: 'template', type: 'button', onclick: () => edit(null, t) },
      h('div', { class: 'row' }, productBadge(t.engine), h('span', { class: 'grow' }), h('span', { class: 'muted small' }, whenText(t.schedule))),
      h('b', null, t.name), h('p', null, t.prompt))));
    const scheduled = ws.some((w) => w.enabled && w.schedule.kind !== 'manual');
    const app = (store.overview && store.overview.app) || {};
    clear(startup, ...(scheduled && store.isLocal && !app.start_with_windows ? [h('div', { class: 'card soft row wrap' }, icon('info'),
      h('div', { class: 'grow' }, h('b', null, 'Scheduled workflows run while Crew is open. '), h('span', { class: 'muted' }, 'Start Crew with Windows so they run even if you forget to open it.')),
      btn('Start Crew with Windows', async () => {
        try { const r = await api('/api/startup', { method: 'POST', body: { enabled: true } }); toast(r.message); app.start_with_windows = true; draw(); } catch (e) { fail(e); }
      }, { cls: 'sm primary' }))] : []));
  }

  function card(w) {
    const last = w.last;
    const status = w.running ? h('span', { class: 'pill live' }, 'Running') : last ? h('span', { class: 'pill ' + (last.status === 'done' ? 'ok' : last.status === 'failed' ? 'bad' : '') },
      last.status === 'done' ? 'Last run finished' : last.status === 'failed' ? 'Last run failed' : last.status) : null;
    const sw = h('label', { class: 'switch', title: w.enabled ? 'Switched on' : 'Switched off' }, h('input', {
      type: 'checkbox', checked: w.enabled, 'aria-label': 'Switched on', onchange: async (e) => {
        try { await api('/api/workflows/' + w.id, { method: 'PUT', body: { enabled: e.target.checked } }); load(); } catch (x) { fail(x); e.target.checked = !e.target.checked; }
      },
    }), h('i'));
    return h('div', { class: 'wf' },
      h('div', { class: 'wf-top' }, h('span', { class: 'wf-ico ' + w.engine }, icon(PRODUCTS[w.engine].ic)),
        h('div', { class: 'wf-main' }, h('b', null, w.name), h('p', null, w.prompt)), sw),
      h('div', { class: 'wf-meta' },
        h('span', null, icon('clock'), w.when),
        w.enabled && w.next_run ? h('span', null, icon('cal'), 'Next ' + whenAt(w.next_run)) : null,
        w.runs ? h('span', null, icon('history'), `${w.runs} run${w.runs === 1 ? '' : 's'}${w.last_run ? `, last ${ago(w.last_run)}` : ''}`) : null,
        status),
      h('div', { class: 'wf-acts' },
        btn(w.running ? 'Running…' : 'Run now', () => run(w), { cls: 'sm primary', ic: 'play', disabled: w.running }),
        last && (last.chat_id || last.run_id) ? h('a', { class: 'btn sm', href: last.chat_id ? '#/chat/' + last.chat_id : '#/projects/' + last.run_id }, icon('external'), 'Open the last result') : null,
        btn('History', () => history(w), { cls: 'sm ghost', ic: 'history' }),
        btn('Edit', () => edit(w), { cls: 'sm ghost', ic: 'pen' }),
        btn('', async () => {
          if (!(await confirmBox(`Delete “${w.name}”?`, 'The workflow and its history are removed. Chats it made stay.', { ok: 'Delete', danger: true }))) return;
          try { await api('/api/workflows/' + w.id, { method: 'DELETE' }); load(); } catch (e) { fail(e); }
        }, { cls: 'sm ghost icon danger', ic: 'trash', title: 'Delete' })));
  }

  async function run(w) {
    let extra = '';
    if (/\bwhen I run\b|\btopic I give\b/i.test(w.prompt)) {
      const v = await dialog({
        title: `Run “${w.name}”`, body: (() => { const ta = h('textarea', { rows: 3, placeholder: 'Anything to add this time (the topic, a date, a name…)' }); run.ta = ta; return h('div', { class: 'stack' }, ta); })(),
        actions: [{ label: 'Cancel', value: null }, { label: 'Run', primary: true, value: () => run.ta.value }],
      });
      if (v === null) return;
      extra = v || '';
    }
    try {
      const r = await api(`/api/workflows/${w.id}/run`, { method: 'POST', body: { extra } });
      toast('Started. You will get a notice when it finishes.', { action: 'Watch it', onAction: () => { location.hash = r.chat ? '#/chat/' + r.chat : '#/projects/' + r.project; } });
      load();
    } catch (e) { fail(e); }
  }

  async function history(w) {
    let runs = [];
    try { runs = (await api(`/api/workflows/${w.id}/runs`)).runs; } catch (e) { fail(e); return; }
    let closeBox = null;
    dialog({
      title: `${w.name} — history`, wide: true, onOpen: (form, close) => { closeBox = close; },
      body: h('div', { class: 'list' }, runs.length ? runs.map((r) => h(r.chat_id || r.run_id ? 'a' : 'div', {
        class: 'li', href: r.chat_id ? '#/chat/' + r.chat_id : r.run_id ? '#/projects/' + r.run_id : null,
        onclick: () => closeBox && closeBox(null),  // closed properly: its keyboard listener goes with it
      }, h('span', { class: 'li-ico' }, icon(r.status === 'done' ? 'check' : r.status === 'running' ? 'clock' : 'x')),
      h('span', { class: 'li-main' }, h('b', null, `${new Date(r.started * 1000).toLocaleString()} · ${r.trigger === 'schedule' ? 'on schedule' : 'by you'}`),
        h('small', null, r.summary || (r.status === 'running' ? 'Running…' : ''))),
      h('span', { class: 'pill ' + (r.status === 'done' ? 'ok' : r.status === 'failed' ? 'bad' : 'live') }, r.status))) : h('div', { class: 'li muted' }, 'Not run yet.')),
    });
  }

  async function edit(w, template = null) {
    const src = w || template || { name: '', prompt: '', engine: 'claude', schedule: { kind: 'manual' } };
    const sch = { kind: 'manual', time: '08:00', days: [0, 1, 2, 3, 4], every: 2, at: '', ...(src.schedule || {}) };
    const name = h('input', { type: 'text', value: src.name || '', placeholder: 'e.g. Morning briefing' });
    const prompt = h('textarea', { rows: 6, placeholder: 'What should it do? Write it as you would ask Claude.' }, src.prompt || '');
    let engine = src.engine || 'claude';
    const engineSeg = h('div', { class: 'seg' }, ['claude', 'codex', 'team'].map((p) => h('button', {
      type: 'button', class: p === engine ? 'on' : '', onclick: (e) => { engine = p; [...engineSeg.children].forEach((b) => b.classList.toggle('on', b === e.currentTarget)); drawEffort(); },
    }, icon(PRODUCTS[p].ic), PRODUCTS[p].name)));
    const effortSel = h('select', { style: { width: 'auto' } });
    const effortRow = h('label', { class: 'field' }, h('span', null, 'Effort'), effortSel, h('small', null, 'auto lets the model decide. The team’s CEO sets efforts itself.'));
    const accountSel = h('select', { style: { width: 'auto' } });
    const accountRow = h('label', { class: 'field' }, h('span', null, 'Subscription'), accountSel,
      h('small', null, 'Automatic uses whichever has the most room, and moves on if one runs out.'));
    const drawEffort = () => {
      effortRow.classList.toggle('hidden', engine === 'team');
      clear(effortSel, ...efforts(engine).map((e) => h('option', { value: e, selected: e === (src.effort || 'auto') }, e)));
      const mine = ((store.overview && store.overview.accounts) || []).filter((a) => a.vendor === engine);
      accountRow.classList.toggle('hidden', engine === 'team' || mine.length < 2);
      clear(accountSel, h('option', { value: '' }, 'Automatic'),
        ...mine.map((a) => h('option', { value: a.name, selected: a.name === (src.account || '') }, a.name)));
    };
    drawEffort();
    const kind = h('select', null, [['manual', 'Only when I run it'], ['daily', 'Every day'], ['weekdays', 'Weekdays (Monday to Friday)'], ['weekly', 'Every week, on chosen days'],
      ['hourly', 'Every few hours'], ['once', 'Once, at a set time']].map(([v, l]) => h('option', { value: v }, l)));
    kind.value = sch.kind === 'daily' && sch.days && sch.days.length === 5 && sch.days.join() === '0,1,2,3,4' ? 'weekdays' : sch.kind;
    const time = h('input', { type: 'time', value: sch.time || '08:00', style: { width: '140px' } });
    const every = h('input', { type: 'number', min: 1, max: 24, value: String(sch.every || 2), style: { width: '90px' } });
    const at = h('input', { type: 'datetime-local', value: sch.at || '', style: { width: '240px' } });
    const chosen = new Set(sch.days || [0]);
    const days = h('div', { class: 'seg' }, DAYS.map((d, i) => h('button', {
      type: 'button', class: chosen.has(i) ? 'on' : '', onclick: (e) => { if (chosen.has(i)) chosen.delete(i); else chosen.add(i); e.currentTarget.classList.toggle('on', chosen.has(i)); },
    }, d)));
    const when = h('div', { class: 'row wrap' });
    const drawWhen = () => {
      const k = kind.value;
      clear(when, ...(k === 'daily' || k === 'weekdays' ? [h('span', { class: 'muted small' }, 'at'), time]
        : k === 'weekly' ? [days, h('span', { class: 'muted small' }, 'at'), time]
          : k === 'hourly' ? [h('span', { class: 'muted small' }, 'every'), every, h('span', { class: 'muted small' }, 'hours')]
            : k === 'once' ? [at] : [h('span', { class: 'muted small' }, 'Use Run now whenever you want it.')]));
    };
    kind.addEventListener('change', drawWhen);
    drawWhen();
    const body = h('div', { class: 'stack', style: { gap: '14px' } },
      h('label', { class: 'field' }, h('span', null, 'Name'), name),
      h('label', { class: 'field' }, h('span', null, 'What it does'), prompt),
      h('div', { class: 'field' }, h('span', null, 'Who does it'), engineSeg),
      effortRow,
      accountRow,
      h('div', { class: 'field' }, h('span', null, 'When'), kind, when));
    // Until it is saved or the owner cancels: a refused save opens the same form again, with everything in it.
    for (;;) {
      const v = await dialog({
        title: w ? 'Edit workflow' : 'New workflow', wide: true, body,
        actions: [{ label: 'Cancel', value: null }, {
          label: w ? 'Save' : 'Create', primary: true, value: () => {
            if (!name.value.trim()) { name.focus(); toast('Give it a name.', { bad: true }); return undefined; }
            if (prompt.value.trim().length < 5) { prompt.focus(); toast('Say what it should do.', { bad: true }); return undefined; }
            const k = kind.value;
            const schedule = k === 'weekdays' ? { kind: 'daily', time: time.value || '08:00', days: [0, 1, 2, 3, 4] }
              : k === 'daily' ? { kind: 'daily', time: time.value || '08:00', days: [0, 1, 2, 3, 4, 5, 6] }
                : k === 'weekly' ? { kind: 'weekly', time: time.value || '08:00', days: [...chosen].sort() }
                  : k === 'hourly' ? { kind: 'hourly', every: Math.max(1, parseInt(every.value, 10) || 1) }
                    : k === 'once' ? { kind: 'once', at: at.value } : { kind: 'manual' };
            if (k === 'weekly' && !chosen.size) { toast('Choose at least one day.', { bad: true }); return undefined; }
            if (k === 'once' && !at.value) { toast('Choose the date and time.', { bad: true }); return undefined; }
            return { name: name.value.trim(), prompt: prompt.value.trim(), engine, effort: engine === 'team' ? 'auto' : effortSel.value,
              account: engine === 'team' ? '' : accountSel.value, schedule };
          },
        }],
      });
      if (!v) return;
      try {
        if (w) await api('/api/workflows/' + w.id, { method: 'PUT', body: v });
        else await api('/api/workflows', { method: 'POST', body: v });
        toast(w ? 'Saved.' : 'Workflow created.');
        load();
        return;
      } catch (e) { fail(e); }
    }
  }

  load();
  const off = bus.on('workflows', load);
  const t = setInterval(load, 15000);
  return () => { off(); clearInterval(t); };
}

function whenText(s) {
  if (!s || s.kind === 'manual') return 'When you run it';
  if (s.kind === 'hourly') return `Every ${s.every || 1} h`;
  if (s.kind === 'once') return 'Once';
  const days = s.days || [];
  if (s.kind === 'daily' && days.length === 5) return `Weekdays ${s.time}`;
  if (s.kind === 'daily') return `Daily ${s.time}`;
  return `${days.map((d) => DAYS[d]).join(', ')} ${s.time}`;
}
