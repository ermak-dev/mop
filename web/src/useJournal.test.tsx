// HYPOTHESIS: второй fetch/cache useJournal обходит общий context/read generation.
// SPEC: GET-only frozen audit; decimal/UTC DTO, bounded pages, no stale facts on revoke.
// SOLUTION: hook только подписывается на владельца DTO SharedPoolNetwork.
// RESULT: RED → GREEN: общий owner сохраняет DTO и отсекает устаревшие ответы.
// STATUS: FIXED — see #627
import { act, render, renderHook, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import { POLL_S, useJournal } from './useJournal';
import { poolNetwork } from './poolNetwork';
import { ADMIN, deferred, EPOCH, ID, networkFixture, PROJECT, reply } from './network-test-utils';
import { auditError, auditItem, auditPage } from './audit-test-utils';
import type { Session } from './useSession';

function fixture() {
  const net = networkFixture(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? Promise.resolve(reply(auditPage())) : base(url, init));
  return net;
}
const reads = (net: ReturnType<typeof fixture>) => net.http.mock.calls.filter(([url]) => url.startsWith('/api/v1/audit'));
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test('active authenticated readers share one owner, GET transport and exact immutable DTO, no browser storage', async () => {
  const net = fixture(), storage = vi.spyOn(Storage.prototype, 'setItem');
  const a = renderHook(() => useJournal(ADMIN, true));
  const b = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(a.result.current.journal).toEqual([auditItem()]));
  expect(b.result.current.journal).toBe(a.result.current.journal);
  expect(Object.isFrozen(a.result.current.journal[0].target)).toBe(true);
  expect(reads(net)).toHaveLength(1);
  expect(reads(net)[0][0]).toBe('/api/v1/audit?limit=100');
  expect(reads(net)[0][1]).toMatchObject({ cache: 'no-store', credentials: 'same-origin' });
  expect(net.posts()).toHaveLength(0); expect(storage).not.toHaveBeenCalled();
  storage.mockRestore(); a.unmount(); b.unmount();
});

test('guests and closed journal read nothing, any signed-in role defers permission to server', async () => {
  const net = fixture();
  const guest = renderHook(() => useJournal({ login: null, role: null }, true));
  const closed = renderHook(() => useJournal(ADMIN, false));
  expect(reads(net)).toHaveLength(0); expect(guest.result.current.error).toMatch(/unavailable/i);
  const user = renderHook(() => useJournal({ login: '42', role: 'user', authenticated: true }, true));
  await waitFor(() => expect(user.result.current.journal).toHaveLength(1));
  guest.unmount(); closed.unmount(); user.unmount();
});

test('project intersection and limit are encoded once; older pages preserve server order and decimal ids', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => {
    if (url === `/api/v1/audit?limit=2&project_id=${PROJECT}`) return Promise.resolve(reply(auditPage([auditItem('9007199254740994')], 'opaque+/=')));
    if (url === `/api/v1/audit?limit=2&project_id=${PROJECT}&cursor=opaque%2B%2F%3D`) return Promise.resolve(reply(auditPage([auditItem('9007199254740993')])));
    return base(url, init);
  });
  const view = renderHook(() => useJournal(ADMIN, true, { projectId: PROJECT, limit: 2 }));
  await waitFor(() => expect(view.result.current.nextCursor).toBe('opaque+/='));
  await act(async () => view.result.current.more());
  expect(view.result.current.journal.map(row => row.id)).toEqual(['9007199254740994', '9007199254740993']);
  expect(view.result.current.nextCursor).toBeNull(); expect(net.posts()).toHaveLength(0); view.unmount();
});

test('410 cursor expiry clears old pages and restarts GET once without repeating effects', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!; let first = 0;
  net.http.mockImplementation((url, init) => {
    if (url === '/api/v1/audit?limit=100') return Promise.resolve(reply(auditPage([auditItem(String(++first))], first === 1 ? 'expired' : null)));
    if (url.includes('cursor=expired')) return Promise.resolve(reply(auditError('reset_required', 'restart pagination'), 410));
    return base(url, init);
  });
  const view = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(view.result.current.nextCursor).toBe('expired'));
  await act(async () => view.result.current.more());
  await waitFor(() => expect(view.result.current.journal.map(row => row.id)).toEqual(['2']));
  expect(reads(net)).toHaveLength(3); expect(net.posts()).toHaveLength(0); view.unmount();
});

