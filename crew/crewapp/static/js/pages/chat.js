// Conversations with Claude or ChatGPT, and the start screen. Everything the model does shows as it happens:
// thinking, each step, its to-do list, helpers it starts, its plan (plan mode), files it makes, how full its
// context is. One composer for everything: pick Claude, ChatGPT or the Team; the model; the effort (named
// exactly as Anthropic and OpenAI name them); plan mode; slash commands.

import { h, icon, btn, api, stream, opened, toast, fail, confirmBox, ask, dialog, store, bus, markdown, ago, autosize, isSmall, copyText,
  bytes, menu, closeMenu, tokens, greeting, PRODUCTS, productBadge, clear, pct, whenAt } from '../ui.js';
import { dictate, canDictate, canSpeak, speech, conversation, stopDictation } from '../voice.js';

// ------------------------------------------------------------------ choices

export const EFFORT_HINTS = {
  claude: {
    auto: 'Claude decides how hard to think', low: 'Quickest, light thinking', medium: 'Balanced speed and depth',
    high: 'Thinks carefully', xhigh: 'Thinks very carefully', max: 'Deepest thinking — uses the most of your limits',
  },
  codex: {  // OpenAI's own descriptions of GPT-6's levels
    auto: 'ChatGPT decides how hard to think', low: 'Fast responses with lighter reasoning',
    medium: 'Balances speed and reasoning depth for everyday tasks', high: 'Greater reasoning depth for complex problems',
    xhigh: 'Extra high reasoning depth for complex problems', max: 'Maximum reasoning depth for the hardest problems',
    ultra: 'Maximum reasoning, and it hands parts of the job to helpers — uses the most',
  },
};
export const TEAM_MODES = [
  { value: 'auto', label: 'Decide for me', hint: 'One agent for small jobs, the full team for big ones' },
  { value: 'solo', label: 'One agent', hint: 'One builder, plus an independent check' },
  { value: 'team', label: 'Full team', hint: 'Several agents build different parts at once' },
];
const TIMERS = [[0, 'No time limit'], [1, '1 hour'], [2, '2 hours'], [4, '4 hours'], [8, '8 hours'], [24, '24 hours']];

