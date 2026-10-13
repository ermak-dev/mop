// HYPOTHESIS: read и input имеют разные очереди; неизвестный input нельзя
// повторять, а поздние callback не должны писать в следующую цель.
// SOLUTION: поколения attach, очередь seq и явный reconnect без replay.
// RESULT: RED до реализации; STATUS: FIXED — see #601
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { useHybridTerminal } from './useHybridTerminal';
import { encodeInput } from './hybridProtocol';
const admin = { login: 'anton', role: 'admin' as const };
const ok = (body: unknown) => ({ ok: true, status: 200, json: async () => body }) as Response;
const attach = (mode: string, tid = 't1') => ({ tid, epoch: 'e1', cursor: 0, mode, cols: 80, rows: 24 });
const sink = () => ({ write: vi.fn(), reset: vi.fn() });
const body = (init?: RequestInit) => JSON.parse(String(init?.body ?? '{}'));
afterEach(() => { vi.unstubAllGlobals(); vi.restoreAllMocks(); });
test('reader is explicit; control closes only own reader before opening writer, cookie JSON only', async () => {
  const calls: Array<[string, RequestInit | undefined]> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    calls.push([url, init]);
    if (url.endsWith('/read?cursor=0&wait=15')) return new Promise(() => {});
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach(body(init).mode)));
  }));
  const view = renderHook(() => useHybridTerminal(admin, 'pu-mop-1', sink()));
  expect(calls).toHaveLength(0);
  await act(async () => { await view.result.current.open('ro'); });
  expect(view.result.current.mode).toBe('ro');
  expect(view.result.current.canInput).toBe(false);
  await act(async () => { await view.result.current.resize(100, 30); await view.result.current.send(encodeInput('x')); });
  expect(calls.some(([url]) => url.endsWith('/resize') || url.endsWith('/input'))).toBe(false);
  await act(async () => { await view.result.current.open('rw'); });
  expect(calls.filter(([, init]) => init?.method === 'POST').map(([url]) => url)).toEqual(['/api/puppet/pu-mop-1/term', '/api/puppet/pu-mop-1/term/t1/close', '/api/puppet/pu-mop-1/term']);
  expect(view.result.current.canInput).toBe(true);
  for (const [, init] of calls) {
    expect(init?.credentials).toBe('same-origin');
    if (init?.method === 'POST') expect(init.headers).toEqual({ 'Content-Type': 'application/json' });
  }
  expect(body(calls[0][1])).toEqual({ mode: 'ro', cols: 80, rows: 24 });
  view.unmount();
});
test('Stop/input does not wait for long read and seq is serialized; unknown disables all later input without replay', async () => {
  let finish!: (response: Response) => void;
  const inputs: Array<{ seq: number; data: string }> = [];
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/input')) { inputs.push(body(init)); return new Promise(resolve => { finish = resolve; }); }
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach('rw')));
  }));
  const view = renderHook(() => useHybridTerminal(admin, 'pu-mop-1', sink()));
  await act(async () => { await view.result.current.open('rw'); });
  let first!: Promise<boolean>, next!: Promise<boolean>;
  await act(async () => { first = view.result.current.send(encodeInput('\x1b')); next = view.result.current.send(encodeInput('x')); });
  expect(inputs).toEqual([{ seq: 1, data: 'Gw==' }]);
  await act(async () => { finish(ok({ seq: 1, written: 0, outcome: 'unknown', error: 'write uncertain' })); await first; await next; });
  expect(inputs).toHaveLength(1);
  expect(view.result.current.canInput).toBe(false);
  expect(view.result.current.error).toContain('write uncertain');
  await act(async () => { await view.result.current.send(encodeInput('later')); });
  expect(inputs).toHaveLength(1);
  view.unmount();
});
test.each([{ reset: true }, { epoch: 'e2' }, { closed: true, exit: 1 }])('reset/epoch/close disconnects writer, never auto opens another', async (change) => {
  const output = sink();
  let read!: (response: Response) => void;
  const mock = vi.fn((url: string) => url.includes('/read?') ? new Promise(resolve => { read = resolve; }) : Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach('rw'))));
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'pu-mop-1', output));
  await act(async () => { await view.result.current.open('rw'); });
  await act(async () => { read(ok({ data: btoa('old frame'), cursor: 10, reset: false, closed: false, exit: null, epoch: 'e1', ...change })); });
  expect(view.result.current.canInput).toBe(false);
  expect(output.write).not.toHaveBeenCalled();
  expect(mock.mock.calls.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
  view.unmount();
});
test('late open after target change closes stale attach, never displays it or writes', async () => {
  let release!: (response: Response) => void;
  const output = sink();
  const mock = vi.fn((url: string) => url.endsWith('/close') ? Promise.resolve(ok({ ok: true })) : new Promise(resolve => { release = resolve; }));
  vi.stubGlobal('fetch', mock);
  const view = renderHook(({ target }) => useHybridTerminal(admin, target, output), { initialProps: { target: 'old' } });
  let pending!: Promise<void>;
  await act(async () => { pending = view.result.current.open('rw'); });
  view.rerender({ target: 'new' });
  await act(async () => { release(ok(attach('rw'))); await pending; });
  expect(view.result.current.mode).toBeNull();
  expect(mock).toHaveBeenCalledWith('/api/puppet/old/term/t1/close', expect.anything());
  expect(output.write).not.toHaveBeenCalled();
  view.unmount();
});
test('input transport failure is unknown and is never automatically replayed', async () => {
  const mock = vi.fn((url: string) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/input')) return Promise.reject(new Error('offline'));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach('rw')));
  });
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'pu-mop-1', sink()));
  await act(async () => { await view.result.current.open('rw'); await view.result.current.send(encodeInput('x')); });
  expect(view.result.current.phase).toBe('unknown');
  expect(view.result.current.canInput).toBe(false);
  expect(mock.mock.calls.filter(([url]) => url.endsWith('/input'))).toHaveLength(1);
  view.unmount();
});
test.each(['input', 'resize', 'open'])('%s authorization refusal removes private output and disables control', async (action) => {
  const output = sink();
  let expired = false;
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (expired && (url.endsWith('/input') || url.endsWith('/resize') || url.endsWith('/term'))) return Promise.resolve({ ok: false, status: 401, json: async () => ({ error: 'session expired' }) });
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach('rw')));
  }));
  const view = renderHook(() => useHybridTerminal(admin, 'p', output));
  await act(async () => { await view.result.current.open('rw'); });
  const resets = output.reset.mock.calls.length;
  expired = true;
  await act(async () => {
    if (action === 'resize') await view.result.current.resize(100, 30);
    else if (action === 'open') await view.result.current.open('rw');
    else await view.result.current.send(encodeInput('x'));
  });
  expect(view.result.current.canInput).toBe(false);
  expect(output.reset.mock.calls.length).toBeGreaterThan(resets);
  view.unmount();
});
test('late close refusal cannot disconnect a newly opened attach in the same target', async () => {
  let read!: (response: Response) => void;
  let refuse!: (response: Response) => void;
  let opens = 0, closes = 0;
  vi.stubGlobal('fetch', vi.fn((url: string) => {
    if (url.includes('/read?')) return new Promise(resolve => { read = resolve; });
    if (url.endsWith('/close')) { closes++; return closes === 1 ? new Promise(resolve => { refuse = resolve; }) : Promise.resolve(ok({ ok: true })); }
    return Promise.resolve(ok(attach('rw', `t${++opens}`)));
  }));
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('rw'); });
  await act(async () => { read(ok({ data: '', cursor: 0, reset: true, closed: false, exit: null, epoch: 'e1' })); });
  await act(async () => { await view.result.current.open('rw'); });
  expect(view.result.current.canInput).toBe(true);
  await act(async () => { refuse(ok({ ok: false, error: 'old close refused' })); });
  expect(view.result.current.canInput).toBe(true);
  view.unmount();
});
test.each(['open', 'close'])('failed %s retains own tid for explicit reconnect, never steals an unrelated writer', async (action) => {
  const closes: string[] = [];
  let failure = true;
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/close')) { closes.push(url); return Promise.resolve(ok({ ok: !failure, error: failure ? 'close refused' : undefined })); }
    return Promise.resolve(ok(attach(body(init).mode)));
  }));
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('ro'); if (action === 'close') await view.result.current.close(); else await view.result.current.open('rw'); });
  expect(view.result.current.canInput).toBe(false);
  failure = false;
  await act(async () => { await view.result.current.open('rw'); });
  expect(closes).toHaveLength(2);
  expect(closes.every(url => url.endsWith('/t1/close'))).toBe(true);
  expect(view.result.current.canInput).toBe(true);
  view.unmount();
});
test('logout aborts read, clears private terminal and closes only attach', async () => {
  let signal: AbortSignal | null | undefined;
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) { signal = init?.signal; return new Promise(() => {}); }
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach('rw')));
  }));
  const output = sink();
  const view = renderHook(({ session }) => useHybridTerminal(session, 'pu-mop-1', output), { initialProps: { session: admin as { login: string | null; role: 'admin' | null } } });
  await act(async () => { await view.result.current.open('rw'); });
  view.rerender({ session: { login: null, role: null } });
  await waitFor(() => expect(view.result.current.mode).toBeNull());
  expect(signal?.aborted).toBe(true);
  expect(output.reset).toHaveBeenCalled();
  expect(view.result.current.canInput).toBe(false);
  view.unmount();
});
