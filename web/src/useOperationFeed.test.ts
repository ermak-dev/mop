// HYPOTHESIS: SSE и синтетический deadline создавали второго владельца verdict.
// SOLUTION: проверяем доставку только после применения REST/WS в общем store.
// RESULT: дедуп/ownership/cleanup сохранены; deadline не выдумывает доменный исход.
// STATUS: FIXED — see #627
import { act } from '@testing-library/react';
import { afterEach, beforeEach, expect, test, vi } from 'vitest';
import { poolNetwork } from './poolNetwork';
import { operations, type OperationFrame } from './useOperationFeed';
import { ID, OP, networkFixture, reply } from './network-test-utils';
let net: ReturnType<typeof networkFixture>;
beforeEach(() => { net = networkFixture(); });
afterEach(() => { net.stop(); operations.resetForTest(); vi.unstubAllGlobals(); vi.useRealTimers(); });

test.each(['succeeded', 'failed', 'refused', 'unknown', 'expired', 'partial'])('confirmed %s reaches subscribers once after application', async status => {
  await net.start(); const got: OperationFrame[] = [];
  const un = operations.subscribe(frame => got.push(frame));
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/operations') ? Promise.resolve(reply(net.operation(status, '3', 'safe reason'))) : base(url, init));
  await act(async () => { await poolNetwork.command(ID, 'restart', {}, crypto.randomUUID()); });
  expect(got).toEqual([{ id: OP, target: ID, action: 'restart', status, error: 'safe reason' }]);
  await net.invalidateOperation('3');
  expect(got).toHaveLength(1);
  un();
});
test('running is not a terminal notification; later WS invalidation fetches verdict', async () => {
  await net.start(); const got: OperationFrame[] = [], un = operations.subscribe(frame => got.push(frame));
  await act(async () => { await poolNetwork.command(ID, 'restart', {}, crypto.randomUUID()); });
  expect(got).toEqual([]);
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/operations') ? Promise.resolve(reply(net.operation('succeeded', '3'))) : base(url, init));
  await net.invalidateOperation(); expect(got.map(frame => frame.status)).toEqual(['succeeded']); un();
});
test('elapsed time never fabricates unknown or failure for an accepted request', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true }); await net.start();
  const got: OperationFrame[] = [], un = operations.subscribe(frame => got.push(frame));
  await act(async () => { await poolNetwork.command(ID, 'restart', {}, crypto.randomUUID()); });
  // Дольше прежнего fallback: recovery читает реальную Operation, но не строит verdict.
  await act(async () => { await vi.advanceTimersByTimeAsync(20 * 60_000); });
  expect(got).toEqual([]); expect(poolNetwork.operation(net.requestId)?.operation?.status).toBe('running');
  expect(net.posts()).toHaveLength(1); un();
});
test('a terminal result stays terminal across reconciliation, without duplicate notifications', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true }); await net.start();
  const got: OperationFrame[] = [], un = operations.subscribe(frame => got.push(frame));
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/operations') ? Promise.resolve(reply(net.operation('succeeded', '3'))) : base(url, init));
  await act(async () => { await poolNetwork.command(ID, 'restart', {}, crypto.randomUUID()); });
  await act(async () => { await vi.advanceTimersByTimeAsync(30_000); });
  expect(got.map(frame => frame.status)).toEqual(['succeeded']); expect(net.posts()).toHaveLength(1); un();
});
test('ownership is marked for the accepted local request, not arbitrary IDs', async () => {
  await net.start(); expect(operations.owns(OP)).toBe(false);
  await act(async () => { await poolNetwork.command(ID, 'restart', {}, crypto.randomUUID()); });
  expect(operations.owns(OP)).toBe(true); expect(operations.owns('foreign')).toBe(false);
});
test('foreign and targetless UI notifications never claim ownership', () => {
  const got: OperationFrame[] = [], un = operations.subscribe(frame => got.push(frame));
  operations.frame({ id: 'foreign', target: null, action: 'add', status: 'succeeded' });
  expect(got).toHaveLength(1); expect(operations.owns('foreign')).toBe(false); un();
});
test('unsubscribe removes listener; clearing a context drops dedup and own IDs', () => {
  const got: OperationFrame[] = [], un = operations.subscribe(frame => got.push(frame));
  operations.accepted(ID, OP); un(); operations.frame({ id: OP, target: ID, action: 'restart', status: 'succeeded' });
  expect(got).toEqual([]); operations.clear(); expect(operations.owns(OP)).toBe(false);
  const again = operations.subscribe(frame => got.push(frame));
  operations.frame({ id: OP, target: ID, action: 'restart', status: 'succeeded' }); expect(got).toHaveLength(1); again();
});
