// Settings: the few choices that matter up front, in plain words; everything technical under "Advanced".
// Changes save immediately.

import { h, icon, btn, api, toast, fail, ask, confirmBox, store, bus, humanize, clear } from '../ui.js';
import { speech, canSpeak, canDictate } from '../voice.js';
import { modelOptions, efforts, EFFORT_HINTS, TEAM_MODES } from './chat.js';
import qrcode from '../../vendor/qrcode.mjs';

const SECTIONS = [
  ['general', 'General', 'sun'],
  ['subscriptions', 'Subscriptions', 'key'],
  ['models', 'Models & effort', 'brain'],
  ['team', 'The team', 'users'],
  ['instructions', 'Instructions', 'book'],
  ['voice', 'Voice', 'mic'],
  ['phone', 'Use on your phone', 'phone'],
  ['lessons', 'Lessons learned', 'bulb'],
  ['updates', 'Updates & check-up', 'cloud'],
];

const LANGS = [
  ['en-US', 'English (United States)'], ['en-GB', 'English (United Kingdom)'], ['en-IN', 'English (India / Pakistan)'],
  ['ar-SA', 'العربية — Arabic'], ['hi-IN', 'Hindi'], ['pa-IN', 'Punjabi (Gurmukhi)'],
  ['zh-CN', 'Chinese (Mandarin)'], ['tr-TR', 'Turkish'], ['fr-FR', 'French'], ['de-DE', 'German'], ['es-ES', 'Spanish'],
];

// ------------------------------------------------------------------ small controls

function row(label, hint, control) {
  return h('div', { class: 'setting' }, h('div', { class: 'l' }, h('b', null, label), hint ? h('small', null, hint) : null), control);
}

function seg(options, value, onchange) {
  const btns = options.map(([v, label]) => h('button', {
    type: 'button', class: v === value ? 'on' : '', onclick: (e) => { btns.forEach((b) => b.classList.toggle('on', b === e.currentTarget)); onchange(v); },
  }, label));
  return h('div', { class: 'seg' }, btns);
}

function toggle(checked, onchange, label = '') {
  return h('label', { class: 'switch', title: label || null }, h('input', { type: 'checkbox', checked, 'aria-label': label || 'On or off', onchange: (e) => onchange(e.target.checked) }), h('i'));
}

function select(options, value, onchange) {
  // options: [value, label] pairs, or { group, options } for labelled groups (e.g. ChatGPT and Claude models)
  const opt = ([v, label]) => h('option', { value: v, selected: v === value }, label);
  return h('select', { onchange: (e) => onchange(e.target.value) }, options.map((o) => (Array.isArray(o) ? opt(o)
    : h('optgroup', { label: o.group }, o.options.map(opt)))));
}

function number(value, { min = 0, max = 1000, step = 1 } = {}, onchange) {
  let t = null;
  return h('input', {
    type: 'number', value: String(value), min, max, step, style: { width: '110px' },
    oninput: (e) => {
      clearTimeout(t);
      t = setTimeout(() => {
        const v = parseFloat(e.target.value);
        if (!Number.isNaN(v) && v >= min && v <= max) onchange(v);
        else if (e.target.value !== '') toast(`Choose a number from ${min} to ${max}. This one is not saved.`, { bad: true });
      }, 600);
    },
  });
}

function advanced(...kids) {
  return h('details', { class: 'advanced' }, h('summary', null, icon('chev'), 'Advanced'), ...kids);
}

async function save(partial, quiet = false) {
  try {
    const s = await api('/api/settings', { method: 'PUT', body: partial });
    if (store.overview) Object.assign(store.overview, { models: s.models, team: s.team, app: s.app, accounts: s.accounts });
    store.settings = { ...store.settings, ...s };
    bus.emit('settings', s);
    if (!quiet) toast('Saved.');
    return s;
  } catch (e) { fail(e); throw e; }
}

export function applyLook(app) {
  const root = document.documentElement;
  if (!app.theme || app.theme === 'system') delete root.dataset.theme; else root.dataset.theme = app.theme;
  if (!app.accent || app.accent === 'clay') delete root.dataset.accent; else root.dataset.accent = app.accent;
  if (app.font === 'sans') root.dataset.font = 'sans'; else delete root.dataset.font;
  try {
    localStorage.setItem('crew.theme', app.theme || 'system');
    localStorage.setItem('crew.accent', app.accent || 'clay');
    localStorage.setItem('crew.font', app.font || 'serif');
  } catch (e) { /* private mode */ }
  const meta = document.querySelector('meta[name=theme-color]');
  if (meta) meta.content = getComputedStyle(root).getPropertyValue('--bg').trim() || '#FAF9F5';
}

