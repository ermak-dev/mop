// HYPOTHESIS: модальный экран блокирует ростер, узлы и расход загромождают пул.
// SOLUTION: отдельные страницы и единственная правая панель выбранного папета.
// RESULT: вкладки, панель и безопасность переключений сначала упали, затем прошли.
// STATUS: FIXED — see #664
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import App from './App';
import { emptySnapshot, renderUi } from './test-utils';
import type { Puppet, Snapshot } from './types';
import type { Session } from './useSession';
const state = vi.hoisted(() => ({ snapshot: null as Snapshot | null, session: { login: 'admin', role: 'admin' } as Session }));
vi.mock('./usePool', () => ({ usePool: () => state }));
vi.mock('./useSession', async original => ({ ...await original<typeof import('./useSession')>(), fetchSession: async () => state.session }));
vi.mock('./components/NodesPanel', () => ({ NodesPanel: () => <div data-testid="nodes-page">nodes content</div> }));
vi.mock('./components/UsageChart', () => ({ UsageChart: () => <div data-testid="tokens-chart">tokens chart</div> }));
vi.mock('./components/UsageByUser', () => ({ UsageByUser: () => <div data-testid="tokens-users">tokens users</div> }));
vi.mock('@xterm/xterm', () => ({ Terminal: class {
  cols = 80; rows = 24; options = { disableStdin: true };
  parser = { registerOscHandler: () => ({ dispose() {} }) };
  loadAddon() {} open() {} dispose() {} reset() {} write() {}
  onData() { return { dispose() {} }; } onBinary() { return { dispose() {} }; } onResize() { return { dispose() {} }; }
} }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} dispose() {} } }));
const puppet = (name: string): Puppet => ({ name, node: `node-${name}`, alloc_status: 'running', state: 'free (master)', kind: 'free', bucket: 'free', branch: 'master', owner: '', llm: 'model', origin: 'git@host:project.git', disk_kb: null });
const response = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;
const posts: string[] = [];
beforeEach(() => {
  window.history.replaceState(null, '', '/');
  state.session = { login: 'admin', role: 'admin' };
  state.snapshot = emptySnapshot({ projects: [{ name: 'project', counts: { puppets: 2, free: 2, busy: 0, sick: 0, silent: 0, down: 0 }, puppets: [puppet('one'), puppet('two')] }] });
  posts.length = 0;
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (init?.method === 'POST') posts.push(url);
    const name = url.includes('/one/') ? 'one' : 'two';
    if (url.includes('/read?')) return new Promise<Response>(() => {});
    if (url.includes('/history')) return Promise.resolve(response({ epoch: name, session_id: name, cursor: 'c', records: [{ type: 'assistant', message: { content: `conversation ${name}` } }], reset: false, partial: false, has_more: false }));
    if (url.endsWith('/tail')) return Promise.resolve(response({ source: 'screen', lines: [], truncated: false }));
    if (url.endsWith('/term')) return Promise.resolve(response({ tid: name, epoch: name, cursor: 0, mode: 'rw', cols: 80, rows: 24 }));
    return Promise.resolve(response({ ok: true }));
  }));
});
afterEach(() => { vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });
const row = (name: string) => screen.getByText(`node-${name}`).closest('tr')!;
const clickTab = async (name: string) => { fireEvent.click(screen.getByRole('tab', { name })); await waitFor(() => expect(screen.getByRole('tab', { name })).toHaveAttribute('aria-selected', 'true')); };

test('#664 nodes and token statistics have separate public tabs and hash routes', async () => {
  renderUi(<App />);
  const nav = screen.getByRole('tablist', { name: 'страницы' });
  expect(within(nav).getAllByRole('tab').map(tab => tab.textContent)).toEqual(['Пул', 'Узлы', 'Токены']);
  expect(screen.queryByTestId('nodes-page')).toBeNull();
  expect(screen.queryByTestId('tokens-chart')).toBeNull();
  await clickTab('Узлы');
  expect(window.location.hash).toBe('#nodes');
  expect(screen.getByTestId('nodes-page')).toBeVisible();
  expect(screen.queryByTestId('pool')).toBeNull();
  expect(screen.queryByTestId('tokens-chart')).toBeNull();
  await clickTab('Токены');
  expect(window.location.hash).toBe('#tokens');
  expect(screen.getByTestId('tokens-chart')).toBeVisible();
  expect(screen.getByTestId('tokens-users')).toBeVisible();
  expect(screen.queryByTestId('nodes-page')).toBeNull();
  await clickTab('Пул');
  expect(screen.getByTestId('pool')).toBeVisible();
});