export function efforts(product) {
  const ov = store.overview || {};
  if (product === 'codex') return ov.codex_efforts || ['auto', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
  return ov.efforts || ['auto', 'low', 'medium', 'high', 'xhigh', 'max'];
}

export function modelOptions(product = 'claude') {
  const ov = store.overview || {};
  if (product === 'codex') {
    const known = (ov.known_models || []).filter((m) => m.engine === 'codex').map((m) => ({ value: m.id, label: m.label, hint: m.note }));
    const custom = ov.app && ov.app.codex_model;
    const extra = custom && !known.some((m) => m.value === custom) ? [{ value: custom, label: custom, hint: 'Chosen in Settings' }] : [];
    return known.length ? [...known, ...extra] : [{ value: '', label: 'ChatGPT', hint: 'Its best model' }, ...extra];
  }
  const known = (ov.known_models || []).filter((m) => (m.engine || 'claude') === 'claude');
  const allowed = ov.models && ov.models.allowed && ov.models.allowed.length ? ov.models.allowed : known.map((m) => m.id);
  return allowed.map((id) => { const k = known.find((m) => m.id === id); return { value: id, label: k ? k.label : id, hint: k ? k.note : '' }; });
}

const modelLabel = (product, id) => (modelOptions(product).find((m) => m.value === (id || '')) || { label: id || 'ChatGPT' }).label;
const LEGACY_EFFORT = { minimal: 'low' };  // chats started before GPT-6, which has no "minimal"

// The owner's subscriptions of one product, with how much of each is used (for choosing one).
function subscriptions(vendor) {
  const accs = ((store.overview && store.overview.accounts) || []).filter((a) => !vendor || a.vendor === vendor);
  const usage = Object.fromEntries(((store.usage && store.usage.accounts) || []).map((u) => [u.name, u.limits || {}]));
  return accs.map((a) => {
    const lim = usage[a.name] || {};
    const hint = lim.limited_until ? `At its limit until ${whenAt(lim.limited_until)}`
      : lim.five_util != null ? `${pct(Number(lim.five_util))} of its 5-hour limit used` : 'Not used yet today';
    return { name: a.name, vendor: a.vendor, hint };
  });
}

// Slash commands. `run` ones act in the app; the rest go to Claude Code, which runs them itself.
const APP_COMMANDS = [
  { cmd: '/plan', hint: 'Plan first: nothing changes until you approve', app: 'plan' },
  { cmd: '/auto', hint: 'Leave plan mode and work straight away', app: 'auto' },
  { cmd: '/compact', hint: 'Summarise the conversation to free up context', claude: true },
  { cmd: '/context', hint: 'Show what is filling the context', claude: true },
  { cmd: '/usage', hint: 'This conversation’s usage and cost', claude: true },
  { cmd: '/model', hint: 'Choose the model', app: 'model' },
  { cmd: '/effort', hint: 'Choose how hard it thinks', app: 'effort' },
  { cmd: '/files', hint: 'Show the files in this conversation', app: 'files' },
  { cmd: '/browser', hint: 'Open the browser beside the chat', app: 'browser' },
  { cmd: '/team', hint: 'Build what we discussed with the team', app: 'team' },
  { cmd: '/new', hint: 'Start a new chat', app: 'new' },
];
const SKILL_NAMES = {
  xlsx: 'Excel spreadsheets', docx: 'Word documents', pptx: 'PowerPoint presentations', pdf: 'PDF files', 'frontend-design': 'Beautiful web pages',
  'canvas-design': 'Posters and visual designs', 'doc-coauthoring': 'Write documents together', 'internal-comms': 'Internal announcements and memos',
  'brand-guidelines': 'Apply brand guidelines', 'skill-creator': 'Create a new skill', 'algorithmic-art': 'Generative art', 'theme-factory': 'Themes for slides and pages',
  'web-artifacts-builder': 'Interactive web apps', 'webapp-testing': 'Test a web app', 'mcp-builder': 'Build a connection (MCP server)',
  'slack-gif-creator': 'Animated GIFs', dataviz: 'Charts and data visualisation', 'code-review': 'Review code', 'security-review': 'Security review',
  review: 'Review a pull request', init: 'Describe this folder for Claude', simplify: 'Simplify code', loop: 'Repeat a task on a schedule', schedule: 'Schedule a task',
};

// ------------------------------------------------------------------ the composer

class Composer {
  constructor(opts) {
    this.o = opts;
    const app = (store.overview && store.overview.app) || {};
    this.product = opts.product || app.chat_engine || 'claude';
    this.fixed = !!opts.fixedProduct;
    this.model = opts.model ?? (this.product === 'codex' ? app.codex_model || '' : app.chat_model || 'claude-opus-5-5');
    this.effort = opts.effort || (this.product === 'codex' ? app.codex_effort : app.chat_effort) || 'auto';
    this.effort = LEGACY_EFFORT[this.effort] || this.effort;
    this.account = opts.account || '';  // the subscription chosen for this chat ('' = automatic)
    this.teamAccounts = null;           // the subscriptions a team project may use (null = all of them)
    this.headToHead = null;             // head-to-head comparisons for this project (null = the Settings default)
    this.mode = opts.mode || 'auto';
    this.teamMode = 'auto';
    this.hours = 0;
    this.busy = false;
    this.attachments = [];
    this.uploads = 0;
    this.build();
  }

  build() {
    this.ta = h('textarea', { rows: 1, 'aria-label': 'Message', placeholder: this.o.placeholder || 'Reply…' });
    this.fit = autosize(this.ta, 0.42);
    this.ta.addEventListener('keydown', (e) => this.onKey(e));
    this.ta.addEventListener('input', () => { this.slash(); this.sync(); });
    this.ta.addEventListener('paste', (e) => {
      const files = [...(e.clipboardData && e.clipboardData.files || [])];
      if (files.length) { e.preventDefault(); files.forEach((f) => this.upload(f)); }
    });
    this.atts = h('div', { class: 'atts' });
    this.fileIn = h('input', { type: 'file', multiple: true, class: 'hidden', onchange: (e) => { [...e.target.files].forEach((f) => this.upload(f)); e.target.value = ''; } });
    this.plusBtn = h('button', { class: 'icon-btn sm', type: 'button', title: 'Add files, pictures and more', 'aria-label': 'Add', onclick: () => this.plusMenu() }, icon('plus'));
    this.planBtn = h('button', { class: 'chip-btn', type: 'button', title: 'Plan mode: it plans first and waits for your approval', onclick: () => this.setMode(this.mode === 'plan' ? 'auto' : 'plan') },
      icon('map'), h('span', { class: 'hide-sm' }, 'Plan'));
    this.prodBtn = h('button', { class: 'chip-btn plain', type: 'button', title: 'Who answers, and with which model', onclick: () => this.productMenu() });
    this.effortBtn = h('button', { class: 'chip-btn plain', type: 'button', title: 'How hard it thinks', onclick: () => this.effortMenu() });
    this.timerBtn = h('button', { class: 'chip-btn plain', type: 'button', title: 'An optional time limit for the team', onclick: () => this.timerMenu() });
    this.acctBtn = h('button', { class: 'chip-btn plain acct-btn', type: 'button', title: 'Which subscription does the work', onclick: () => this.accountMenu() });
    this.ring = h('div', { class: 'ctx-ring hidden', title: '' });
    this.mic = h('button', { class: 'icon-btn sm mic', type: 'button', title: 'Speak instead of typing', 'aria-label': 'Speak', onclick: () => dictate(this.ta, this.mic) }, icon('mic'));
    if (!canDictate) this.mic.classList.add('hidden');
    this.talkBtn = h('button', { class: 'icon-btn sm', type: 'button', title: 'A spoken conversation', 'aria-label': 'Talk', onclick: () => this.o.onTalk && this.o.onTalk() }, icon('wave'));
    if (!(canDictate && canSpeak) || !this.o.onTalk) this.talkBtn.classList.add('hidden');
    this.sendBtn = h('button', { class: 'send-btn idle', type: 'button', title: 'Send', 'aria-label': 'Send', onclick: () => (this.busy ? this.o.onStop() : this.submit()) }, icon('send2'));
    this.improveBtn = h('button', { class: 'icon-btn sm improve-btn', type: 'button', 'aria-label': 'Improve my message',
      title: 'Improve my message: the prompt writer turns your words into a clear, precise prompt — you see it before it is sent', onclick: () => this.improve() }, icon('sparkles'));
    this.banner = h('div', { class: 'mode-banner hidden' }, icon('map'), h('span', null, 'Plan mode — it looks into things and writes a plan. Nothing changes until you approve it.'));
    this.el = h('div', { class: 'composer' }, this.banner, this.atts, this.ta,
      h('div', { class: 'c-row' }, this.plusBtn, this.fileIn, this.planBtn, this.prodBtn, this.effortBtn, this.acctBtn, this.timerBtn,
        h('div', { class: 'c-end' }, this.ring, this.improveBtn, this.mic, this.talkBtn, this.sendBtn)));
    // Drop files onto the composer
    let depth = 0;
    const zone = h('div', { class: 'dropzone hidden' }, 'Drop to attach');
    this.el.append(zone);
    this.el.addEventListener('dragenter', (e) => { if (e.dataTransfer && [...e.dataTransfer.types].includes('Files')) { depth++; zone.classList.remove('hidden'); } });
    this.el.addEventListener('dragleave', () => { if (--depth <= 0) { depth = 0; zone.classList.add('hidden'); } });
    this.el.addEventListener('dragover', (e) => e.preventDefault());
    this.el.addEventListener('drop', (e) => { e.preventDefault(); depth = 0; zone.classList.add('hidden'); [...(e.dataTransfer.files || [])].forEach((f) => this.upload(f)); });
    this.sync();
  }

  sync() {
    const p = this.product;
    const info = PRODUCTS[p];
    const label = p === 'team' ? (TEAM_MODES.find((m) => m.value === this.teamMode) || TEAM_MODES[0]).label : modelLabel(p, this.model);
    clear(this.prodBtn, icon(info.ic, 'prod-' + p), h('span', null, p === 'team' ? `Team · ${label}` : (p === 'codex' && !this.model ? 'ChatGPT' : `${p === 'codex' ? 'ChatGPT · ' : ''}${label}`)), icon('down', 'down'));
    const subs = subscriptions(p === 'team' ? null : p);
    this.acctBtn.classList.toggle('hidden', subs.length < 2 || (p === 'team' && !this.o.allowTeam));
    const acctLabel = p === 'team' ? (this.teamAccounts ? `${this.teamAccounts.length} of ${subs.length} subscriptions` : 'All subscriptions')
      : (this.account || 'Automatic');
    clear(this.acctBtn, icon('key'), h('span', { class: 'acct-tag' }, acctLabel), icon('down', 'down'));
    this.acctBtn.title = p === 'team' ? `Subscriptions this project may use: ${this.teamAccounts ? this.teamAccounts.join(', ') : 'all'}`
      : this.account ? `Runs on ${this.account}` : 'Which subscription does the work (automatic: the one with the most room)';
    clear(this.effortBtn, icon('brain'), h('span', { class: 'hide-sm' }, 'Effort: '), h('span', null, this.effort), icon('down', 'down'));
    this.effortBtn.classList.toggle('hidden', p === 'team');
    clear(this.timerBtn, icon('clock'), h('span', null, this.hours ? `${this.hours} h limit` : 'No time limit'), icon('down', 'down'));
    this.timerBtn.classList.toggle('hidden', p !== 'team');
    this.planBtn.classList.toggle('hidden', p === 'team');
    this.planBtn.classList.toggle('on', this.mode === 'plan');
    this.el.classList.toggle('plan', this.mode === 'plan' && p !== 'team');
    this.banner.classList.toggle('hidden', !(this.mode === 'plan' && p !== 'team'));
    const empty = !this.ta.value.trim() && !this.attachments.length;
    this.sendBtn.classList.toggle('idle', empty && !this.busy);
    this.sendBtn.classList.toggle('stop', this.busy);
    clear(this.sendBtn, icon(this.busy ? 'stop' : 'send2'));
    this.sendBtn.title = this.busy ? 'Stop' : 'Send';
    this.sendBtn.setAttribute('aria-label', this.sendBtn.title);
    this.ta.placeholder = this.o.placeholderFor ? this.o.placeholderFor(p) : this.ta.placeholder;
  }

  setBusy(b) { this.busy = b; this.sync(); }

  setMode(mode, tell = true) {
    if (this.product === 'team') return;
    this.mode = mode;
    this.sync();
    if (tell && this.o.onMode) this.o.onMode(mode);
  }

  setProduct(p) {
    if (this.fixed && p !== this.product) {
      toast(`This chat is with ${PRODUCTS[this.product].name}. Start a new chat to use ${PRODUCTS[p].name}.`, { action: 'New chat', onAction: () => { store.pendingProduct = p; location.hash = '#/new'; } });
      return;
    }
    const app = (store.overview && store.overview.app) || {};
    if (p !== this.product) {
      this.product = p;
      this.account = '';  // a subscription belongs to one product
      if (p === 'codex') { this.model = app.codex_model || ''; this.effort = app.codex_effort || 'auto'; }
      if (p === 'claude') { this.model = app.chat_model || 'claude-opus-5-5'; this.effort = app.chat_effort || 'auto'; }
      if (!efforts(p).includes(this.effort)) this.effort = 'auto';
    }
    this.sync();
    this.o.onProduct && this.o.onProduct(p);
  }

  context(ctx) {
    const used = ctx && ctx.used, win = ctx && ctx.window;
    if (!used || !win) { this.ring.classList.add('hidden'); return; }
    const frac = Math.min(1, used / win);
    const r = 8, c = 2 * Math.PI * r;
    this.ring.classList.remove('hidden');
    this.ring.classList.toggle('warn', frac >= 0.6 && frac < 0.8);
    this.ring.classList.toggle('bad', frac >= 0.8);
    const shown = Math.max(frac, 0.04);
    this.ring.innerHTML = `<svg viewBox="0 0 20 20"><circle class="track" cx="10" cy="10" r="${r}"/><circle class="fill" cx="10" cy="10" r="${r}" stroke-dasharray="${(shown * c).toFixed(2)} ${c.toFixed(2)}"/></svg><span class="ctx-pct">${Math.max(1, Math.round(frac * 100))}%</span>`;
    this.ring.title = `Context: ${tokens(used)} of ${tokens(win)} tokens used (${Math.max(1, Math.round(frac * 100))}%).` +
      (ctx.auto_at ? ` It is summarised automatically at ${tokens(ctx.auto_at)}.` : '') + ' Type /compact to summarise it now.';
  }

  // ---------------------------------------------------------------- menus

  productMenu() {
    const items = [];
    const sections = this.fixed ? [this.product] : ['claude', 'codex', ...(this.o.allowTeam ? ['team'] : [])];
    for (const p of sections) {
      if (p === 'team') {
        items.push({ section: 'Team — several agents build it together' });
        for (const m of TEAM_MODES) items.push({ label: m.label, hint: m.hint, ic: 'users', checked: this.product === 'team' && this.teamMode === m.value, value: { p, team: m.value } });
        continue;
      }
      items.push({ section: p === 'claude' ? 'Claude — by Anthropic' : 'ChatGPT — by OpenAI' });
      for (const m of modelOptions(p)) items.push({ label: m.label, hint: m.hint, ic: PRODUCTS[p].ic, checked: this.product === p && (this.model || '') === m.value, value: { p, model: m.value } });
    }
    if (this.product === 'team' && this.o.allowTeam) {
      const dflt = ((store.overview && store.overview.team) || {}).head_to_head || 'off';
      const h2h = this.headToHead || dflt;
      items.push({ section: 'Head-to-head — both models build some parts; the better is kept' },
        ...[['off', 'Off', 'Each part is built once'], ['some', 'A few parts', 'Up to two parts, where the scorecard knows least'], ['all', 'Every part', 'Uses the most tokens']]
          .map(([v, label, hint]) => ({ label, hint: v === dflt ? `${hint} · your default` : hint, ic: 'users', checked: h2h === v, value: { headToHead: v } })));
    }
    if (this.fixed) items.push('sep', { label: 'Use another product…', hint: 'Starts a new chat', ic: 'plus', value: { newChat: true } });
    menu(this.prodBtn, items, {
      above: true, minWidth: 260, onPick: (v) => {
        if (v.newChat) { location.hash = '#/new'; return; }
        if (v.headToHead) { this.headToHead = v.headToHead; this.sync(); return; }
        this.setProduct(v.p);
        if (v.team) this.teamMode = v.team;
        if (v.model !== undefined) this.model = v.model;
        this.sync();
        this.o.onChange && this.o.onChange();
      },
    });
  }

  accountMenu() {
    if (this.product === 'team') { this.chooseTeamAccounts(); return; }
    const mine = subscriptions(this.product);
    menu(this.acctBtn, [{ section: `Subscription — ${PRODUCTS[this.product].name}` },
      { label: 'Automatic', hint: 'The one with the most room; moves on if one runs out', ic: 'gauge', checked: !this.account, value: '' },
      ...mine.map((a) => ({ label: a.name, hint: a.hint, ic: 'key', checked: this.account === a.name, value: a.name }))], {
      above: true, minWidth: 280, onPick: (v) => {
        this.account = v;
        this.sync();
        if (v) toast(`This chat runs on ${v}. If it reaches its limit, Crew moves on to another and says so.`);
        this.o.onChange && this.o.onChange();
      },
    });
  }

  async chooseTeamAccounts() {
    const all = subscriptions();
    const chosen = new Set(this.teamAccounts || all.map((a) => a.name));
    const rows = all.map((a) => [a, h('input', { type: 'checkbox', checked: chosen.has(a.name) })]);
    const names = await dialog({
      title: 'Subscriptions for this project',
      body: h('div', { class: 'stack' },
        h('p', { class: 'muted small', style: { margin: 0 } }, 'The team uses only the subscriptions you tick. Keep at least one Claude subscription: the lead runs on Claude.'),
        ...rows.map(([a, cb]) => h('label', { class: 'check-row' }, cb, h('span', { class: 'grow' }, h('b', null, a.name), h('small', null, `${a.vendor === 'codex' ? 'ChatGPT' : 'Claude'} · ${a.hint}`))))),
      actions: [{ label: 'Cancel', value: null }, { label: 'Use these', primary: true, value: () => {
        const picked = rows.filter(([, cb]) => cb.checked).map(([a]) => a);
        if (!picked.some((a) => a.vendor === 'claude')) { toast('Tick at least one Claude subscription: the lead runs on Claude.', { bad: true }); return undefined; }
        return picked.map((a) => a.name);
      } }],
    });
    if (!names) return;
    this.teamAccounts = names.length === all.length ? null : names;
    this.sync();
  }

  effortMenu() {
    const p = this.product;
    menu(this.effortBtn, [{ section: `Effort — ${PRODUCTS[p].name}’s own levels` },
      ...efforts(p).map((e) => ({ label: e, hint: (EFFORT_HINTS[p] || EFFORT_HINTS.claude)[e] || '', checked: e === this.effort, value: e }))], {
      above: true, minWidth: 250, onPick: (v) => { this.effort = v; this.sync(); this.o.onChange && this.o.onChange(); },
    });
  }

  timerMenu() {
    menu(this.timerBtn, [{ section: 'Stop the team after…' }, ...TIMERS.map(([v, l]) => ({ label: l, hint: v ? 'The team wraps up and reports when time is up' : 'They work until the job is done', checked: this.hours === v, value: v }))], {
      above: true, onPick: (v) => { this.hours = v; this.sync(); },
    });
  }

  plusMenu() {
    const items = [
      { label: 'Add files or pictures', ic: 'clip', value: 'files' },
      { label: 'Add a capture', hint: 'A screenshot or recording you made', ic: 'camera', value: 'capture' },
      'sep',
      { label: 'Open the browser beside the chat', ic: 'globe', value: 'browser' },
      { label: 'Connections and API keys', ic: 'plug', value: 'connections' },
      { label: 'Skills', ic: 'sparkles', value: 'skills' },
    ];
    menu(this.plusBtn, items, {
      above: true, onPick: async (v) => {
        if (v === 'files') this.fileIn.click();
        else if (v === 'capture') this.pickCapture();
        else if (v === 'browser') bus.emit('panel:open', 'browser');
        else location.hash = '#/' + v;
      },
    });
  }

  async pickCapture() {
    let list = [];
    try { list = (await api('/api/captures')).captures.slice(0, 12); } catch (e) { fail(e); return; }
    if (!list.length) { toast('No captures yet. Take one from the Library or the browser.'); return; }
    menu(this.plusBtn, list.map((c) => ({ label: c.name, hint: `${c.kind === 'video' ? 'Recording' : 'Picture'} · ${ago(c.created)}`, ic: c.kind === 'video' ? 'video' : 'image', value: c })), {
      above: true, minWidth: 300, onPick: async (c) => {
        try {
          const id = await this.o.ensureChat();
          const info = await api(`/api/chats/${id}/attach-capture`, { method: 'POST', body: { name: c.name } });
          this.attachments.push({ ...info, name: c.name });
          this.renderAtts();
        } catch (e) { fail(e); }
      },
    });
  }

  // ---------------------------------------------------------------- slash commands

  commands() {
    const info = store.claudeInfo || {};
    const cmds = APP_COMMANDS.filter((c) => !c.claude || this.product === 'claude').filter((c) => c.app !== 'team' || this.o.onTeam);
    if (this.product === 'claude') {
      const skills = [...new Set([...(info.skills || []), ...Object.keys(SKILL_NAMES).filter((k) => (info.slash_commands || []).includes(k))])];
      for (const s of skills) {
        if (cmds.some((c) => c.cmd === '/' + s)) continue;
        cmds.push({ cmd: '/' + s, hint: SKILL_NAMES[s] || 'Skill', skill: true });
      }
    }
    return cmds;
  }

  slash() {
    const v = this.ta.value;
    const m = v.match(/^\/([\w:-]*)$/);
    if (!m) { if (this.slashMenu) { this.slashMenu.close(); this.slashMenu = null; } return; }
    const q = m[1].toLowerCase();
    const list = this.commands().filter((c) => c.cmd.slice(1).toLowerCase().includes(q)).slice(0, 14);
    if (!list.length) { if (this.slashMenu) { this.slashMenu.close(); this.slashMenu = null; } return; }
    this.slashMenu = menu(this.el, list.map((c) => ({ cmd: c.cmd, hint: c.hint, ic: c.skill ? 'sparkles' : 'slash', value: c })), {
      above: true, keepFocus: true, minWidth: 320, onPick: (c) => this.runCommand(c),
    });
  }

  runCommand(c) {
    this.slashMenu = null;
    if (!c.app) {  // Claude Code runs it (or the skill): send it as the message
      this.ta.value = c.cmd + (c.skill ? ' ' : '');
      this.fit();
      if (!c.skill) this.submit(); else this.ta.focus();
      return;
    }
    this.ta.value = '';
    this.fit();
    if (c.app === 'plan' || c.app === 'auto') this.setMode(c.app);
    else if (c.app === 'model') this.productMenu();
    else if (c.app === 'effort') this.effortMenu();
    else if (c.app === 'files') this.o.onFiles && this.o.onFiles();
    else if (c.app === 'browser') bus.emit('panel:open', 'browser');
    else if (c.app === 'team') this.o.onTeam && this.o.onTeam();
    else if (c.app === 'new') location.hash = '#/new';
    this.sync();
  }

  onKey(e) {
    if (this.slashMenu && !this.slashMenu.closed) {
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') { e.preventDefault(); this.slashMenu.move(e.key === 'ArrowDown' ? 1 : -1); return; }
      if (e.key === 'Enter' || e.key === 'Tab') { e.preventDefault(); if (this.slashMenu.pick()) return; }
      if (e.key === 'Escape') { this.slashMenu.close(); this.slashMenu = null; return; }
    }
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey || (!e.shiftKey && !isSmall()))) { e.preventDefault(); this.submit(); }
    if (e.key === 'Tab' && e.shiftKey && this.product !== 'team') { e.preventDefault(); this.setMode(this.mode === 'plan' ? 'auto' : 'plan'); }
  }

  // The prompt writer: the owner's words, typed or dictated, become a clear and precise prompt in the message box.
  async improve(thenSend = false) {
    const text = this.ta.value.trim();
    if (!text) { toast('Write or say your message first; the prompt writer then makes it clear and precise.'); return false; }
    if (this.improving) return false;
    this.improving = true;
    this.improveBtn.classList.add('busy');
    this.improveBtn.disabled = true;
    try {
      const r = await api('/api/improve', { method: 'POST', body: { text, reader: this.product } });
      const original = text;
      this.ta.value = r.text;
      this.improvedText = r.text.trim();
      this.fit();
      this.sync();
      if (!thenSend) {
        toast(r.changed ? 'Written up by the prompt writer. Check it, then send.' : 'Your message is already clear.', {
          ms: 7000, action: r.changed ? 'Undo' : null,
          onAction: () => { this.ta.value = original; this.improvedText = null; this.fit(); this.sync(); },
        });
      }
      return true;
    } catch (e) {
      fail(e);
      return false;
    } finally {
      this.improving = false;
      this.improveBtn.classList.remove('busy');
      this.improveBtn.disabled = false;
    }
  }

  submit() {
    // One send at a time: Enter pressed again, or the button clicked as well, before Crew has answered must not
    // start a second team or make a second chat.
    if (this.busy || this.sending) return;
    if (this.improving) { toast('One moment — the prompt writer is still writing your message up.'); return; }
    if (this.uploads) { toast('One moment — still attaching your file.'); return; }
    const text = this.ta.value.trim();
    if (!text && !this.attachments.length) { this.ta.focus(); return; }
    const auto = store.overview && store.overview.app && store.overview.app.improve_prompts;
    if (auto && text && !text.startsWith('/') && this.improvedText !== text && !this.improving) {
      this.improve(true).then(() => { this.improvedText = this.ta.value.trim(); this.submit(); });  // sent as written up (or as typed, if the writer could not help)
      return;
    }
    closeMenu();
    stopDictation();
    speech.stop();
    const atts = this.attachments;
    this.sending = true;
    Promise.resolve(this.o.onSend(text, atts, { product: this.product, model: this.model, effort: this.effort, mode: this.mode, teamMode: this.teamMode, hours: this.hours,
      account: this.account, accounts: this.teamAccounts, headToHead: this.headToHead }))
      .catch(fail).finally(() => { this.sending = false; });
  }

  clear() {
    this.ta.value = '';
    this.attachments = [];
    this.renderAtts();
    this.fit();
    this.sync();
  }

  // ---------------------------------------------------------------- attachments

  async upload(file) {
    if (this.product === 'team') { toast('Attach files in a chat; the team works from your description.'); return; }
    if (file.size > 300 * 1024 * 1024) { toast('That file is too large (300 MB at most).', { bad: true }); return; }
    const chip = { name: file.name, path: '', uploading: true };
    this.attachments.push(chip);
    this.uploads++;
    this.renderAtts();
    try {
      const id = await this.o.ensureChat();
      const info = await api(`/api/chats/${id}/upload?name=${encodeURIComponent(file.name)}`, { method: 'POST', raw: file });
      Object.assign(chip, info, { uploading: false });
    } catch (e) {
      fail(e);
      this.attachments = this.attachments.filter((a) => a !== chip);
    }
    this.uploads--;
    this.renderAtts();
  }

  renderAtts() {
    clear(this.atts, ...this.attachments.map((a) => h('span', { class: 'att' + (a.uploading ? ' up' : '') },
      icon(a.uploading ? 'reload' : 'clip'), h('span', null, a.name + (a.size ? ` · ${bytes(a.size)}` : '')),
      h('button', { type: 'button', title: 'Remove', 'aria-label': 'Remove', onclick: () => { this.attachments = this.attachments.filter((x) => x !== a); this.renderAtts(); } }, icon('x')))));
    this.sync();
  }
}

