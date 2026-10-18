// HYPOTHESIS: смонтированный useJournal ещё вызывает неподдержанный /api/journal.
// SPEC: frozen #626/83b75ffa; настоящий App/hooks, только внешние HTTP/WS заменены.
// SOLUTION: audit DTO/read tokens принадлежат shared network, UI хранит selection.
// RESULT: RED → GREEN на mounted network fixture и отдельно в реальном браузере.
// STATUS: FIXED — see #627; live controller/pool не проверялся.
import { act, fireEvent, screen, waitFor, within } from '@testing-library/react';
import { afterEach, expect, test, vi } from 'vitest';
import App from './App';
import { renderUi } from './test-utils';
import { deferred, networkFixture, reply, OP } from './network-test-utils';
import { auditError, auditItem, auditPage } from './audit-test-utils';

function fixture(kind = 'human') {
  const net = networkFixture(), base = net.http.getMockImplementation()!;
  let allowed = true;
  net.http.mockImplementation((url, init) => {
    if (url === '/api/v1/identity') return Promise.resolve(reply({ schema_version: 1, user_id: 42, kind }));
    if (url === '/api/v1/auth/logout') return Promise.resolve(reply({ schema_version: 1 }));
    if (url.startsWith('/api/v1/audit')) return Promise.resolve(allowed ? reply(auditPage()) : reply(auditError(), 403));
    return base(url, init);
  });
  return { ...net, deny: () => { allowed = false; } };
}
afterEach(() => { vi.unstubAllGlobals(); vi.useRealTimers(); });

test('mounted journal reads v1 operation audit, not bus events, and opens only safe evidence', async () => {
  const net = fixture(); window.location.hash = '#journal';
  const store = vi.spyOn(Storage.prototype, 'setItem');
  const view = renderUi(<App />);
  await screen.findByText('target does not receive dispatch');
  expect(net.http.mock.calls.some(([url]) => url === '/api/v1/audit?limit=100')).toBe(true);
  expect(screen.getByRole('heading', { name: 'Operation audit' })).toBeInTheDocument();
  expect(screen.getByText(/not all bus events/i)).toBeInTheDocument();
  expect(screen.queryByText(/сообщение от|дождался простоя|события шины/)).toBeNull();
  fireEvent.click(screen.getByRole('button', { name: /Open audit evidence/ }));
  const dialog = await screen.findByRole('dialog');
  expect(within(dialog).getByText(OP)).toBeInTheDocument();
  expect(dialog.textContent).toContain('9007199254740995');
  expect(dialog.textContent).toContain('2026-10-18T10:05:00Z');
  expect(dialog.textContent).not.toContain('from ermak');
  expect(net.http.mock.calls.every(([url]) => url.startsWith('/api/v1/'))).toBe(true);
  expect(net.posts()).toHaveLength(0);
  expect(store.mock.calls.some(([, value]) => /operation_id|dispatch_declined/.test(value))).toBe(false);
  store.mockRestore(); view.unmount();
});

test.each(['agent', 'service'])('mounted %s reader defers audit permission to DRF, not actor kind', async kind => {
  const net = fixture(kind); window.location.hash = '#journal';
  const view = renderUi(<App />);
  await screen.findByText('target does not receive dispatch');
  expect(net.http.mock.calls.some(([url]) => url.startsWith('/api/v1/audit'))).toBe(true);
  view.unmount();
});

test('server permission denial is visible to a human without inventing admin authority', async () => {
  const net = fixture(); net.deny(); window.location.hash = '#journal';
  const view = renderUi(<App />);
  expect(await screen.findByRole('tab', { name: 'Operation audit' })).toBeInTheDocument();
  expect(await screen.findByText(/Current audit permission is required/)).toHaveAttribute('role', 'alert');
  expect(screen.queryByText('target does not receive dispatch')).toBeNull();
  view.unmount();
});

test('mounted project intersection and page-size selection replace the query without keeping old evidence', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!;
  const project = '00000000-0000-4000-8000-000000000002';
  net.http.mockImplementation((url, init) => url === `/api/v1/audit?limit=25&project_id=${project}`
    ? Promise.resolve(reply(auditPage([auditItem('3', 'scoped evidence')]))) : base(url, init));
  window.location.hash = '#journal'; const view = renderUi(<App />);
  await screen.findByText('target does not receive dispatch');
  fireEvent.change(screen.getByRole('textbox', { name: 'Project id (optional)' }), { target: { value: project } });
  fireEvent.change(screen.getByRole('spinbutton', { name: 'Page size (1–100)' }), { target: { value: '25' } });
  fireEvent.click(screen.getByRole('button', { name: 'Apply audit scope' }));
  await screen.findByText('scoped evidence');
  expect(screen.queryByText('target does not receive dispatch')).toBeNull();
  expect(net.posts()).toHaveLength(0); view.unmount();
});

test('mounted pagination restarts an expired cursor only with GET and does not invent native events', async () => {
  const net = fixture(), base = net.http.getMockImplementation()!;
  let first = 0;
  net.http.mockImplementation((url, init) => {
    if (url === '/api/v1/audit?limit=100') return Promise.resolve(reply(auditPage([auditItem('9', ++first === 1 ? 'old page' : 'fresh page')], first === 1 ? 'opaque+cursor/=' : null)));
    if (url.startsWith('/api/v1/audit?')) return Promise.resolve(reply(auditError('reset_required', 'audit cursor expired; restart pagination'), 410));
    return base(url, init);
  });
  window.location.hash = '#journal'; const view = renderUi(<App />);
  await screen.findByText('old page');
  fireEvent.click(screen.getByRole('button', { name: 'Load older evidence' }));
  await screen.findByText('fresh page');
  expect(screen.queryByText('old page')).toBeNull();
  expect(net.http.mock.calls.some(([url]) => url === '/api/v1/audit?limit=100&cursor=opaque%2Bcursor%2F%3D')).toBe(true);
  expect(net.posts()).toHaveLength(0); view.unmount();
});

test('logout closes open evidence and a late audit body cannot restore it', async () => {
  vi.useFakeTimers({ shouldAdvanceTime: true });
  const net = fixture(), base = net.http.getMockImplementation()!, late = deferred();
  let reads = 0;
  net.http.mockImplementation((url, init) => {
    if (url.startsWith('/api/v1/audit') && ++reads > 1) return Promise.resolve({ ok: true, json: () => late.promise.then(r => r.json()) } as Response);
    return base(url, init);
  });
  window.location.hash = '#journal'; const view = renderUi(<App />);
  await screen.findByText('target does not receive dispatch');
  fireEvent.click(screen.getByRole('button', { name: /Open audit evidence/ }));
  expect(await screen.findByRole('dialog')).toBeInTheDocument();
  await act(async () => { await vi.advanceTimersByTimeAsync(10_001); });
  await waitFor(() => expect(reads).toBeGreaterThan(1));
  fireEvent.keyDown(screen.getByRole('dialog'), { key: 'Escape' });
  fireEvent.click(screen.getByRole('button', { name: 'Выйти' }));
  await waitFor(() => expect(screen.queryByText('target does not receive dispatch')).toBeNull());
  await act(async () => late.resolve(reply(auditPage())));
  expect(screen.queryByRole('dialog')).toBeNull();
  expect(screen.queryByText('target does not receive dispatch')).toBeNull();
  expect(net.posts().map(([url]) => url)).toEqual(['/api/v1/auth/logout']); view.unmount();
});
