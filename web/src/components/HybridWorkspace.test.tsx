// HYPOTHESIS: ручное управление и технические панели заслоняют переписку.
// SOLUTION: открытие диалога подключает writer; метаданные включаются отдельно.
// RESULT: проверки сначала упали на старом интерфейсе, затем прошли.
// STATUS: FIXED — see #661
import { act, createEvent, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
const { readFileSync } = await vi.importActual<{ readFileSync: (path: string, encoding: string) => string }>('node:fs');
import { renderUi } from '../test-utils';
import { HybridWorkspace } from './HybridWorkspace';
const terminals = vi.hoisted(() => [] as Array<{ options: Record<string, unknown>; dispose: ReturnType<typeof vi.fn>; reset: ReturnType<typeof vi.fn> }>);
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
const history = { epoch: 'history-digest', session_id: 's', cursor: 'c', reset: false, partial: true, has_more: false, records: [
  { type: 'user', uuid: 'u', message: { content: 'hello' } },
  { type: 'assistant', uuid: 'a', message: { content: [{ type: 'text', text: '<img src="https://evil.test/i">' }, { type: 'tool_use', id: 't', name: 'Bash', input: { command: 'pwd' } }], model: 'actual-model' } },
  { type: 'user', origin: 'peer', message: { content: [{ type: 'tool_result', tool_use_id: 't', content: 'denied', is_error: true }] } },
  { type: 'future', data: 'kept' },
] };
function wire() {
  let page = history, outcome = 'written', source = 'screen';
  const posts: Array<[string, Record<string, unknown>]> = [];
  const mock = vi.fn((url: string, init?: RequestInit) => {
    const body = JSON.parse(String(init?.body ?? '{}'));
    if (init?.method === 'POST') posts.push([url, body]);
    if (url.includes('/read?')) return new Promise<Response>(() => {});
    if (url.includes('/history')) return Promise.resolve(ok(page));
    if (url.endsWith('/tail')) return Promise.resolve(ok({ source, lines: ['startup output'], truncated: false }));
    if (url.endsWith('/input')) return Promise.resolve(ok({ seq: body.seq, written: atob(body.data).length, outcome, error: outcome === 'unknown' ? 'uncertain' : undefined }));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { tid: 'writer', epoch: 'terminal-random', cursor: 0, mode: body.mode, cols: 80, rows: 24 }));
  });
  vi.stubGlobal('fetch', mock);
  return { posts, mock, setPage: (next: typeof history) => { page = next; }, setOutcome: (next: string) => { outcome = next; }, setSource: (next: string) => { source = next; } };
}
const open = () => renderUi(<HybridWorkspace session={admin} target="p" />);
const input = () => screen.getByRole('textbox', { name: 'Ввод человека в CLI' });
const send = () => screen.getByRole('button', { name: 'Отправить' });
const ready = async () => { fireEvent.change(input(), { target: { value: 'draft' } }); await waitFor(() => expect(send()).toBeEnabled()); };
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); terminals.length = 0; });

test('#661 opening takes control once; tabs keep one writer and closing detaches it', async () => {
  const api = wire(); const view = open();
  await ready();
  expect(api.posts).toEqual([['/api/puppet/p/term', { mode: 'rw', cols: 80, rows: 24 }]]);
  expect(screen.getByRole('tab', { name: 'Чат' })).toHaveAttribute('aria-selected', 'true');
  for (const text of ['Взять управление', 'Отдать управление', 'Переподключить чтение', 'Обновить историю', 'Закрыть подключение', 'Команды и ввод CLI', 'Сообщение папету через канал', 'История сессии, не поток токенов', 'Источник не подтверждён']) expect(screen.queryByText(text)).toBeNull();
  expect(screen.queryByRole('switch', { name: 'Терминал рядом' })).toBeNull();
  expect(screen.getAllByRole('button').map(el => el.textContent)).toEqual(['Отправить']);
  fireEvent.click(screen.getByRole('tab', { name: 'Терминал' }));
  expect(screen.getByRole('region', { name: 'Терминал: управление' })).toBeVisible();
  fireEvent.click(screen.getByRole('tab', { name: 'Чат' }));
  expect(input()).toHaveValue('draft');
  expect(terminals).toHaveLength(1);
  expect(api.posts).toHaveLength(1);
  view.unmount();
  await waitFor(() => expect(api.posts.filter(([url]) => url.endsWith('/writer/close'))).toHaveLength(1));
  expect(terminals[0].dispose).toHaveBeenCalled();
});