// ------------------------------------------------------------------ one answer, as it happens

function withCursor(html) {
  const m = html.match(/(<\/(?:p|li|h\d|td|th)>)((?:\s*<\/(?:ul|ol|tr|tbody|table|blockquote)>)*)\s*$/);
  const cur = '<span class="cursor"></span>';
  return m ? html.slice(0, m.index) + cur + html.slice(m.index) : html + cur;
}

const SHOWN_AT_FIRST = 120;  // messages of a chat drawn when it opens (the rest on request)
const STEP_ICONS = { plan: 'map', web: 'globe', file: 'doc', search: 'search', run: 'terminal', agent: 'bot', skill: 'sparkles', setup: 'tool', todo: 'listcheck',
  schedule: 'clock', browser: 'globe', phone: 'phone', computer: 'monitor', connection: 'plug', tool: 'tool', account: 'key' };
const FILE_ICONS = { web: 'globe', doc: 'doc', image: 'image', pdf: 'doc', table: 'table', file: 'doc' };

class AiTurn {
  constructor(page, { engine = 'claude', live = false, meta = {}, text = '', last = false } = {}) {
    this.page = page;
    this.engine = engine;
    this.live = live;
    this.meta = meta;
    this.text = '';
    this.steps = new Map();
    this.agents = new Map();
    this.todos = [];
    this.thinkTokens = 0;
    this.started = Date.now();
    this.mark = h('div', { class: 'ai-mark' + (live ? ' work' : '') }, icon(engine === 'codex' ? 'gpt' : 'spark'));
    this.thinkRow = h('button', { class: 'thinking hidden', type: 'button' });
    this.stepsBox = h('div', { class: 'steps-box hidden' });
    this.stepsHead = h('button', { class: 'steps-head', type: 'button', onclick: () => this.stepsBox.classList.toggle('open') });
    this.stepsList = h('div', { class: 'steps-list' });
    this.stepsBox.append(this.stepsHead, this.stepsList);
    this.agentsCard = h('div', { class: 'work-card hidden' });
    this.todoCard = h('div', { class: 'work-card hidden' });
    this.answer = h('div', { class: 'answer md' });
    this.typing = h('div', { class: 'typing' + (live ? '' : ' hidden') }, h('i'), h('i'), h('i'));
    this.planBox = h('div', { class: 'hidden' });
    this.filesRow = h('div', { class: 'files-row hidden' });
    this.foot = h('div', { class: 'ai-foot' });
    this.el = h('div', { class: 'turn-ai' + (engine === 'codex' ? ' codex' : '') + (last ? ' last' : '') },
      this.mark, this.thinkRow, this.stepsBox, this.agentsCard, this.todoCard, this.typing, this.answer, this.planBox, this.filesRow, this.foot);
    if (!live) this.finish(text, meta, false);
    else {
      for (const s of meta.steps || []) this.step(s);
      for (const a of meta.agents || []) this.agent(a);
      if (meta.todos && meta.todos.length) this.setTodos(meta.todos);
      if (meta.thinking) this.thinking(meta.thinking, false);
      if (meta.plan) this.plan(meta.plan, false);
      if (text) this.delta(text);
    }
  }

