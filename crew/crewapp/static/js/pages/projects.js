// Projects: jobs handed to the team. The list, and one project's live view — the team's chat on the left;
// on the right every agent and helper (product, model, effort, what it is doing, tokens), estimates, and the plan.

import { h, icon, btn, api, toast, fail, confirmBox, store, bus, markdown, ago, clock, colorFor, tokens, pct, minutes, productBadge, menu, clear } from '../ui.js';
import { dictate, canDictate } from '../voice.js';
import { TEAM_MODES } from './chat.js';

const PHASES = [['refine', 'Brief'], ['plan', 'Plan'], ['build', 'Build'], ['deliver', 'Final checks'], ['done', 'Done']];
const TASK_PILL = { done: 'ok', 'being built': 'live', 'being checked': 'live', 'being improved': 'warn', 'needs a decision': 'bad', approved: 'ok', dropped: '', waiting: '' };
const MODE_LABEL = { solo: 'One agent', team: 'Full team', auto: 'Decide for me' };
const PRODUCT_KEY = { Claude: 'claude', ChatGPT: 'codex' };
const STATE = { idle: 'Ready', standby: 'Standing by', starting: 'Starting', stopped: 'Stopped', waiting: 'Waiting', down: 'Unavailable' };

// ------------------------------------------------------------------ list

export function projectsPage(view) {
  const list = h('div', { class: 'tiles' });
  store.setTop(null, [h('a', { class: 'btn sm', href: '#/new', onclick: () => { store.pendingProduct = 'team'; } }, icon('plus'), 'New project')]);
  view.append(h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Projects'),
      h('p', null, 'Jobs you handed to the team. They plan, split the work, build in parallel, check each other and report back in plain words.'))),
    list));
  const load = async () => {
    try {
      const r = await api('/api/runs');
      clear(list, ...(r.runs.length ? r.runs.map(projectTile)
        : [h('div', { class: 'empty', style: { gridColumn: '1/-1' } }, icon('layers'), 'No projects yet.',
          h('a', { class: 'btn accent sm', href: '#/new', onclick: () => { store.pendingProduct = 'team'; } }, icon('plus'), 'Start one'))]));
    } catch (e) { fail(e); }
  };
  load();
  const t = setInterval(load, 6000);
  return () => clearInterval(t);
}

export function projectTile(r) {
  const [d, t] = r.progress || [0, 0];
  return h('a', { class: 'tile', href: '#/projects/' + r.id },
    h('div', { class: 'row between' }, h('span', { class: 'kicker' }, icon('layers'), MODE_LABEL[r.mode] || 'Project'),
      h('span', { class: 'pill' + (r.running ? ' live' : r.done ? ' ok' : '') }, r.running ? r.phase : r.done ? 'Finished' : r.phase)),
    h('div', { class: 't' }, r.title),
    t ? h('div', { class: 'bar' }, h('i', { class: 'accent', style: { width: Math.round((100 * d) / t) + '%' } })) : null,
    h('div', { class: 'muted small' }, (t ? `${d} of ${t} parts done · ` : '') + ago(r.started)));
}

// ------------------------------------------------------------------ one project

export function projectPage(view, params) {
  const p = new ProjectView(view, params[0]);
  return () => p.destroy();
}

class ProjectView {
  constructor(view, id) {
    this.view = view;
    this.id = id;
    this.after = 0;
    this.alive = true;
    this.seen = new Set();
    this.last = null;
    this.build();
    this.tick();
  }

