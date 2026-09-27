// Connections: API keys for other services (Hunter, Google, OpenAI …) and connected services (MCP servers) that
// Claude, ChatGPT and the team may use. Keys stay on this computer and are hidden from every chat, log and report.

import { h, icon, btn, api, toast, fail, confirmBox, dialog, store, clear } from '../ui.js';

export function connectionsPage(view) {
  const keys = h('div', { class: 'list' });
  const servers = h('div', { class: 'list' });
  const importBox = h('div');
  store.setTop(null, []);
  view.append(h('div', { class: 'page narrow' },
    h('div', { class: 'page-head' }, h('div', { class: 't' }, h('h1', null, 'Connections'),
      h('p', null, 'Give Claude, ChatGPT and the team access to your other services. Keys stay on this computer, and are hidden from every chat, log and report.'))),
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'API keys'), btn('Add a key', () => addKey(), { cls: 'sm accent', ic: 'plus' })),
    h('p', { class: 'muted small', style: { marginTop: '-12px' } }, 'For services with a key — Hunter.io, Google, SerpApi, OpenAI, Twilio… The assistant uses them in its work when useful; ask it, for example, “find the e-mail of the CEO of X with Hunter”.'),
    keys,
    h('div', { class: 'row' }, h('h2', { class: 'grow' }, 'Connected services (MCP)'), btn('Add a service', () => addServer(), { cls: 'sm accent', ic: 'plus' })),
    h('p', { class: 'muted small', style: { marginTop: '-12px' } }, 'An MCP server gives Claude new tools: search your CRM, read Notion, send e-mail… Many services publish one; paste its address (and key) here.'),
    servers, importBox));

  let presets = [];
  async function load() {
    try {
      const r = await api('/api/connections');
      presets = r.presets || [];
      clear(keys, ...(r.keys.length ? r.keys.map((k) => h('div', { class: 'conn' },
        h('span', { class: 'c-ico' }, icon('key')),
        h('div', { class: 'c-main' }, h('b', null, k.label || k.name), h('small', { class: 'mono' }, `${k.name} · ${k.hint}`)),
        btn('Replace', () => addKey(k.name), { cls: 'sm ghost' }),
        btn('', async () => {
          if (!(await confirmBox(`Delete ${k.label || k.name}?`, 'Claude, ChatGPT and the team will no longer be able to use this key.', { ok: 'Delete', danger: true }))) return;
          try { await api('/api/secrets', { method: 'PUT', body: { name: k.name, value: null } }); load(); } catch (e) { fail(e); }
        }, { cls: 'sm ghost icon danger', ic: 'trash', title: 'Delete' })))
        : [h('div', { class: 'conn muted' }, 'No keys yet.')]));
      clear(servers, ...(r.mcp.length ? r.mcp.map((m) => h('div', { class: 'conn' },
        h('span', { class: 'c-ico' }, icon(m.type === 'stdio' ? 'terminal' : 'plug')),
        h('div', { class: 'c-main' }, h('b', null, m.name), h('small', { class: 'mono' }, m.url || m.command),
          Object.keys(m.headers || {}).length || (m.env || []).length ? h('small', null, [...Object.keys(m.headers || {}), ...(m.env || [])].join(', ') + ' (hidden)') : null),
        m.source && m.source !== 'you' ? h('span', { class: 'pill outline' }, `from ${m.source}`) : null,
        h('label', { class: 'switch', title: m.enabled ? 'On' : 'Off' }, h('input', {
          type: 'checkbox', checked: m.enabled, 'aria-label': 'Switched on', onchange: async (e) => {
            try { await api(`/api/connections/mcp/${encodeURIComponent(m.name)}/toggle`, { method: 'POST', body: { enabled: e.target.checked } }); toast(e.target.checked ? 'Switched on for new chats.' : 'Switched off.'); } catch (x) { fail(x); e.target.checked = !e.target.checked; }
          },
        }), h('i')),
        btn('', async () => {
          if (!(await confirmBox(`Remove ${m.name}?`, 'New chats and projects will no longer have its tools.', { ok: 'Remove', danger: true }))) return;
          try { await api(`/api/connections/mcp/${encodeURIComponent(m.name)}`, { method: 'DELETE' }); load(); } catch (e) { fail(e); }
        }, { cls: 'sm ghost icon danger', ic: 'trash', title: 'Remove' })))
        : [h('div', { class: 'conn muted' }, 'No services connected yet.')]));
      clear(importBox, ...(r.claude_desktop ? [h('div', { class: 'card soft row wrap' }, icon('spark'),
        h('div', { class: 'grow' }, h('b', null, 'Use the connectors you set up in the Claude desktop app'), h('div', { class: 'muted small' }, 'Copies them into Crew; your existing ones stay as they are.')),
        btn('Import', async () => {
          try { const x = await api('/api/connections/import-claude', { method: 'POST', body: {} }); toast(x.added.length ? `Imported: ${x.added.join(', ')}` : 'Nothing new to import.'); load(); } catch (e) { fail(e); }
        }, { cls: 'sm primary', ic: 'download' }))] : []));
    } catch (e) { fail(e); }
  }

  async function addKey(existing = '') {
    const sel = h('select', null, h('option', { value: '' }, 'Choose a service…'), presets.map((p) => h('option', { value: p.name, selected: p.name === existing }, p.label)),
      h('option', { value: '__other' }, 'Another service (type its name)'));
    const name = h('input', { type: 'text', placeholder: 'e.g. WEATHER_API_KEY', class: 'mono', value: existing && !presets.some((p) => p.name === existing) ? existing : '' });
    const nameRow = h('label', { class: 'field hidden' }, h('span', null, 'Name'), name, h('small', null, 'Capital letters, digits and underscores. Claude sees this name, never the key.'));
    if (existing && !presets.some((p) => p.name === existing)) { sel.value = '__other'; nameRow.classList.remove('hidden'); }
    sel.addEventListener('change', () => nameRow.classList.toggle('hidden', sel.value !== '__other'));
    const value = h('input', { type: 'password', placeholder: 'Paste the key', autocomplete: 'off' });
    const v = await dialog({
      title: existing ? 'Replace a key' : 'Add an API key',
      body: h('div', { class: 'stack', style: { gap: '14px' } }, h('label', { class: 'field' }, h('span', null, 'Service'), sel), nameRow,
        h('label', { class: 'field' }, h('span', null, 'The key'), value, h('small', null, 'Stored only on this computer, in Crew’s folder.'))),
      actions: [{ label: 'Cancel', value: null }, {
        label: 'Save key', primary: true, value: () => {
          const n = (sel.value === '__other' ? name.value : sel.value).trim().toUpperCase().replace(/[^A-Z0-9_]/g, '_');
          if (!n) { toast('Choose the service.', { bad: true }); return undefined; }
          if (!value.value.trim()) { value.focus(); toast('Paste the key.', { bad: true }); return undefined; }
          return { name: n, value: value.value.trim() };
        },
      }],
    });
    if (!v) return;
    try { await api('/api/secrets', { method: 'PUT', body: v }); toast('Key saved. New chats can use it.'); load(); } catch (e) { fail(e); }
  }

  async function addServer() {
    let kind = 'http';
    const name = h('input', { type: 'text', placeholder: 'e.g. hunter, notion, crm' });
    const url = h('input', { type: 'url', placeholder: 'https://…/mcp' });
    const headers = h('textarea', { rows: 2, class: 'mono', placeholder: 'Authorization: Bearer your-key' });
    const command = h('input', { type: 'text', class: 'mono', placeholder: 'npx' });
    const args = h('input', { type: 'text', class: 'mono', placeholder: '-y @some/mcp-server' });
    const env = h('textarea', { rows: 2, class: 'mono', placeholder: 'SOME_API_KEY=…' });
    const web = h('div', { class: 'stack', style: { gap: '14px' } },
      h('label', { class: 'field' }, h('span', null, 'Address (URL)'), url),
      h('label', { class: 'field' }, h('span', null, 'Headers (optional)'), headers, h('small', null, 'One per line, like “Authorization: Bearer …”. Hidden after saving.')));
    const prog = h('div', { class: 'stack hidden', style: { gap: '14px' } },
      h('label', { class: 'field' }, h('span', null, 'Program'), command),
      h('label', { class: 'field' }, h('span', null, 'Arguments'), args),
      h('label', { class: 'field' }, h('span', null, 'Settings (optional)'), env, h('small', null, 'One per line: NAME=value. Hidden after saving.')));
    const seg = h('div', { class: 'seg' }, [['http', 'A web address'], ['stdio', 'A program on this computer']].map(([k, l]) => h('button', {
      type: 'button', class: k === kind ? 'on' : '', onclick: (e) => {
        kind = k;
        [...seg.children].forEach((b) => b.classList.toggle('on', b === e.currentTarget));
        web.classList.toggle('hidden', k !== 'http');
        prog.classList.toggle('hidden', k !== 'stdio');
      },
    }, l)));
    const v = await dialog({
      title: 'Connect a service', wide: true,
      body: h('div', { class: 'stack', style: { gap: '14px' } },
        h('p', { class: 'muted small' }, 'The service’s instructions give you either a web address (most common) or a program to run.'),
        h('label', { class: 'field' }, h('span', null, 'Name'), name), seg, web, prog),
      actions: [{ label: 'Cancel', value: null }, {
        label: 'Connect', primary: true, value: () => {
          if (!name.value.trim()) { name.focus(); toast('Give it a short name.', { bad: true }); return undefined; }
          const envObj = Object.fromEntries(env.value.split('\n').map((l) => l.trim()).filter((l) => l.includes('=')).map((l) => [l.slice(0, l.indexOf('=')).trim(), l.slice(l.indexOf('=') + 1).trim()]));
          return kind === 'http' ? { name: name.value.trim(), type: 'http', url: url.value.trim(), headers: headers.value }
            : { name: name.value.trim(), type: 'stdio', command: command.value.trim(), args: args.value.trim(), env: envObj };
        },
      }],
    });
    if (!v) return;
    try { await api('/api/connections/mcp', { method: 'POST', body: v }); toast('Connected. New chats and projects can use it.'); load(); } catch (e) { fail(e); }
  }

  load();
}
