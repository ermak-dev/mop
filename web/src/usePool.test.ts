// HYPOTHESIS: UI hooks still bypass the versioned store through legacy HTTP/SSE.
// SOLUTION: exercise the production hooks together, with controlled HTTP/WS ordering.
// RESULT: network fixtures are not live controller/pool acceptance.
// STATUS: FIXED — see #627
import { act, renderHook, waitFor } from '@testing-library/react';
import { vi, type Mock } from 'vitest';
import { usePool, POLL_MS } from './usePool';
import { usePuppetCard } from './usePuppetCard';
import { useAdminOp } from './useAdminOp';
import type { Session } from './useSession';

const session: Session = { login: 'operator', role: 'admin' };
const id = '00000000-0000-4000-8000-000000000001';
const project = '00000000-0000-4000-8000-000000000002';
const epoch = '00000000-0000-4000-8000-000000000003';
const incarnation = '00000000-0000-4000-8000-000000000004';
const target = { id, incarnation };
const row = (revision = '1') => ({ kind: 'puppet', id, incarnation, revision, deleted: false,
  name: null, aliases: [], project_id: project, project_name: 'project', node_id: 'node-id', node_name: 'node',
  desired: { revision: '7', role: 'worker', owner_id: '42', bundle_digest: 'digest' },
  observed: { state: { freshness: 'fresh', expires_at: new Date(Date.now() + 30_000).toISOString() },
    clone: { freshness: 'fresh', expires_at: new Date(Date.now() + 30_000).toISOString(), value: { branch: 'main' } } },
  state: { kind: revision === '1' ? 'busy' : 'free', detail: 'confirmed', branch: 'main' },
  usage: { freshness: 'unavailable', reason: 'usage_query_not_bound' } });
const snapshot = (revision = '1', extra = {}) => ({ schema_version: 1, epoch, results: [row(revision)],
  snapshot_id: 'snapshot', cursor: 'vector', next_cursor: null, coverage: { operations: false }, handshake_complete: false, ...extra });
const operation = (state = 'accepted', revision = '1') => ({ schema_version: 1, epoch,
  operation: { id: 'op-id', request_id: requestId, revision, state, target, action: 'restart', reason: '' } });
let requestId = '';
let http: Mock<(url: string, init?: RequestInit) => Promise<Response>>;
class Socket {
  static all: Socket[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  readyState = 1;
  sent: unknown[] = [];
  closed = false;
  constructor(public url: string) { Socket.all.push(this); }
  send(raw: string) { this.sent.push(JSON.parse(raw)); }
  close() { this.closed = true; }
  frame(data: unknown) { this.onmessage?.({ data: JSON.stringify(data) } as MessageEvent); }
}
const reply = (value: unknown, status = 200) => new Response(JSON.stringify(value), { status });
const deferred = () => { let resolve!: (v: Response) => void; const promise = new Promise<Response>(r => { resolve = r; }); return { promise, resolve }; };
const urls = () => http.mock.calls.map(([url]) => String(url));
const latest = () => Socket.all.at(-1)!;
async function ready() {
  await waitFor(() => expect(Socket.all.length).toBeGreaterThan(0));
  await act(async () => latest().frame({ schema_version: 1, type: 'ready', handshake_complete: false }));
}
function hooks() { return renderHook(() => ({ pool: usePool(session), card: usePuppetCard(session, id), op: useAdminOp(session, id) })); }
beforeEach(() => {
  Socket.all = []; requestId = '';
  vi.stubGlobal('WebSocket', Socket);
  vi.stubGlobal('EventSource', class { addEventListener() {} close() {} });
  document.cookie = 'csrftoken=currentToken; path=/';
  http = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/v1/projects') return reply({ schema_version: 1, results: [{ id: project, name: 'project' }], next_cursor: null });
    if (url.startsWith('/api/v1/snapshot')) return reply(snapshot());
    if (url === `/api/v1/puppets/${id}`) return reply({ schema_version: 1, epoch, puppet: row() });
    if (url.endsWith('/commands/restart')) { requestId = JSON.parse(String(init?.body)).request_id; return reply(operation(), 202); }
    if (url.startsWith('/api/v1/operations')) return reply(operation('succeeded', '3'));
    throw new Error(`Unexpected URL ${url}`);
  });
  vi.stubGlobal('fetch', http);
});
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test('binds table and card to one store and subscribes only after ready + complete paginated snapshot', async () => {
  const old = deferred();
  http.mockImplementation(async (url: string) => {
    if (url === '/api/v1/projects') return reply({ schema_version: 1, results: [{ id: project }], next_cursor: null });
    if (url === '/api/v1/snapshot') return old.promise;
    if (url === '/api/v1/snapshot?cursor=page2') return reply(snapshot('1', { results: [] }));
    if (url === `/api/v1/puppets/${id}`) return reply({ schema_version: 1, epoch, puppet: row('2') });
    throw new Error(`Unexpected URL ${url}`);
  });
  const { result, unmount } = hooks();
  await ready();
  expect(latest().sent).toEqual([]);
  await act(async () => old.resolve(reply(snapshot('1', { next_cursor: 'page2' }))));
  await waitFor(() => expect(latest().sent).toEqual([{ type: 'subscribe', cursor: 'vector' }]));
  await waitFor(() => expect(result.current.card.card?.kind).toBe('free'));
  expect(result.current.pool.snapshot?.projects[0].puppets[0].kind).toBe('free');
  expect(urls().every(url => url.startsWith('/api/v1/'))).toBe(true);
  unmount(); expect(latest().closed).toBe(true);
});

