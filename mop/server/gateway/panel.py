"""Ограниченный same-origin adapter stock JS; здесь нет upstream credential."""
import json
import re
from .policy import MAX_BODY
from mop.server.management_policy import SENTINEL
CSP = ("default-src 'none'; script-src 'self' 'unsafe-inline'; style-src 'self' 'unsafe-inline'; "
       "connect-src 'self'; img-src 'self' data:; font-src 'self' data:; "
       "frame-src 'none'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'")


def bootstrap():
    # Только публичный sentinel, не ключ и не hash; очищаем прежний base URL.
    return """try {
for (const k of ['cli-proxy-auth','apiBase','apiUrl']) localStorage.removeItem(k);
localStorage.setItem('managementKey',SENTINEL);
localStorage.setItem('isLoggedIn','true');
} catch (_) {}""".replace('SENTINEL', json.dumps(SENTINEL))


def interceptor(token):
    if not re.fullmatch(r'[A-Za-z0-9]{32,64}', token):
        raise ValueError('invalid Django CSRF token')
    return """(() => {
'use strict';
const csrf = TOKEN;
const unsafe = m => !['GET','HEAD','OPTIONS','TRACE'].includes(m.toUpperCase());
const managed = u => u.pathname.startsWith('/v8/management/');
const check = u => { if (managed(u) && u.origin !== location.origin) throw new TypeError('Management origin refused'); };
const fetch = window.fetch.bind(window);
window.fetch = (input, init) => {
  const r = new Request(input, init), u = new URL(r.url, location.href);
  check(u);
  if (!managed(u)) return fetch(input, init);
  const headers = new Headers(r.headers);
  headers.delete('Authorization');
  if (unsafe(r.method)) headers.set('X-CSRFToken', csrf);
  return fetch(new Request(r, {headers, credentials:'same-origin', redirect:'error'}));
};
const states = new WeakMap(), proto = XMLHttpRequest.prototype;
const open = proto.open, send = proto.send, header = proto.setRequestHeader;
proto.open = function(method, url, ...rest) {
  const u = new URL(url, location.href); check(u);
  states.set(this, {method, managed:managed(u)});
  return open.call(this, method, url, ...rest);
};
proto.setRequestHeader = function(name, value) {
  const s = states.get(this);
  if (s && s.managed && ['authorization','x-csrftoken'].includes(name.toLowerCase())) return;
  return header.call(this, name, value);
};
proto.send = function(body) {
  const s = states.get(this);
  if (s && s.managed) {
    this.withCredentials = true;
    if (unsafe(s.method)) header.call(this, 'X-CSRFToken', csrf);
  }
  return send.call(this, body);
};
})();""".replace('TOKEN', json.dumps(token))


def shim():
    return ('<!doctype html><html><head><meta charset="utf-8"><title>LLM Proxy</title>'
            '</head><body><script>' + bootstrap() +
            "location.replace('/llm/raw/management.html');</script></body></html>").encode()


def adapt(body, token):
    if len(body) > MAX_BODY:
        raise ValueError('proxy management panel too large')
    text = body.decode('utf-8')
    head = re.search(r'<head(?:\s[^<>]{0,1024})?>', text, flags=re.IGNORECASE)
    if head is None:
        raise ValueError('proxy management panel has no bounded head')
    # Перед любым third-party script, без iframe и без скачивания других assets.
    script = '<script>' + bootstrap() + interceptor(token) + '</script>'
    result = (text[:head.end()] + script + text[head.end():]).encode()
    if len(result) > MAX_BODY:
        raise ValueError('proxy management adapted panel too large')
    return result
