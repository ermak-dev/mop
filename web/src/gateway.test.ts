// HYPOTHESIS: стоковый fetch/XHR не добавляет Django CSRF автоматически.
// SOLUTION: исполняем настоящий шаблон interceptor из server adapter, не его копию.
// RESULT: same-origin unsafe получает token, foreign/unrelated запросы — нет.
// STATUS: FIXED — see #647
import { expect, test, vi } from 'vitest';
const { readFileSync } = await vi.importActual<{ readFileSync: (path: string, encoding: string) => string }>('node:fs');
const source = readFileSync('../mop/server/gateway/panel.py', 'utf8');
const template = source.match(/return """(\(\(\) => \{[\s\S]*?)"""\.replace/)!;
const csrf = 'c'.repeat(64);
const origin = 'https://gateway.example';

class BrowserRequest {
  url: string;
  method: string;
  headers: Headers;
  credentials: RequestCredentials;
  redirect: RequestRedirect;
  body: BodyInit | null;
  constructor(input: string | BrowserRequest, init: RequestInit = {}) {
    const previous = typeof input === 'string' ? null : input;
    this.url = new URL(typeof input === 'string' ? input : input.url, origin).href;
    this.method = init.method ?? previous?.method ?? 'GET';
    this.headers = new Headers(init.headers ?? previous?.headers);
    this.credentials = init.credentials ?? previous?.credentials ?? 'same-origin';
    this.redirect = init.redirect ?? previous?.redirect ?? 'follow';
    this.body = init.body ?? previous?.body ?? null;
  }
}

function fixture() {
  const sent: BrowserRequest[] = [];
  const window = { fetch: vi.fn(async (input: string | BrowserRequest, init?: RequestInit) => {
    sent.push(new BrowserRequest(input, init));
  }) };
  class XHR {
    withCredentials = false;
    headers = new Headers();
    opened = false;
    sent = false;
    open() { this.opened = true; }
    setRequestHeader(name: string, value: string) { this.headers.set(name, value); }
    send() { this.sent = true; }
  }
  const install = new Function('window', 'location', 'XMLHttpRequest', 'Request', 'Headers', 'URL',
    template[1].replace('TOKEN', JSON.stringify(csrf)));
  install(window, { origin, href: origin + '/llm/raw/management.html' }, XHR, BrowserRequest, Headers, URL);
  return { window, XHR, sent };
}

test('#647 stock fetch uses the session and CSRF without forwarding its sentinel auth', async () => {
  const { window, sent } = fixture();
  await window.fetch('/v8/management/config', {method: 'PUT', body: '{}',
    credentials: 'omit', headers: {Authorization: 'Bearer public-sentinel'}});
  expect(sent[0].method).toBe('PUT');
  expect(sent[0].body).toBe('{}');
  expect(sent[0].headers.get('X-CSRFToken')).toBe(csrf);
  expect(sent[0].headers.has('Authorization')).toBe(false);
  expect(sent[0].credentials).toBe('same-origin');
  expect(sent[0].redirect).toBe('error');
  await window.fetch('/v8/management/config');
  expect(sent[1].headers.has('X-CSRFToken')).toBe(false);
});

test('#647 stock interceptor never sends its CSRF token to another origin or unrelated inference', async () => {
  const { window, sent } = fixture();
  expect(() => window.fetch('https://evil.example/v8/management/config', {method:'POST'})).toThrow();
  await window.fetch('https://evil.example/unrelated', {method:'POST'});
  await window.fetch('/v1/messages', {method:'POST', headers:{Authorization:'Bearer inference-key'}});
  expect(sent).toHaveLength(2);
  expect(sent.every(r => !r.headers.has('X-CSRFToken'))).toBe(true);
  expect(sent[1].headers.get('Authorization')).toBe('Bearer inference-key');
});

test('#647 stock XHR receives one trusted CSRF header only for same-origin management', () => {
  const { XHR } = fixture();
  const xhr = new XHR();
  // Сигнатуры реального open/send сохраняются interceptor-ом; fixture не использует args.
  const open = xhr.open as unknown as (method: string, url: string) => void;
  const send = xhr.send as unknown as (body: string) => void;
  open.call(xhr, 'PATCH', '/v8/management/config');
  xhr.setRequestHeader('Authorization', 'Bearer sentinel');
  xhr.setRequestHeader('X-CSRFToken', 'forged');
  send.call(xhr, '{}');
  expect(xhr.sent).toBe(true);
  expect(xhr.withCredentials).toBe(true);
  expect(xhr.headers.get('X-CSRFToken')).toBe(csrf);
  expect(xhr.headers.has('Authorization')).toBe(false);
  const foreign = new XHR();
  expect(() => open.call(foreign, 'POST', 'https://evil.example/v8/management/config')).toThrow();
  expect(foreign.opened).toBe(false);
});
