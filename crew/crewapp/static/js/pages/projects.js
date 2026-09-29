// Projects: jobs handed to the team. The list, and one project's live view — the team's chat on the left;
// on the right every agent and helper (product, model, effort, what it is doing, tokens), estimates, and the plan.
// The owner can also talk to one agent privately, or put a question to the CEO: only that agent sees the message,
// and its answer comes back into the same conversation. Helpers are reached through the agent that runs them.

import { h, icon, btn, api, toast, fail, confirmBox, store, bus, markdown, ago, clock, colorFor, tokens, minutes, productBadge, menu, clear } from '../ui.js';
import { dictate, canDictate } from '../voice.js';

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
    this.gen = 0;  // one round of checks at a time: a newer round retires the older one
    this.build();
    this.tick(this.gen);
  }

  build() {
    this.titleEl = h('h1', null, 'Your project');
    this.pills = h('div', { class: 'row wrap', style: { gap: '6px' } });
    this.actions = h('div', { class: 'row wrap', style: { gap: '6px' } });
    this.phases = h('div', { class: 'phases' });
    this.stats = h('div', { class: 'proj-stats' });
    this.feed = h('div', { class: 'feed' });
    this.report = h('div', { class: 'card report-card hidden' });
    this.to = '';          // who the owner writes to: '' the whole team, else one agent's name, or 'ceo'
    this.convLast = {};    // per private conversation: who spoke last ('you' or 'them')
    this.unreadConv = {};  // per private conversation: answers the owner has not opened yet
    this.convBar = h('div', { class: 'conv-bar hidden' });
    this.convEmpty = h('div', { class: 'conv-empty hidden' });
    this.waitEl = h('div', { class: 'sysline conv-wait' }, h('span', { class: 'spinner' }), h('span', null, ''));
    this.toBtn = h('button', { class: 'chip-btn to-btn', type: 'button', title: 'Who reads your message: the whole team, or one agent privately', onclick: () => this.toMenu() });
    this.hint = h('span', { class: 'muted small say-hint' });
    this.say = h('textarea', { rows: 1, placeholder: 'Message the team — they read it at their next step', 'aria-label': 'Message the team' });
    this.say.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); this.send(); } });
    this.say.addEventListener('input', () => { this.say.style.height = 'auto'; this.say.style.height = Math.min(this.say.scrollHeight, 200) + 'px'; });
    const mic = h('button', { class: 'icon-btn sm mic', type: 'button', title: 'Speak', onclick: () => dictate(this.say, mic) }, icon('mic'));
    if (!canDictate) mic.classList.add('hidden');
    const sendBtn = h('button', { class: 'send-btn', type: 'button', title: 'Send', onclick: () => this.send() }, icon('send2'));
    this.estimate = h('div', { class: 'estimate' });
    this.shares = h('div', { class: 'card pad-sm shares' });
    this.agents = h('div', { class: 'stack', style: { gap: '8px' } });
    this.tasks = h('div', { class: 'card pad-sm' });
    this.accounts = h('div', { class: 'card pad-sm stack' });
    this.body = h('div', { class: 'proj-body show-chat' },
      h('div', { class: 'col main' }, h('div', { class: 'team-chat' }, this.convBar, this.report, this.feed, this.convEmpty,
        h('div', { class: 'say-dock' }, h('div', { class: 'composer' }, this.say, h('div', { class: 'c-row' },
          this.toBtn, this.hint, h('span', { class: 'grow' }), mic, sendBtn))))),
      h('div', { class: 'col side' },
        h('div', { class: 'side-title' }, icon('clock'), h('span', { class: 'grow' }, 'Estimates')), this.estimate,
        h('div', { class: 'side-title' }, icon('layers'), h('span', { class: 'grow' }, 'Who did the work'),
          h('small', { class: 'muted', title: 'Share of all tokens this project used, against the targets you set' }, 'share of tokens')),
        this.shares,
        h('div', { class: 'side-title' }, icon('bot'), h('span', { class: 'grow' }, 'Agents and helpers')), this.agents,
        h('div', { class: 'side-title' }, icon('listcheck'), h('span', { class: 'grow' }, 'The plan')), this.tasks,
        h('div', { class: 'side-title' }, icon('gauge'), h('span', { class: 'grow' }, 'Subscriptions')), this.accounts));
    this.tabs = h('div', { class: 'seg proj-tabs' }, [['chat', 'Team chat'], ['agents', 'Agents & plan']].map(([k, l]) => h('button', {
      type: 'button', class: k === 'chat' ? 'on' : '', dataset: { tab: k }, onclick: () => this.showTab(k),
    }, l)));
    this.root = h('div', { class: 'proj' },
      h('div', { class: 'proj-top' },
        h('div', { class: 'proj-title' }, h('div', { class: 'stack tight grow' }, this.pills, this.titleEl), this.actions),
        this.phases, this.stats, this.tabs),
      this.body);
    this.view.append(this.root);
    this.syncTo();
    this.view.style.overflow = 'hidden';
    store.setTop(h('a', { class: 'title-btn', href: '#/projects' }, icon('left'), 'Projects'), []);
    this.feed.append(h('div', { class: 'sysline', dataset: { placeholder: '1' } }, 'Getting the team ready…'));
  }

  async tick(gen) {
    if (!this.alive || gen !== this.gen) return;
    let s = null;
    try {
      s = await api(`/api/runs/${this.id}?after=${this.after}`);
      if (!this.alive || gen !== this.gen) return;  // a newer check has taken over (a message was sent meanwhile)
      if (!s.starting) this.update(s);
      else if (s.problem) this.cannotStart(s.problem);
    } catch (e) {
      if (e.status === 404) { toast('That project could not be found.'); location.hash = '#/projects'; return; }
      if (gen !== this.gen) return;
    }
    this.timer = setTimeout(() => this.tick(gen), s && (s.running || (s.starting && !s.problem)) ? 1500 : 6000);
  }

  // Check now (or after `delay` ms), instead of the round of checks already going: never beside it.
  poll(delay = 0) {
    clearTimeout(this.timer);
    const gen = ++this.gen;
    if (delay) this.timer = setTimeout(() => this.tick(gen), delay);
    else this.tick(gen);
  }

  // Its program ended before the team began (git missing, a settings problem …): say why, instead of
  // "Getting the team ready…" for ever.
  cannotStart(text) {
    const line = this.feed.querySelector('[data-placeholder]');
    if (line && line.textContent !== text) { line.textContent = text; line.classList.add('problem'); }
  }

  update(s) {
    this.last = s;
    this.titleEl.textContent = s.title;
    document.title = s.title + ' · Crew';
    const phase = s.raw_phase;
    clear(this.pills, 
      h('span', { class: 'pill' + (s.running ? ' live' : phase === 'done' ? ' ok' : phase === 'failed' ? ' bad' : '') }, s.running ? s.phase : phase === 'done' ? 'Finished' : s.phase),
      MODE_LABEL[s.mode] ? h('span', { class: 'pill outline' }, MODE_LABEL[s.mode]) : null,
      s.timer ? h('span', { class: 'pill outline' }, icon('clock'), `${s.timer} h limit`) : null,
      (s.accounts_chosen || []).length ? h('span', { class: 'pill outline', title: 'This project uses only these subscriptions' }, icon('key'), s.accounts_chosen.join(', ')) : null,
      s.head_to_head && s.head_to_head !== 'off' ? h('span', { class: 'pill t-ceo', title: 'Parts built by both models; the better version is kept' }, 'Head-to-head') : null);

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

    // who did the work: each tier's share of the tokens, against the owner's targets
    const sh = s.shares || {};
    const shareTiers = sh.tiers || [];
    const total = sh.total || 0;
    clear(this.shares,
      h('div', { class: 'sbar', role: 'img', 'aria-label': shareTiers.map((x) => `${x.name} ${Math.round(x.pct)}%`).join(', ') },
        ...shareTiers.map((x) => h('i', { class: 't-' + x.tier, style: { width: (total ? x.pct : 0) + '%' }, title: `${x.name}: ${x.pct}%` }))),
      ...shareTiers.map((x) => h('div', { class: 'srow' + (total && !x.on_target ? ' off' : '') },
        h('span', { class: 'sdot t-' + x.tier }),
        h('span', { class: 'grow' }, h('b', null, x.name), ' ', h('small', { class: 'muted' }, tierModel(x.tier))),
        h('b', { class: 'spct' }, total ? `${Math.round(x.pct)}%` : '—'),
        h('small', { class: 'muted starget', title: 'Your target for this tier' }, x.target[0] ? `${x.target[0]}–${x.target[1]}%` : `~${x.target[1]}%`))),
      total ? null : h('div', { class: 'muted small' }, 'Fills in as the team works.'));

    // plan
    const tasks = s.tasks || [];
    clear(this.tasks, ...(tasks.length ? tasks.map((x) => h('div', { class: 'task-line' },
      h('span', { class: 'num' }, `#${x.id}`),
      h('div', { class: 'grow' }, h('span', null, x.title), h('small', null, [x.who ? cap(x.who) : '', x.model || '', x.effort ? `effort ${x.effort}` : '', x.tokens ? `${tokens(x.tokens)} tokens` : ''].filter(Boolean).join(' · '))),
      x.tier ? h('span', { class: 'pill t-' + x.tier, title: TIER_HINT[x.tier] || '' }, TIER_LABEL[x.tier] || x.tier) : null,
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

    // team chat (and the owner's private conversations with single agents)
    const scroller = this.body.querySelector('.col.main');
    const stick = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 120;
    for (const m of s.messages || []) {
      this.after = Math.max(this.after, m.id);
      if (this.seen.has(m.id)) continue;
      this.seen.add(m.id);
      const conv = convOf(m);
      const el = message(m, s);
      el.dataset.conv = conv;
      if (this.to && conv !== this.to) el.classList.add('conv-off');
      // the prompt writer's version of the owner's message takes the place of the owner's draft
      const draft = m.ref ? this.feed.querySelector(`[data-id="${m.ref}"]`) : null;
      if (draft) draft.replaceWith(el); else this.feed.append(el);
      if (!conv) continue;
      this.convLast[conv] = m.who === 'you' ? 'you' : 'them';
      if (m.who !== 'you' && this.loaded) {
        const seenHere = this.to === conv || (!this.to && this.body.classList.contains('show-chat'));
        if (!seenHere) this.unreadConv[conv] = (this.unreadConv[conv] || 0) + 1;
        if (this.to && this.to !== conv) toast(`${cap(this.nameOf(conv))} answered you.`, { action: 'Open', onAction: () => this.talkTo(conv) });
      }
    }
    const ph = this.feed.querySelector('[data-placeholder]');
    if (ph && this.seen.size) ph.remove();
    this.syncTo();
    this.syncWait();
    this.applyEmpty();
    if (stick) scroller.scrollTop = scroller.scrollHeight;

    // agents
    const agents = s.agents || [];
    const talk = { open: (to, prefill) => this.talkTo(to, prefill), unread: this.unreadConv, active: this.to };
    clear(this.agents, ...(agents.length ? agents.map((a) => agentCard(a, phase, talk)) : [h('div', { class: 'muted small' }, 'The team is starting…')]));
    this.loaded = true;
  }

  showTab(k) {
    [...this.tabs.children].forEach((b) => b.classList.toggle('on', b.dataset.tab === k));
    this.body.className = 'proj-body show-' + k;
  }

  nameOf(to) {
    if (!to) return 'the team';
    if (to === 'ceo') return 'the CEO';
    const a = ((this.last && this.last.agents) || []).find((x) => x.name === to);
    return a ? (a.title || cap(a.name)) : cap(to);
  }

  agentOf(to) {
    return to && to !== 'ceo' ? ((this.last && this.last.agents) || []).find((x) => x.name === to) || null : null;
  }

  // The picker: the whole team, or one agent privately — grouped by subscription, each agent with its helpers —
  // or the CEO.
  toMenu() {
    const s = this.last || {};
    const done = s.raw_phase === 'done';
    const groups = new Map();
    for (const a of (s.agents || []).filter((x) => x.standing)) {
      const key = `${a.product || 'Claude'}${a.account ? ' · ' + a.account : ''}`;
      if (!groups.has(key)) groups.set(key, []);
      groups.get(key).push(a);
    }
    const items = [{ label: 'Everyone — the team chat', hint: 'The whole team reads it at their next step', ic: 'users', checked: !this.to, value: { to: '' } }];
    for (const [key, list] of groups) {
      items.push({ section: key });
      for (const a of list) {
        const name = a.title || cap(a.name);
        items.push({ label: `${name} · ${a.role}`, hint: [modelName(a.model), statusWord(a)].filter(Boolean).join(' · '),
          ic: PRODUCT_KEY[a.product] === 'codex' ? 'gpt' : 'spark', checked: this.to === a.name, value: { to: a.name }, disabled: done });
        for (const x of (a.helpers || []).filter((y) => y.status === 'working')) {
          items.push({ label: `↳ ${name}’s helper: ${x.what}`, hint: `Helpers take instructions from ${name}, so your message goes to ${name}, who passes it on`,
            ic: 'bot', value: { to: a.name, about: x.what }, disabled: done });
        }
      }
    }
    items.push({ section: 'The CEO' });
    items.push({ label: `The CEO · ${tierModel('ceo')}`, hint: 'Questions about the plan, priorities and progress; each is a short, separate review',
      ic: 'brain', checked: this.to === 'ceo', value: { to: 'ceo' }, disabled: done });
    menu(this.toBtn, items, { above: true, minWidth: 300, onPick: (v) => this.talkTo(v.to, v.about ? `About your helper “${v.about}”: ` : '') });
  }

  talkTo(to, prefill = '') {
    this.to = to || '';
    if (this.to) delete this.unreadConv[this.to];
    for (const el of this.feed.children) {
      if (el !== this.waitEl) el.classList.toggle('conv-off', !!this.to && el.dataset.conv !== this.to);
    }
    this.syncTo();
    this.syncWait();
    this.applyEmpty();
    if (this.last) this.update({ ...this.last, messages: [] });  // redraw the cards (their Message buttons)
    if (this.body.classList.contains('show-agents')) this.showTab('chat');
    if (prefill) { this.say.value = prefill; this.say.dispatchEvent(new Event('input')); }
    const scroller = this.body.querySelector('.col.main');
    scroller.scrollTop = scroller.scrollHeight;
    this.say.focus();
  }

  // The composer and the conversation bar follow who the owner is talking to.
  syncTo() {
    const s = this.last || {};
    const to = this.to;
    const name = this.nameOf(to);
    clear(this.toBtn, icon(!to ? 'users' : to === 'ceo' ? 'brain' : 'lock'), h('span', null, to ? `To ${name} only` : 'To everyone'), icon('down', 'down'));
    this.toBtn.classList.toggle('on', !!to);
    this.say.placeholder = to ? `Message ${name} — only ${name} sees it` : 'Message the team — they read it at their next step';
    this.say.setAttribute('aria-label', to ? `Message ${name} privately` : 'Message the team');
    this.hint.textContent = to ? 'A private conversation — the answer comes back here' : 'The team chat — everything they say, as it happens';
    this.root && this.root.classList.toggle('in-conv', !!to);
    if (!to) { this.convBar.classList.add('hidden'); return; }
    const a = this.agentOf(to);
    const phase = s.raw_phase;
    const busy = a && (a.status === 'busy' || a.status === 'working');
    const Name = cap(name);
    let status = '';
    if (phase === 'done') status = `This project is finished, so ${name} no longer runs and cannot answer.`;
    else if (!s.running) status = `The team is paused. ${Name} answers when you press Continue.`;
    else if (to === 'ceo') status = 'Each question is a short, separate review; the answer comes back here, usually within a few minutes.';
    else if (a && a.status === 'down') status = `${Name} is unavailable for this run and cannot answer.`;
    else if (a && a.status === 'waiting') status = `${Name} is waiting for ${a.account || 'its subscription'} to reset, and answers then.`;
    else if (busy) status = `Working${a.task ? ` on task #${a.task}` : ''}. ` + (a.product === 'ChatGPT'
      ? 'Reads your message when this step ends; “Ask now” stops the step at once.'
      : 'Reads your message at its next step, usually within a minute.');
    else if (a) status = a.status === 'standby' ? 'Standing by — starts up to answer you.' : 'Ready — answers straight away.';
    const waiting = this.convLast[to] === 'you';
    const sub = to === 'ceo' ? ['Checks the work; does not build', tierModel('ceo')] : a ? [a.role, modelName(a.model), a.account] : [];
    clear(this.convBar,
      to === 'ceo' ? h('span', { class: 'av', style: { background: 'var(--tier-ceo)' } }, icon('brain'))
        : h('span', { class: 'av', style: { background: colorFor(to) } }, to.replace(/[^a-z0-9]/gi, '').slice(0, 2)),
      h('div', { class: 'grow cb-text' }, h('div', { class: 'cb-title' }, h('b', null, Name),
        h('span', { class: 'pill dm-on', title: 'Only you and this agent see this conversation' }, icon('lock'), 'Private conversation')),
        h('small', null, sub.filter(Boolean).join(' · ')),
        status ? h('small', { class: 'cb-status' }, status) : null),
      busy && waiting && s.running && to !== 'ceo'
        ? btn('Ask now', () => this.askNow(), { cls: 'sm', ic: 'zap', title: `${Name} stops its current step, answers you, then carries on` }) : null,
      h('button', { class: 'icon-btn sm', type: 'button', title: 'Back to the team chat', 'aria-label': 'Back to the team chat', onclick: () => this.talkTo('') }, icon('x')));
    this.convBar.classList.remove('hidden');
  }

  // "Waiting for Ada's answer…" under the owner's last message in a private conversation
  syncWait() {
    const s = this.last || {};
    if (this.to && this.convLast[this.to] === 'you' && s.running && s.raw_phase !== 'done') {
      this.waitEl.lastChild.textContent = `Waiting for ${this.nameOf(this.to)}’s answer…`;
      this.feed.append(this.waitEl);
    } else this.waitEl.remove();
  }

  applyEmpty() {
    const to = this.to;
    const any = !!to && [...this.feed.children].some((el) => el !== this.waitEl && el.dataset.conv === to);
    this.convEmpty.classList.toggle('hidden', !to || any);
    if (!to || any) return;
    const name = this.nameOf(to);
    clear(this.convEmpty, icon(to === 'ceo' ? 'brain' : 'lock'), h('b', null, `Talk to ${name} privately`),
      h('p', null, to === 'ceo'
        ? 'Ask about the plan, the priorities or how the project is going. The CEO reads the brief, the plan and the team chat, answers in plain words, and can make a binding decision for the team if your question needs one.'
        : `Only ${name} sees what you write here, and the answer comes back here. If what you say changes anyone else’s work, ${name} passes it on to them and to the lead, so the whole team stays on the same page.`));
  }

  async askNow() {
    const name = cap(this.nameOf(this.to));
    try {
      await api(`/api/runs/${this.id}/interrupt`, { method: 'POST', body: { seat: this.to } });
      toast(`${name} stops its current step, answers you, then carries on.`);
    } catch (e) { fail(e); }
  }

  async send() {
    const text = this.say.value.trim();
    if (!text) return;
    if (this.to && this.last && this.last.raw_phase === 'done') {
      toast(`This project is finished, so ${this.nameOf(this.to)} no longer runs and cannot answer.`);
      return;
    }
    this.say.value = '';
    this.say.style.height = 'auto';
    try {
      await api(`/api/runs/${this.id}/say`, { method: 'POST', body: { text, to: this.to || null } });
      if (this.to) this.convLast[this.to] = 'you';
      this.poll();
    } catch (e) { fail(e); this.say.value = text; }
  }

  async stop() {
    if (!(await confirmBox('Stop the team?', 'They save their work first. You can continue the project later.', { ok: 'Stop', danger: true }))) return;
    try { await api(`/api/runs/${this.id}/stop`, { method: 'POST', body: {} }); toast('The team is stopping after saving its work.'); } catch (e) { fail(e); }
  }

  async resume() {
    try {
      const r = await api(`/api/runs/${this.id}/resume`, { method: 'POST', body: {} });
      toast(r.already_running ? 'The team is still working on this project — nothing needed restarting.' : 'The team is picking up where it left off.');
      this.poll(1200);
    } catch (e) { fail(e); }
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
  if (k) return k.label;
  if (!id || id === 'codex-default') return 'ChatGPT';
  return String(id).replace(/^claude-/, '');
};

// The team's three tiers, as the owner set them (Settings → Team).
const TIER_LABEL = { workhorse: 'Workhorse', manager: 'Manager', ceo: 'CEO' };
const TIER_HINT = {
  workhorse: 'Routine, fully specified work for the workhorse model',
  manager: 'Work that needs high intelligence, for the manager model',
  ceo: 'Plan review and final approval',
};
const tierModel = (tier) => {
  const m = (store.overview && store.overview.models) || {};
  return modelName({ workhorse: m.workhorse, manager: m.work, ceo: m.ceo }[tier] || '');
};

const statusWord = (a) => {
  if (a.status === 'busy' || a.status === 'working') return `working${a.task ? ` on task #${a.task}` : ''}`;
  return { idle: 'ready', standby: 'standing by', starting: 'starting', stopped: 'stopped', waiting: 'waiting for its subscription', down: 'unavailable', done: 'done' }[a.status] || a.status || '';
};

// Which private conversation a message belongs to ('' for the team chat).
const convOf = (m) => (!m.to ? '' : m.who === 'you' ? m.to : m.to === 'you' ? m.who : '');

function agentCard(a, phase, talk) {
  const busy = a.status === 'busy' || a.status === 'working';
  if (phase === 'done' && ['stopped', 'idle', 'standby'].includes(a.status)) a = { ...a, status: 'done', doing: '' };
  const product = PRODUCT_KEY[a.product] || 'claude';
  const helpers = a.helpers || [];
  const workingHelpers = helpers.filter((x) => x.status === 'working').length;
  const name = a.title || cap(a.name);
  const to = a.role === 'CEO' ? 'ceo' : a.name;
  const canTalk = talk && phase !== 'done' && (a.standing || a.role === 'CEO');
  const unread = canTalk ? talk.unread[to] || 0 : 0;
  return h('div', { class: 'agent' + (a.status === 'done' ? ' done' : '') },
    h('div', { class: 'a-top' },
      h('span', { class: 'av' + (busy ? ' busy' : ''), style: { background: a.role === 'CEO' ? 'var(--team)' : colorFor(a.name) } },
        a.role === 'CEO' ? icon(product === 'codex' ? 'gpt' : 'spark') : a.name.replace(/[^a-z0-9]/gi, '').slice(0, 2)),
      h('div', { class: 'a-name' }, h('b', null, name), h('small', null, [a.title && a.title.startsWith(a.role) ? '' : a.role, a.account].filter(Boolean).join(' · '))),
      a.status === 'done' ? h('span', { class: 'pill ok' }, 'Done') : a.status === 'failed' ? h('span', { class: 'pill bad' }, 'Stopped') : busy ? h('span', { class: 'pill live' }, 'Working') : h('span', { class: 'pill' }, STATE[a.status] || a.status || 'Ready')),
    h('div', { class: 'a-tags' }, productBadge(product, a.product || 'Claude'), a.model ? h('span', { class: 'pill outline' }, modelName(a.model)) : null,
      a.tier ? h('span', { class: 'pill t-' + a.tier, title: TIER_HINT[a.tier] || '' }, TIER_LABEL[a.tier] || a.tier) : null,
      h('span', { class: 'pill accent', title: 'Effort — set by the CEO for each job' }, icon('brain'), a.effort || 'auto')),
    a.doing && !['stopped', 'finished', 'ready'].includes(String(a.doing).toLowerCase()) ? h('div', { class: 'a-doing' }, busy ? h('span', { class: 'spinner' }) : null, h('span', null, cap(a.doing) + (a.task ? ` · task #${a.task}` : ''))) : null,
    h('div', { class: 'a-stats' }, h('span', null, `${tokens(a.tokens || 0)} tokens`), a.turns ? h('span', null, `${a.turns} turn${a.turns === 1 ? '' : 's'}`) : null,
      a.seconds ? h('span', null, minutes(a.seconds / 60)) : null, a.restarts ? h('span', null, `${a.restarts} restart${a.restarts === 1 ? '' : 's'}`) : null,
      a.helpers_total ? h('span', null, `${a.helpers_total} helper${a.helpers_total === 1 ? '' : 's'}${workingHelpers ? ` (${workingHelpers} working)` : ''}`) : null,
      canTalk ? h('button', {
        class: 'talk-btn' + (talk.active === to ? ' on' : ''), type: 'button', onclick: () => talk.open(to),
        title: a.role === 'CEO' ? 'Ask the CEO a question privately' : `Talk to ${name} privately — only ${name} sees your message`,
      }, icon(a.role === 'CEO' ? 'brain' : 'chat'), a.role === 'CEO' ? 'Ask' : 'Message', unread ? h('span', { class: 'dot-count', title: `${unread} new answer${unread === 1 ? '' : 's'}` }, String(unread)) : null) : null),
    helpers.length ? h('div', { class: 'helpers' }, helpers.slice(-5).map((x) => h('div', { class: 'hl' },
      x.status === 'working' ? h('span', { class: 'spinner' }) : icon(x.status === 'failed' ? 'x' : 'check'),
      h('span', null, x.what), h('small', { class: 'muted' }, x.seconds ? `${x.seconds}s` : x.type),
      canTalk && a.standing ? h('button', {
        class: 'hl-ask', type: 'button', title: `Helpers take instructions from ${name}: your message goes to ${name}, who passes it on`,
        onclick: () => talk.open(a.name, `About your helper “${x.what}”${x.status === 'working' ? '' : ' (finished)'}: `),
      }, 'Ask') : null))) : null);
}

function message(m, s) {
  const who = m.who === 'you' ? 'You' : m.who === 'crew' ? 'Crew' : m.who === 'ceo' ? 'CEO' : cap(m.who);
  const direct = !!m.to;
  const drafting = m.kind === 'draft' || m.kind === 'drafted';
  const kind = m.who === 'you' ? 'you' : ({ decision: 'decision', blocker: 'blocker', concern: 'blocker', lesson: 'lesson', system: 'system' }[m.kind] || '');
  const label = { question: 'question', answer: 'answer', blocker: 'needs help', concern: 'concern', decision: 'decision', lesson: 'lesson learned', share: 'shared' }[m.kind];
  if (!direct && (kind === 'system' || m.who === 'crew')) return h('div', { class: 'sysline', dataset: { id: m.id } }, h('time', null, clock(m.t)), h('span', null, m.text));
  const toName = m.to === 'ceo' ? 'the CEO' : cap(m.to);
  const dm = direct ? h('span', { class: 'pill dm-pill', title: 'Private: only you and this agent see it' }, icon('lock'), m.who === 'you' ? `to ${toName} · private` : 'to you · private') : null;
  const pending = drafting && m.kind === 'draft'
    ? h('span', { class: 'pill writer-pill', title: 'The prompt writer turns your words into a clear instruction before the team reads them' },
      s && s.running ? h('span', { class: 'spinner' }) : icon('pen'), s && s.running ? 'writing it up for the team…' : 'written up when the team continues')
    : null;
  // The prompt writer's version: the owner's own words are one tap away.
  const own = !drafting && m.original && m.original.trim() !== m.text.trim()
    ? h('details', { class: 'own-words' }, h('summary', null, icon('pen'), 'Written up by the prompt writer · your words'), h('div', null, m.original))
    : null;
  const avatar = m.who === 'ceo' ? h('span', { class: 'av', style: { background: 'var(--tier-ceo)' } }, icon('brain'))
    : h('span', { class: 'av', style: { background: m.who === 'you' ? 'var(--ink-2)' : colorFor(m.who) } }, m.who === 'you' ? 'Y' : m.who.replace(/[^a-z0-9]/gi, '').slice(0, 2));
  return h('div', { class: 'tmsg ' + kind + (direct ? ' direct' : '') + (m.kind === 'share' ? ' share' : ''), dataset: { id: m.id } },
    avatar,
    h('div', { class: 'stack tight' }, h('div', { class: 'who' }, h('b', null, who), h('span', null, clock(m.t)), label ? h('span', { class: 'pill' }, m.kind === 'share' ? icon('clip') : null, label) : null, dm, pending),
      h('div', { class: 'body' }, m.text), own));
}
