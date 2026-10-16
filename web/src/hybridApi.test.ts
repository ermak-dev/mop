// HYPOTHESIS: unsafe private HTTP без CSRF не работает с Django session auth.
// SOLUTION: same-origin boundary, текущий CSRF cookie и no-store для приватных ответов.
// RESULT: пять behavioral red, затем HTTP tests и весь interaction suite green.
// STATUS: FIXED — see #627 (HTTP boundary, не полный REST/WS cutover)
import { afterEach, expect, test, vi } from 'vitest';
import { hybridRequest, HybridHttpError } from './hybridApi';

// HYPOTHESIS: статус 403 сам по себе не отличает CSRF/права от потери сессии;
// зависший fetch или json не должен держать private request бесконечно.
// RESULT: 10 behavioral red до исправления; finite fetch/body и caller abort green.
// STATUS: FIXED — see #627; DTO/WS/UI cutover этим не подтверждён.
const ok = () => ({ ok: true, status: 200, json: async () => ({ accepted: true }) }) as Response;
afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
  document.cookie = 'csrftoken=; Max-Age=0; path=/';
});

test('unsafe session request uses the current CSRF cookie and never caches private replies', async () => {
  const fetcher = vi.fn().mockResolvedValue(ok());
  vi.stubGlobal('fetch', fetcher);
  document.cookie = 'csrftoken=first-token; path=/';
  await hybridRequest('/api/v1/commands/restart', { request_id: 'fixture' });
  expect(fetcher).toHaveBeenLastCalledWith('/api/v1/commands/restart', expect.objectContaining({
    credentials: 'same-origin', cache: 'no-store', redirect: 'error', method: 'POST',
    headers: { 'Content-Type': 'application/json', 'X-CSRFToken': 'first-token' },
  }));
  document.cookie = 'csrftoken=rotated-token; path=/';
  await hybridRequest('/api/v1/commands/restart', { request_id: 'another' });
  expect(fetcher.mock.calls[1][1].headers['X-CSRFToken']).toBe('rotated-token');
});

test('missing CSRF refuses a mutation before fetch instead of bypassing the gate', async () => {
  document.cookie = 'csrftoken=; Max-Age=0; path=/';
  const fetcher = vi.fn().mockResolvedValue(ok());
  vi.stubGlobal('fetch', fetcher);
  await expect(hybridRequest('/api/v1/commands/restart', {})).rejects.toThrow('CSRF');
  expect(fetcher).not.toHaveBeenCalled();
});

test('read needs no CSRF but keeps credentials, abort signal and cache boundary', async () => {
  document.cookie = 'csrftoken=; Max-Age=0; path=/';
  const fetcher = vi.fn().mockResolvedValue(ok());
  vi.stubGlobal('fetch', fetcher);
  const caller = new AbortController();
  await hybridRequest('/api/v1/pool', undefined, caller.signal);
  expect(fetcher).toHaveBeenCalledWith('/api/v1/pool', expect.objectContaining({
    credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal: expect.any(AbortSignal),
  }));
  expect(fetcher.mock.calls[0][1].headers).toBeUndefined();
  // Завершённый запрос уже не держит listener caller; текущий проверен ниже.
  expect(fetcher.mock.calls[0][1].signal.aborted).toBe(false);
});

test('external and protocol-relative targets never receive the CSRF-bearing request', async () => {
  const fetcher = vi.fn().mockResolvedValue(ok());
  vi.stubGlobal('fetch', fetcher);
  document.cookie = 'csrftoken=private-csrf; path=/';
  for (const target of ['https://other.invalid/api', '//other.invalid/api', '/\\other.invalid/api', 'data:text/plain,body']) {
    await expect(hybridRequest(target, {})).rejects.toThrow('same-origin');
  }
  expect(fetcher).not.toHaveBeenCalled();
});

test.each([
  [401, 'not_authenticated'], [403, 'not_authenticated'], [403, 'permission_denied'], [403, 'csrf_failed'],
  [410, 'cursor_expired'], [409, 'epoch_mismatch'],
])('DRF preserves machine code and epoch for HTTP %s/%s without inventing auth rules', async (status, code) => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status,
    json: async () => ({ schema_version: 1, epoch: 'current-epoch',
      error: { code, message: 'Current authority refused this request.', retryable: false } }),
  });
  vi.stubGlobal('fetch', fetcher);
  await expect(hybridRequest('/api/v1/identity')).rejects.toMatchObject({
    status, code, epoch: 'current-epoch', retryable: false,
    message: `HTTP ${status}: Current authority refused this request.`,
  });
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test.each(['fetch', 'body'])('private request has a finite deadline even when %s ignores abort', async stage => {
  vi.useFakeTimers();
  const never = new Promise<Response>(() => {});
  const fetcher = vi.fn().mockImplementation(() => stage === 'fetch' ? never : Promise.resolve({
    ok: true, status: 200, json: () => never,
  }));
  vi.stubGlobal('fetch', fetcher);
  let outcome: unknown = 'still waiting';
  void hybridRequest('/api/v1/identity').then(value => { outcome = value; }, error => { outcome = error; });
  await vi.advanceTimersByTimeAsync(30_001);
  expect(outcome).toMatchObject({ status: 0, code: 'request_timeout' });
  expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
  expect(fetcher).toHaveBeenCalledTimes(1);
});

test('caller cancellation aborts the in-flight request and rejects even a transport ignoring abort', async () => {
  vi.useFakeTimers();
  const fetcher = vi.fn().mockImplementation(() => new Promise(() => {}));
  vi.stubGlobal('fetch', fetcher);
  const caller = new AbortController();
  let outcome: unknown = 'still waiting';
  void hybridRequest('/api/v1/identity', undefined, caller.signal).catch(error => { outcome = error; });
  caller.abort();
  await vi.advanceTimersByTimeAsync(0);
  expect(outcome).toMatchObject({ name: 'AbortError' });
  expect(fetcher.mock.calls[0][1].signal.aborted).toBe(true);
  expect(vi.getTimerCount()).toBe(0);
});

test('aborted caller never dispatches a private mutation and cannot keep a pending request alive', async () => {
  const fetcher = vi.fn().mockResolvedValue(ok());
  vi.stubGlobal('fetch', fetcher);
  document.cookie = 'csrftoken=current-token; path=/';
  const controller = new AbortController(); controller.abort();
  await expect(hybridRequest('/api/v1/commands/restart', {}, controller.signal)).rejects.toMatchObject({ name: 'AbortError' });
  expect(fetcher).not.toHaveBeenCalled();
});

test('DRF public refusal retains HTTP status and its safe reason without retrying POST', async () => {
  const fetcher = vi.fn().mockResolvedValue({ ok: false, status: 409,
    json: async () => ({ error: { code: 'request_conflict', message: 'Request identity belongs to another intent.' } }),
  });
  vi.stubGlobal('fetch', fetcher);
  document.cookie = 'csrftoken=current-token; path=/';
  try {
    await hybridRequest('/api/v1/commands/restart', {});
    expect.fail('conflicting request was accepted');
  } catch (error) {
    expect(error).toBeInstanceOf(HybridHttpError);
    expect((error as HybridHttpError).status).toBe(409);
    expect((error as Error).message).toContain('Request identity belongs to another intent.');
  }
  expect(fetcher).toHaveBeenCalledTimes(1);
});
