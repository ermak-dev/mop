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
afterEach(() => { vi.useRealTimers(); vi.unstubAllGlobals(); vi.restoreAllMocks(); });
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
    if (init?.method === 'POST') expect(init.headers).toEqual({ 'Content-Type': 'application/json', 'X-CSRFToken': 'fixtureSessionCsrfToken0123456789' });
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
// HYPOTHESIS: invalidate(true) не отменяет поколение pending open без attach,
// поэтому поздний writer включает ввод уже после смены сессии (#661).
// SOLUTION: pending open тоже проходит через disconnect и отмену поколения;
// поздний tid закрывается существующим cleanup, invalidate(false) не меняется.
// RESULT: проверка красная до правки; поздний writer больше не включает ввод.
// STATUS: FIXED — see #661
test.each([true, false])('pending writer open respects sessionChanged=%s invalidation', async sessionChanged => {
  let release!: (response: Response) => void;
  const output = sink();
  const mock = vi.fn((url: string) => {
    if (url.endsWith('/term')) return new Promise<Response>(resolve => { release = resolve; });
    if (url.endsWith('/close')) return Promise.resolve(ok({ ok: true }));
    return new Promise<Response>(() => {});
  });
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'p', output));
  let pending!: Promise<void>;
  await act(async () => { pending = view.result.current.open('rw'); });
  expect(view.result.current.phase).toBe('opening');
  act(() => { view.result.current.invalidate(sessionChanged); });
  await act(async () => { release(ok(attach('rw', 'late'))); await pending; });
  expect(view.result.current.canInput).toBe(!sessionChanged);
  expect(view.result.current.mode).toBe(sessionChanged ? null : 'rw');
  expect(view.result.current.phase).toBe(sessionChanged ? 'disconnected' : 'connected');
  expect(mock.mock.calls.filter(([url]) => url.endsWith('/late/close'))).toHaveLength(sessionChanged ? 1 : 0);
  expect(mock.mock.calls.filter(([url]) => url.includes('/read?'))).toHaveLength(sessionChanged ? 0 : 1);
  if (sessionChanged) {
    await act(async () => { expect(await view.result.current.send(encodeInput('must not send'))).toBe(false); });
    expect(mock.mock.calls.some(([url]) => url.endsWith('/input'))).toBe(false);
  }
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
// Изменённый контракт: транспортная неопределённость сохраняет кандидата,
// но ok:false означает уже отсоединённую серверную binding, не retry старого tid.
test.each(['open', 'close'])('network failure during %s retains uncertain own tid for explicit cleanup, never steals an unrelated writer', async (action) => {
  const closes: string[] = [];
  let failure = true;
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/close')) { closes.push(url); return failure ? Promise.reject(new Error('offline; close unknown')) : Promise.resolve(ok({ ok: true })); }
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
// HYPOTHESIS: удалённая binding не оживает от повторного close; ro retry
// ошибочно считает окончательный отказ временным. STATUS: FIXED — see #601
const http = (status: number, error: string) => ({ ok: false, status, json: async () => ({ error }) }) as Response;
test.each([http(404, 'attach not found'), http(410, 'expired'), ok({ ok: false, error: 'already closed' })])('definite detached close permits manual writer reopen without old close loop %j', async response => {
  let opens = 0;
  const mock = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/close')) return Promise.resolve(response);
    return Promise.resolve(ok(attach(body(init).mode, `t${++opens}`)));
  });
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('ro'); await view.result.current.close(); await view.result.current.open('rw'); });
  expect(view.result.current.canInput).toBe(true);
  expect(mock.mock.calls.filter(([url]) => url.endsWith('/t1/close'))).toHaveLength(1);
  view.unmount();
});
test('detached close reports node cleanup refusal without retaining dead binding', async () => {
  const mock = vi.fn((url: string) => url.includes('/read?') ? new Promise(() => {}) : Promise.resolve(ok(url.endsWith('/close') ? { ok: false, error: 'node cleanup refused' } : attach('ro'))));
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('ro'); await view.result.current.close(); });
  expect(view.result.current.error).toContain('node cleanup refused');
  await act(async () => { await view.result.current.open('ro'); });
  expect(mock.mock.calls.filter(([url]) => url.endsWith('/close'))).toHaveLength(1);
  view.unmount();
});
test.each([404, 410, 409])('reader definitive HTTP %s disconnects, never retries or auto opens', async status => {
  vi.useFakeTimers();
  const mock = vi.fn((url: string, init?: RequestInit) => url.includes('/read?') ? Promise.resolve(http(status, 'epoch mismatch')) : Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach(body(init).mode))));
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('ro'); });
  expect(view.result.current.phase).toBe('disconnected');
  await act(async () => { await vi.advanceTimersByTimeAsync(10000); });
  expect(mock.mock.calls.filter(([url]) => url.includes('/read?'))).toHaveLength(1);
  view.unmount(); vi.useRealTimers();
});
test('refused writer releases attach and stops read heartbeat until explicit reopen', async () => {
  const mock = vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/input')) return Promise.resolve(ok({ seq: body(init).seq, written: 0, outcome: 'refused', error: 'writer revoked' }));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach('rw')));
  });
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('rw'); await view.result.current.send(encodeInput('x')); });
  expect(view.result.current.canInput).toBe(false);
  expect(mock.mock.calls.some(([url]) => url.endsWith('/close'))).toBe(true);
  view.unmount();
});
test.each(['read already in progress', 'network', 'server unavailable'])('transient reader %s retries only bounded reads, never takes writer', async reason => {
  vi.useFakeTimers();
  const mock = vi.fn((url: string) => {
    if (url.includes('/read?')) return reason === 'network' ? Promise.reject(new Error('offline')) : Promise.resolve(http(reason === 'read already in progress' ? 409 : 503, reason));
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : attach('ro')));
  });
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('ro'); await vi.advanceTimersByTimeAsync(6000); });
  expect(view.result.current.phase).toBe('disconnected');
  expect(mock.mock.calls.filter(([url]) => url.includes('/read?'))).toHaveLength(3);
  expect(mock.mock.calls.filter(([url]) => url.endsWith('/term'))).toHaveLength(1);
  view.unmount();
});
test.each([undefined, -1, 10101, 1.5])('unknown or invalid timing %s never sends combined input', async delay => {
  const mock = vi.fn((url: string) => url.includes('/read?') ? new Promise(() => {}) : Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { ...attach('rw'), interrupt_delay_ms: delay })));
  vi.stubGlobal('fetch', mock);
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('rw'); });
  expect(view.result.current.canInterruptSend).toBe(false);
  await act(async () => { expect(await view.result.current.send(encodeInput('prompt'), true)).toBe(false); });
  expect(mock.mock.calls.some(([url]) => url.endsWith('/input'))).toBe(false);
  expect(view.result.current.canInput).toBe(true);
  view.unmount();
});
test('combined action uses separate Escape POST and known delay before next seq; unknown Escape stops paste', async () => {
  vi.useFakeTimers();
  const inputs: Array<{ seq: number; data: string }> = [];
  let outcome = 'written';
  vi.stubGlobal('fetch', vi.fn((url: string, init?: RequestInit) => {
    if (url.includes('/read?')) return new Promise(() => {});
    if (url.endsWith('/input')) { const value = body(init); inputs.push(value); return Promise.resolve(ok({ seq: value.seq, written: atob(value.data).length, outcome })); }
    return Promise.resolve(ok(url.endsWith('/close') ? { ok: true } : { ...attach('rw'), interrupt_delay_ms: 350 }));
  }));
  const view = renderHook(() => useHybridTerminal(admin, 'p', sink()));
  await act(async () => { await view.result.current.open('rw'); });
  let pending!: Promise<boolean>;
  await act(async () => { pending = view.result.current.send(encodeInput('\x1b[200~hello\x1b[201~\r'), true); });
  expect(inputs).toEqual([{ seq: 1, data: 'Gw==' }]);
  await act(async () => { await vi.advanceTimersByTimeAsync(349); });
  expect(inputs).toHaveLength(1);
  await act(async () => { await vi.advanceTimersByTimeAsync(1); await pending; });
  expect(inputs.map(value => value.seq)).toEqual([1, 2]);
  expect(atob(inputs[1].data)).toBe('\x1b[200~hello\x1b[201~\r');
  outcome = 'unknown';
  await act(async () => { pending = view.result.current.send(encodeInput('must not paste'), true); });
  await act(async () => { await vi.advanceTimersByTimeAsync(1000); await pending; });
  expect(inputs).toHaveLength(3);
  expect(atob(inputs[2].data)).toBe('\x1b');
  view.unmount(); vi.useRealTimers();
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