test('a first-page 410 never enters a retry loop and preserves safe reason', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? Promise.resolve(reply(auditError('reset_required', 'authority changed again'), 410)) : base(url, init));
  const view = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(view.result.current.error).toContain('authority changed again'));
  expect(reads(net)).toHaveLength(1); expect(view.result.current.journal).toEqual([]); view.unmount();
});

test('repeated cursor is a visible bounded-pagination failure, never a loop or silent incomplete history', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? Promise.resolve(reply(auditPage([auditItem()], 'same'))) : base(url, init));
  const view = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(view.result.current.nextCursor).toBe('same'));
  await act(async () => view.result.current.more());
  expect(view.result.current.error).toMatch(/pagination/i); expect(view.result.current.journal).toEqual([]);
  expect(reads(net)).toHaveLength(2); view.unmount();
});

test('pagination has a finite 100-page bound with an explicit unavailable reason', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!; let count = 0;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? Promise.resolve(reply(auditPage([auditItem(String(++count))], `c${count}`))) : base(url, init));
  const view = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(view.result.current.nextCursor).toBe('c1'));
  for (let i = 1; i < 100; i++) await act(async () => view.result.current.more());
  expect(view.result.current.error).toMatch(/pagination.*limit/i);
  expect(view.result.current.nextCursor).toBeNull(); expect(reads(net)).toHaveLength(100); view.unmount();
});

test('scope, identity, role and credential changes erase old evidence in the first render, before effects', async () => {
  const net = fixture(); const late = deferred();
  let capture = false; const frames: unknown[] = [];
  function Probe({ session, projectId }: { session: Session; projectId?: string }) {
    const journal = useJournal(session, true, { projectId });
    if (capture) { frames.push(journal.journal); capture = false; }
    return null;
  }
  const view = render(<Probe session={ADMIN} />);
  await waitFor(() => expect(poolNetwork.auditView(ADMIN, true).journal).toEqual([auditItem()]));
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? late.promise : base(url, init));
  for (const session of [{ ...ADMIN, login: '43' }, { ...ADMIN, role: 'user' as const }, { ...ADMIN, generation: 'rotated' }]) {
    capture = true; view.rerender(<Probe session={session} />);
    expect(frames.at(-1)).toEqual([]);
  }
  capture = true; view.rerender(<Probe session={ADMIN} projectId={PROJECT} />);
  expect(frames.at(-1)).toEqual([]);
  view.unmount(); await act(async () => late.resolve(reply(auditPage())));
});

test('revocation barriers erase shared facts; an older 200 after 403 cannot restore them', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const net = fixture(), base = net.http.getMockImplementation()!, late = deferred(); let n = 0;
  net.http.mockImplementation((url, init) => {
    if (url.startsWith('/api/v1/audit')) return ++n === 1 ? late.promise : Promise.resolve(reply(auditError(), 403));
    return base(url, init);
  });
  const view = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(reads(net)).toHaveLength(1));
  await act(async () => vi.advanceTimersByTimeAsync(POLL_S * 1000 + 1));
  await waitFor(() => expect(view.result.current.error).toContain('Current audit permission'));
  await act(async () => late.resolve(reply(auditPage())));
  expect(view.result.current.journal).toEqual([]);
  expect(Object.keys(poolNetwork.state.entities)).toHaveLength(0); view.unmount();
});

test('a late 403 is authority evidence even after a newer 200 was accepted', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const net = fixture(), base = net.http.getMockImplementation()!, late = deferred(); let n = 0;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? (++n === 1 ? late.promise : Promise.resolve(reply(auditPage()))) : base(url, init));
  const view = renderHook(() => useJournal(ADMIN, true));
  await act(async () => vi.advanceTimersByTimeAsync(POLL_S * 1000 + 1));
  await waitFor(() => expect(view.result.current.journal).toHaveLength(1));
  await act(async () => late.resolve(reply(auditError(), 403)));
  expect(view.result.current.journal).toEqual([]); expect(view.result.current.error).toContain('Current audit permission'); view.unmount();
});

