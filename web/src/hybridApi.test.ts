// HYPOTHESIS: unsafe private HTTP без CSRF не работает с Django session auth.
// SOLUTION: same-origin boundary, текущий CSRF cookie и no-store для приватных ответов.
// RESULT: пять behavioral red, затем HTTP tests и весь interaction suite green.
// STATUS: FIXED — see #627 (HTTP boundary, не полный REST/WS cutover)
import { afterEach, expect, test, vi } from 'vitest';
import { hybridRequest, HybridHttpError } from './hybridApi';

const ok = () => ({ ok: true, status: 200, json: async () => ({ accepted: true }) }) as Response;
afterEach(() => {
  vi.unstubAllGlobals();
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
  const signal = new AbortController().signal;
  await hybridRequest('/api/v1/pool', undefined, signal);
  expect(fetcher).toHaveBeenCalledWith('/api/v1/pool', expect.objectContaining({
    credentials: 'same-origin', cache: 'no-store', redirect: 'error', signal,
  }));
  expect(fetcher.mock.calls[0][1].headers).toBeUndefined();
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
