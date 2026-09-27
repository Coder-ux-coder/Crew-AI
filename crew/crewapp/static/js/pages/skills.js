// Skills: Anthropic's official skills (Word, Excel, PowerPoint, PDF, design, writing …), the ones built into
// Claude Code, and Crew's and your own ways of working — which the assistant and the team follow automatically.

import { h, icon, btn, api, toast, fail, ask, dialog, confirmBox, store, markdown, humanize, clear } from '../ui.js';

const ANTHROPIC = {
  docx: ['Word documents', 'Create and edit .docx files: letters, reports, briefs with tracked changes.', 'doc'],
  xlsx: ['Excel spreadsheets', 'Build spreadsheets with formulas, formatting and charts.', 'table'],
  pptx: ['PowerPoint presentations', 'Make slide decks with layouts, charts and speaker notes.', 'layers'],
  pdf: ['PDF files', 'Read, fill, merge and create PDFs.', 'doc'],
  'doc-coauthoring': ['Write documents together', 'Draft long documents with you, section by section.', 'feather'],
  'internal-comms': ['Internal communications', 'Memos, announcements, status updates in a clear house style.', 'chat'],
  'brand-guidelines': ['Brand guidelines', 'Apply colours, fonts and tone consistently.', 'sparkles'],
  'canvas-design': ['Posters and visuals', 'Design posters, social images and one-pagers.', 'image'],
  'frontend-design': ['Beautiful web pages', 'Distinctive, polished pages and interfaces.', 'globe'],
  'web-artifacts-builder': ['Interactive web apps', 'Richer single-page tools and dashboards.', 'apps'],
  'theme-factory': ['Themes', 'Ready-made colour and font themes for slides and pages.', 'sun'],
  'algorithmic-art': ['Generative art', 'Art made from code.', 'sparkles'],
  'slack-gif-creator': ['Animated GIFs', 'Small animations for messages.', 'video'],
  'skill-creator': ['Skill creator', 'Turns a way of working into a new skill.', 'plus'],
  'mcp-builder': ['Connection builder', 'Builds a new MCP connection to a service.', 'plug'],
  'webapp-testing': ['Web app testing', 'Checks a web app works by using it in a browser.', 'check'],
};
const BUILT_IN = {
  dataviz: 'Charts and data visualisation', 'code-review': 'Review code for problems', 'security-review': 'Security review of code', simplify: 'Simplify code',
  loop: 'Repeat a task on a schedule', schedule: 'Schedule a task', 'claude-api': 'Build with the Claude API', init: 'Describe a folder for Claude',
  review: 'Review changes', 'update-config': 'Change Claude Code settings', run: 'Run a project', 'keybindings-help': 'Keyboard shortcuts',
};