test('#661 metadata is opt-in and can be hidden again without hiding conversation', async () => {
  wire(); open();
  expect(await screen.findByText('hello')).toBeVisible();
  expect(screen.getByText('<img src="https://evil.test/i">')).toBeVisible();
  expect(document.querySelector('img')).toBeNull();
  const checkbox = screen.getByRole('checkbox', { name: 'Отображать метаданные' });
  expect(checkbox).not.toBeChecked();
  expect(screen.queryByText(/Bash/)).toBeNull();
  expect(screen.queryByText(/неполная запись/i)).toBeNull();
  expect(screen.queryByText(/неизвестная запись/i)).toBeNull();
  expect(screen.queryByText(/метаданные/i, { selector: 'summary' })).toBeNull();
  fireEvent.click(checkbox);
  expect(screen.getByText(/неполная запись/i)).toBeVisible();
  expect(screen.getByText(/неизвестная запись/i)).toBeVisible();
  fireEvent.click(screen.getByText(/Bash/, { selector: 'summary' }));
  expect(screen.getByText('denied')).toBeVisible();
  expect(screen.getByText(/ошибка инструмента/i)).toBeVisible();
  expect(screen.getAllByText(/actual-model/).length).toBeGreaterThan(0);
  expect(screen.queryByText(/Источник не подтверждён/)).toBeNull();
  fireEvent.click(checkbox);
  expect(screen.queryByText('denied')).toBeNull();
  expect(screen.getByText('hello')).toBeVisible();
});

test('#661 puppet role is only displayed with metadata enabled', async () => {
  wire(); renderUi(<HybridWorkspace session={admin} target="p" role="builder" />);
  await screen.findByText('hello');
  expect(screen.queryByText('Role: builder')).toBeNull();
  fireEvent.click(screen.getByRole('checkbox', { name: 'Отображать метаданные' }));
  expect(screen.getByText('Role: builder')).toBeVisible();
});

test('#661 Enter sends CLI input, Ctrl+Enter inserts a newline at the selection, IME never sends', async () => {
  const api = wire(); open(); await ready();
  fireEvent.change(input(), { target: { value: '/model' } });
  fireEvent.keyDown(input(), { key: 'Enter', isComposing: true, keyCode: 229 });
  expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(0);
  fireEvent.keyDown(input(), { key: 'Enter' });
  await waitFor(() => expect(input()).toHaveValue(''));
  expect(atob(String(api.posts.find(([url]) => url.endsWith('/input'))?.[1].data))).toBe('\x1b[200~/model\x1b[201~\r');
  expect(api.posts.some(([url]) => url.includes('/admin/ops') || url.includes('/send'))).toBe(false);
  expect(screen.queryByText(/Байты записаны|Выполнение команды/)).toBeNull();
  fireEvent.change(input(), { target: { value: 'before selected after' } });
  (input() as HTMLTextAreaElement).setSelectionRange(7, 15);
  fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true });
  expect(input()).toHaveValue('before \n after');
  expect((input() as HTMLTextAreaElement).selectionStart).toBe(8);
  expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(1);
  fireEvent.change(input(), { target: { value: 'first\nlast' } });
  (input() as HTMLTextAreaElement).setSelectionRange(5, 6);
  fireEvent.keyDown(input(), { key: 'Enter', ctrlKey: true });
  expect(input()).toHaveValue('first\nlast');
  expect((input() as HTMLTextAreaElement).selectionStart).toBe(6);
  expect((input() as HTMLTextAreaElement).selectionEnd).toBe(6);
  fireEvent.change(input(), { target: { value: '   ' } });
  fireEvent.keyDown(input(), { key: 'Enter' });
  expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(1);
});