test('#664 clicking row cells selects one nonmodal screen panel and switches its connection', async () => {
  renderUi(<App />);
  await screen.findByRole('button', { name: 'one' });
  expect(screen.queryByRole('button', { name: 'Экран' })).toBeNull();
  expect(within(row('one')).queryByRole('button', { name: 'Правка' })).toBeNull();
  fireEvent.click(screen.getByText('node-one'));
  const first = await screen.findByRole('complementary', { name: 'экран one' });
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(within(first).getByRole('button', { name: 'Правка' })).toBeVisible();
  expect(await screen.findByText('conversation one')).toBeVisible();
  await waitFor(() => expect(posts.filter(url => url.endsWith('/term'))).toHaveLength(1));
  fireEvent.change(within(first).getByRole('textbox', { name: 'Ввод человека в CLI' }), { target: { value: 'unsent' } });
  fireEvent.click(screen.getByText('node-two'));
  const second = await screen.findByRole('complementary', { name: 'экран two' });
  expect(screen.getAllByRole('complementary')).toHaveLength(1);
  expect(screen.queryByText('conversation one')).toBeNull();
  expect(await screen.findByText('conversation two')).toBeVisible();
  expect(within(second).getByRole('textbox', { name: 'Ввод человека в CLI' })).toHaveValue('');
  await waitFor(() => expect(posts.filter(url => url.endsWith('/one/close'))).toHaveLength(1));
  expect(row('two')).toHaveAttribute('aria-selected', 'true');
  fireEvent.click(within(second).getByRole('button', { name: 'Закрыть' }));
  expect(screen.queryByRole('complementary')).toBeNull();
  await waitFor(() => expect(posts.filter(url => url.endsWith('/two/close'))).toHaveLength(1));
  expect(screen.getByTestId('pool')).toBeVisible();
});

test('#664 switching the screen closes the old edit wizard without opening one for the new target', async () => {
  renderUi(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'one' }));
  const panel = await screen.findByRole('complementary', { name: 'экран one' });
  fireEvent.click(within(panel).getByRole('button', { name: 'Правка' }));
  expect(screen.getByTestId('control-window')).toBeVisible();
  fireEvent.click(screen.getByText('node-two'));
  await screen.findByRole('complementary', { name: 'экран two' });
  await waitFor(() => expect(screen.queryByRole('dialog')).toBeNull());
  fireEvent.click(screen.getByText('node-one'));
  await screen.findByRole('complementary', { name: 'экран one' });
  expect(screen.queryByRole('dialog')).toBeNull();
});

test('#664 navigation away and role loss close the screen instead of keeping a hidden writer', async () => {
  const view = renderUi(<App />);
  fireEvent.click(await screen.findByRole('button', { name: 'one' }));
  await screen.findByText('conversation one');
  fireEvent.change(screen.getByRole('textbox', { name: 'Ввод человека в CLI' }), { target: { value: 'draft' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
  await clickTab('Узлы');
  expect(screen.queryByRole('complementary')).toBeNull();
  await waitFor(() => expect(posts.some(url => url.endsWith('/one/close'))).toBe(true));
  await clickTab('Пул');
  expect(screen.queryByRole('complementary')).toBeNull();
  fireEvent.click(screen.getByText('node-two'));
  await screen.findByText('conversation two');
  state.session = { login: 'reader', role: 'user' };
  await act(async () => { window.dispatchEvent(new Event('focus')); });
  expect(screen.queryByRole('complementary')).toBeNull();
  expect(screen.queryByText('conversation two')).toBeNull();
  state.session = { login: 'admin', role: 'admin' };
  await act(async () => { window.dispatchEvent(new Event('focus')); });
  expect(screen.queryByRole('complementary')).toBeNull();
  view.unmount();
});

test('#664 the workspace is fluid and fills the available viewport', () => {
  renderUi(<App />);
  expect(screen.getByTestId('dashboard')).toHaveClass('dashboard');
  expect(document.querySelector('.mantine-Container-root')).toBeNull();
});
