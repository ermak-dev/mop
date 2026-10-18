// HYPOTHESIS: legacy auth routes bypass Django's shared session and rotated CSRF.
// SOLUTION: identity + Django login/logout, using the current cookie for every POST.
// STATUS: FIXED — see #627
import { afterEach, expect, test, vi } from 'vitest';
import { fetchSession, GUEST, LoginRefused, postLogin, postLogout } from './useSession';
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status });
afterEach(() => vi.unstubAllGlobals());
// HYPOTHESIS: human-only browser gate invents kind-based audit authority.
// SPEC: identity classifies; DRF current permissions/scope decide disclosure.
// STATUS: FIXED — see #627
test.each(['human', 'agent', 'service'] as const)('recognizes canonical %s identity without manufacturing admin rights', async kind => {
  vi.stubGlobal('fetch', vi.fn(async () => json({ schema_version: 1, user_id: 42, kind, human_owner_id: null, node_id: null, grant_id: null })));
  expect(await fetchSession()).toMatchObject({ login: '42', role: 'user', kind, authenticated: true });
});
test('uses the authenticated identity and treats only auth codes as guest', async () => {
  const mock = vi.fn().mockResolvedValueOnce(json({ schema_version: 1, user_id: 42, principal_id: 'principal', kind: 'human' }))
    .mockResolvedValueOnce(json({ error: { code: 'not_authenticated', message: 'Authentication required' } }, 403))
    .mockResolvedValueOnce(json({ error: { code: 'authority_unavailable', message: 'Database unavailable' } }, 503));
  vi.stubGlobal('fetch', mock);
  expect(await fetchSession()).toMatchObject({ login: '42', role: 'user', authenticated: true });
  expect(mock.mock.calls[0][0]).toBe('/api/v1/identity');
  expect(await fetchSession()).toEqual(GUEST);
  await expect(fetchSession()).rejects.toThrow('503');
});
test('bootstraps CSRF before login, accepts rotation, and logs out of the same Django session', async () => {
  document.cookie = 'csrftoken=beforeLogin; path=/';
  const mock = vi.fn(async (url: string, init?: RequestInit) => {
    if (url === '/api/v1/auth/csrf') return json({ schema_version: 1 });
    if (url === '/api/v1/auth/login') {
      expect(JSON.parse(String(init?.body))).toEqual({ username: 'operator', password: 'private' });
      expect(init?.headers).toMatchObject({ 'X-CSRFToken': 'beforeLogin' });
      document.cookie = 'csrftoken=afterLogin; path=/';
      return json({ schema_version: 1, user_id: 42 });
    }
    if (url === '/api/v1/identity') return json({ schema_version: 1, user_id: 42, principal_id: 'principal', kind: 'human' });
    if (url === '/api/v1/auth/logout') {
      expect(init?.headers).toMatchObject({ 'X-CSRFToken': 'afterLogin' });
      return json({ schema_version: 1 });
    }
    throw new Error(`Unexpected ${url}`);
  });
  vi.stubGlobal('fetch', mock);
  const first = await postLogin('operator', 'private');
  expect(first.authenticated).toBe(true);
  expect(await postLogout()).toEqual(GUEST);
  document.cookie = 'csrftoken=beforeLogin; path=/';
  const second = await postLogin('operator', 'private');
  expect(second.generation).not.toBe(first.generation);
});
test('Django login refusal preserves the safe server reason', async () => {
  vi.stubGlobal('fetch', vi.fn(async (url: string) => url.endsWith('/csrf') ? json({ schema_version: 1 })
    : json({ schema_version: 1, error: { code: 'permission_denied', message: 'authentication refused' } }, 403)));
  await expect(postLogin('operator', 'wrong')).rejects.toBeInstanceOf(LoginRefused);
});