test('#661 uncertain send keeps draft and error visible even with metadata disabled, never retries writer', async () => {
  vi.useFakeTimers(); const api = wire(); api.setOutcome('unknown'); open();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.change(input(), { target: { value: 'keep draft' } });
  await act(async () => { fireEvent.keyDown(input(), { key: 'Enter' }); });
  expect(input()).toHaveValue('keep draft');
  expect(screen.getByRole('alert')).toHaveTextContent('uncertain');
  expect(send()).toBeDisabled();
  await act(async () => { await vi.advanceTimersByTimeAsync(6000); });
  expect(api.posts.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
});

test('#661 startup gates automatic control until a confirmed screen arrives', async () => {
  vi.useFakeTimers(); const api = wire(); api.setSource('initialization'); open();
  fireEvent.change(input(), { target: { value: 'wait' } });
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(send()).toBeDisabled();
  expect(api.posts).toHaveLength(0);
  api.setSource('screen');
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(send()).toBeEnabled();
  expect(api.posts.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
});

test('#661 history session change disconnects the writer and preserves draft', async () => {
  vi.useFakeTimers(); const api = wire(); open();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.change(input(), { target: { value: 'unsent draft' } });
  expect(send()).toBeEnabled();
  api.setPage({ ...history, session_id: 'replacement' });
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(send()).toBeDisabled();
  expect(input()).toHaveValue('unsent draft');
  expect(screen.getByRole('alert')).toHaveTextContent('сессия изменилась');
  expect(api.posts.filter(([url]) => url.endsWith('/close'))).toHaveLength(1);
});

test('#661 history authorization loss clears private output and revokes control', async () => {
  vi.useFakeTimers(); const api = wire(); open();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  expect(terminals).toHaveLength(1);
  const resets = terminals[0].reset.mock.calls.length;
  api.mock.mockImplementation((url: string) => url.includes('/history')
    ? Promise.resolve({ ok: false, status: 403, json: async () => ({ error: 'session revoked' }) } as Response)
    : url.includes('/read?') ? new Promise<Response>(() => {}) : Promise.resolve(ok({ ok: true })));
  await act(async () => { await vi.advanceTimersByTimeAsync(3000); });
  expect(screen.getAllByRole('alert').some(el => el.textContent?.includes('session revoked'))).toBe(true);
  expect(screen.queryByText('hello')).toBeNull();
  fireEvent.change(input(), { target: { value: 'draft' } });
  expect(send()).toBeDisabled();
  expect(terminals[0].reset.mock.calls.length).toBeGreaterThan(resets);
});

// HYPOTHESIS: Escape в composer не попадает в writer и закрывает внешний диалог.
// SOLUTION: поглощать Escape, отправлять STOP через очередь текущего writer без изменения черновика.
// RESULT: восемь новых проверок упали до реализации; после неё весь файл проходит.
// STATUS: FIXED — see #663
const escape = (options: KeyboardEventInit = {}) => {
  const event = createEvent.keyDown(input(), { key: 'Escape', bubbles: true, cancelable: true, ...options });
  fireEvent(input(), event);
  return event;
};

test('#663 Escape sends only ESC through the current writer, preserves draft and cannot close the parent', async () => {
  const api = wire(); const parentKeyDown = vi.fn();
  renderUi(<div onKeyDown={parentKeyDown}><HybridWorkspace session={admin} target="p" /></div>);
  await ready();
  const event = escape();
  expect(event.defaultPrevented).toBe(true);
  expect(parentKeyDown).not.toHaveBeenCalled();
  await waitFor(() => expect(api.posts.filter(([url]) => url.endsWith('/input'))).toEqual([
    ['/api/puppet/p/term/writer/input', { seq: 1, data: 'Gw==' }],
  ]));
  expect(input()).toHaveValue('draft');
  expect(api.posts.some(([url]) => url.includes('/admin/ops') || url.includes('/send'))).toBe(false);
  expect(api.posts.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
  fireEvent.change(input(), { target: { value: '' } });
  escape();
  await waitFor(() => expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(2));
  expect(api.posts.filter(([url]) => url.endsWith('/input'))[1][1]).toEqual({ seq: 2, data: 'Gw==' });
  expect(input()).toHaveValue('');
});

test('#663 Escape without a confirmed writer consumes the key but sends nothing', async () => {
  vi.useFakeTimers(); const api = wire(); api.setSource('initialization'); open();
  await act(async () => { await vi.advanceTimersByTimeAsync(0); });
  fireEvent.change(input(), { target: { value: 'keep draft' } });
  expect(escape().defaultPrevented).toBe(true);
  await act(async () => {});
  expect(api.posts).toHaveLength(0);
  expect(input()).toHaveValue('keep draft');
});

test.each([
  { repeat: true }, { isComposing: true }, { keyCode: 229 },
])('#663 Escape ignores repeat and unconfirmed IME input: %j', async options => {
  const api = wire(); open(); await ready();
  expect(escape(options).defaultPrevented).toBe(true);
  await act(async () => {});
  expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(0);
  expect(input()).toHaveValue('draft');
});

test.each(['written', 'unknown'])('#663 queued Escape never interleaves paste chunks or replays an uncertain send: %s', async outcome => {
  const api = wire(); open(); await ready();
  const original = api.mock.getMockImplementation()!;
  let release!: (response: Response) => void;
  let firstBody!: Record<string, unknown>;
  api.mock.mockImplementation((url: string, init?: RequestInit) => {
    if (url.endsWith('/input') && !firstBody) {
      firstBody = JSON.parse(String(init?.body));
      api.posts.push([url, firstBody]);
      return new Promise<Response>(resolve => { release = resolve; });
    }
    return original(url, init);
  });
  fireEvent.change(input(), { target: { value: 'x'.repeat(5000) } });
  fireEvent.keyDown(input(), { key: 'Enter' });
  await waitFor(() => expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(1));
  fireEvent.change(input(), { target: { value: 'next draft' } });
  expect(escape().defaultPrevented).toBe(true);
  await act(async () => {});
  expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(1);
  await act(async () => { release(ok({ seq: firstBody.seq, written: atob(String(firstBody.data)).length, outcome, error: outcome === 'unknown' ? 'uncertain' : undefined })); });
  const writes = api.posts.filter(([url]) => url.endsWith('/input')).map(([, body]) => body);
  if (outcome === 'written') {
    expect(writes.map(body => body.seq)).toEqual([1, 2, 3]);
    expect(writes.slice(0, 2).map(body => atob(String(body.data))).join('')).toBe(`\x1b[200~${'x'.repeat(5000)}\x1b[201~\r`);
    expect(writes[2].data).toBe('Gw==');
  } else {
    expect(writes).toHaveLength(1);
    expect(screen.getByRole('alert')).toHaveTextContent('uncertain');
    escape();
    await act(async () => {});
    expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(1);
    expect(api.posts.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
  }
  expect(input()).toHaveValue('next draft');
});

test('#663 uncertain Escape keeps draft and error visible without retrying', async () => {
  const api = wire(); open(); await ready(); api.setOutcome('unknown');
  escape();
  expect(await screen.findByRole('alert')).toHaveTextContent('uncertain');
  expect(input()).toHaveValue('draft');
  expect(send()).toBeDisabled();
  escape();
  await act(async () => {});
  expect(api.posts.filter(([url]) => url.endsWith('/input'))).toHaveLength(1);
  expect(api.posts.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
});

test('#661 transcript and dialog use the available width without a reading-column cap', () => {
  const css = readFileSync('src/components/hybridWorkspace.css', 'utf8');
  const transcript = css.match(/\.hybrid-transcript\s*\{([^}]+)\}/)?.[1] ?? '';
  expect(transcript).not.toMatch(/max-width:\s*76ch/);
  expect(transcript).toMatch(/width:\s*100%/);
  const card = readFileSync('src/components/PuppetCard.tsx', 'utf8');
  expect(card).not.toContain('min(1000px,');
  expect(card).toContain('xOffset={16}');
});