  thinking(tokensN, active = true) {
    this.thinkTokens = Math.max(this.thinkTokens, Number(tokensN || 0));
    this.thinkRow.classList.remove('hidden');
    this.thinkRow.classList.toggle('active', active && !this.text);
    const secs = Math.max(1, Math.round((Date.now() - this.started) / 1000));
    clear(this.thinkRow, icon('brain'), h('span', { class: 'lbl' }, active && !this.text ? 'Thinking…' : this.live ? `Thought for ${secs}s` : 'Thought it through'),
      this.thinkTokens ? h('span', { class: 'faint small' }, `~${tokens(this.thinkTokens)} tokens`) : null);
    this.thinkRow.title = 'Claude thinks before it answers. Higher effort means longer thinking.';
  }

  step(s) {
    const prev = this.steps.get(s.id);
    this.steps.set(s.id, { ...(prev || {}), ...s });
    this.drawSteps();
    if (this.live && !prev) {
      if (!isSmall() && s.kind === 'browser') bus.emit('panel:open', 'browser');
      if (!isSmall() && s.kind === 'phone') bus.emit('panel:open', 'phone');
      if (s.kind === 'computer' && !this.page.warnedComputer) {
        this.page.warnedComputer = true;
        toast('The assistant is using your computer. To stop it, push the mouse pointer into the top-left corner.', { ms: 8000 });
      }
    }
  }