  build() {
    this.titleEl = h('h1', null, 'Your project');
    this.pills = h('div', { class: 'row wrap', style: { gap: '6px' } });
    this.actions = h('div', { class: 'row wrap', style: { gap: '6px' } });
    this.phases = h('div', { class: 'phases' });
    this.stats = h('div', { class: 'proj-stats' });
    this.feed = h('div', { class: 'feed' });
    this.report = h('div', { class: 'card report-card hidden' });
    this.say = h('textarea', { rows: 1, placeholder: 'Message the team — they read it at their next step', 'aria-label': 'Message the team' });
    this.say.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); } });
    this.say.addEventListener('input', () => { this.say.style.height = 'auto'; this.say.style.height = Math.min(this.say.scrollHeight, 200) + 'px'; });
    const mic = h('button', { class: 'icon-btn sm mic', type: 'button', title: 'Speak', onclick: () => dictate(this.say, mic) }, icon('mic'));
    if (!canDictate) mic.classList.add('hidden');
    const sendBtn = h('button', { class: 'send-btn', type: 'button', title: 'Send', onclick: () => this.send() }, icon('send2'));
    this.estimate = h('div', { class: 'estimate' });
    this.agents = h('div', { class: 'stack', style: { gap: '8px' } });
    this.tasks = h('div', { class: 'card pad-sm' });
    this.accounts = h('div', { class: 'card pad-sm stack' });
    this.body = h('div', { class: 'proj-body show-chat' },
      h('div', { class: 'col main' }, h('div', { class: 'team-chat' }, this.report, this.feed,
        h('div', { class: 'say-dock' }, h('div', { class: 'composer' }, this.say, h('div', { class: 'c-row' },
          h('span', { class: 'muted small' }, 'The team chat — everything they say, as it happens'), h('span', { class: 'grow' }), mic, sendBtn))))),
      h('div', { class: 'col side' },
        h('div', { class: 'side-title' }, icon('clock'), h('span', { class: 'grow' }, 'Estimates')), this.estimate,
        h('div', { class: 'side-title' }, icon('bot'), h('span', { class: 'grow' }, 'Agents and helpers')), this.agents,
        h('div', { class: 'side-title' }, icon('listcheck'), h('span', { class: 'grow' }, 'The plan')), this.tasks,
        h('div', { class: 'side-title' }, icon('gauge'), h('span', { class: 'grow' }, 'Subscriptions')), this.accounts));
    const tabs = h('div', { class: 'seg proj-tabs' }, [['chat', 'Team chat'], ['agents', 'Agents & plan']].map(([k, l]) => h('button', {
      type: 'button', class: k === 'chat' ? 'on' : '', onclick: (e) => {
        [...tabs.children].forEach((b) => b.classList.toggle('on', b === e.currentTarget));
        this.body.className = 'proj-body show-' + k;
      },
    }, l)));
    this.root = h('div', { class: 'proj' },
      h('div', { class: 'proj-top' },
        h('div', { class: 'proj-title' }, h('div', { class: 'stack tight grow' }, this.pills, this.titleEl), this.actions),
        this.phases, this.stats, tabs),
      this.body);
    this.view.append(this.root);
    this.view.style.overflow = 'hidden';
    store.setTop(h('a', { class: 'title-btn', href: '#/projects' }, icon('left'), 'Projects'), []);
    this.feed.append(h('div', { class: 'sysline', dataset: { placeholder: '1' } }, 'Getting the team ready…'));
  }

  async tick() {
    if (!this.alive) return;
    let s = null;
    try {
      s = await api(`/api/runs/${this.id}?after=${this.after}`);
      if (!this.alive) return;
      if (!s.starting) this.update(s);
    } catch (e) {
      if (e.status === 404) { toast('That project could not be found.'); location.hash = '#/projects'; return; }
    }
    this.timer = setTimeout(() => this.tick(), s && (s.running || s.starting) ? 1500 : 6000);
  }

  update(s) {
    this.last = s;
    this.titleEl.textContent = s.title;
    document.title = s.title + ' · Crew';
    const phase = s.raw_phase;
    clear(this.pills, 
      h('span', { class: 'pill' + (s.running ? ' live' : phase === 'done' ? ' ok' : phase === 'failed' ? ' bad' : '') }, s.running ? s.phase : phase === 'done' ? 'Finished' : s.phase),
      MODE_LABEL[s.mode] ? h('span', { class: 'pill outline' }, MODE_LABEL[s.mode]) : null,
      s.timer ? h('span', { class: 'pill outline' }, icon('clock'), `${s.timer} h limit`) : null);

    // actions
    const acts = [];
    if (s.preview && s.preview.url) acts.push(btn('Preview', () => bus.emit('panel:preview', { name: s.title, kind: s.preview.kind, url: s.preview.url }), { cls: 'accent sm', ic: 'play' }));
    if (store.isLocal && s.folder) acts.push(btn('Open folder', () => api(`/api/runs/${this.id}/open-folder`, { method: 'POST', body: {} }).catch(fail), { cls: 'sm', ic: 'folder' }));
    if (s.running) acts.push(btn('Stop', () => this.stop(), { cls: 'sm danger', ic: 'stop' }));
    else if (phase !== 'done') acts.push(btn('Continue', () => this.resume(), { cls: 'sm primary', ic: 'play' }));
    clear(this.actions, ...acts);

    // phases
    const order = PHASES.map((x) => x[0]);
    const at = order.indexOf(phase);
    const parts = [];
    PHASES.forEach(([k, label], i) => {
      const done = phase === 'done' || (at >= 0 && i < at);
      const now = at === i && phase !== 'done';
      if (i) parts.push(h('span', { class: 'phase-sep' }));
      parts.push(h('span', { class: 'phase' + (done ? ' done' : now ? ' now' : '') }, h('span', { class: 'pd' }, done ? icon('check') : null), label,
        now && !s.running ? ' (paused)' : ''));
    });
    clear(this.phases, ...parts);

    // stats and estimates
    const est = s.estimate || {};
    const [d, t] = s.progress || [0, 0];
    clear(this.stats, 
      h('span', { class: 'stat' }, icon('listcheck'), h('b', null, t ? `${d} of ${t}` : '—'), ' parts done'),
      est.elapsed ? h('span', { class: 'stat' }, icon('clock'), h('b', null, minutes(est.elapsed / 60)), ' so far') : null,
      s.running && est.minutes_left ? h('span', { class: 'stat' }, icon('history'), 'about ', h('b', null, minutes(est.minutes_left)), ' left') : null,
      h('span', { class: 'stat' }, icon('zap'), h('b', null, tokens(est.tokens_used || 0)), ' tokens used'));
    const e = (label, value, hint) => h('div', { class: 'e', title: hint || '' }, h('small', null, label), h('b', null, value));
    clear(this.estimate, 
      e('Time left', s.running && est.minutes_left ? `~${minutes(est.minutes_left)}` : phase === 'done' ? 'Done' : '—',
        est.basis === 'typical pace' ? 'A first guess from a typical pace; it sharpens as parts finish.' : 'Based on how long finished parts took.'),
      e('Parts left', String(est.tasks_left ?? Math.max(0, t - d))),
      e('Tokens used', tokens(est.tokens_used || 0)),
      e('Tokens to go', est.tokens_left ? `~${tokens(est.tokens_left)}` : '—', 'Estimated from the parts finished so far.'));

    // agents
    const agents = s.agents || [];
    clear(this.agents, ...(agents.length ? agents.map((a) => agentCard(a, phase)) : [h('div', { class: 'muted small' }, 'The team is starting…')]));

    // plan
    const tasks = s.tasks || [];
    clear(this.tasks, ...(tasks.length ? tasks.map((x) => h('div', { class: 'task-line' },
      h('span', { class: 'num' }, `#${x.id}`),
      h('div', { class: 'grow' }, h('span', null, x.title), h('small', null, [x.who ? cap(x.who) : '', x.effort ? `effort ${x.effort}` : '', x.tokens ? `${tokens(x.tokens)} tokens` : ''].filter(Boolean).join(' · '))),
      h('span', { class: 'pill ' + (TASK_PILL[x.status] || '') }, x.status))) : [h('div', { class: 'muted small' }, 'The plan is being made…')]));

    // subscriptions
    clear(this.accounts, ...((s.accounts || []).length ? s.accounts.map((a) => {
      const u = a.util == null ? null : Math.round(a.util * 100);
      const cls = a.raw === 'parked' ? 'bad' : a.raw === 'conserve' ? 'warn' : 'accent';
      return h('div', { class: 'stack tight' }, h('div', { class: 'row between small' }, h('b', null, a.name),
        h('span', { class: 'muted' }, [a.mode, u != null ? `${u}% used` : '', a.reset && u != null ? `resets ${clock(a.reset)}` : ''].filter(Boolean).join(' · '))),
      h('div', { class: 'bar' }, h('i', { class: cls, style: { width: (u || 0) + '%' } })));
    }) : [h('div', { class: 'muted small' }, 'Waiting for the first report.')]));

    // report
    if (s.report) {
      this.report.classList.remove('hidden');
      clear(this.report, h('div', { class: 'row', style: { marginBottom: '8px' } }, h('span', { class: 'pill ' + (phase === 'done' ? 'ok' : '') }, phase === 'done' ? 'Finished' : 'Report so far'),
        h('span', { class: 'grow' }),
        s.preview && s.preview.url ? btn('See the result', () => bus.emit('panel:preview', { name: s.title, kind: s.preview.kind, url: s.preview.url }), { cls: 'accent sm', ic: 'play' }) : null),
      h('div', { class: 'answer md', html: markdown(s.report) }));
    }

    // team chat
    const scroller = this.body.querySelector('.col.main');
    const stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
    for (const m of s.messages || []) {
      this.after = Math.max(this.after, m.id);
      if (this.seen.has(m.id)) continue;
      this.seen.add(m.id);
      this.feed.append(message(m));
    }
    const ph = this.feed.querySelector('[data-placeholder]');
    if (ph && this.seen.size) ph.remove();
    if (stick) scroller.scrollTop = scroller.scrollHeight;
  }

  async send() {
    const text = this.say.value.trim();
    if (!text) return;
    this.say.value = '';
    this.say.style.height = 'auto';
    try {
      await api(`/api/runs/${this.id}/say`, { method: 'POST', body: { text } });
      clearTimeout(this.timer);
      this.tick();
    } catch (e) { fail(e); this.say.value = text; }
  }

  async stop() {
    if (!(await confirmBox('Stop the team?', 'They save their work first. You can continue the project later.', { ok: 'Stop', danger: true }))) return;
    try { await api(`/api/runs/${this.id}/stop`, { method: 'POST', body: {} }); toast('The team is stopping after saving its work.'); } catch (e) { fail(e); }
  }

  async resume() {
    try { await api(`/api/runs/${this.id}/resume`, { method: 'POST', body: {} }); toast('The team is picking up where it left off.'); clearTimeout(this.timer); setTimeout(() => this.tick(), 1200); } catch (e) { fail(e); }
  }

  destroy() {
    this.alive = false;
    clearTimeout(this.timer);
    this.view.style.overflow = '';
    document.title = 'Crew';
  }
}

