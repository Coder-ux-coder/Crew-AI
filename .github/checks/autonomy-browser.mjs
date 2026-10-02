// Exercise the real app in an isolated home. No subscription calls or owner device changes.
import { createRequire } from 'node:module';
import { spawn } from 'node:child_process';
import { mkdtempSync, mkdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import assert from 'node:assert/strict';

const require = createRequire(import.meta.url);
const { chromium } = require(process.env.CREW_PLAYWRIGHT_MODULE || 'playwright');
const repo = resolve(fileURLToPath(new URL('../..', import.meta.url)));
const home = mkdtempSync(join(tmpdir(), 'crew-browser-'));
const output = resolve(process.env.CREW_QA_OUTPUT || join(home, 'screenshots'));
mkdirSync(output, { recursive: true });
const proc = spawn(process.env.CREW_PYTHON || 'python', [join(repo, 'crew/tests/autonomy_server.py')], {
  env: { ...process.env, CREW_HOME: home, PYTHONUTF8: '1', PYTHONUNBUFFERED: '1' }, windowsHide: true,
});
let browser;
const errors = [];
try {
  const info = await new Promise((res, rej) => {
    let data = '';
    const timer = setTimeout(() => rej(new Error('Fixture did not start')), 45000);
    proc.stderr.on('data', (d) => process.stderr.write(d));
    proc.stdout.on('data', (d) => {
      data += d;
      for (const line of data.split('\n')) {
        if (line.trim().startsWith('{"port"')) { clearTimeout(timer); res(JSON.parse(line)); return; }
      }
    });
    proc.on('exit', (code) => { clearTimeout(timer); rej(new Error(`Fixture exited ${code}: ${data}`)); });
  });
  const url = `http://127.0.0.1:${info.port}`;
  browser = await chromium.launch({ headless: true, ...(process.env.CREW_CHROMIUM ? { executablePath: process.env.CREW_CHROMIUM } : {}) });
  for (const [name, size] of [['desktop', { width: 1440, height: 1000 }], ['mobile', { width: 390, height: 844 }]]) {
    const context = await browser.newContext({ viewport: size });
    const page = await context.newPage();
    page.on('pageerror', (e) => errors.push(`${name}: ${e.message}`));
    page.on('response', (r) => { if (r.status() >= 500) errors.push(`${name}: HTTP ${r.status()} ${r.url()}`); });
    await page.goto(`${url}/#/settings/models`);
    try { await page.getByRole('heading', { name: 'Agents', exact: true }).waitFor(); }
    catch (error) { console.error({ errors, body: await page.locator('body').innerText() }); throw error; }
    assert.match(await page.locator('main').innerText(), /gpt-6\.1-sol/);
    await page.getByRole('button', { name: 'Add an agent', exact: true }).click();
    await page.getByRole('dialog').waitFor();
    await page.getByLabel('Name', { exact: true }).fill(`extra-${name}`);
    await page.getByLabel('Subscription', { exact: true }).selectOption('codex-1');
    await page.getByLabel('Model ID', { exact: true }).fill('gpt-6.1-sol');
    await page.getByRole('button', { name: 'Save agent', exact: true }).click();
    await page.getByText(`extra-${name}`, { exact: true }).waitFor();
    await page.screenshot({ path: join(output, `${name}-agents.png`), fullPage: true });
    const settingsData = await page.evaluate(() => fetch('/api/settings').then((r) => r.json()));
    assert(settingsData.seats.some((s) => s.name === `extra-${name}` && s.vendor === 'codex'));
    await page.goto(`${url}/#/chat/${info.chat}`);
    await page.getByText('Cobalt is recorded', { exact: true }).waitFor();
    // Use the composer's model menu to change product in the same conversation.
    await page.getByTitle('Who answers, and with which model', { exact: true }).click();
    await page.getByText('GPT-6.1 Sol', { exact: true }).last().click();
    const prompt = page.locator('.composer textarea');
    const words = `Continue the cobalt design on ${name}`;
    await prompt.fill(words);
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await page.waitForFunction(async ({ id, words }) => {
      const d = await fetch(`/api/chats/${id}`).then((r) => r.json());
      const at = d.messages.findLastIndex((m) => m.role === 'user' && m.text === words);
      return at >= 0 && d.messages.slice(at + 1).some((m) => m.role === 'assistant');
    }, { id: info.chat, words });
    await page.waitForFunction(() => document.querySelector('.composer textarea').value === '');
    await page.getByText('Continuing on gpt-6.1-sol. Cobalt context retained.', { exact: true }).last().waitFor();
    const chatData = await page.evaluate((id) => fetch(`/api/chats/${id}`).then((r) => r.json()), info.chat);
    assert.equal(chatData.engine, 'codex');
    assert(chatData.messages.some((m) => m.text === 'Use cobalt and retain this layout'));
    assert(chatData.messages.some((m) => m.text === words));
    await page.getByTitle('Files in this chat', { exact: true }).click();
    await page.getByRole('button', { name: 'result.html', exact: false }).click();
    await page.locator('#panel iframe').waitFor();
    assert.match(await page.locator('#panel iframe').contentFrame().locator('body').innerText(), /Saved preview/);
    await page.waitForFunction(() => {
      const tabs = document.querySelector('#panelTabs').getBoundingClientRect();
      const active = document.querySelector('#panelTabs .on').getBoundingClientRect();
      return active.left >= tabs.left - 1 && active.right <= tabs.right + 1;
    });
    await page.screenshot({ path: join(output, `${name}-preview.png`), fullPage: true });
    await page.locator('#panelClose').click();
    await page.screenshot({ path: join(output, `${name}-chat.png`), fullPage: true });
    await page.goto(`${url}/#/projects/finished`);
    await page.getByText('The original design is saved.', { exact: true }).waitFor({ state: 'attached' });
    await page.locator('.to-btn').click();
    await page.getByText(/The CEO .*GPT/).last().click();
    await page.locator('.say-dock textarea').fill('Continue with an indigo version');
    await page.getByRole('button', { name: 'Send', exact: true }).click();
    await page.getByText('Continue with an indigo version', { exact: true }).waitFor();
    await page.screenshot({ path: join(output, `${name}-followup.png`), fullPage: true });
    const runData = await page.evaluate(() => fetch('/api/runs/finished').then((r) => r.json()));
    assert.equal(runData.raw_phase, 'build');
    assert(runData.messages.some((m) => m.text === 'Keep the original cobalt design'));
    assert(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth + 1));
    await context.close();
  }
  // Visible sidebar deletion, with a real backend record and its confirmation.
  const page = await browser.newPage({ viewport: { width: 1440, height: 1000 } });
  await page.goto(`${url}/#/new`);
  await page.getByRole('button', { name: 'Delete Cobalt design', exact: true }).click();
  await page.getByRole('dialog').getByRole('button', { name: 'Delete', exact: true }).click();
  await page.getByRole('button', { name: 'Delete Cobalt design', exact: true }).waitFor({ state: 'detached' });
  const status = await page.evaluate((id) => fetch(`/api/chats/${id}`).then((r) => r.status), info.chat);
  assert.equal(status, 404);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ passed: ['desktop settings/agent edits', 'mobile settings/agent edits', 'cross-product context', 'completed-project followup', 'sidebar deletion', 'no horizontal overflow', 'no page errors'], screenshots: output }));
} finally {
  if (browser) await browser.close();
  proc.kill();
}