  drawSteps() {
    const all = [...this.steps.values()];
    if (!all.length) return;
    this.stepsBox.classList.remove('hidden');
    const running = all.filter((s) => s.status === 'running');
    const current = running[running.length - 1] || all[all.length - 1];
    const failed = all.filter((s) => s.status === 'error').length;
    clear(this.stepsHead, icon('chev', 'chev'),
      running.length ? h('span', { class: 'spinner' }) : icon(STEP_ICONS[current.kind] || 'tool'),
      h('span', { class: 'now' }, running.length ? current.label + (current.detail ? ` — ${current.detail}` : '')
        : all.length === 1 ? current.label : `Worked through ${all.length} steps`),
      h('span', { class: 'count' }, running.length ? `${all.length} step${all.length === 1 ? '' : 's'}` : failed ? `${failed} failed` : 'Show'));
    clear(this.stepsList, ...all.map((s) => h('div', { class: 'step-row' },
      h('span', { class: 'st' }, s.status === 'running' ? h('span', { class: 'spinner' }) : s.status === 'error' ? icon('x', 'bad') : icon('check', 'ok')),
      h('span', { class: 'kind' }, icon(STEP_ICONS[s.kind] || 'tool')),
      h('span', { class: 'lbl' }, s.label, s.detail ? h('span', { class: 'det', title: s.detail }, s.detail) : null))));
  }

  agent(a) {
    this.agents.set(a.id, a);
    const all = [...this.agents.values()];
    this.agentsCard.classList.remove('hidden');
    const running = all.filter((x) => x.status === 'running').length;
    clear(this.agentsCard, h('div', { class: 'wc-head' }, icon('bot'), h('span', { class: 'grow' }, `Helpers (${all.length})`),
      running ? h('span', { class: 'pill live' }, `${running} working`) : h('span', { class: 'pill ok' }, 'All done')),
    ...all.map((x) => h('div', { class: 'helper' }, h('span', { class: 'h-ico' }, x.status === 'running' ? h('span', { class: 'spinner' }) : icon(x.status === 'error' ? 'x' : 'check')),
      h('div', { class: 'grow' }, h('span', null, x.description || 'A helper'),
        h('small', null, [x.type && x.type !== 'helper' ? x.type : '', x.steps ? `${x.steps} step${x.steps === 1 ? '' : 's'}` : '', x.last || '', x.seconds ? `${x.seconds}s` : ''].filter(Boolean).join(' · '))))));
  }

  setTodos(items) {
    this.todos = items || [];
    if (!this.todos.length) { this.todoCard.classList.add('hidden'); return; }
    const done = this.todos.filter((t) => t.status === 'completed').length;
    this.todoCard.classList.remove('hidden');
    clear(this.todoCard, h('div', { class: 'wc-head' }, icon('listcheck'), h('span', { class: 'grow' }, 'To-do list'),
      h('span', { class: 'muted small' }, `${done} of ${this.todos.length} done`)),
    ...this.todos.map((t) => h('div', { class: 'todo ' + (t.status || 'pending') }, h('span', { class: 'box' }, t.status === 'completed' ? icon('check') : null), h('span', { class: 't' }, t.text))));
  }

  plan(text, fresh = true) {
    this.planText = text;
    this.drawPlan();
    if (fresh) this.page.scrollDown();
  }

  drawPlan() {
    if (!this.planText) { this.planBox.classList.add('hidden'); return; }
    const canApprove = !this.live && this.page.isLatestPlan(this);
    const approved = !canApprove && this.page.turns.indexOf(this) >= 0 && this.page.turns.indexOf(this) < this.page.turns.length - 1;
    this.planBox.className = 'plan-card';
    clear(this.planBox, h('div', { class: 'pc-head' }, icon('map'), h('span', { class: 'grow' }, 'The plan'), approved ? h('span', { class: 'pill ok' }, icon('check'), 'Approved') : null),
      h('div', { class: 'pc-body md', html: markdown(this.planText) }),
      canApprove ? h('div', { class: 'pc-foot' },
        btn('Approve and start', () => this.page.approvePlan(), { cls: 'accent sm', ic: 'check' }),
        btn('Change something', () => { this.page.composer.ta.focus(); toast('Say what to change; it will revise the plan.'); }, { cls: 'sm' }),
        h('span', { class: 'muted small' }, 'Nothing is changed until you approve.')) : null);
  }

  delta(piece) {
    this.text += piece;
    this.typing.classList.add('hidden');
    if (this.thinkRow.classList.contains('active')) this.thinking(this.thinkTokens, false);
    if (!this.raf) {
      this.raf = requestAnimationFrame(() => {
        this.raf = 0;
        this.answer.innerHTML = withCursor(markdown(this.text));
        this.page.scrollDown();
      });
    }
  }

  finish(text, meta = {}, fromLive = true) {
    cancelAnimationFrame(this.raf);
    this.live = false;
    this.meta = meta;
    this.mark.classList.remove('work');
    this.typing.classList.add('hidden');
    if (!fromLive) {
      for (const s of meta.steps || []) this.steps.set(s.id, s);
      this.drawSteps();
      for (const a of meta.agents || []) this.agents.set(a.id, a);
      if (this.agents.size) this.agent([...this.agents.values()].pop());
      if (meta.todos && meta.todos.length) this.setTodos(meta.todos);
      if (meta.thinking) { this.thinkTokens = meta.thinking; this.thinking(meta.thinking, false); }
    } else {
      if (meta.todos && meta.todos.length) this.setTodos(meta.todos);
      for (const s of meta.steps || []) this.steps.set(s.id, s);
      this.drawSteps();
      if (this.thinkTokens) this.thinking(this.thinkTokens, false);
    }
    this.planText = meta.plan || this.planText || '';
    this.text = text || '';
    const cls = meta.error ? ' error' : meta.notice || meta.command ? ' notice-text' : '';
    this.answer.className = 'answer md' + cls;
    this.answer.innerHTML = markdown(this.text);
    this.drawPlan();
    const files = meta.files || [];
    this.filesRow.classList.toggle('hidden', !files.length);
    clear(this.filesRow, ...files.map((f) => h('button', { class: 'file-card', type: 'button', onclick: () => bus.emit('panel:preview', f) },
      h('span', { class: 'f-ico ' + (f.kind || '') }, icon(FILE_ICONS[f.kind] || 'doc')),
      h('span', { class: 'txt' }, h('b', null, f.name.split('/').pop()), h('small', null, f.kind === 'web' ? 'Web page · click to open beside the chat' : 'Click to open')))));
    const usage = meta.usage || {};
    const total = (usage.input || 0) + (usage.output || 0) + (usage.cache_read || 0) + (usage.cache_write || 0);
    const bits = [
      meta.model !== undefined && meta.engine ? (meta.engine === 'codex' ? `ChatGPT · ${modelLabel('codex', meta.model)}` : `Claude · ${modelLabel('claude', meta.model)}`) : '',
      meta.effort && meta.engine ? `effort ${meta.effort}` : '',
      meta.seconds ? `${Math.round(meta.seconds)}s` : '',
      usage.output ? `${tokens(usage.output)} tokens written` : '',
      total && !usage.output ? `${tokens(total)} tokens` : '',
      meta.account ? meta.account : '',
    ].filter(Boolean);
    clear(this.foot, 
      h('button', { class: 'icon-btn sm', type: 'button', title: 'Copy', onclick: () => copyText(this.text) }, icon('copy')),
      canSpeak ? h('button', { class: 'icon-btn sm', type: 'button', title: 'Read aloud', onclick: () => speech.speak(this.text) }, icon('volume')) : null,
      bits.length ? h('span', { class: 'meta', title: total ? `Tokens: ${tokens(usage.input || 0)} in, ${tokens(usage.output || 0)} out, ${tokens(usage.cache_read || 0)} from cache` : '' }, bits.join(' · ')) : null);
  }
}