const cap = (s) => String(s || '').charAt(0).toUpperCase() + String(s || '').slice(1);

const modelName = (id) => {
  const k = ((store.overview && store.overview.known_models) || []).find((m) => m.id === id);
  return k ? k.label : String(id || '').replace(/^claude-/, '');
};

function agentCard(a, phase) {
  const busy = a.status === 'busy' || a.status === 'working';
  if (phase === 'done' && ['stopped', 'idle', 'standby'].includes(a.status)) a = { ...a, status: 'done', doing: '' };
  const product = PRODUCT_KEY[a.product] || 'claude';
  const helpers = a.helpers || [];
  const workingHelpers = helpers.filter((x) => x.status === 'working').length;
  return h('div', { class: 'agent' + (a.status === 'done' ? ' done' : '') },
    h('div', { class: 'a-top' },
      h('span', { class: 'av' + (busy ? ' busy' : ''), style: { background: a.role === 'CEO' ? 'var(--accent)' : colorFor(a.name) } }, a.role === 'CEO' ? icon('spark') : a.name.replace(/[^a-z0-9]/gi, '').slice(0, 2)),
      h('div', { class: 'a-name' }, h('b', null, a.title || cap(a.name)), h('small', null, [a.title && a.title.startsWith(a.role) ? '' : a.role, a.account].filter(Boolean).join(' · '))),
      a.status === 'done' ? h('span', { class: 'pill ok' }, 'Done') : a.status === 'failed' ? h('span', { class: 'pill bad' }, 'Stopped') : busy ? h('span', { class: 'pill live' }, 'Working') : h('span', { class: 'pill' }, STATE[a.status] || a.status || 'Ready')),
    h('div', { class: 'a-tags' }, productBadge(product, a.product || 'Claude'), a.model ? h('span', { class: 'pill outline' }, modelName(a.model)) : null,
      h('span', { class: 'pill accent', title: 'Effort — set by the CEO for each job' }, icon('brain'), a.effort || 'auto')),
    a.doing && !['stopped', 'finished', 'ready'].includes(String(a.doing).toLowerCase()) ? h('div', { class: 'a-doing' }, busy ? h('span', { class: 'spinner' }) : null, h('span', null, cap(a.doing) + (a.task ? ` · task #${a.task}` : ''))) : null,
    h('div', { class: 'a-stats' }, h('span', null, `${tokens(a.tokens || 0)} tokens`), a.turns ? h('span', null, `${a.turns} turn${a.turns === 1 ? '' : 's'}`) : null,
      a.seconds ? h('span', null, minutes(a.seconds / 60)) : null, a.restarts ? h('span', null, `${a.restarts} restart${a.restarts === 1 ? '' : 's'}`) : null,
      a.helpers_total ? h('span', null, `${a.helpers_total} helper${a.helpers_total === 1 ? '' : 's'}${workingHelpers ? ` (${workingHelpers} working)` : ''}`) : null),
    helpers.length ? h('div', { class: 'helpers' }, helpers.slice(-5).map((x) => h('div', { class: 'hl' },
      x.status === 'working' ? h('span', { class: 'spinner' }) : icon(x.status === 'failed' ? 'x' : 'check'),
      h('span', null, x.what), h('small', { class: 'muted' }, x.seconds ? `${x.seconds}s` : x.type)))) : null);
}

function message(m) {
  const who = m.who === 'you' ? 'You' : m.who === 'crew' ? 'Crew' : cap(m.who);
  const kind = m.who === 'you' ? 'you' : ({ decision: 'decision', blocker: 'blocker', concern: 'blocker', lesson: 'lesson', system: 'system' }[m.kind] || '');
  const label = { question: 'question', answer: 'answer', blocker: 'needs help', concern: 'concern', decision: 'decision', lesson: 'lesson learned' }[m.kind];
  if (kind === 'system' || m.who === 'crew') return h('div', { class: 'sysline' }, h('time', null, clock(m.t)), h('span', null, m.text));
  return h('div', { class: 'tmsg ' + kind },
    h('span', { class: 'av', style: { background: m.who === 'you' ? 'var(--ink-2)' : colorFor(m.who) } }, m.who === 'you' ? 'You'.slice(0, 1) : m.who.replace(/[^a-z0-9]/gi, '').slice(0, 2)),
    h('div', { class: 'stack tight' }, h('div', { class: 'who' }, h('b', null, who), h('span', null, clock(m.t)), label ? h('span', { class: 'pill' }, label) : null),
      h('div', { class: 'body' }, m.text)));
}
