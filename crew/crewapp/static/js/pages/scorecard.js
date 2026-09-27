// The model scorecard: how each model has done on the owner's own projects — how often its work passed the
// manager's first check, how long it took, how many tokens it used, head-to-head results — and what Crew does
// with that evidence (kinds of work it moves up to the manager automatically, and suggestions for the lead).

import { h, icon, api, fail, tokens, clear } from '../ui.js';

const KIND = { build: 'Building', fix: 'Fixing', test: 'Tests', docs: 'Documents', research: 'Research', verify: 'Checking', foundation: 'Foundations' };
const SIZE = { S: 'small', M: 'medium', L: 'large' };
const TIER = { workhorse: 'Workhorse', manager: 'Manager', ceo: 'CEO' };
const VERDICT = {
  strong: ['check', 'Strong'], fair: [null, 'Fair'], weak: ['info', 'Weak'], thin: [null, 'Too few to judge'],
};

const pctOf = (r) => (r == null ? '–' : `${Math.round(r * 100)}%`);
const mins = (m) => (m == null ? '–' : m < 1 ? 'under a minute' : `${Math.round(m)} min`);
const kindName = (kind, size) => `${KIND[kind] || kind} · ${SIZE[size] || size}`;

export function scorecardView(box) {
  clear(box, h('p', { class: 'muted' }, 'Loading the scorecard…'));
  api('/api/scorecard').then((d) => draw(box, d)).catch((e) => { clear(box); fail(e); });
}