// ------------------------------------------------------------------ the conversation page

class ChatView {
  constructor(view, id, { home = false } = {}) {
    this.view = view;
    this.id = id || null;
    this.home = home;
    this.chat = null;
    this.es = null;
    this.live = null;
    this.alive = true;
    this.voice = null;
    this.turns = [];
    const pendingProduct = store.pendingProduct;
    store.pendingProduct = null;
    this.composer = new Composer({
      product: pendingProduct || undefined,
      allowTeam: home,
      placeholderFor: (p) => (home ? (p === 'team' ? 'Describe what the team should build…' : 'How can I help you today?') : 'Reply…'),
      onSend: (text, atts, s) => this.send(text, atts, s),
      onStop: () => this.stop(),
      onMode: (m) => this.changeMode(m),
      onTalk: () => this.talk(),
      onFiles: () => this.showFiles(true),
      onTeam: home ? null : () => this.toTeam(),
      onProduct: () => { if (home) this.drawHome(); },
      ensureChat: () => this.ensureChat(),
    });
    view.classList.add('chat-view');
    if (home) this.buildHome(); else this.buildChat();
    if (!home) this.load();
    this.offHome = bus.on('home:reset', () => { if (this.home) { this.composer.clear(); this.composer.ta.focus(); } });
  }

  // ---------------------------------------------------------------- layout

  buildHome() {
    const app = (store.overview && store.overview.app) || {};
    const name = (app.owner_name || '').trim().split(/\s+/)[0];
    this.tabs = h('div', { class: 'product-tabs', role: 'tablist' }, ['claude', 'codex', 'team'].map((p) => h('button', {
      type: 'button', role: 'tab', dataset: { p }, onclick: () => { this.composer.setProduct(p); this.composer.ta.focus(); },
    }, icon(PRODUCTS[p].ic), PRODUCTS[p].name)));
    this.suggest = h('div', { class: 'suggest' });
    this.strip = h('div', { class: 'home-strip' });
    this.root = h('div', { class: 'home' },
      h('div', { class: 'greet' }, icon('spark'), h('h1', null, `${greeting()}${name ? ', ' + name : ''}`)),
      this.tabs, this.composer.el, this.suggest, this.strip);
    this.view.append(this.root);
    store.setTop(null, []);
    this.drawHome();
    this.drawStrip();
    setTimeout(() => this.composer.ta.focus(), 60);
  }

  drawHome() {
    const p = this.composer.product;
    [...this.tabs.children].forEach((b) => b.classList.toggle('on', b.dataset.p === p));
    const ideas = p === 'team' ? [
      ['globe', 'A website', 'Build a one-page website for an investment conference, with the agenda, speakers and a registration form.'],
      ['table', 'A dashboard', 'Build a dashboard that shows project approvals from a spreadsheet, with charts and filters.'],
      ['tool', 'A tool', 'Build a simple app to log investor meetings, follow-ups and reminders.'],
    ] : [
      ['feather', 'Write', 'Draft a formal letter inviting investors to a Punjab investment roadshow.'],
      ['search', 'Research', 'Summarise this week’s news on Pakistan’s industrial sector, with sources.'],
      ['table', 'Analyse', 'Compare Punjab’s special economic zones with Vietnam’s in a table.'],
      ['globe', 'Browse', 'Open the Punjab Board of Investment website and summarise its incentives page.'],
      ['doc', 'Documents', 'Turn my notes into a one-page Word briefing for the Chief Secretary.'],
    ];
    clear(this.suggest, ...ideas.map(([ic, label, text]) => h('button', { type: 'button', onclick: () => { this.composer.ta.value = text; this.composer.fit(); this.composer.sync(); this.composer.ta.focus(); } },
      icon(ic), label)));
  }

  drawStrip() {
    const ov = store.overview || {};
    const running = (ov.runs || []).filter((r) => r.running).slice(0, 3);
    clear(this.strip, ...running.map((r) => h('a', { href: '#/projects/' + r.id }, h('span', { class: 'dot live' }), h('span', { class: 'grow ellipsis' }, r.title),
      h('span', { class: 'muted small' }, r.phase), icon('chev'))));
  }

  buildChat() {
    this.thread = h('div', { class: 'thread' });
    this.dock = h('div', { class: 'composer-dock' }, this.composer.el,
      h('div', { class: 'composer-note' }, 'Claude and ChatGPT can make mistakes. Check important facts.'));
    this.root = h('div', { class: 'chat' }, this.thread, this.dock);
    this.view.append(this.root);
    this.titleBtn = h('button', { class: 'title-btn', type: 'button', title: 'Rename', onclick: () => this.rename() }, h('span', { class: 'ellipsis' }, '…'), icon('down'));
    this.drawTop();
  }

  drawTop() {
    const c = this.chat || {};
    this.titleBtn.firstChild.textContent = c.title || 'New chat';
    document.title = (c.title || 'Chat') + ' · Crew';
    store.setTop(h('div', { class: 'row', style: { gap: '4px', minWidth: 0 } }, this.titleBtn, c.engine ? productBadge(c.engine) : null), [
      h('button', { class: 'icon-btn', type: 'button', title: 'Files in this chat', onclick: () => this.showFiles(true) }, icon('folder')),
      h('button', { class: 'icon-btn', type: 'button', title: 'Open the browser beside the chat', onclick: () => bus.emit('panel:open', 'browser') }, icon('globe')),
      h('button', { class: 'icon-btn', type: 'button', title: 'More', onclick: (e) => this.moreMenu(e.currentTarget) }, icon('dots')),
    ]);
  }

  moreMenu(anchor) {
    const c = this.chat || {};
    menu(anchor, [
      { label: 'Rename', ic: 'pen', value: 'rename' },
      { label: c.pinned ? 'Unpin' : 'Pin to the top', ic: 'pin', value: 'pin' },
      { label: 'Files in this chat', ic: 'folder', value: 'files' },
      { label: 'Build this with the team', hint: 'The team reads this chat and builds what you asked for', ic: 'users', value: 'team' },
      'sep',
      { label: 'Delete', ic: 'trash', value: 'delete', danger: true },
    ], {
      align: 'end', onPick: async (v) => {
        if (v === 'rename') this.rename();
        else if (v === 'pin') { try { await api('/api/chats/' + this.id, { method: 'PUT', body: { pinned: !c.pinned } }); c.pinned = !c.pinned; bus.emit('chats'); } catch (e) { fail(e); } }
        else if (v === 'files') this.showFiles(true);
        else if (v === 'team') this.toTeam();
        else if (v === 'delete') this.remove();
      },
    });
  }

  // ---------------------------------------------------------------- loading and rendering

  async load() {
    try {
      this.chat = await api('/api/chats/' + this.id);
    } catch (e) {
      if (e.status === 404) { toast('That chat is gone.'); location.hash = '#/new'; return; }
      fail(e);
      return;
    }
    if (!this.alive) return;
    const c = this.chat;
    const cm = this.composer;
    cm.product = c.engine || 'claude';
    cm.fixed = c.messages.length > 0;
    cm.model = c.model ?? cm.model;
    cm.effort = c.effort || 'auto';
    cm.account = c.account || '';
    cm.mode = c.mode || 'auto';
    cm.sync();
    cm.context(c.context);
    this.drawTop();
    clear(this.thread);
    this.turns = [];
    this.lastTurn = null;
    const msgs = c.messages;
    // A long chat opens at once: its newest messages first, the earlier ones when asked for.
    const first = Math.max(0, msgs.length - SHOWN_AT_FIRST);
    if (first) this.thread.append(this.earlierButton(msgs.slice(0, first), c));
    msgs.slice(first).forEach((m, j) => {
      if (m.role === 'user') this.thread.append(this.userEl(m.text, m.meta || {}));
      else this.addTurn(new AiTurn(this, { engine: (m.meta && m.meta.engine) || c.engine || 'claude', meta: m.meta || {}, text: m.text, last: first + j === msgs.length - 1 }));
    });
    this.refreshPlans();
    this.panelFiles();
    await this.ensureStream();
    if (c.busy) {
      this.composer.setBusy(true);
      this.startLive(c.live || {}, c.partial || '');
    }
    const pending = store.pendingSend;
    store.pendingSend = null;
    if (pending && pending.id === this.id) {
      cm.attachments = pending.attachments || [];
      if (pending.voice) this.voice = pending.voice;
      this.send(pending.text, cm.attachments, pending.settings);
    }
    this.scrollDown(true);
    if (!pending) setTimeout(() => cm.ta.focus(), 50);
  }

