// HYPOTHESIS: opaque cursor принадлежит выбранной сессии, а не карточке.
// SOLUTION: раздельное поколение чтения истории, reset и очистка при logout.
// RESULT: RED до реализации; STATUS: FIXED — see #601
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { useHybridHistory } from './useHybridHistory';
const admin = { login: 'anton', role: 'admin' as const };
const page = (changes = {}) => ({ epoch: 'e1', session_id: 's1', cursor: 'a+/=', records: [{ uuid: 'u', type: 'user', message: { content: 'one' } }], reset: false, has_more: false, partial: true, ...changes });
const ok = (value: unknown) => ({ ok: true, status: 200, json: async () => value }) as Response;
afterEach(() => vi.unstubAllGlobals());
test('history explicit enable loads raw records then uses encoded opaque cursor without fields of identity', async () => {
  const mock = vi.fn().mockResolvedValueOnce(ok(page())).mockResolvedValue(ok(page({ cursor: 'b', records: [{ uuid: 'v', type: 'system' }] })));
  vi.stubGlobal('fetch', mock);
  const view = renderHook(({ enabled }) => useHybridHistory(admin, 'pu 1', enabled), { initialProps: { enabled: false } });
  expect(mock).not.toHaveBeenCalled();
  view.rerender({ enabled: true });
  await waitFor(() => expect(view.result.current.records).toHaveLength(1));
  expect(mock).toHaveBeenCalledWith('/api/puppet/pu%201/history', expect.objectContaining({ credentials: 'same-origin' }));
  await act(async () => { await view.result.current.load(); });
  expect(mock).toHaveBeenCalledWith('/api/puppet/pu%201/history?cursor=a%2B%2F%3D', expect.anything());
  expect(view.result.current.records).toHaveLength(2);
  expect(view.result.current.partial).toBe(true);
  view.unmount();
});
test('target changes fence body decode and clear history immediately; 403 clears private records', async () => {
  let body!: (value: unknown) => void;
  const mock = vi.fn().mockResolvedValueOnce({ ok: true, json: () => new Promise(resolve => { body = resolve; }) })
    .mockResolvedValueOnce(ok(page({ records: [{ type: 'assistant', message: { content: 'new' } }] })))
    .mockResolvedValue({ ok: false, status: 403, json: async () => ({ error: 'denied' }) });
  vi.stubGlobal('fetch', mock);
  const view = renderHook(({ target }) => useHybridHistory(admin, target, true), { initialProps: { target: 'old' } });
  await waitFor(() => expect(mock).toHaveBeenCalled());
  view.rerender({ target: 'new' });
  await waitFor(() => expect(view.result.current.records).toHaveLength(1));
  await act(async () => { body(page()); });
  expect(view.result.current.records[0].type).toBe('assistant');
  await act(async () => { await view.result.current.load(); });
  expect(view.result.current.records).toEqual([]);
  expect(view.result.current.error).toContain('denied');
  const count = mock.mock.calls.length;
  await act(async () => { await view.result.current.load(); });
  expect(mock).toHaveBeenCalledTimes(count);
  view.unmount();
});
test('invalid page is a visible error rather than a fabricated empty conversation', async () => {
  vi.stubGlobal('fetch', vi.fn(async () => ok({ lines: ['legacy screen'] })));
  const view = renderHook(() => useHybridHistory(admin, 'p', true));
  await waitFor(() => expect(view.result.current.error).toContain('invalid history'));
  expect(view.result.current.records).toEqual([]);
  view.unmount();
});