test('invalidations refetch detail; late REST cannot roll back newer WS revision or resurrect tombstone', async () => {
  const { result } = hooks(); await ready();
  await waitFor(() => expect(result.current.pool.snapshot?.counts.puppets).toBe(1));
  const late = deferred();
  http.mockImplementation(async (url: string) => url.includes('/puppets/') ? late.promise : reply(snapshot()));
  await act(async () => latest().frame({ type: 'events', schema_version: 1, epoch, cursor: 'v2', coverage: ['observations'], handshake_complete: true,
    results: [{ event_id: 'e2', epoch, entity: { kind: 'puppet', ...target, revision: '2' } }] }));
  await waitFor(() => expect(urls().filter(url => url.includes('/puppets/')).length).toBeGreaterThan(1));
  await act(async () => latest().frame({ type: 'events', schema_version: 1, epoch, cursor: 'v3', coverage: ['observations'], handshake_complete: true,
    results: [{ event_id: 'e3', epoch, entity: { kind: 'puppet', ...target, revision: '3', deleted: true } }] }));
  await act(async () => late.resolve(reply({ schema_version: 1, epoch, puppet: row('2') })));
  expect(result.current.card.card).toBeNull();
  expect(result.current.pool.snapshot?.counts.puppets).toBe(0);
});

test('lost accepted response recovers by request ID on reconnect, without repeating POST; reset refreshes snapshot', async () => {
  const { result } = hooks(); await ready();
  await waitFor(() => expect(result.current.card.card).not.toBeNull());
  const base = http.getMockImplementation()!;
  http.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') { requestId = JSON.parse(String(init.body)).request_id; throw new TypeError('lost after commit'); }
    return base(url, init);
  });
  await act(async () => { await result.current.op.submit(); });
  await waitFor(() => expect(result.current.op.op.status).toBe('succeeded'));
  const call = http.mock.calls.find(([, init]) => init?.method === 'POST')!;
  expect(call[0]).toBe(`/api/v1/puppets/${id}/commands/restart`);
  const envelope = JSON.parse(String(call[1]!.body));
  expect(envelope).toMatchObject({ schema_version: 1, epoch, incarnation, expected_revision: '7', params: { fresh: false } });
  expect(call[1]!.headers).toMatchObject({ 'X-CSRFToken': 'currentToken' });
  expect(urls()).toContain(`/api/v1/operations?request_id=${requestId}&epoch=${epoch}`);
  const before = urls().filter(url => url.startsWith('/api/v1/snapshot')).length;
  await act(async () => latest().frame({ type: 'reset_required', reason: 'cursor expired' }));
  await waitFor(() => expect(Socket.all.length).toBe(2));
  await act(async () => latest().frame({ type: 'ready', schema_version: 1, handshake_complete: false }));
  await waitFor(() => expect(urls().filter(url => url.startsWith('/api/v1/snapshot')).length).toBeGreaterThan(before));
  expect(http.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
  await waitFor(() => expect(result.current.op.op.status).toBe('succeeded'));
});

test('clears private rows synchronously on logout and ignores a late detail', async () => {
  const late = deferred();
  const base = http.getMockImplementation()!;
  http.mockImplementation((url: string, init?: RequestInit) => url.includes('/puppets/') ? late.promise : base(url, init));
  const { result, rerender } = renderHook(({ s }) => ({ pool: usePool(s), card: usePuppetCard(s, id) }), { initialProps: { s: session } });
  await ready();
  await waitFor(() => expect(result.current.pool.snapshot?.counts.puppets).toBe(1));
  rerender({ s: { login: null, role: null } });
  expect(result.current.pool.snapshot).toBeNull();
  expect(result.current.card.card).toBeNull();
  await act(async () => late.resolve(reply({ schema_version: 1, epoch, puppet: row('2') })));
  expect(result.current.pool.snapshot).toBeNull();
  expect(latest().closed).toBe(true);
});

test('periodic reconciliation is present even with a healthy socket and unbound operation coverage', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const { result } = hooks(); await ready();
  await waitFor(() => expect(result.current.pool.snapshot).not.toBeNull());
  const before = http.mock.calls.length;
  await act(async () => { await vi.advanceTimersByTimeAsync(POLL_MS); });
  expect(http.mock.calls.length).toBeGreaterThan(before);
});

