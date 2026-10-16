// Общая внешняя граница проверок #627: настоящий runtime, только HTTP/WS подменены.
import { act, waitFor } from '@testing-library/react';
import { expect, vi } from 'vitest';
import { poolNetwork } from './poolNetwork';
import type { Session } from './useSession';
export const ADMIN: Session = { login: '42', role: 'admin', generation: 'test' };
export const ID = '00000000-0000-4000-8000-000000000001';
export const PROJECT = '00000000-0000-4000-8000-000000000002';
export const EPOCH = '00000000-0000-4000-8000-000000000003';
export const INCARNATION = '00000000-0000-4000-8000-000000000004';
export const OP = '00000000-0000-4000-8000-000000000005';
export const puppet = (id = ID, revision = '1') => ({ kind: 'puppet', id, incarnation: INCARNATION, revision,
  project_id: PROJECT, project_name: 'project', node_name: 'node',
  desired: { revision: '7', role: 'worker', owner_id: '42' },
  observed: { state: { freshness: 'fresh', expires_at: new Date(Date.now() + 60_000).toISOString() },
    clone: { freshness: 'fresh', expires_at: new Date(Date.now() + 60_000).toISOString() } },
  state: { kind: 'busy', detail: 'confirmed', branch: 'main' } });
export const reply = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
export const deferred = () => { let resolve!: (v: Response) => void; const promise = new Promise<Response>(r => { resolve = r; }); return { promise, resolve }; };
export class NetworkSocket {
  static all: NetworkSocket[] = [];
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose: (() => void) | null = null;
  onerror: (() => void) | null = null;
  send = vi.fn(); close = vi.fn();
  constructor(public url: string) { NetworkSocket.all.push(this); }
  frame(body: unknown) { this.onmessage?.({ data: JSON.stringify(body) } as MessageEvent); }
}
export function networkFixture() {
  NetworkSocket.all = [];
  vi.stubGlobal('WebSocket', NetworkSocket);
  let requestId = '';
  const operation = (state = 'running', revision = '2', reason = '') => ({ schema_version: 1, epoch: EPOCH,
    operation: { id: OP, request_id: requestId, revision, state, target: { id: ID, incarnation: INCARNATION }, action: 'restart', reason } });
  const http = vi.fn(async (url: string, init?: RequestInit): Promise<Response> => {
    if (url === '/api/v1/projects') return reply({ schema_version: 1, results: [{ id: PROJECT }], next_cursor: null });
    if (url === '/api/v1/snapshot') return reply({ schema_version: 1, epoch: EPOCH, snapshot_id: 'snapshot', cursor: 'vector', next_cursor: null, results: [puppet()], coverage: {} });
    if (url === `/api/v1/puppets/${ID}`) return reply({ schema_version: 1, epoch: EPOCH, puppet: puppet() });
    if (url === `/api/v1/puppets/${ID}/commands/restart` && init?.method === 'POST') {
      requestId = JSON.parse(String(init.body)).request_id;
      return reply(operation('accepted', '1'), 202);
    }
    if (url.startsWith('/api/v1/operations')) return reply(operation());
    throw new Error(`Unexpected URL ${url}`);
  });
  vi.stubGlobal('fetch', http);
  let stop: (() => void) | undefined;
  return { http, operation,
    get requestId() { return requestId; },
    capture(init?: RequestInit) { requestId = JSON.parse(String(init?.body)).request_id; },
    posts: () => http.mock.calls.filter(([, init]) => init?.method === 'POST'),
    async start(session = ADMIN) {
      act(() => { stop = poolNetwork.start(session); });
      await waitFor(() => expect(NetworkSocket.all.length).toBeGreaterThan(0));
      await act(async () => NetworkSocket.all.at(-1)!.frame({ schema_version: 1, type: 'ready' }));
      await waitFor(() => expect(poolNetwork.view(session)).not.toBeNull());
    },
    stop() { act(() => { stop?.(); }); },
    async invalidateOperation(revision = '3') {
      await act(async () => NetworkSocket.all.at(-1)!.frame({ schema_version: 1, type: 'events', epoch: EPOCH,
        coverage: ['operations'], handshake_complete: true,
        results: [{ event_id: `operation-${revision}`, epoch: EPOCH, entity: { kind: 'operation', id: OP, incarnation: null, revision } }] }));
    },
  };
}