// ------------------------------------------------------------------ page

export function settingsPage(view, params) {
  const section = SECTIONS.some((s) => s[0] === params[0]) ? params[0] : 'general';
  const body = h('div', { class: 'stack', style: { gap: '16px' } }, h('div', { class: 'muted' }, 'Loading…'));
  const nav = h('nav', { class: 'subnav' }, SECTIONS.map(([k, label, ic]) => h('a', { href: '#/settings/' + k, class: k === section ? 'on' : '' }, icon(ic), label)));
  store.setTop(null, []);
  view.append(h('div', { class: 'page' }, h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Settings'))), h('div', { class: 'settings' }, nav, body)));
  let alive = true;
  api('/api/settings').then((s) => {
    if (!alive) return;
    store.settings = s;
    clear(body, ...[].concat(RENDER[section](s)));
  }).catch(fail);
  return () => { alive = false; };
}

const card = (title, intro, ...kids) => h('section', { class: 'card set-card' }, h('h2', null, title), intro ? h('p', { class: 'muted' }, intro) : null, ...kids);

const RENDER = {
  general(s) {
    const app = s.app;
    const accents = [['clay', '#C6613F', 'Clay'], ['blue', '#3767C4', 'Blue'], ['green', '#2E7D5B', 'Green'], ['plum', '#8A4BB0', 'Plum']];
    const sw = h('div', { class: 'swatches' }, accents.map(([k, c, label]) => h('button', {
      type: 'button', class: (app.accent || 'clay') === k ? 'on' : '', style: { background: c }, title: label, 'aria-label': label,
      onclick: async (e) => {
        sw.querySelectorAll('button').forEach((b) => b.classList.toggle('on', b === e.currentTarget));
        app.accent = k; applyLook(app); await save({ app: { accent: k } }, true);
      },
    })));
    const name = h('input', { type: 'text', value: app.owner_name || '', placeholder: 'Your name', style: { width: '220px' } });
    name.addEventListener('change', async () => { await save({ app: { owner_name: name.value.trim() } }); bus.emit('chats'); });
    const p = s.problem;
    const recent = p && p.at && Date.now() / 1000 - p.at < 30 * 86400;
    return [
      recent ? card('Your settings file', '',
        h('p', { class: 'muted', style: { margin: 0 } }, `On ${new Date(p.at * 1000).toLocaleString()} Crew could not read its settings file (${p.error}). `
          + `So that Crew still opens, it is using ${p.restored}; the file as it was is kept as ${p.kept} in Crew’s folder, `
          + 'where you can compare the two. Settings you change here are saved as usual.')) : null,
      card('You', '', row('Your name', 'Used to greet you', name)),
      card('Look', '',
        row('Theme', 'Light, dark, or follow Windows', seg([['system', 'Automatic'], ['light', 'Light'], ['dark', 'Dark']], app.theme || 'system',
          async (v) => { app.theme = v; applyLook(app); await save({ app: { theme: v } }, true); })),
        row('Colour', 'For buttons and highlights', sw),
        row('Answers in', 'Claude’s answers are set in a book face by default', seg([['serif', 'Book (serif)'], ['sans', 'Plain (sans)']], app.font || 'serif',
          async (v) => { app.font = v; applyLook(app); await save({ app: { font: v } }, true); }))),
      store.isLocal ? card('This computer', '',
        row('Start Crew with Windows', 'Keeps Crew ready — and scheduled workflows running — without opening it yourself', toggle(!!app.start_with_windows, async (v) => {
          try { const r = await api('/api/startup', { method: 'POST', body: { enabled: v } }); toast(r.message); app.start_with_windows = v; } catch (e) { fail(e); }
        })),
        row('Microphone and paste without asking', 'Edge and Chrome allow them for Crew’s own window only; pages the team builds still have to ask. The browser’s settings then say it is “managed”.', toggle(app.allow_devices !== false, async (v) => {
          try { const r = await api('/api/devices', { method: 'POST', body: { enabled: v } }); toast(r.message); app.allow_devices = v; } catch (e) { fail(e); }
        })),
        row('Crew’s folder', 'Your chats, projects, captures, settings and keys live here', btn('Open', () => api('/api/open-home', { method: 'POST', body: {} }).catch(fail), { ic: 'folder', cls: 'sm' }))) : null,
    ].filter(Boolean);
  },

  subscriptions(s) {
    const list = h('div');
    const vendorName = (v) => (v === 'claude' ? 'Claude (Pro or Max)' : 'ChatGPT (Plus or Pro), through Codex');
    async function load(refresh = false) {
      clear(list, h('div', { class: 'muted', style: { padding: '10px 0' } }, 'Checking your subscriptions…'));
      try {
        const r = await api('/api/accounts/status' + (refresh ? '?refresh=1' : ''));
        clear(list, ...r.accounts.map((a) => h('div', { class: 'acct' },
          h('span', { class: 'logo-b ' + a.vendor }, icon(a.vendor === 'claude' ? 'spark' : 'gpt')),
          h('div', { class: 'grow' }, h('b', null, a.name), h('div', { class: 'muted small' }, vendorName(a.vendor) + (a.detail ? ' · ' + a.detail : ''))),
          h('span', { class: 'pill ' + (a.signed_in ? 'ok' : a.signed_in === false ? 'bad' : '') }, a.signed_in ? 'Signed in' : a.signed_in === false ? 'Not signed in' : 'Unknown'),
          store.isLocal ? btn(a.signed_in ? 'Sign in again' : 'Sign in', async () => {
            try { const m = await api(`/api/accounts/${encodeURIComponent(a.name)}/login`, { method: 'POST', body: {} }); toast(m.message, { ms: 9000 }); } catch (e) { fail(e); }
          }, { cls: 'sm' + (a.signed_in ? ' ghost' : ' accent') }) : null,
          btn('', async () => {
            if (!(await confirmBox(`Remove ${a.name}?`, 'Crew stops using this subscription. Its sign-in stays on this computer.', { ok: 'Remove', danger: true }))) return;
            const rest = s.accounts.filter((x) => x.name !== a.name);
            if (!rest.some((x) => x.vendor === 'claude')) { toast('At least one Claude subscription is needed: the team lead runs on Claude.', { bad: true }); return; }
            try { const n = await save({ accounts: rest }); s.accounts = n.accounts; load(); } catch (e) { /* shown */ }
          }, { cls: 'sm icon ghost', ic: 'trash', title: 'Remove' }))));
      } catch (e) { fail(e); }
    }
    async function add() {
      const v = await ask('Add a subscription', [
        { name: 'vendor', label: 'Which service', type: 'select', value: 'claude', options: [{ value: 'claude', label: 'Claude (Pro or Max)' }, { value: 'codex', label: 'ChatGPT (Plus or Pro) — through Codex' }] },
        { name: 'name', label: 'A short name for it', placeholder: 'e.g. claude-2, work-max, chatgpt', required: true },
      ], { ok: 'Add', intro: 'Each subscription signs in once, separately. Crew spreads the work across all of them and moves on when one reaches its limit.' });
      if (!v) return;
      const name = v.name.toLowerCase().replace(/[^a-z0-9._-]+/g, '-');
      if (s.accounts.some((a) => a.name === name)) { toast('That name is already used.', { bad: true }); return; }
      try {
        const n = await save({ accounts: [...s.accounts, { name, vendor: v.vendor, profile: '' }] });
        s.accounts = n.accounts;
        await load();
        if (store.isLocal) {
          const m = await api(`/api/accounts/${encodeURIComponent(name)}/login`, { method: 'POST', body: {} });
          toast(m.message, { ms: 9000 });
        }
      } catch (e) { /* shown */ }
    }
    load();
    return [card('Subscriptions', 'Your Claude and ChatGPT subscriptions. Work is shared across them, and when one reaches its limit Crew carries on with another.',
      list,
      h('div', { class: 'row wrap', style: { marginTop: '10px' } }, btn('Add a subscription', add, { cls: 'accent', ic: 'plus' }), btn('Check again', () => load(true), { ic: 'reload' })),
      h('p', { class: 'muted small', style: { margin: '10px 0 0' } }, 'Use only your own subscriptions, for your own work, and never share a sign-in.'))];
  },

  models(s) {
    const m = s.models, app = s.app;
    const claudeModels = modelOptions('claude').map((o) => [o.value, o.label]);
    const effortOpts = (p) => efforts(p).map((e) => [e, `${e} — ${(EFFORT_HINTS[p] || {})[e] || ''}`]);
    const claudeAccounts = s.accounts.filter((a) => a.vendor === 'claude');
    const chips = (list, key, warn) => {
      const box = h('div', { class: 'chiplist' });
      const draw = () => clear(box, ...list.map((x) => h('span', { class: 'pill' }, x, h('button', {
        type: 'button', title: 'Remove', 'aria-label': 'Remove ' + x, onclick: async () => {
          if (warn && !(await warn(x))) return;
          const at = list.indexOf(x);
          list.splice(at, 1); draw();
          try { await save({ models: { [key]: list } }); } catch (e) { list.splice(at, 0, x); draw(); }  // refused: shown as it still is
        },
      }, '×'))), h('button', {
        class: 'btn sm ghost', type: 'button', onclick: async () => {
          const v = await ask(key === 'allowed' ? 'Allow a model' : 'Ban a word', [{ name: 'v', label: key === 'allowed' ? 'Model name' : 'Word in the model name', required: true, placeholder: key === 'allowed' ? 'claude-…' : 'e.g. haiku' }], { ok: 'Add' });
          if (!v || list.includes(v.v)) return;
          list.push(v.v); draw();
          try { await save({ models: { [key]: list } }); } catch (e) { list.pop(); draw(); }
        },
      }, icon('plus'), 'Add'));
      draw();
      return box;
    };
    const codexModels = modelOptions('codex').map((o) => [o.value, o.label]);
    const withCurrent = (opts, v) => (v && !opts.some(([x]) => x === v) ? [...opts, [v, v]] : opts);
    const isGpt = (id) => /^(gpt|o\d|codex)/i.test(id || '');
    const ceoEfforts = efforts(isGpt(m.ceo) ? 'codex' : 'claude').filter((e) => e !== 'auto')
      .map((e) => [e, `${e} — ${(EFFORT_HINTS[isGpt(m.ceo) ? 'codex' : 'claude'] || {})[e] || ''}`]);
    const tierRow = (tier, badge, name, hint, control) => h('div', { class: 'tier-row' },
      h('span', { class: 'tier-ico t-' + tier }, badge), h('div', null, h('b', null, name), h('small', null, hint)), control);
    return [
      card('Claude', 'The defaults for new chats with Claude. You can change them in any chat from the composer.',
        row('Model', '', select(claudeModels, app.chat_model, (v) => save({ app: { chat_model: v } }))),
        row('Effort', 'Anthropic’s own levels. auto lets Claude decide.', select(effortOpts('claude'), app.chat_effort || 'auto', (v) => save({ app: { chat_effort: v } }))),
        claudeAccounts.length > 1 ? row('Subscription', 'Which one chats use first (Crew switches when it runs low)', select([['', 'Whichever has the most room'], ...claudeAccounts.map((a) => [a.name, a.name])], app.chat_account || '', (v) => save({ app: { chat_account: v } }))) : null,
        row('Improve my messages automatically', 'Before a chat message is sent, the prompt writer turns your words (typed or spoken) into a clear, precise prompt — it adds a few seconds. You can also press ✦ in any message box to improve one message and check it first.',
          toggle(app.improve_prompts === true, (v) => save({ app: { improve_prompts: v } })))),
      card('ChatGPT', 'The defaults for new chats with ChatGPT (through Codex).',
        row('Model', '', select(withCurrent(codexModels, app.codex_model), app.codex_model || '', (v) => save({ app: { codex_model: v } }))),
        row('Effort', 'OpenAI’s own levels. auto lets ChatGPT decide.', select(effortOpts('codex'), app.codex_effort || 'auto', (v) => save({ app: { codex_effort: v } })))),
      card('The team’s three tiers', 'Routine work goes to the workhorse, anything that needs high intelligence to the manager, and the CEO checks rather than builds. Your targets for a project’s tokens: manager 60–70%, CEO about 5%, workhorse the rest. Each project shows how close it came.',
        h('div', { class: 'tier-card' },
          tierRow('workhorse', 'W', 'Workhorse', 'Most tasks by count: research, text and styling changes, small design tweaks, repetitive edits, docs. Runs on your Claude subscriptions, alongside the managers.',
            select(withCurrent(claudeModels, m.workhorse), m.workhorse, (v) => save({ models: { workhorse: v } }))),
          tierRow('manager', 'M', 'Manager', 'Plans the work, checks every workhorse task, and builds what needs high intelligence: security, design plans, shared foundations, hard problems.',
            select(claudeModels, m.work, (v) => save({ models: { work: v } }))),
          tierRow('ceo', 'C', 'CEO', 'Used sparingly: reviews the plan (each task’s tier and effort) and gives the final approval. If its model cannot run, the backup below takes over.',
            select([{ group: 'ChatGPT (OpenAI)', options: codexModels }, { group: 'Claude (Anthropic)', options: claudeModels },
              ...(m.ceo && ![...codexModels, ...claudeModels].some(([x]) => x === m.ceo) ? [[m.ceo, m.ceo]] : [])], m.ceo, (v) => save({ models: { ceo: v } })))),
        advanced(
          row('CEO backup', 'Runs when the CEO’s model cannot (no ChatGPT subscription, a usage limit, an error)', select(claudeModels, m.ceo_backup, (v) => save({ models: { ceo_backup: v } }))),
          row('Workhorse agents in a team', 'How many work at the same time, spread over your Claude subscriptions', select([['1', '1'], ['2', '2 (recommended)'], ['3', '3'], ['4', '4']], String(s.team.workhorse_seats || 2), (v) => save({ team: { workhorse_seats: Number(v) } }))),
          row('CEO’s effort', 'Recommended: max', select(withCurrent(ceoEfforts, m.effort_ceo), m.effort_ceo, (v) => save({ models: { effort_ceo: v } }))),
          row('Effort for building', 'auto (recommended): the CEO decides for each task of a team project; the manager decides for small jobs', select(effortOpts('claude'), m.effort_work, (v) => save({ models: { effort_work: v } }))),
          row('Effort for light jobs', 'Checking, notes, research. auto: decided per job', select(effortOpts('claude'), m.effort_light, (v) => save({ models: { effort_light: v } }))),
          row('Allowed Claude models', 'Only these may run', chips([...m.allowed], 'allowed')),
          row('Banned', 'Any model whose name contains one of these is refused everywhere', chips([...m.banned], 'banned', (x) => confirmBox(`Lift the ban on “${x}”?`, 'Your rule was to never use it. Lift the ban anyway?', { ok: 'Lift the ban', danger: true }))))),
    ];
  },

  team(s) {
    const t = s.team;
    return [
      card('How the team works', '',
        row('Who builds', TEAM_MODES.find((x) => x.value === t.mode)?.hint || '', select(TEAM_MODES.map((x) => [x.value, x.label]), t.mode, (v) => save({ team: { mode: v } }))),
        row('Final approval by the CEO', 'One last look at the whole result before it is handed over', toggle(t.ceo_reviews, (v) => save({ team: { ceo_reviews: v } }))),
        row('Head-to-head comparisons', 'The workhorse and a manager each build the same part; a manager compares the two versions without knowing which is which. Fills the model scorecard; uses more tokens.',
          select([['off', 'Off'], ['some', 'A few parts per project'], ['all', 'Every part that can be compared']], t.head_to_head || 'off', (v) => save({ team: { head_to_head: v } }))),
        row('When both versions are built', 'Combine: the better version also takes in what the other did better (one joint result). Compete: the better one is kept as it is.',
          select([['combine', 'Combine both'], ['compete', 'Keep the better']], t.head_to_head_style || 'combine', (v) => save({ team: { head_to_head_style: v } }))),
        row('Prompt writer', 'Before the team reads your messages, a writer turns your words (typed or spoken) into a clear instruction. Your own words stay attached, so nothing is lost.',
          toggle(t.prompt_writer !== false, (v) => save({ team: { prompt_writer: v } }))),
        row('When the work is finished', '', select([['merge', 'Put it in the project folder'], ['branch', 'Keep it as a separate version for me to check'], ['push', 'Put it in the folder and upload it online']], t.deliver, (v) => save({ team: { deliver: v } }))),
        row('Let agents act without asking', 'On: fully automatic. Off: a safety check approves each action.',
          toggle(t.permission_mode === 'bypassPermissions', (v) => save({ team: { permission_mode: v ? 'bypassPermissions' : 'auto' } }))),
        advanced(
          row('Checking each piece', 'Every piece is checked by someone who did not build it',
            select([['cross', 'The manager checks every piece (recommended)'], ['off', 'No checks (not recommended)']], t.review === 'off' ? 'off' : 'cross', (v) => save({ team: { review: v } }))),
          row('Default time limit (hours)', '0 = no limit (you can still set a timer per project)', number(t.max_hours, { min: 0, max: 168, step: 0.5 }, (v) => save({ team: { max_hours: v } }))),
          row('Spending limit (US$)', '0 = no limit (subscriptions are flat-rate)', number(t.max_cost_usd, { min: 0, max: 10000, step: 1 }, (v) => save({ team: { max_cost_usd: v } }))),
          row('Nudge a quiet member after (minutes)', '', number(t.stall_minutes, { min: 2, max: 60 }, (v) => save({ team: { stall_minutes: v } }))),
          row('Progress review every (minutes)', 'The lead re-plans when progress stalls', number(t.ledger_minutes, { min: 3, max: 120 }, (v) => save({ team: { ledger_minutes: v } }))),
          row('Team-chat messages per member per step', '', number(t.chat_budget, { min: 2, max: 50 }, (v) => save({ team: { chat_budget: Math.round(v) } }))),
          row('Rounds of corrections before the lead decides', '', number(t.max_review_rounds, { min: 1, max: 10 }, (v) => save({ team: { max_review_rounds: Math.round(v) } }))),
          row('Time allowed for automatic tests (minutes)', '', number(t.checks_timeout_minutes, { min: 1, max: 120 }, (v) => save({ team: { checks_timeout_minutes: v } }))))),
    ];
  },

  instructions(s) {
    const editor = (value, path, label) => {
      const ta = h('textarea', { class: 'editor', rows: 14, 'aria-label': label }, value);
      const saveBtn = btn('Save', async () => {
        try { await api(path, { method: 'PUT', body: { text: ta.value } }); toast('Saved. New work follows these instructions.'); saveBtn.disabled = true; } catch (e) { fail(e); }
      }, { cls: 'primary sm', disabled: true });
      ta.addEventListener('input', () => { saveBtn.disabled = false; });
      return [ta, h('div', { class: 'row end', style: { marginTop: '8px' } }, saveBtn)];
    };
    return [
      card('Instructions for Claude and ChatGPT', 'How chats should behave: tone, format, what to ask before acting.', ...editor(s.assistant, '/api/assistant-instructions', 'Chat instructions')),
      card('Team rules', 'Standing instructions every team member follows on every project. Write them like a memo to your staff.', ...editor(s.rules, '/api/rules', 'Team rules')),
    ];
  },

  voice(s) {
    const app = s.app;
    const langs = !app.dictation_lang || LANGS.some(([v]) => v === app.dictation_lang) ? LANGS
      : [...LANGS, [app.dictation_lang, app.dictation_lang]];  // a language chosen earlier that is no longer listed
    const voiceSel = h('select', { onchange: (e) => save({ app: { voice_name: e.target.value } }) });
    const fillVoices = () => {
      const voices = speech.voices().slice().sort((a, b) => a.lang.localeCompare(b.lang) || a.name.localeCompare(b.name));
      clear(voiceSel, h('option', { value: '' }, 'Automatic'), ...voices.map((v) => h('option', { value: v.name, selected: v.name === app.voice_name }, `${v.name} — ${v.lang}`)));
    };
    fillVoices();
    document.addEventListener('crew:voices', fillVoices, { once: true });
    const rate = h('input', { type: 'range', min: 0.6, max: 1.8, step: 0.1, value: String(app.voice_rate || 1) });
    const rateLabel = h('span', { class: 'muted small', style: { width: '40px' } }, `${Number(app.voice_rate || 1).toFixed(1)}×`);
    let rt = null;
    rate.addEventListener('input', () => {
      rateLabel.textContent = `${Number(rate.value).toFixed(1)}×`;
      if (store.overview) store.overview.app.voice_rate = Number(rate.value);
      clearTimeout(rt);
      rt = setTimeout(() => save({ app: { voice_rate: Number(rate.value) } }, true), 500);
    });
    return [
      card('Speaking to Crew', canDictate ? 'Press the microphone to type with your voice, or the sound-wave button for a spoken conversation.' : 'Voice typing needs Microsoft Edge or Google Chrome.',
        row('Language you speak', '', select(langs, app.dictation_lang, (v) => save({ app: { dictation_lang: v } })))),
      card('Crew speaking to you', canSpeak ? 'Answers can be read aloud. Microsoft Edge has the most natural voices.' : 'Reading aloud needs Microsoft Edge or Google Chrome.',
        row('Voice', '', voiceSel),
        row('Speed', '', h('div', { class: 'row' }, rate, rateLabel)),
        row('Read every answer aloud', 'Otherwise press the speaker button under an answer', toggle(app.auto_read, (v) => save({ app: { auto_read: v } }))),
        row('Try it', '', btn('Play a sample', () => speech.speak('Hello. This is how I will sound when I read my answers to you.'), { ic: 'volume', cls: 'sm' }))),
    ];
  },

  phone() {
    if (!store.isLocal) {
      return [card('Use on your phone', 'You are already using Crew on this device. To pair another phone or sign phones out, open Settings on the computer running Crew.')];
    }
    const out = h('div', { class: 'stack' });
    const draw = (p) => {
      const sw = toggle(p.enabled, async (v) => {
        try { const r = await api('/api/phone-access', { method: 'POST', body: { enabled: v } }); toast(r.message, { ms: 8000 }); draw(r); } catch (e) { fail(e); }
      }, 'Phone access');
      const codes = h('div', { class: 'stack' });
      if (p.enabled && p.urls.length) {
        for (const url of p.urls) {
          const qr = qrcode(0, 'M');
          qr.addData(url);
          qr.make();
          codes.append(h('div', { class: 'qr-wrap' },
            h('div', { class: 'qr', html: qr.createSvgTag({ cellSize: 5, margin: 2, scalable: false }) }),
            h('ol', { class: 'connect-steps' },
              h('li', null, h('div', null, 'Make sure the phone is on the same Wi-Fi as this computer.')),
              h('li', null, h('div', null, 'Open the phone’s camera and point it at this code. Tap the link that appears.')),
              h('li', null, h('div', null, 'In Chrome, tap ⋮ then “Add to Home screen” for a Crew icon.')))));
        }
      } else if (p.enabled) {
        codes.append(h('p', { class: 'muted' }, 'This computer is not connected to a network.'));
      }
      clear(out,
        row('Allow my phone to open Crew', 'Only phones you pair with the code below can get in', sw),
        codes,
        p.enabled ? row('Sign out all paired phones', 'Makes a new code; phones must scan again', btn('Sign out phones', async () => {
          if (!(await confirmBox('Sign out all phones?', 'Every paired phone will need to scan the new code.', { ok: 'Sign out', danger: true }))) return;
          try { draw(await api('/api/phone-access/new-code', { method: 'POST', body: {} })); toast('Done. Scan the new code to pair again.'); } catch (e) { fail(e); }
        }, { cls: 'sm danger' })) : null);
    };
    api('/api/pair').then(draw).catch(fail);
    return [
      card('Use Crew on your phone', 'Chat, start projects and follow them from your Samsung. Crew keeps running on this computer; the phone is a remote screen for it.', out),
      card('Voice on the phone', 'The phone’s browser only allows the microphone on secure connections. Two easy ways:',
        h('ol', { class: 'connect-steps' },
          h('li', null, h('div', null, h('b', null, 'At your desk: '), 'connect the phone on the Phone page. Crew then also opens on the phone at ', h('span', { class: 'mono' }, 'localhost:' + (location.port || '8765')), ', where the microphone works.')),
          h('li', null, h('div', null, h('b', null, 'Anywhere: '), 'install Tailscale (free) on both the computer and the phone. It gives Crew a private, secure address that works away from home too.')))),
    ];
  },

  lessons() {
    const team = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
    const ceo = h('div', null, h('p', { class: 'muted' }, 'Loading…'));
    const record = h('div');
    api('/api/lessons').then((r) => {
      clear(team, ...(r.lessons.length ? r.lessons.map((l) => h('div', { class: 'lesson' },
        h('div', { class: 'row' }, h('span', { class: 'pill' }, humanize(l.category)), l.weight > 1 ? h('span', { class: 'muted small' }, `confirmed ${l.weight}×`) : null),
        h('div', null, l.text))) : [h('p', { class: 'muted' }, 'No lessons yet. They are written after each project.')]));
      clear(ceo, ...(r.ceo.length ? r.ceo.map((l) => h('div', { class: 'lesson' }, h('div', null, l.text),
        l.weight > 1 ? h('span', { class: 'muted small' }, `confirmed ${l.weight}×`) : null)) : [h('p', { class: 'muted' }, 'The CEO writes its first effort lessons after a few projects.')]));
      const rows = r.effort_record || [];
      clear(record, ...(rows.length ? [h('table', { class: 'plain', style: { marginTop: '10px' } },
        h('thead', null, h('tr', null, h('th', null, 'Built by'), h('th', null, 'Kind of job'), h('th', null, 'Size'), h('th', null, 'Effort'), h('th', null, 'Jobs'), h('th', null, 'Passed first check'),
          h('th', null, 'Typical time'), h('th', null, 'Typical tokens'))),
        h('tbody', null, rows.map((x) => h('tr', null, h('td', null, x.tier === 'workhorse' ? 'Workhorse' : 'Manager'), h('td', null, humanize(x.kind)), h('td', null, x.size), h('td', null, x.effort), h('td', null, String(x.n)),
          h('td', null, `${Math.round(100 * (x.first_pass || 0))}%`), h('td', null, `${Math.round(x.minutes || 0)} min`),
          h('td', null, x.tokens ? Math.round(x.tokens).toLocaleString() : '—')))))] : []));
    }).catch(fail);
    return [
      card('What the team has learned', 'After every project the team writes down what worked and what did not. Future teams read the most useful lessons before they start.', team),
      card('What the CEO has learned about effort', 'The CEO keeps its own notes: which effort level suits which kind of job, judged by how often the work passed its first check and how many tokens it took.', ceo, record),
    ];
  },

  updates(s) {
    const crew = h('div', { class: 'stack' }, h('p', { class: 'muted' }, 'Checking…'));
    const claude = h('div', { class: 'stack' });
    const health = h('div', { class: 'health' }, h('p', { class: 'muted' }, 'Checking…'));
    const meta = h('p', { class: 'muted small', style: { marginTop: '10px' } });
    const drawCrew = (u) => {
      clear(crew, 
        h('div', { class: 'row wrap' }, h('div', { class: 'grow' }, h('b', null, `Version ${u.current || ''}`),
          h('div', { class: 'muted small' }, u.available ? `Version ${u.latest} is ready.` : u.error ? u.error : u.latest ? 'You have the newest version.' : 'Not checked yet.')),
        store.isLocal && u.available ? btn('Update now', () => store.installUpdate(u), { cls: 'accent sm', ic: 'download' }) : null,
        btn('Check now', async (e) => {
          const b = e.currentTarget;
          b.disabled = true;
          try { drawCrew(await api('/api/update?refresh=1')); } catch (x) { fail(x); b.disabled = false; }  // it can be tried again
        }, { cls: 'sm', ic: 'reload' })),
        u.available && (u.notes || []).length ? h('ul', { class: 'small', style: { margin: 0, paddingLeft: '20px' } }, u.notes.map((n) => h('li', null, n))) : null,
        h('p', { class: 'muted small' }, 'Updates replace only Crew’s program. Your chats, projects, captures, sign-ins, API keys and settings stay exactly as they are — no reinstalling, nothing to enter again.'),
        row('Update automatically', 'When a new version is ready, Crew updates itself at a quiet moment: nothing running, and you have not used it for half an hour. The window reconnects by itself.',
          toggle(s.app.auto_update !== false, (v) => save({ app: { auto_update: v } }))));
    };
    api('/api/update').then(drawCrew).catch(fail);
    api('/api/claude').then((c) => {
      clear(claude, h('div', { class: 'row wrap' }, h('div', { class: 'grow' }, h('b', null, `Version ${c.version || 'not found'}`),
        h('div', { class: 'muted small' }, 'Crew updates it by itself once a day, and at once if a model asks for a newer version.')),
      store.isLocal ? btn('Update now', async (e) => {
        const b = e.currentTarget;
        b.disabled = true;
        try { const r = await api('/api/claude/update', { method: 'POST', body: {} }); toast(r.message, { bad: !r.ok, ms: 8000 }); } catch (x) { fail(x); }
        b.disabled = false;
      }, { cls: 'sm', ic: 'download' }) : null));
    }).catch(() => clear(claude, h('p', { class: 'muted' }, 'Could not check.')));
    api('/api/health').then((r) => {
      clear(health, ...r.items.map((i) => h('div', { class: 'row' },
        h('span', { class: 'dot ' + (i.ok ? 'ok' : i.optional ? 'warn' : 'bad') }), h('span', { class: 'grow' }, i.label),
        h('span', { class: 'pill ' + (i.ok ? 'ok' : i.optional ? 'warn' : 'bad') }, i.ok ? 'Ready' : i.optional ? 'Optional — not installed' : 'Missing'))));
      meta.textContent = `Python ${r.python} · Crew’s folder: ${r.home}`;
    }).catch(fail);
    return [
      card('Crew', '', crew),
      card('Claude Code', '', claude),
      card('Check-up', 'What Crew needs on this computer. Anything missing? Run the Crew installer again; it only adds what is missing and keeps everything you have.', health, meta),
    ];
  },
};