test('a shared detail permission revocation clears audit and exposes the safe unavailable reason', async () => {
  const net = fixture(); await net.start();
  const view = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(view.result.current.journal).toHaveLength(1));
  const base = net.http.getMockImplementation()!;
  net.http.mockImplementation((url, init) => url === `/api/v1/puppets/${ID}` ? Promise.resolve(reply(auditError('permission_denied', 'Current project scope revoked'), 403)) : base(url, init));
  let unwatch!: () => void; act(() => { unwatch = poolNetwork.watchDetail(ID); });
  await waitFor(() => expect(view.result.current.journal).toEqual([]));
  expect(view.result.current.error).toContain('Current project scope revoked');
  act(() => unwatch()); view.unmount(); net.stop();
});

test('network refusal also bars a stale success, and slow reads still make progress', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const net = fixture(), base = net.http.getMockImplementation()!, late = deferred(); let n = 0;
  net.http.mockImplementation((url, init) => {
    if (url.startsWith('/api/v1/audit')) return ++n === 1 ? late.promise : Promise.reject(new TypeError('network down'));
    return base(url, init);
  });
  const view = renderHook(() => useJournal(ADMIN, true));
  await act(async () => vi.advanceTimersByTimeAsync(POLL_S * 1000 + 1));
  await waitFor(() => expect(view.result.current.error).toContain('network down'));
  await act(async () => late.resolve(reply(auditPage())));
  expect(view.result.current.journal).toEqual([]); view.unmount();
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? new Promise(resolve => setTimeout(() => resolve(reply(auditPage())), POLL_S * 1500)) : base(url, init));
  const slow = renderHook(() => useJournal(ADMIN, true));
  await act(async () => vi.advanceTimersByTimeAsync(POLL_S * 3000 + 1));
  expect(slow.result.current.journal).toHaveLength(1); slow.unmount();
});

test('new epoch restarts first-page GET and invalidates every old read instead of restoring an old epoch', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!;
  const changed = '00000000-0000-4000-8000-000000000099'; let n = 0;
  net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? Promise.resolve(reply(auditPage([auditItem(String(++n))], n === 1 ? 'old-cursor' : null, n === 1 ? EPOCH : changed))) : base(url, init));
  const view = renderHook(() => useJournal(ADMIN, true));
  await waitFor(() => expect(view.result.current.nextCursor).toBe('old-cursor'));
  await act(async () => view.result.current.more());
  await waitFor(() => expect(view.result.current.journal.map(row => row.id)).toEqual(['3']));
  expect(poolNetwork.state.epoch).toBe(changed);
  expect(reads(net).map(([url]) => url)).toEqual(['/api/v1/audit?limit=100', '/api/v1/audit?limit=100&cursor=old-cursor', '/api/v1/audit?limit=100']); view.unmount();
});

test('invalid DTO is refused instead of displaying arbitrary payload or private command input', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!;
  for (const bad of [
    { ...auditPage(), full_entry: { private: 'secret' } },
    auditPage([{ ...auditItem(), revision: '01' }]),
    auditPage([{ ...auditItem(), created_at: '2026-10-18T10:05:00+03:00' }]),
    { ...auditPage(), coverage: ['bus'] },
    { ...auditPage(), results: [{ ...auditItem(), payload: { message: 'secret' } }] },
  ]) {
    net.http.mockImplementation((url, init) => url.startsWith('/api/v1/audit') ? Promise.resolve(reply(bad)) : base(url, init));
    const view = renderHook(() => useJournal(ADMIN, true));
    await waitFor(() => expect(view.result.current.error).toMatch(/invalid.*audit/i));
    expect(view.result.current.journal).toEqual([]); view.unmount();
  }
});
