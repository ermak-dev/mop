// HYPOTHESIS: удаление старой вкладки без переноса её источников теряет
// логи инициализации, ограниченный tail и отдельный peer-send.
// SOLUTION: один Терминал показывает startup, живой PTY или явный снимок;
// чат сохраняет адресную отправку отдельно от человеческого CLI.
// RESULT: три сценария сначала упали, затем прошли с сохранением старых проверок.
// STATUS: FIXED — see #622
import { fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { renderUi } from '../test-utils';
import { PuppetCard } from './PuppetCard';

vi.mock('@xterm/xterm', () => ({ Terminal: class {
  cols = 80; rows = 24; options = { disableStdin: true };
  parser = { registerOscHandler: () => ({ dispose() {} }) };
  loadAddon() {} open() {} dispose() {} reset() {} write() {}
  onData() { return { dispose() {} }; }
  onBinary() { return { dispose() {} }; }
  onResize() { return { dispose() {} }; }
} }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} dispose() {} } }));

const session = { login: 'anton', role: 'admin' as const };
const response = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;
const history = { epoch: 'history', session_id: 's', cursor: 'c', records: [], reset: false, has_more: false, partial: false };
const render = () => renderUi(<PuppetCard session={session} target="pu-mop-1" onClose={() => {}} />);
afterEach(() => vi.unstubAllGlobals());

test('#622 startup output is in the only terminal, then screen opens one read-only PTY', async () => {
  let source: 'initialization' | 'screen' = 'initialization';
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url.includes('/history')) return Promise.resolve(response(history));
    if (url.endsWith('/tail')) return Promise.resolve(response(source === 'initialization'
      ? { source, lines: ['TASK [Prepare body]'], note: 'waiting for setup', truncated: false }
      : { source, lines: ['earlier terminal line'], note: null, truncated: false }));
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/term')) return Promise.resolve(response({ tid: 'reader', epoch: 'pty', cursor: 0, mode: 'ro', cols: 80, rows: 24 }));
    return Promise.resolve(response({ ok: true }));
  }));
  render();
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  expect(await screen.findByText('TASK [Prepare body]')).toBeVisible();
  expect(screen.getByText('waiting for setup')).toBeVisible();
  expect(calls.filter(([url, init]) => url.endsWith('/term') && init?.method === 'POST')).toHaveLength(0);
  source = 'screen';
  fireEvent.click(screen.getByRole('button', { name: 'Обновить' }));
  await waitFor(() => expect(calls.filter(([url, init]) => url.endsWith('/term') && init?.method === 'POST')).toHaveLength(1));
  expect(screen.getByRole('region', { name: 'Терминал: только чтение' })).toBeVisible();
  expect(screen.queryByRole('tab', { name: 'Экран' })).toBeNull();
});

test('#622 old scrollback is inspected on demand without closing the live PTY', async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url.includes('/history')) return Promise.resolve(response(history));
    if (url.endsWith('/tail')) return Promise.resolve(response({ source: 'screen', lines: ['older line'], truncated: true }));
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/term')) return Promise.resolve(response({ tid: 'reader', epoch: 'pty', cursor: 0, mode: 'ro', cols: 80, rows: 24 }));
    return Promise.resolve(response({ ok: true }));
  }));
  render(); fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  await screen.findByRole('region', { name: 'Терминал: только чтение' });
  fireEvent.click(screen.getByRole('button', { name: 'Последние строки' }));
  expect(screen.getByText('older line')).toBeVisible();
  expect(screen.getByText(/обрезано/)).toBeVisible();
  expect(screen.getByRole('button', { name: 'Увеличить' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: 'Вернуться в терминал' }));
  expect(screen.getByRole('region', { name: 'Терминал: только чтение' })).toBeVisible();
  expect(calls.filter(([url]) => url.endsWith('/close'))).toHaveLength(0);
});

test('#622 peer message remains available in chat without taking CLI control', async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url.includes('/history')) return Promise.resolve(response(history));
    if (url.endsWith('/tail')) return Promise.resolve(response({ source: 'screen', lines: [], truncated: false }));
    if (url === '/api/admin/ops' && init?.method === 'POST') return Promise.resolve(response({ id: 'op-1', status: 'accepted' }));
    return Promise.resolve(response({ id: 'op-1', status: 'succeeded' }));
  }));
  render();
  fireEvent.click(screen.getByText('Сообщение папету через канал', { selector: 'summary' }));
  const peer = await screen.findByRole('textbox', { name: 'Сообщение' });
  fireEvent.change(peer, { target: { value: 'привет' } });
  fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
  await waitFor(() => expect(calls.some(([url, init]) => url === '/api/admin/ops' && init?.method === 'POST')).toBe(true));
  const sent = calls.find(([url, init]) => url === '/api/admin/ops' && init?.method === 'POST')!;
  expect(JSON.parse(String(sent[1]?.body))).toEqual({ action: 'send', target: 'pu-mop-1', params: { message: 'привет' } });
  expect(calls.some(([url]) => url.endsWith('/term'))).toBe(false);
});
