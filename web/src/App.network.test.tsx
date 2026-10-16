// HYPOTHESIS: изолированный store green ещё не доказывает подключение App.
// SOLUTION: настоящий App и hooks, подменены только внешние HTTP/WS.
// RESULT: это browser-runtime fixture, не проверка живого controller/pool.
// STATUS: FIXED — see #627
import { act, fireEvent, screen, waitFor } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import App from './App';
import { renderUi } from './test-utils';
const id = '00000000-0000-4000-8000-000000000001';
const project = '00000000-0000-4000-8000-000000000002';
const epoch = '00000000-0000-4000-8000-000000000003';
class Socket {
  static last: Socket;
  onmessage: ((event: MessageEvent) => void) | null = null;
  onclose = null; onerror = null;
  constructor(public url: string) { Socket.last = this; }
  send = vi.fn(); close = vi.fn();
  frame(body: unknown) { this.onmessage?.({ data: JSON.stringify(body) } as MessageEvent); }
}
afterEach(() => vi.unstubAllGlobals());
test('the production App loads Django identity, renders the new snapshot and binds selected detail without legacy pool routes', async () => {
  vi.stubGlobal('WebSocket', Socket);
  const row = { kind: 'puppet', id, revision: '1', incarnation: 'runtime', project_id: project, project_name: 'new-project', node_name: 'new-node',
    desired: { revision: '1', role: 'worker', owner_id: '42' }, observed: {}, state: { kind: 'unknown', detail: 'Not observed' } };
  let requestId = '';
  const fetcher = vi.fn(async (url: string, init?: RequestInit) => {
    if (init?.method === 'POST') {
      expect(url).toBe(`/api/v1/puppets/${id}/commands/restart`);
      const command = JSON.parse(String(init.body)); requestId = command.request_id;
      expect(command).toMatchObject({ epoch, expected_revision: '1', incarnation: 'runtime', params: { fresh: false } });
      return new Response(JSON.stringify({ schema_version: 1, epoch, operation: { id: 'op', request_id: requestId, state: 'accepted', revision: '1', target: { id, incarnation: 'runtime' } } }), { status: 202 });
    }
    if (url.startsWith('/api/v1/operations/')) return new Response(JSON.stringify({ schema_version: 1, epoch,
      operation: { id: 'op', request_id: requestId, state: 'succeeded', revision: '2', target: { id, incarnation: 'runtime' } } }));
    const body = url === '/api/v1/identity' ? { schema_version: 1, user_id: 42, kind: 'human', principal_id: 'principal' }
      : url === '/api/v1/projects' ? { schema_version: 1, results: [{ id: project }], next_cursor: null }
      : url === '/api/v1/snapshot' ? { schema_version: 1, epoch, snapshot_id: 'snapshot', cursor: 'vector', next_cursor: null, results: [row], coverage: { operations: false } }
      : url === `/api/v1/puppets/${id}` ? { schema_version: 1, epoch, puppet: row }
      : null;
    if (!body) throw new Error(`Unexpected request ${url}`);
    return new Response(JSON.stringify(body));
  });
  vi.stubGlobal('fetch', fetcher);
  renderUi(<App />);
  await waitFor(() => expect(Socket.last?.url).toContain('/ws/v1/events?project_id='));
  await act(async () => Socket.last.frame({ schema_version: 1, type: 'ready', handshake_complete: false }));
  await screen.findByRole('heading', { name: /new-project/ });
  fireEvent.click(screen.getByRole('button', { name: id }));
  await waitFor(() => expect(fetcher.mock.calls.some(([url]) => url === `/api/v1/puppets/${id}`)).toBe(true));
  expect(screen.getByTestId('puppet-card')).toBeInTheDocument();
  expect(screen.getByRole('link', { name: 'LLM Proxy' })).toHaveAttribute('href', '/llm/management.html');
  fireEvent.click(screen.getByRole('button', { name: 'Правка' }));
  fireEvent.click(await screen.findByRole('button', { name: 'Рестарт' }));
  fireEvent.click(screen.getByRole('button', { name: /^Завершить$/ }));
  await waitFor(() => expect(fetcher.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1));
  await screen.findByText('Перезапущен');
  expect(fetcher.mock.calls.every(([url]) => url.startsWith('/api/v1/'))).toBe(true);
});