function draw(box, d) {
  const models = d.models || [];
  const intro = h('p', { class: 'muted', style: { margin: '0 0 14px' } },
    `How often each model's work passed the manager's first check on your own projects, over the last ${d.window_days} days. `
    + 'Crew uses this record to decide who builds what.',
    d.records ? ` Based on ${d.records} ${d.records === 1 ? 'piece' : 'pieces'} of work from ${d.projects} ${d.projects === 1 ? 'project' : 'projects'}.` : '');
  if (!models.length) {
    clear(box, intro, h('div', { class: 'card empty' },
      h('b', null, 'No record yet'),
      h('p', { class: 'muted' }, 'The scorecard fills in as the team finishes work: every piece is scored when it passes, or fails, the manager’s check. '
        + 'Turn on head-to-head comparisons (Settings → The team) to compare the models on the same work.')));
    return;
  }
  const th = d.thresholds || { evidence: 4, weak: 0.5, strong: 0.8 };

  // 1. one tile per model: the headline number and what it rests on
  const tiles = h('div', { class: 'score-tiles' }, models.map((m) => {
    const sizes = Object.entries(m.by_size || {}).map(([s, v]) => `${SIZE[s] || s} ${mins(v.minutes)}, ${tokens(v.tokens)} tokens`);
    const strong = (m.strong || []).map((k) => kindName(...k.split(' ')));
    const weak = (m.weak || []).map((k) => kindName(...k.split(' ')));
    return h('div', { class: `card score-tile t-${m.tier}` },
      h('div', { class: 'who' }, h('b', null, m.label), h('span', { class: `pill t-${m.tier}` }, TIER[m.tier] || m.tier)),
      h('div', { class: 'hero', title: `${m.passed} of ${m.n} passed the first check` }, pctOf(m.rate),
        h('small', null, m.n ? `passed the first check · ${m.passed} of ${m.n}` : 'no pieces built yet')),
      h('div', { class: 'facts' },
        sizes.length ? h('div', null, h('span', null, 'Typical: '), sizes.join(' · ')) : null,
        m.wins || m.losses ? h('div', null, h('span', null, 'Head-to-head: '), `won ${m.wins}, lost ${m.losses}`) : null,
        m.moved_up ? h('div', null, h('span', null, 'Moved up to the manager: '), `${m.moved_up} after two failed checks`) : null,
        strong.length ? h('div', null, h('span', null, 'Strong at: '), strong.join(', ')) : null,
        weak.length ? h('div', null, h('span', null, 'Weak at: '), weak.join(', ')) : null,
        m.n < th.evidence ? h('div', null, h('span', null, `Needs ${th.evidence} pieces of a kind before Crew acts on it.`)) : null));
  }));

  // 2. by kind of work: the same numbers side by side (a table, readable without colour)
  const verdict = (cell) => {
    const [ic, word] = VERDICT[cell.label] || VERDICT.thin;
    return h('span', { class: `verdict ${cell.label}` }, ic ? icon(ic) : null, word);
  };
  const table = h('table', { class: 'score-table' },
    h('thead', null, h('tr', null, h('th', null, 'Kind of work'),
      ...models.map((m) => h('th', null, h('span', { class: `th-model t-${m.tier}` }, m.label))))),
    h('tbody', null, (d.matrix || []).map((row) => h('tr', null,
      h('td', null, kindName(row.kind, row.size)),
      ...models.map((m) => {
        const c = row.cells[m.model];
        if (!c) return h('td', null, h('span', { class: 'muted' }, '–'));
        const tip = `${m.label} · ${kindName(row.kind, row.size)}: passed the first check ${c.passed} of ${c.n} times; `
          + `typically ${mins(c.minutes)} and ${tokens(c.tokens || 0)} tokens.`;
        return h('td', { title: tip }, h('span', { class: 'score-cell' + (row.best === m.model ? ' best' : ''), style: { '--tier-c': `var(--tier-${m.tier})` } },
          h('b', null, pctOf(c.rate)), h('small', null, `${c.n} ${c.n === 1 ? 'piece' : 'pieces'}`), verdict(c)));
      })))));

  // 3. what Crew does with it
  const rules = d.rules || { up: [], down: [] };
  const wh = models.find((m) => m.model === d.workhorse);
  const mg = models.find((m) => m.model === d.manager);
  const ruleRows = [
    ...rules.up.map((r) => h('div', { class: 'score-rule' }, icon('zap'), h('div', null,
      h('b', null, `${kindName(r.kind, r.size)} goes to the manager${mg ? ` (${mg.label})` : ''}, automatically`), h('small', null, `${r.why}.`)))),
    ...rules.down.map((r) => h('div', { class: 'score-rule' }, icon('bulb'), h('div', null,
      h('b', null, `${kindName(r.kind, r.size)} could go to the workhorse${wh ? ` (${wh.label})` : ''}`),
      h('small', null, `${r.why}. Suggested to the lead and the CEO; they decide.`)))),
  ];
  const rulesCard = h('div', { class: 'card' }, h('h2', null, 'What Crew does with this'),
    ruleRows.length ? h('div', null, ...ruleRows)
      : h('p', { class: 'muted' }, `Nothing yet. When the workhorse passes the first check on fewer than ${Math.round(th.weak * 100)}% of at least ${th.evidence} pieces of one kind, `
        + 'that kind of work moves up to the manager automatically. Where it does as well as the manager, Crew suggests giving it more.'),
    h('p', { class: 'muted small', style: { margin: '10px 0 0' } }, 'A routine task that fails its check twice also moves up to the manager at once, mid-project.'));

  // 4. head-to-head results
  const contests = d.contests || [];
  const contestCard = contests.length ? h('div', { class: 'card' }, h('h2', null, 'Head-to-head'),
    h('p', { class: 'muted small', style: { marginTop: 0 } }, 'Both models built the same part; the manager compared the two versions without knowing which was which, and the better one was kept.'),
    h('div', null, contests.map((c) => h('div', { class: 'score-rule' }, icon('users'), h('div', null,
      h('b', null, `${c.winner} won · ${kindName(c.kind, c.size)}`), h('small', null,
        `${c.task}${c.project ? ` (${c.project})` : ''} · ${c.both_passed ? 'both versions passed' : `${c.loser}'s did not pass`}${c.reason ? ` · ${c.reason}` : ''}`)))))) : null;

  clear(box, intro, tiles,
    h('div', { class: 'card', style: { marginTop: '14px' } }, h('h2', null, 'By kind of work'),
      h('p', { class: 'muted small', style: { marginTop: 0 } }, `Strong: ${Math.round(th.strong * 100)}% or more passed first time. Weak: under ${Math.round(th.weak * 100)}%. `
        + `Judged only once there are ${th.evidence} pieces. Underlined: the better model for that kind of work.`),
      h('div', { class: 'table-scroll' }, table)),
    h('div', { class: 'grid-2', style: { marginTop: '14px' } }, rulesCard, contestCard));
}