  userEl(text, meta = {}) {
    const atts = meta.attachments || [];
    return h('div', { class: 'turn-user' },
      meta.mode === 'plan' ? h('span', { class: 'mode-tag' }, icon('map'), 'Plan mode') : null,
      atts.length ? h('div', { class: 'atts' }, atts.map((a) => h('span', { class: 'att' }, icon('clip'), h('span', null, String(a.name || a.path || a).split('/').pop())))) : null,
      h('div', { class: 'bubble' }, text));
  }

  addTurn(turn) {
    this.turns.push(turn);
    this.thread.append(turn.el);
    if (this.lastTurn && this.lastTurn !== turn) this.lastTurn.el.classList.remove('last');
    turn.el.classList.add('last');
    this.lastTurn = turn;
  }

  // The earlier part of a long chat, drawn above the newest when the owner asks, keeping their place.
  earlierButton(older, c) {
    const b = h('button', { class: 'btn ghost sm earlier', type: 'button' }, icon('history'), `Show ${older.length} earlier messages`);
    b.onclick = () => {
      const fromBottom = this.view.scrollHeight - this.view.scrollTop;
      const frag = document.createDocumentFragment();
      const turns = [];
      for (const m of older) {
        if (m.role === 'user') frag.append(this.userEl(m.text, m.meta || {}));
        else {
          const t = new AiTurn(this, { engine: (m.meta && m.meta.engine) || c.engine || 'claude', meta: m.meta || {}, text: m.text });
          turns.push(t);
          frag.append(t.el);
        }
      }
      b.replaceWith(frag);
      this.turns = [...turns, ...this.turns];
      turns.forEach((t) => t.drawPlan());
      this.view.scrollTop = this.view.scrollHeight - fromBottom;
    };
    return b;
  }

  isLatestPlan(turn) {
    if (!this.chat || this.chat.mode !== 'plan' || this.composer.busy) return false;
    const withPlan = this.turns.filter((t) => t.planText);
    return withPlan[withPlan.length - 1] === turn && this.turns[this.turns.length - 1] === turn;
  }

  refreshPlans() {
    // In plan mode, the latest answer is the plan when no separate plan file was written (e.g. ChatGPT).
    const last = this.turns[this.turns.length - 1];
    if (last && this.chat && this.chat.mode === 'plan' && !last.planText && last.meta && last.meta.mode === 'plan' && !last.meta.error && last.text && !last.meta.command) {
      last.planText = last.text;
      last.answer.classList.add('hidden');
    }
    this.turns.forEach((t) => t.drawPlan());
  }

  startLive(meta = {}, partial = '') {
    if (this.live) return this.live;
    this.live = new AiTurn(this, { engine: this.composer.product, live: true, meta, text: partial });
    this.addTurn(this.live);
    this.scrollDown(true);
    return this.live;
  }

  scrollDown(force = false) {
    const s = this.view;
    if (force || s.scrollHeight - s.scrollTop - s.clientHeight < 200) s.scrollTop = s.scrollHeight;
  }

  // ---------------------------------------------------------------- the live stream

  async ensureStream() {
    if (this.es || !this.id) return;
    const L = () => this.live || this.startLive();
    this.es = stream(`/api/chats/${this.id}/events`, {
      start: (d) => { this.composer.setBusy(true); const t = L(); if (d.engine) t.engine = d.engine; },
      thinking: (d) => L().thinking(d.tokens, d.active !== false),
      delta: (d) => { L().delta(d.text || ''); this.voice && this.voice.onDelta(d.text || ''); },
      step: (d) => L().step(d),
      agent: (d) => L().agent(d),
      todos: (d) => L().setTodos(d.items),
      plan: (d) => L().plan(d.text),
      context: (d) => { this.composer.context(d); if (this.chat) this.chat.context = d; },
      compacted: () => this.notice('The conversation was summarised to free up context.', 'good'),
      status: (d) => this.notice(d.text),
      rate: (d) => bus.emit('rate', d),
      notice: (d) => this.notice(d.text, d.kind === 'error' ? 'bad' : d.kind === 'updated' || d.kind === 'failover' ? 'good' : ''),
      done: (d) => this.finishLive(d),
    });
    let drops = 0;
    this.es.onerror = () => { if (++drops > 1 && this.composer.busy) setTimeout(() => this.resync(), 1500); };
    await opened(this.es);
  }

  notice(text, cls = '') {
    if (!text) return;
    this.thread.append(h('div', { class: 'notice-line ' + cls }, icon(cls === 'bad' ? 'info' : cls === 'good' ? 'check' : 'info'), text));
    this.scrollDown();
  }

  finishLive(d) {
    const meta = d.meta || {};
    const turn = this.live || this.startLive();
    this.live = null;
    turn.finish(d.text || '', meta, true);
    if (this.chat) {
      this.chat.messages = this.chat.messages || [];
      this.chat.messages.push({ role: 'assistant', text: d.text, meta });
      if (meta.mode) this.chat.mode = meta.mode;
      this.composer.fixed = true;
    }
    this.composer.setBusy(false);
    this.refreshPlans();
    this.scrollDown();
    if (this.voice) {
      const v = this.voice;
      this.voice = null;
      if (meta.error) v.onError(d.text); else v.onDone(d.text);
    } else if (!meta.error && !meta.notice && store.overview && store.overview.app && store.overview.app.auto_read && canSpeak) {
      speech.speak(d.text || '');
    }
    if (meta.files && meta.files.length) this.panelFiles();
    this.refreshTitle();
    bus.emit('chats');
  }

  async refreshTitle() {
    try {
      const c = await api('/api/chats/' + this.id);
      if (!this.alive) return;
      Object.assign(this.chat, { title: c.title, pinned: c.pinned, mode: c.mode });
      this.drawTop();
    } catch (e) { /* ignore */ }
  }

  async resync() {
    if (!this.alive || !this.id) return;
    try {
      const c = await api('/api/chats/' + this.id);
      if (!c.busy && this.composer.busy) {
        const last = c.messages[c.messages.length - 1];
        if (last && last.role === 'assistant') this.finishLive({ text: last.text, meta: last.meta });
        // Crew restarted in the middle of the answer: nothing more is coming, so do not wait for it for ever.
        else this.finishLive({ text: 'This answer was interrupted: Crew restarted before it was finished. Please send your message again.', meta: { error: true } });
      }
    } catch (e) { /* still offline */ }
  }

  // ---------------------------------------------------------------- actions

  async ensureChat() {
    if (this.id) return this.id;
    const cm = this.composer;
    const c = await api('/api/chats', { method: 'POST', body: { engine: cm.product === 'team' ? 'claude' : cm.product, model: cm.model, effort: cm.effort, mode: cm.mode, account: cm.product === 'team' ? '' : cm.account } });
    this.id = c.id;
    this.chat = c;
    if (!this.home) history.replaceState(null, '', '#/chat/' + c.id);
    bus.emit('chats');
    return this.id;
  }

  async send(text, atts, s) {
    const cm = this.composer;
    if (s.product === 'team') return this.startProject(text, s);
    if (this.home) {
      // Create the chat, then continue on its own page (so the address can be bookmarked and reopened).
      try {
        await this.ensureChat();
        // A spoken conversation goes along too: the chat page hears (and reads out) the answer.
        store.pendingSend = { id: this.id, text, attachments: atts, settings: s, voice: this.voice };
        this.voice = null;
        cm.clear();
        location.hash = '#/chat/' + this.id;
      } catch (e) {
        if (this.voice) { this.voice.onError(e.message); this.voice = null; }
        fail(e);
      }
      return;
    }
    try { await this.ensureChat(); await this.ensureStream(); } catch (e) { fail(e); return; }
    const shown = text || 'Please look at what I attached.';
    this.thread.append(this.userEl(shown, { attachments: atts, mode: s.mode }));
    cm.clear();
    cm.setBusy(true);
    this.startLive();
    if (this.chat && /^New (chat|conversation)$/.test(this.chat.title) && text && !text.startsWith('/')) { this.chat.title = text.slice(0, 60); this.drawTop(); }
    try {
      await api(`/api/chats/${this.id}/send`, {
        method: 'POST', body: { text, model: s.model, effort: s.effort, mode: s.mode, engine: s.product, account: s.account || '', attachments: atts.map((a) => a.path).filter(Boolean) },
      });
      if (this.chat) { this.chat.mode = s.mode; this.chat.engine = s.product; }
    } catch (e) {
      this.finishLive({ text: e.message, meta: { error: true } });
    }
  }

