// HYPOTHESIS: прежние проверки обходили общий store и подтверждали только старый wire.
// SOLUTION: HTTP/WS проходят production runtime; неизвестность не выдаётся за отказ/успех.
// RESULT: отсутствующие send/add остаются отдельными красными проверками ниже.
import { act, renderHook, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { useAdminOp } from './useAdminOp';
import { ADMIN, ID, EPOCH, INCARNATION, OP, deferred, networkFixture, reply } from './network-test-utils';
let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); });
afterEach(() => { net.stop(); vi.unstubAllGlobals(); });
const mount = () => renderHook(() => useAdminOp(ADMIN, ID));

test('no command rides until submit', async () => {
  await net.start(); mount(); expect(net.posts()).toHaveLength(0);
});
test('restart uses assignment CAS and WS invalidation fetches the confirmed verdict', async () => {
  await net.start(); const view = mount();
  await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op).toMatchObject({ status: 'running', id: OP, error: null });
  expect(net.posts()).toHaveLength(1);
  const [url, init] = net.posts()[0];
  expect(url).toBe(`/api/v1/puppets/${ID}/commands/restart`);
  expect(init).toMatchObject({ credentials: 'same-origin' });
  expect(JSON.parse(String(init?.body))).toEqual({ schema_version: 1, epoch: EPOCH, incarnation: INCARNATION,
    expected_revision: '7', request_id: net.requestId, not_after: expect.any(String), params: { fresh: false } });
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/operations') ? Promise.resolve(reply(net.operation('succeeded', '3'))) : base(url, init));
  await net.invalidateOperation();
  await waitFor(() => expect(view.result.current.op.status).toBe('succeeded'));
  expect(net.posts()).toHaveLength(1);
});
test('double submit while POST is outstanding and after accepted never repeats the effect', async () => {
  await net.start(); const slow = deferred(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => { if (init?.method === 'POST') { net.capture(init); return slow.promise; } return base(url, init); });
  const view = mount(); let first!: Promise<string | null>, second!: Promise<string | null>;
  act(() => { first = view.result.current.submit(); second = view.result.current.submit(); });
  await act(async () => slow.resolve(reply(net.operation('accepted', '1'), 202)));
  expect(await first).toBe(OP); expect(await second).toBe(OP);
  await act(async () => { expect(await view.result.current.submit()).toBe(OP); });
  expect(net.posts()).toHaveLength(1);
});
test.each(['unknown', 'failed', 'refused', 'partial', 'expired'])('server %s is preserved with its reason', async status => {
  await net.start(); const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/operations') ? Promise.resolve(reply(net.operation(status, '2', 'server reason'))) : base(url, init));
  const view = mount(); await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op).toMatchObject({ status, error: 'server reason', lost: false });
});
test('missing or unreachable result does not turn accepted into failure or fabricated unknown', async () => {
  await net.start(); const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/operations') ? Promise.resolve(reply({ error: { code: 'not_found', message: 'not visible' } }, 404)) : base(url, init));
  const view = mount(); await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op.status).toBe('accepted');
  expect(net.posts()).toHaveLength(1);
});
test.each(['dropped', 'malformed'])('lost %s first response is unknown; repeat click recovers only by request_id GET', async mode => {
  await net.start(); const base = net.http.getMockImplementation()!;
  net.http.mockImplementation(async (url, init) => {
    if (init?.method === 'POST') { net.capture(init); if (mode === 'dropped') throw new Error('connection reset'); return reply({ schema_version: 1, epoch: EPOCH }, 202); }
    if (url.startsWith('/api/v1/operations')) return reply({ error: { code: 'not_found', message: 'not visible' } }, 404);
    return base(url, init);
  });
  const view = mount(); await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op).toMatchObject({ status: 'unknown', lost: true, id: null });
  await act(async () => { await view.result.current.submit(); });
  expect(net.posts()).toHaveLength(1);
  expect(net.http.mock.calls.some(([url]) => url === `/api/v1/operations?request_id=${net.requestId}&epoch=${EPOCH}`)).toBe(true);
});
test.each([403, 409])('POST %s preserves safe refusal reason; explicit retry is a new attempt', async status => {
  await net.start(); const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => init?.method === 'POST' ? Promise.resolve(reply({ error: { code: 'conflict', message: 'another operation queued' } }, status))
    : url.startsWith('/api/v1/operations') ? Promise.resolve(reply({ error: { code: 'not_found', message: 'not found' } }, 404)) : base(url, init));
  const view = mount(); await act(async () => { await view.result.current.submit(); });
  expect(view.result.current.op).toMatchObject({ status: 'refused', error: `HTTP ${status}: another operation queued`, lost: false });
  await act(async () => { await view.result.current.submit(); }); expect(net.posts()).toHaveLength(2);
});
test.each(['target', 'logout'])('late acceptance after %s never resurrects the selected operation', async change => {
  await net.start(); const slow = deferred(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => { if (init?.method === 'POST') { net.capture(init); return slow.promise; } return base(url, init); });
  const view = renderHook(({ session, target }) => useAdminOp(session, target), { initialProps: { session: ADMIN, target: ID } });
  act(() => { void view.result.current.submit(); });
  view.rerender({ session: change === 'logout' ? { login: null, role: null } : ADMIN, target: change === 'target' ? 'other' : ID });
  expect(view.result.current.op.status).toBe('idle');
  await act(async () => slow.resolve(reply(net.operation('accepted', '1'), 202)));
  expect(view.result.current.op.status).toBe('idle');
});
test('guest cannot POST and command data never enters browser storage', async () => {
  const guest = renderHook(() => useAdminOp({ login: null, role: null }, ID));
  await act(async () => { await guest.result.current.submit(); }); expect(net.http).not.toHaveBeenCalled();
  await net.start(); const view = mount(); await act(async () => { await view.result.current.submit(); });
  expect(localStorage.length).toBe(0); expect(sessionStorage.length).toBe(0);
});
// Пробелы адаптеров не маскируются fake endpoints: эти пользовательские действия
// должны работать после настоящего контракта #625, а не считаться unavailable-as-correct.
test('#469 send passes its message through the controller command adapter', async () => {
  await net.start(); const view = mount(); await act(async () => { await view.result.current.submit('send', { message: 'hello' }); });
  expect(net.posts()).toHaveLength(1);
  expect(JSON.parse(String(net.posts()[0][1]?.body)).params).toEqual({ message: 'hello' });
});
test('#472 add without target reaches the controller command adapter', async () => {
  await net.start(); const view = renderHook(() => useAdminOp(ADMIN, null));
  await act(async () => { await view.result.current.submit('add', { origin: 'git@h:g/mop.git' }); });
  expect(net.posts()).toHaveLength(1);
});
