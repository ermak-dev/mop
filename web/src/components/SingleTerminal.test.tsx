// #661: один Терминал сохраняет startup/fallback-снимок, затем открывает
// writer один раз; канал удалён, единственная отправка пишет в CLI.
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

test('#661 startup output is in the only terminal, then screen opens one writable PTY', async () => {
  let source: 'initialization' | 'screen' = 'initialization';
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url.includes('/history')) return Promise.resolve(response(history));
    if (url.endsWith('/tail')) return Promise.resolve(response(source === 'initialization'
      ? { source, lines: ['TASK [Prepare body]'], note: 'waiting for setup', truncated: false }
      : { source, lines: ['earlier terminal line'], note: null, truncated: false }));
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/term')) return Promise.resolve(response({ tid: 'reader', epoch: 'pty', cursor: 0, mode: 'rw', cols: 80, rows: 24 }));
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
  expect(screen.getByRole('region', { name: 'Терминал: управление' })).toBeVisible();
  expect(screen.queryByRole('tab', { name: 'Экран' })).toBeNull();
});

test('#661 a failed attach exposes the snapshot without reopening or closing a PTY', async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url.includes('/history')) return Promise.resolve(response(history));
    if (url.endsWith('/tail')) return Promise.resolve(response({ source: 'screen', lines: ['older line'], truncated: true }));
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/term')) return Promise.resolve({ ok: false, status: 502, json: async () => ({ error: 'attach unavailable' }) } as Response);
    return Promise.resolve(response({ ok: true }));
  }));
  render(); fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('attach unavailable');
  expect(screen.getByText('older line')).toBeVisible();
  expect(screen.getByText(/обрезано/)).toBeVisible();
  expect(screen.getByRole('button', { name: 'Увеличить' })).toBeEnabled();
  fireEvent.click(screen.getByRole('button', { name: 'Обновить' }));
  await waitFor(() => expect(calls.filter(([url]) => url.endsWith('/tail'))).toHaveLength(2));
  expect(calls.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
  expect(calls.filter(([url]) => url.endsWith('/close'))).toHaveLength(0);
});

test('#661 chat sends only CLI input, never a peer-channel operation', async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url.includes('/history')) return Promise.resolve(response(history));
    if (url.endsWith('/tail')) return Promise.resolve(response({ source: 'screen', lines: [], truncated: false }));
    if (url.endsWith('/term')) return Promise.resolve(response({ tid: 'writer', epoch: 'pty', cursor: 0, mode: 'rw', cols: 80, rows: 24 }));
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/input')) {
      const body = JSON.parse(String(init?.body));
      return Promise.resolve(response({ seq: body.seq, written: atob(body.data).length, outcome: 'written' }));
    }
    return Promise.resolve(response({ ok: true }));
  }));
  render();
  const input = await screen.findByRole('textbox', { name: 'Ввод человека в CLI' });
  fireEvent.change(input, { target: { value: 'привет' } });
  await waitFor(() => expect(screen.getByRole('button', { name: 'Отправить' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Отправить' }));
  await waitFor(() => expect(input).toHaveValue(''));
  const sent = calls.filter(([url]) => url.endsWith('/input'));
  const bytes = sent.map(([, init]) => Uint8Array.from(atob(JSON.parse(String(init?.body)).data), char => char.charCodeAt(0)));
  expect(bytes).toHaveLength(1);
  expect(new TextDecoder().decode(bytes[0])).toBe('\u001b[200~привет\u001b[201~\r');
  expect(calls.some(([url]) => url === '/api/admin/ops')).toBe(false);
  expect(calls.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
  expect(screen.queryByText('Сообщение папету через канал')).toBeNull();
});