  async startProject(text, s) {
    try {
      const r = await api('/api/runs', { method: 'POST', body: { request: text, mode: s.teamMode, hours: s.hours || 0, accounts: s.accounts || null, head_to_head: s.headToHead || null } });
      this.composer.clear();
      bus.emit('runs');
      location.hash = '#/projects/' + r.id;
    } catch (e) { fail(e); }
  }

  async stop() {
    if (!this.id) return;
    try { await api(`/api/chats/${this.id}/stop`, { method: 'POST', body: {} }); } catch (e) { fail(e); }
  }

  async changeMode(mode) {
    if (this.chat) this.chat.mode = mode;
    this.refreshPlans();
    if (!this.id || this.home) return;
    try { await api(`/api/chats/${this.id}/mode`, { method: 'POST', body: { mode } }); } catch (e) { fail(e); }
    toast(mode === 'plan' ? 'Plan mode is on: it will plan first and wait for your approval.' : 'Plan mode is off: it works straight away.');
  }

  async approvePlan() {
    if (this.composer.busy) return;
    try { await this.ensureStream(); } catch (e) { fail(e); return; }
    this.chat.mode = 'auto';
    this.composer.setMode('auto', false);
    this.thread.append(this.userEl('The plan is approved. Carry it out now.', {}));
    this.composer.setBusy(true);
    this.startLive();
    this.turns.forEach((t) => t.drawPlan());
    try { await api(`/api/chats/${this.id}/approve`, { method: 'POST', body: {} }); } catch (e) { this.finishLive({ text: e.message, meta: { error: true } }); }
  }

  panelFiles() {
    if (!this.id) return;
    bus.emit('panel:files', { title: 'Files in this chat', load: async () => (await api(`/api/chats/${this.id}/files`)).files.filter((f) => !f.name.startsWith('attachments/') || true) });
  }

  showFiles(open) {
    if (!this.id) { toast('Files appear here once the chat has started.'); return; }
    bus.emit('panel:files', { title: 'Files in this chat', show: open, load: async () => (await api(`/api/chats/${this.id}/files`)).files });
  }

  async rename() {
    if (!this.id) return;
    const v = await ask('Rename this chat', [{ name: 'title', label: 'Name', value: this.chat.title, required: true }], { ok: 'Rename' });
    if (!v) return;
    try { await api('/api/chats/' + this.id, { method: 'PUT', body: { title: v.title } }); this.chat.title = v.title; this.drawTop(); bus.emit('chats'); } catch (e) { fail(e); }
  }

  async remove() {
    if (!(await confirmBox('Delete this chat?', 'It disappears from your list. Files it made stay in Crew’s folder on this computer.', { ok: 'Delete', danger: true }))) return;
    try { await api('/api/chats/' + this.id, { method: 'DELETE' }); bus.emit('chats'); location.hash = '#/new'; } catch (e) { fail(e); }
  }

  async toTeam() {
    if (!this.id || !this.chat || !this.chat.messages.some((m) => m.role === 'user')) { toast('Describe what you want first; then the team can build it.'); return; }
    const ok = await confirmBox('Build this with the team?', 'The team reads this chat and builds what you asked for as a project. You can follow along, see every agent, and chat with them.', { ok: 'Start the project' });
    if (!ok) return;
    try {
      const r = await api(`/api/chats/${this.id}/project`, { method: 'POST', body: { mode: 'auto' } });
      bus.emit('runs');
      location.hash = '#/projects/' + r.id;
    } catch (e) { fail(e); }
  }

  talk() {
    conversation({
      send: (text, handlers) => {
        if (this.composer.busy || this.composer.sending) { handlers.onError('Still answering — one moment.'); return; }
        this.voice = handlers;
        this.composer.ta.value = text;
        this.composer.submit();
      },
    });
  }

  destroy() {
    this.alive = false;
    if (this.es) this.es.close();
    stopDictation();
    closeMenu();
    this.offHome && this.offHome();
    bus.emit('panel:files', null);
  }
}

// ------------------------------------------------------------------ routes

async function loadClaudeInfo() {
  if (store.claudeInfo && Date.now() - (store.claudeInfoAt || 0) < 300000) return;
  try { store.claudeInfo = (await api('/api/claude')).info || {}; store.claudeInfoAt = Date.now(); } catch (e) { /* offline */ }
}

export function homePage(view) {
  loadClaudeInfo();
  const page = new ChatView(view, null, { home: true });
  return () => page.destroy();
}

export function chatPage(view, params) {
  loadClaudeInfo();
  const page = new ChatView(view, params[0]);
  return () => page.destroy();
}

export function chatsPage(view) {
  let all = [];
  let found = null;  // search results from all chats, however old (the list itself holds the newest ones)
  let limit = 300;
  let filter = 'all';
  let alive = true;
  let searchT = null;
  let asked = 0;
  const q = h('input', { type: 'search', placeholder: 'Search your chats', 'aria-label': 'Search' });
  const list = h('div', { class: 'list' });
  const note = h('p', { class: 'muted small hidden', style: { margin: '6px 2px 0' } });
  const seg = h('div', { class: 'seg' }, [['all', 'All'], ['claude', 'Claude'], ['codex', 'ChatGPT'], ['workflow', 'From workflows']].map(([v, l]) => h('button', {
    type: 'button', class: v === filter ? 'on' : '', onclick: (e) => { filter = v; [...seg.children].forEach((b) => b.classList.toggle('on', b === e.currentTarget)); draw(); },
  }, l)));
  function draw() {
    const words = q.value.trim().toLowerCase();
    const source = words && found ? found : all;
    const shown = source.filter((c) => (filter === 'all' ? c.kind !== 'workflow' : filter === 'workflow' ? c.kind === 'workflow' : c.engine === filter && c.kind !== 'workflow'))
      .filter((c) => !words || (c.title || '').toLowerCase().includes(words));
    clear(list, ...(shown.length ? shown.map((c) => h('a', { class: 'li', href: '#/chat/' + c.id },
      h('span', { class: 'li-ico ' + (c.engine || 'claude') }, icon(c.engine === 'codex' ? 'gpt' : 'spark')),
      h('span', { class: 'li-main' }, h('b', null, c.title || 'Untitled chat'), h('small', null, `${c.engine === 'codex' ? 'ChatGPT' : modelLabel('claude', c.model)} · ${ago(c.updated)}`)),
      c.pinned ? icon('pin') : null)) : [h('div', { class: 'li muted' }, words ? 'No chat matches.' : 'No chats yet.')]));
    note.textContent = `Showing your ${limit} most recent chats. Search to find an older one.`;
    note.classList.toggle('hidden', !!words || all.length < limit);
  }
  // Search every chat on the computer, not only the newest ones in the list.
  function search() {
    clearTimeout(searchT);
    const words = q.value.trim();
    if (!words) { found = null; draw(); return; }
    draw();
    const ask = ++asked;
    searchT = setTimeout(() => {
      api('/api/chats?q=' + encodeURIComponent(words)).then((r) => { if (alive && ask === asked) { found = r.chats; draw(); } }).catch(fail);
    }, 250);
  }
  api('/api/chats').then((r) => { all = r.chats; limit = r.limit || limit; draw(); }).catch(fail);
  q.addEventListener('input', search);
  store.setTop(null, [h('a', { class: 'btn sm', href: '#/new' }, icon('plus'), 'New chat')]);
  view.append(h('div', { class: 'page narrow' },
    h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Your chats'))),
    h('div', { class: 'row wrap' }, h('div', { class: 'search grow' }, icon('search'), q), seg),
    list, note));
  setTimeout(() => q.focus(), 50);
  return () => { alive = false; clearTimeout(searchT); };
}
