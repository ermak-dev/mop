#!/usr/bin/env python3
"""Проверка группы прокси доктора без пула: python3 tests/doctor_proxy.py

Контроллер -- единственная точка всего LLM (#379): упавший прокси
останавливает и папетов, и мастеров, и doctor обязан это видеть.
"""
import os
import ssl
import sys
import tempfile
import urllib.error

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.client.doctor import proxy  # noqa: E402
from mop.common import config, context, creds  # noqa: E402


# HYPOTHESIS: doctor проверяет прокси установки из .env, не выбранного join.
# SOLUTION: запрос строится из client.json выбранного сервера.
# RESULT: doctor отправляет запрос на joined URL с ключом этого сервера.
# STATUS: FIXED — see #397
def check_joined_probe_397(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-probe-397-"), "servers")
    creds.write_client(os.path.join(root, "a.test"), "443", "https://a.test/llm", "secret")
    requests = []
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def open_url(req, timeout):
        requests.append((req.full_url, req.get_header("Authorization")))
        return Response()
    with patched(creds, ROOT=root), patched(proxy.urllib.request, urlopen=open_url), \
            context.use(context.resolve({"server": "a.test"}, {}, {})), \
            config.client_sources():
        c.expect("#397 joined proxy probe succeeds", proxy._probe(), 200)
    c.expect("#397 doctor uses joined endpoint and key", requests,
             [("https://a.test/llm/v1/models", "Bearer secret")])


# HYPOTHESIS: doctor передаёт URL и ключ, но не TLS-пин того же сервера;
# исправный самоподписанный прокси читается как недоступный.
# SOLUTION: для URL выбранного сервера использовать закрытый cafile, не
# отключая проверку; для чужого URL оставить системное доверие.
# RESULT: закреплённый сертификат используется только для своего сервера;
# живой mop doctor proxy после join отвечает «pool is healthy».
# STATUS: FIXED — see #406
def check_pinned_proxy_406(c):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-probe-406-"), "servers")
    dest = os.path.join(root, "srv.test")
    creds.write_client(dest, "443", "https://srv.test/llm", "key")
    seen, marker = [], object()
    class Response:
        status = 200
        def __enter__(self):
            return self
        def __exit__(self, *args):
            pass
    def open_url(req, timeout, context=None):
        seen.append(context)
        return Response()
    with patched(creds, ROOT=root, cafile=lambda d: "/private/tls.pem"), \
            patched(ssl, create_default_context=lambda cafile=None: marker if cafile else None), \
            patched(proxy.urllib.request, urlopen=open_url), \
            context.use(context.resolve({"server": "srv.test"}, {}, {})), \
            config.client_sources():
        c.expect("#406 pinned server probe succeeds", proxy._probe(), 200)
        c.expect("#406 doctor passes pinned TLS context", seen, [marker])
        creds.write_client(dest, "443", "https://other.test/llm", "key")
        seen.clear()
        c.expect("#406 foreign proxy probe succeeds", proxy._probe(), 200)
        c.expect("#406 foreign URL does not inherit server pin", seen, [None])


def main():
    c = Checks()
    check_pinned_proxy_406(c)
    check_joined_probe_397(c)
    # ── разбор ответа /v1/models -> диагноз ─────────────────────────────
    # HYPOTHESIS (#383): прокси молчит -- каждый ход всех сессий пула бьётся
    # в таймаут, а doctor об этом не знает: групп, кроме диска и папетов, нет.
    # SOLUTION: группа proxy -- один GET /v1/models клиентским ключом;
    # не 200 -- проблема без автолечения словами «прокси LLM недоступен»,
    # с причиной: рестарт папетов ничего не чинит, чинить надо юнит на
    # контроллере. STATUS: FIXED — see #383
    c.expect("200 -> no issues", proxy.issues_of(200), [])
    c.check("401 -> an issue, words name the unit, not a puppet restart",
            all(w in proxy.issues_of(401)[0]["diagnosis"] for w in ("LLM proxy", "mop-llm-proxy")),
            proxy.issues_of(401))
    c.check("a network error -> an issue with the reason",
            "refused" in proxy.issues_of(urllib.error.URLError("refused"))[0]["diagnosis"],
            proxy.issues_of(urllib.error.URLError("refused")))
    c.check("every issue has no auto-treatment",
            all(i["action"] is None for i in proxy.issues_of(503) + proxy.issues_of(404)))
    return c.report("doctor_proxy")


if __name__ == "__main__":
    sys.exit(main())
