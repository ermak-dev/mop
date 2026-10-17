// HYPOTHESIS: приватная карточка обходила scope/store отдельным GET старого API.
// SOLUTION: проверяем общий runtime с новым DTO и произвольным порядком тела ответа.
// RESULT: цель/контекст/права/ошибки и поздние ответы проверяются без legacy wire.
// STATUS: FIXED — see #627
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { usePuppetCard } from './usePuppetCard';
import { usePool } from './usePool';
import { ADMIN, ID, EPOCH, deferred, networkFixture, puppet, reply } from './network-test-utils';
let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); });
afterEach(() => { net.stop(); vi.unstubAllGlobals(); });

test('no target triggers no detail request', async () => {
  await net.start(); const before = net.http.mock.calls.length;
  renderHook(() => usePuppetCard(ADMIN, null)); expect(net.http.mock.calls).toHaveLength(before);
});
test('guest or another credential generation never sees cached private facts', async () => {
  await net.start();
  for (const session of [{ login: null, role: null }, { ...ADMIN, generation: 'different' }] as const) {
    const before = net.http.mock.calls.length, view = renderHook(() => usePuppetCard(session, ID));
    expect(view.result.current.card).toBeNull(); expect(net.http.mock.calls).toHaveLength(before);
    view.unmount();
  }
});
test('detail and roster share one entity; same target rerender causes no extra detail', async () => {
  await net.start(); const view = renderHook(() => ({ card: usePuppetCard(ADMIN, ID), pool: usePool(ADMIN) }));
  await waitFor(() => expect(net.http.mock.calls.some(([url]) => url === `/api/v1/puppets/${ID}`)).toBe(true));
  expect(view.result.current.card.card).toEqual(view.result.current.pool.snapshot?.projects[0].puppets[0]);
  const before = net.http.mock.calls.filter(([url]) => url.includes('/puppets/')).length;
  view.rerender(); expect(net.http.mock.calls.filter(([url]) => url.includes('/puppets/'))).toHaveLength(before);
});
test.each([404, 502])('HTTP %s is an explicit detail error, not a fabricated empty card', async status => {
  await net.start(); const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.includes('/puppets/') ? Promise.resolve(reply({ error: { code: 'unavailable', message: 'detail unavailable' } }, status)) : base(url, init));
  const view = renderHook(() => usePuppetCard(ADMIN, ID));
  await waitFor(() => expect(view.result.current.error).toBe(`HTTP ${status}: detail unavailable`)); expect(view.result.current.card).toBeNull();
});
test.each([401, 403])('HTTP %s revokes private rows, not just the current detail', async status => {
  await net.start(); const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.includes('/puppets/') ? Promise.resolve(reply({ error: { code: 'permission_denied', message: 'access revoked' } }, status)) : base(url, init));
  const view = renderHook(() => ({ card: usePuppetCard(ADMIN, ID), pool: usePool(ADMIN) }));
  await waitFor(() => expect(view.result.current.pool.snapshot).toBeNull()); expect(view.result.current.card.card).toBeNull();
});
test('late former-target response cannot replace the newly selected card', async () => {
  await net.start(); const slow = deferred(), other = '00000000-0000-4000-8000-000000000006', base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url === `/api/v1/puppets/${ID}` ? slow.promise
    : url === `/api/v1/puppets/${other}` ? Promise.resolve(reply({ schema_version: 1, epoch: EPOCH, puppet: { ...puppet(other), desired: { revision: '8', owner_id: 'alice' } } })) : base(url, init));
  const view = renderHook(({ target }) => usePuppetCard(ADMIN, target), { initialProps: { target: ID } });
  view.rerender({ target: other }); await waitFor(() => expect(view.result.current.card?.owner).toBe('alice'));
  await act(async () => slow.resolve(reply({ schema_version: 1, epoch: EPOCH, puppet: puppet() })));
  expect(view.result.current.card?.name).toBe(other); expect(view.result.current.card?.owner).toBe('alice');
});
test.each(['logout', 'generation', 'role'])('%s clears the shown card synchronously and late JSON cannot resurrect it', async change => {
  await net.start(); const slow = deferred(), base = net.http.getMockImplementation()!;
  // Headers могут уже прийти: задерживаем само JSON-тело, не только fetch.
  net.http.mockImplementation((url, init) => url.includes('/puppets/') ? Promise.resolve({ ok: true, json: () => slow.promise.then(r => r.json()) } as Response) : base(url, init));
  const view = renderHook(({ session }) => ({ pool: usePool(session), card: usePuppetCard(session, ID) }), { initialProps: { session: ADMIN } });
  await waitFor(() => expect(view.result.current.card.card).not.toBeNull());
  view.rerender({ session: change === 'logout' ? { login: null, role: null } : change === 'generation' ? { ...ADMIN, generation: 'new' } : { ...ADMIN, role: 'user' } });
  expect(view.result.current.card.card).toBeNull();
  await act(async () => slow.resolve(reply({ schema_version: 1, epoch: EPOCH, puppet: puppet() })));
  expect(view.result.current.card.card).toBeNull();
});