export function skillsPage(view) {
  const anth = h('div', { class: 'grid-3' });
  const installBox = h('div');
  const builtin = h('div', { class: 'chiplist' });
  const mine = h('div', { class: 'grid-2' });
  store.setTop(null, [btn('Create a skill', () => create(), { cls: 'sm accent', ic: 'plus' })]);
  view.append(h('div', { class: 'page' },
    h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Skills'),
      h('p', null, 'Proven ways of working that Claude and the team use automatically when a task calls for them — like making a Word document or an Excel sheet.'))),
    installBox,
    h('div', { class: 'section-title' }, icon('spark'), 'Anthropic’s skills'), anth,
    h('div', { class: 'section-title' }, icon('terminal'), 'Built into Claude Code'), builtin,
    h('div', { class: 'row' }, h('div', { class: 'section-title grow', style: { margin: 0 } }, icon('sparkles'), 'Crew’s skills and yours'),
      btn('Help me write one', () => {
        store.pendingProduct = 'claude';
        location.hash = '#/new';
        setTimeout(() => { const ta = document.querySelector('.composer textarea'); if (ta) { ta.value = 'Help me create a new skill for Crew. Ask me what it should do and when it applies, then write it as clear, numbered steps.'; ta.dispatchEvent(new Event('input')); } }, 120);
      }, { cls: 'sm ghost', ic: 'chat' })),
    mine));

  async function loadClaude() {
    let info = {};
    let state = { state: 'idle' };
    try { info = (await api('/api/claude')).info || {}; } catch (e) { /* offline */ }
    try { state = await api('/api/skills/anthropic'); } catch (e) { /* ignore */ }
    const have = new Set(info.skills || []);
    const installed = ['docx', 'xlsx', 'pptx', 'pdf'].some((s) => have.has(s));
    clear(anth, ...Object.entries(ANTHROPIC).map(([id, [name, text, ic]]) => h('div', { class: 'skill-card' },
      h('div', { class: 'row' }, h('span', { class: 's-ico' }, icon(ic)), h('h3', null, name),
        have.has(id) ? h('span', { class: 'pill ok' }, 'Ready') : h('span', { class: 'pill outline' }, 'Not added')),
      h('p', null, text), h('div', { class: 'muted small mono' }, '/' + id))));
    const extra = [...have].filter((s) => !ANTHROPIC[s]);
    clear(builtin, ...(extra.length ? extra.map((s) => h('span', { class: 'pill', title: BUILT_IN[s] || '' }, '/' + s, BUILT_IN[s] ? h('span', { class: 'muted', style: { fontWeight: 400 } }, ' — ' + BUILT_IN[s]) : null))
      : [h('span', { class: 'muted small' }, info.skills ? 'None found.' : 'These appear after your first chat with Claude.')]));
    const running = state.state === 'running';
    clear(installBox, ...(installed && state.state !== 'error' ? [] : [h('div', { class: 'card soft row wrap' }, icon(running ? 'reload' : 'download'),
      h('div', { class: 'grow' }, h('b', null, running ? 'Adding Anthropic’s skills…' : 'Add Anthropic’s official skills'),
        h('div', { class: 'muted small' }, state.state === 'error' ? state.message : running ? 'This takes a minute. They are ready from your next chat.'
          : 'Word, Excel, PowerPoint, PDF, design and writing skills from Anthropic, for every Claude subscription you use.')),
      running ? h('span', { class: 'spinner' }) : btn('Add them', async () => {
        try { await api('/api/skills/anthropic', { method: 'POST', body: {} }); toast('Adding Anthropic’s skills…'); poll(); } catch (e) { fail(e); }
      }, { cls: 'sm primary', ic: 'download' }))]));
    return state;
  }

  let pollT = null;
  async function poll() {
    const st = await loadClaude();
    if (st.state === 'running') pollT = setTimeout(poll, 3000);
    else if (st.state === 'done') toast('Anthropic’s skills are added. They are ready from your next chat.', { ms: 7000 });
  }

  async function loadMine() {
    try {
      const r = await api('/api/skills');
      clear(mine, ...r.skills.map(card));
    } catch (e) { fail(e); }
  }

  function card(s) {
    const sw = h('input', {
      type: 'checkbox', checked: s.enabled, 'aria-label': 'Switched on', onchange: async (e) => {
        try { await api(`/api/skills/${s.id}/toggle`, { method: 'POST', body: { enabled: e.target.checked } }); toast(e.target.checked ? 'Skill switched on.' : 'Skill switched off.'); } catch (x) { fail(x); e.target.checked = !e.target.checked; }
      },
    });
    return h('div', { class: 'skill-card' },
      h('div', { class: 'row' }, h('span', { class: 's-ico' }, icon('sparkles')), h('h3', null, humanize(s.name)), h('label', { class: 'switch', title: 'Switch on or off' }, sw, h('i'))),
      h('p', null, s.summary || s.description.replace(/^Use when /i, 'Used when ')),
      h('div', { class: 'row wrap' }, h('span', { class: 'pill' + (s.origin === 'built-in' ? '' : ' accent') }, s.origin === 'built-in' ? 'Comes with Crew' : 'Made by you or your teams'),
        h('span', { class: 'grow' }),
        btn('View', () => viewSkill(s.id), { cls: 'sm ghost' }),
        s.origin !== 'built-in' ? btn('Delete', async () => {
          if (!(await confirmBox('Delete this skill?', 'Claude and the team will no longer use it.', { ok: 'Delete', danger: true }))) return;
          try { await api('/api/skills/' + s.id, { method: 'DELETE' }); loadMine(); } catch (e) { fail(e); }
        }, { cls: 'sm ghost danger' }) : null));
  }

  async function viewSkill(id) {
    try {
      const s = await api('/api/skills/' + id);
      dialog({ title: humanize(s.name), wide: true, body: h('div', { class: 'stack' }, h('p', { class: 'muted' }, s.description), h('div', { class: 'md answer', html: markdown(s.body) })) });
    } catch (e) { fail(e); }
  }

  async function create() {
    const v = await ask('Create a skill', [
      { name: 'name', label: 'Name', placeholder: 'e.g. Formal letter format', required: true },
      { name: 'when', label: 'When should it be used?', type: 'textarea', rows: 2, placeholder: 'e.g. writing any official letter or notification', required: true },
      { name: 'steps', label: 'What should be done — in plain words', type: 'textarea', rows: 8, placeholder: '1. Use the department letterhead…\n2. Reference number and date at the top…\n3. …', required: true },
    ], { ok: 'Create skill', intro: 'A skill is a written procedure Claude and the team follow whenever it applies.' });
    if (!v) return;
    try { await api('/api/skills', { method: 'POST', body: v }); toast('Skill created and switched on.'); loadMine(); } catch (e) { fail(e); }
  }

  poll();
  loadMine();
  return () => clearTimeout(pollT);
}