test('malformed accepted body remains unknown and repeated clicks only look up the original request', async () => {
  const { result } = hooks(); await ready();
  await waitFor(() => expect(result.current.card.card).not.toBeNull());
  const base = http.getMockImplementation()!;
  http.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') { requestId = JSON.parse(String(init.body)).request_id; return reply({ schema_version: 1, epoch }, 202); }
    if (url.includes('/operations')) return reply({ error: { code: 'not_found', message: 'not visible yet' } }, 404);
    return base(url, init);
  });
  await act(async () => { await result.current.op.submit(); });
  expect(result.current.op.op).toMatchObject({ status: 'unknown', lost: true });
  await act(async () => { await result.current.op.submit(); });
  expect(http.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
});

test('event-before-accepted-response and delayed old snapshot converge at the newer confirmed revision', async () => {
  const { result } = hooks(); await ready();
  await waitFor(() => expect(result.current.card.card).not.toBeNull());
  const post = deferred();
  const base = http.getMockImplementation()!;
  http.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') { requestId = JSON.parse(String(init.body)).request_id; return post.promise; }
    return base(url, init);
  });
  let submitted!: Promise<string | null>;
  act(() => { submitted = result.current.op.submit(); });
  await act(async () => latest().frame({ type: 'events', schema_version: 1, epoch, cursor: 'v3', coverage: ['explicit_owner_publication'], handshake_complete: true,
    results: [{ event_id: 'op-event', epoch, entity: { kind: 'operation', id: 'op-id', incarnation: null, revision: '3' } }] }));
  await waitFor(() => expect(result.current.op.op.status).toBe('succeeded'));
  await act(async () => { post.resolve(reply(operation('accepted', '1'), 202)); await submitted; });
  expect(result.current.op.op.status).toBe('succeeded');
});

test('read authorization loss clears all shared projections, while CSRF refusal does not replay a command', async () => {
  const { result } = hooks(); await ready();
  await waitFor(() => expect(result.current.card.card).not.toBeNull());
  const base = http.getMockImplementation()!;
  http.mockImplementation(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') return reply({ schema_version: 1, error: { code: 'csrf_failed', message: 'CSRF validation failed' } }, 403);
    if (url.includes('/operations')) return reply({ error: { code: 'not_found', message: 'not admitted' } }, 404);
    return base(url, init);
  });
  await act(async () => { await result.current.op.submit(); });
  expect(result.current.op.op.error).toContain('CSRF validation failed');
  expect(result.current.pool.snapshot?.counts.puppets).toBe(1);
  http.mockImplementation(async () => reply({ schema_version: 1, error: { code: 'permission_denied', message: 'membership revoked' } }, 403));
  await act(async () => latest().frame({ type: 'events', schema_version: 1, epoch, cursor: 'v2', coverage: ['observations'], handshake_complete: true,
    results: [{ event_id: 'changed', epoch, entity: { kind: 'puppet', ...target, revision: '2' } }] }));
  await waitFor(() => expect(result.current.pool.snapshot).toBeNull());
  expect(result.current.card.card).toBeNull();
});

test('overflow explicitly resets and never feeds terminal bytes into the entity store', async () => {
  const { result } = hooks(); await ready();
  await waitFor(() => expect(result.current.pool.snapshot).not.toBeNull());
  await act(async () => latest().frame('x'.repeat(131073)));
  await waitFor(() => expect(Socket.all.length).toBe(2));
  expect(result.current.pool.snapshot).toBeNull();
  expect(latest().url).toContain('/ws/v1/events?project_id=');
  expect(urls().some(url => url.includes('/terminal'))).toBe(false);
});

test('multiple project audiences use project-specific snapshot cursors, never the global vector on a subset socket', async () => {
  const other = '00000000-0000-4000-8000-000000000099';
  const base = http.getMockImplementation()!;
  http.mockImplementation(async (url: string, init?: RequestInit) => {
    if (url === '/api/v1/projects') return reply({ schema_version: 1, results: [{ id: project }, { id: other }], next_cursor: null });
    if (url.startsWith('/api/v1/snapshot?project_id=')) {
      const audience = new URL(url, 'http://fixture').searchParams.get('project_id');
      return reply(snapshot('1', { results: audience === project ? [row()] : [], cursor: `cursor-${audience}` }));
    }
    return base(url, init);
  });
  hooks();
  await waitFor(() => expect(Socket.all).toHaveLength(2));
  await act(async () => { for (const socket of Socket.all) socket.frame({ schema_version: 1, type: 'ready', handshake_complete: false }); });
  await waitFor(() => expect(Socket.all.every(socket => socket.sent.length === 1)).toBe(true));
  for (const socket of Socket.all) {
    const audience = new URL(socket.url).searchParams.getAll('project_id');
    expect(audience).toHaveLength(1);
    expect(socket.sent).toEqual([{ type: 'subscribe', cursor: `cursor-${audience[0]}` }]);
  }
});
