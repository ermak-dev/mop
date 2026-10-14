// HYPOTHESIS: чат не должен переиспользовать peer-send и выдавать меню
// терминала за исполненную команду. Снимок tail остаётся независимым.
// SOLUTION: вкладки, безопасный текст, реальный xterm и один human writer.
// RESULT: RED до реализации; STATUS: FIXED — see #601
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { renderUi } from '../test-utils';
import { HybridWorkspace } from './HybridWorkspace';
import wire from '../../tests/fixtures/hybrid-wire.json';
const terminals = vi.hoisted(() => [] as Array<{ options: Record<string, unknown>; write: ReturnType<typeof vi.fn>; dispose: ReturnType<typeof vi.fn>; reset: ReturnType<typeof vi.fn> }>);
vi.mock('@xterm/xterm', () => ({ Terminal: class {
  options: Record<string, unknown>; cols = 80; rows = 24;
  write = vi.fn(); reset = vi.fn(); dispose = vi.fn();
  parser = { registerOscHandler: vi.fn(() => ({ dispose() {} })) };
  constructor(options: Record<string, unknown>) { this.options = options; terminals.push(this); }
  loadAddon() {} open() {} onData() { return { dispose() {} }; } onBinary() { return { dispose() {} }; } onResize() { return { dispose() {} }; }
} }));
vi.mock('@xterm/addon-fit', () => ({ FitAddon: class { fit() {} dispose() {} } }));
const admin = { login: 'anton', role: 'admin' as const };
const ok = (data: unknown) => ({ ok: true, status: 200, json: async () => data }) as Response;
const history = { epoch: 'h1-digest', session_id: 's', cursor: 'c', reset: false, partial: true, has_more: false, records: [
  { type: 'assistant', uuid: 'a', message: { content: [{ type: 'text', text: '<img src="https://evil.test/i">' }, { type: 'tool_use', id: 't', name: 'Bash', input: { command: 'pwd' } }], model: 'actual-model' } },
  { type: 'user', origin: 'peer', message: { content: [{ type: 'tool_result', tool_use_id: 't', content: 'denied', is_error: true }] } },
  { type: 'future', data: 'kept' },
] };
const open = () => renderUi(<HybridWorkspace session={admin} target="p" fallback={<div>legacy tail</div>} />);
afterEach(() => { vi.unstubAllGlobals(); terminals.length = 0; });
test('legacy screen initially unchanged; chat renders safe text, tool errors and honest partial/unknown notices without external images', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ok(history)));
  open();
  expect(screen.getByText('legacy tail')).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  expect(await screen.findByText('<img src="https://evil.test/i">')).toBeVisible();
  expect(document.querySelector('img')).toBeNull();
  expect(screen.getByText(/неполная запись/i)).toBeVisible();
  expect(screen.getByText(/неизвестная запись/i)).toBeVisible();
  fireEvent.click(screen.getByText(/Bash/, { selector: 'summary' }));
  expect(screen.getByText('denied')).toBeVisible();
  expect(screen.getByText(/ошибка инструмента/i)).toBeVisible();
  expect(screen.queryByText(/выполнено/i)).toBeNull();
  expect(screen.getByRole('button', { name: 'Добавить в CLI и отправить Enter' })).toBeDisabled();
});
test('reader opens only on terminal tab; rw explicitly closes reader, human slash submits same bytes, preserves draft on refused send', async () => {
  const posts: Array<[string, Record<string, unknown>]> = [];
  let outcome = 'written';
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? '{}'));
    if (init?.method === 'POST') posts.push([url, body]);
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/history')) return Promise.resolve(ok(history));
    if (url.endsWith('/input')) return Promise.resolve(ok({ seq: body.seq, written: atob(body.data).length, outcome, error: outcome === 'unknown' ? 'uncertain' : undefined }));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { tid: body.mode, epoch: 't-random', cursor: 0, mode: body.mode, cols: 80, rows: 24 }));
  }));
  const view = open();
  expect(posts).toHaveLength(0);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  await waitFor(() => expect(posts).toHaveLength(1));
  expect(posts[0][1].mode).toBe('ro');
  expect(terminals[0].options.disableStdin).toBe(true);
  expect(terminals[0].options.windowOptions).toEqual({});
  expect(terminals[0].options.linkHandler).toEqual({ activate: expect.any(Function) });
  fireEvent.click(screen.getByRole('button', { name: 'Взять управление' }));
  await waitFor(() => expect(posts).toHaveLength(3));
  expect(posts[1][0]).toContain('/ro/close');
  expect(posts[2][1].mode).toBe('rw');
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  const composer = screen.getByRole('textbox', { name: 'Ввод человека в CLI' });
  fireEvent.change(composer, { target: { value: '/model' } });
  fireEvent.click(screen.getByRole('button', { name: 'Добавить в CLI и отправить Enter' }));
  await waitFor(() => expect(posts.some(([url]) => url.endsWith('/input'))).toBe(true));
  expect(atob(String(posts.find(([url]) => url.endsWith('/input'))?.[1].data))).toBe('\x1b[200~/model\x1b[201~\r');
  expect(posts.some(([url]) => url.includes('/send') || url.includes('/admin/ops'))).toBe(false);
  await waitFor(() => expect(composer).toHaveValue(''));
  outcome = 'unknown';
  fireEvent.change(composer, { target: { value: 'keep draft' } });
  fireEvent.click(screen.getByRole('button', { name: 'Добавить в CLI и отправить Enter' }));
  await screen.findByText(/uncertain/);
  expect(composer).toHaveValue('keep draft');
  expect(screen.getByRole('button', { name: 'Добавить в CLI и отправить Enter' })).toBeDisabled();
  view.unmount();
  expect(terminals[0].dispose).toHaveBeenCalled();
});
test('history session identity change disconnects even when epoch is unchanged, preserving draft', async () => {
  let sessionId = 's';
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.includes('/history')) return Promise.resolve(ok({ ...history, session_id: sessionId }));
    const body = JSON.parse(String(init?.body ?? '{}'));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { tid: 'rw', epoch: 't-random', cursor: 0, mode: body.mode, cols: 80, rows: 24 }));
  }));
  open(); fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  await screen.findByText('<img src="https://evil.test/i">');
  fireEvent.click(screen.getByRole('button', { name: 'Взять управление' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeEnabled());
  const composer = screen.getByRole('textbox', { name: 'Ввод человека в CLI' });
  fireEvent.change(composer, { target: { value: 'unsent draft' } });
  sessionId = 's2';
  fireEvent.click(screen.getByRole('button', { name: 'Обновить историю' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeDisabled());
  expect(composer).toHaveValue('unsent draft');
});
test('desktop optional terminal is visible alongside chat and remains mounted across tabs', async () => {
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.includes('/history')) return Promise.resolve(ok(history));
    const body = JSON.parse(String(init?.body ?? '{}'));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { tid: 'ro', epoch: 't-random', cursor: 0, mode: body.mode, cols: 80, rows: 24 }));
  }));
  open(); fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  fireEvent.click(screen.getByRole('switch', { name: 'Терминал рядом' }));
  await waitFor(() => expect(terminals).toHaveLength(1));
  expect(screen.getByRole('region', { name: 'Терминал: только чтение' })).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  expect(terminals).toHaveLength(1);
  expect(terminals[0].dispose).not.toHaveBeenCalled();
});
test('history authorization loss also disables human control and clears terminal output', async () => {
  let denied = false;
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.includes('/history')) return Promise.resolve(denied ? { ok: false, status: 403, json: async () => ({ error: 'session revoked' }) } as Response : ok(history));
    const body = JSON.parse(String(init?.body ?? '{}'));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { tid: 'rw', epoch: 't-random', cursor: 0, mode: body.mode, cols: 80, rows: 24 }));
  }));
  open(); fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  await screen.findByText('<img src="https://evil.test/i">');
  fireEvent.click(screen.getByRole('button', { name: 'Взять управление' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeEnabled());
  denied = true;
  expect(terminals).toHaveLength(1);
  const resets = terminals[0].reset.mock.calls.length;
  fireEvent.click(screen.getByRole('button', { name: 'Обновить историю' }));
  await screen.findByText(/session revoked/);
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeDisabled());
  expect(terminals[0].reset.mock.calls.length).toBeGreaterThan(resets);
  expect(screen.queryByText('<img src="https://evil.test/i">')).toBeNull();
});
// HYPOTHESIS: digest истории и случайный terminal epoch независимы;
// первое появление SID и скрытие чата не являются сменой сессии.
// STATUS: FIXED — see #601
test('different wire epochs preserve writer across Chat, Terminal and split without close', async () => {
  const mock = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.includes('/history')) return Promise.resolve(ok(wire.history));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { ...wire.open, mode: JSON.parse(String(init?.body)).mode }));
  });
  vi.stubGlobal('fetch', mock);
  open(); fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Взять управление' })).toBeEnabled());
  fireEvent.click(screen.getByRole('button', { name: 'Взять управление' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeEnabled());
  const closes = mock.mock.calls.filter(([url]) => url.endsWith('/close')).length;
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  await act(async () => {});
  expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeEnabled();
  fireEvent.click(screen.getByRole('switch', { name: 'Терминал рядом' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  await act(async () => {});
  expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeEnabled();
  expect(mock.mock.calls.filter(([url]) => url.endsWith('/close'))).toHaveLength(closes);
});
test('missing timing metadata disables only combined action with reason', async () => {
  vi.stubGlobal('fetch', vi.fn((url: string) => url.includes('/read?') ? new Promise(() => {}) : Promise.resolve(ok(url.includes('/history') ? history : { ...wire.open, interrupt_delay_ms: undefined }))));
  open(); fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  fireEvent.click(screen.getByRole('button', { name: 'Взять управление' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeEnabled());
  fireEvent.change(screen.getByRole('textbox', { name: 'Ввод человека в CLI' }), { target: { value: 'hello' } });
  expect(screen.getByRole('button', { name: 'Escape, затем добавить и отправить' })).toBeDisabled();
  expect(screen.getByText(/задержка Escape неизвестна/i)).toBeVisible();
  expect(screen.getByRole('button', { name: 'Добавить в CLI и отправить Enter' })).toBeEnabled();
});
test('switching tabs preserves legacy draft rather than silently clearing it', async () => {
  vi.stubGlobal('fetch', vi.fn(() => new Promise(() => {})));
  function LegacyDraft() { return <input aria-label="Legacy draft" defaultValue="half typed" />; }
  renderUi(<HybridWorkspace session={admin} target="p" fallback={<LegacyDraft />} />);
  const input = screen.getByRole('textbox', { name: 'Legacy draft' });
  fireEvent.change(input, { target: { value: 'keep this unsent text' } });
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  fireEvent.click(screen.getByRole('tab', { name: 'Экран' }));
  expect(screen.getByRole('textbox', { name: 'Legacy draft' })).toBe(input);
  expect(input).toHaveValue('keep this unsent text');
});
test('Stop sends Escape only while read is pending; shortcut only appends draft and does not claim execution', async () => {
  const inputs: string[] = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? '{}'));
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.includes('/history')) return Promise.resolve(ok(history));
    if (url.endsWith('/input')) { inputs.push(atob(body.data)); return Promise.resolve(ok({ seq: body.seq, written: 1, outcome: 'written' })); }
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { tid: 'rw', epoch: 't-random', cursor: 0, mode: 'rw', cols: 80, rows: 24 }));
  }));
  open(); fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  fireEvent.click(screen.getByRole('button', { name: 'Взять управление' }));
  await waitFor(() => expect(screen.getByRole('button', { name: 'Stop: отправить Escape' })).toBeEnabled());
  await act(async () => { fireEvent.click(screen.getByRole('button', { name: 'Stop: отправить Escape' })); });
  expect(inputs).toEqual(['\x1b']);
  const composer = screen.getByRole('textbox', { name: 'Ввод человека в CLI' });
  fireEvent.change(composer, { target: { value: 'half typed ' } });
  fireEvent.click(screen.getByRole('button', { name: 'Добавить /help в черновик' }));
  expect(composer).toHaveValue('half typed /help');
  expect(inputs).toHaveLength(1);
  expect(screen.getByText(/команды отправляются/i)).toBeVisible();
});
