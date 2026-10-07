#!/usr/bin/env python3
"""Вход на сервер и создание joined-конфига без живой шины: python3 tests/join.py."""
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.cli.pool import login as join  # noqa: E402
from mop.common import busnames, config, context, creds  # noqa: E402


# HYPOTHESIS: join кладёт operator.json сразу после проверки шины, но URL и
# ключ прокси не спрашивает: даже успешный вход оставляет master без модели.
# SOLUTION: сначала проверка TLS и шины, затем запрос и проверка /v1/models;
# лишь после полного успеха -- запись (#533: одна атомарная connection.json,
# прежние operator/client писались двумя и смешивали входы при сбое).
# Старый вход дополняется, а ошибочный новый ключ не заменяет прежнюю запись.
# RESULT: неуспешная проверка не оставляет частичного нового конфига,
# неоднозначный сервер отклоняется, прежний TLS-пин остаётся при отказе.
# STATUS: FIXED — see #395
def check_complete_395(c):
    complete = getattr(join, "complete", None)
    if not c.check("#395 join.complete exists", complete is not None):
        return
    profile = getattr(creds, "human_profile", None)
    if not c.check("#533 human_profile exists", profile is not None):
        return
    root = tempfile.mkdtemp(prefix="mop-join-395-")
    dest = os.path.join(root, "servers", "srv.test")
    asked, probed = [], []
    record = {"https_port": "443", "proxy_url": "https://srv.test/llm",
              "proxy_key": "proxy-secret"}

    def ask(prompt, secret=False, default=None):
        asked.append((prompt, secret))
        return None

    def probe(url, key, directory):
        c.expect("#395 bus credentials are not persisted before proxy check",
                 profile(dest), None)
        probed.append((url, key, directory))

    with patched(join, ask=ask, _login=lambda *args: "bus-secret",
                 _still_valid=lambda *args: False, _fetch=lambda *args: record,
                 _probe=probe):
        complete("srv.test", "alice", dest, "443")
    c.expect("#395 the whole login is written after success", profile(dest),
             {"user": "alice", "password": "bus-secret", "https_port": "443",
              "proxy_url": "https://srv.test/llm", "proxy_key": "proxy-secret"})
    c.expect("#395 proxy URL and key are checked", probed,
             [("https://srv.test/llm", "proxy-secret", dest)])
    c.expect("#405 proxy settings do not need a second prompt", asked, [])

    # На старом входе шина уже работает: донастроить только прокси, пароль
    # оператора и TLS-пин при отказе нового ключа не трогать.
    old = os.path.join(root, "servers", "old.test")
    creds.write_operator(old, "alice", "old-bus")
    with patched(join, ask=lambda *a, **k: c.fail("#405 proxy must not prompt"),
                 _still_valid=lambda *a: True,
                 _login=lambda *a: c.fail("#395 valid bus login must not ask for password"),
                 _fetch=lambda *a: {"https_port": "443", "proxy_url": "https://old.test/llm",
                                    "proxy_key": "wrong"},
                 _probe=lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 401"))):
        try:
            complete("old.test", "alice", old, "443")
            c.fail("#395 wrong proxy key must refuse")
        except RuntimeError:
            pass
    c.expect("#395 failed proxy verification leaves old bus login", creds.operator(old),
             {"user": "alice", "password": "old-bus"})
    c.check("#395 failed proxy verification leaves no new profile",
            not os.path.exists(os.path.join(old, creds.CONNECTION_FILE)),
            os.listdir(old))

    # При повторном входе всё проверяется, но не спрашивается заново.
    with patched(join, ask=lambda *a, **k: c.fail("#395 complete join must not prompt"),
                 _still_valid=lambda *a: True,
                 _login=lambda *a: c.fail("#395 complete join must not ask for bus password"),
                 _fetch=lambda *a: record, _probe=lambda *a: None):
        complete("srv.test", "alice", dest, "443")
    c.expect("#395 rejoin does not change credentials", profile(dest),
             {"user": "alice", "password": "bus-secret", "https_port": "443",
              "proxy_url": "https://srv.test/llm", "proxy_key": "proxy-secret"})


def check_no_local_env_395(c):
    # Даже до введения source-policy (#396) join не зовёт _load напрямую;
    # и без известного сервера спрашивает адрес вместо .env установки.
    asked = []
    def no_load():
        raise AssertionError("join opened installation .env")
    with patched(config, _load=no_load), patched(join, serving=lambda *a: [],
            git=lambda *a: None, ask=lambda prompt, **kw: asked.append(prompt) or "srv.test",
            complete=lambda *a, **k: None), patched_env(MOP_SERVER_LAN=None):
        try:
            join.main(["alice"])
        except AssertionError as e:
            c.fail("#395 join must never directly load installation env", str(e))
            return
    c.check("#395 join prompts for an unknown server address", bool(asked), asked)


def check_ambiguous_server_395(c):
    # HYPOTHESIS: неоднозначность нельзя превращать в запрос адреса: на машине
    # без TTY это скрывало бы настоящую причину отказа.
    errors = []
    with context.use(context.resolve({}, {}, {})), patched(
            join, serving=lambda *a: ["one.test", "two.test"],
            git=lambda *a: "origin" if a and a[0] == "rev-parse" else None,
            ask=lambda *a, **k: c.fail("#395 ambiguous server must not prompt"),
            complete=lambda *a, **k: c.fail("#395 ambiguous server must not connect")), \
            patched(join.lib, cwd_origin=lambda: "origin", fail=errors.append):
        code = join.main(["alice"])
    c.expect("#395 ambiguous server refuses", code, 1)
    c.check("#395 ambiguity names both servers", bool(errors) and
            all(host in errors[0] for host in ("one.test", "two.test")), errors)


def check_failed_login_keeps_pin_395(c):
    # HYPOTHESIS: проверка TLS не должна закреплять новый сертификат, если
    # шина не приняла пароль; прежний пин нужен для следующего входа.
    dest = os.path.join(tempfile.mkdtemp(prefix="mop-pin-395-"), "srv.test")
    old_der, new_der = b"old certificate", b"new certificate"
    creds.write_cert(dest, old_der)
    with patched(creds, untrusted_cert=lambda *a: new_der), \
            patched(join.bus, check=lambda *a: (_ for _ in ()).throw(RuntimeError("wrong password"))), \
            patched_env(MOP_BUS_PASSWORD="wrong"):
        try:
            join._login("srv.test", "alice", dest, "443")
            c.fail("#395 invalid bus login must refuse")
        except RuntimeError:
            pass
    c.expect("#395 failed login retains previous TLS pin", join._pinned(creds.cafile(dest)), old_der)

    with patched(join, _login=lambda *a: (creds.write_cert(dest, new_der) and "bus-secret"),
                 _probe=lambda *a: (_ for _ in ()).throw(RuntimeError("HTTP 401")),
                 _fetch=lambda *a: {"https_port": "443", "proxy_url": "https://srv.test/llm",
                                    "proxy_key": "wrong"}):
        try:
            join.complete("srv.test", "alice", dest, "443")
            c.fail("#395 invalid proxy login must refuse")
        except RuntimeError:
            pass
    c.expect("#395 failed proxy check retains previous TLS pin", join._pinned(creds.cafile(dest)), old_der)
    c.expect("#395 failed proxy check leaves no client record", creds.client(dest), None)


# HYPOTHESIS: после успешного входа join спрашивает общий URL/ключ вручную;
# повторный вход остаётся на старом ключе вместо обновления с сервера.
# SOLUTION: персональный запрос по тем же проверенным кредам шины, затем
# проверка модели и запись; при отказе старый конфиг остаётся неизменным.
# RESULT: личный RPC даёт URL/ключ, повторный join обновляет ротацию,
# отказ или несовпадение портов не меняют прежний конфиг.
# STATUS: FIXED — see #405
def check_fetch_proxy_405(c):
    root = tempfile.mkdtemp(prefix="mop-join-405-")
    dest = os.path.join(root, "srv.test")
    got = {"ok": True, "https_port": "443", "proxy_url": "https://srv.test:443/llm",
           "proxy_key": "server-key"}
    asked, requests, verified = [], [], []

    def ask(prompt, **kw):
        asked.append(prompt)
        return "https://srv.test:443/llm" if "URL" in prompt else "manual-key"

    def request(conf, subject, verb):
        requests.append((conf, subject, verb))
        return got

    with patched(join, ask=ask, _login=lambda *a: "bus-secret",
                 _probe=lambda url, key, directory: verified.append((url, key, directory))), \
            patched(join.bus, ask_once=request):
        join.complete("srv.test", "alice", dest, "443")
    c.expect("#405 join does not ask for proxy URL or key", asked, [])
    c.expect("#405 fetch uses verified personal subject", requests[0][1:] if requests else None,
             (busnames.join_config("alice"), "client_config"))
    if requests:
        c.expect("#405 fetch reuses bus credentials and TLS port",
                 (requests[0][0]["user"], requests[0][0]["password"], requests[0][0]["url"]),
                 ("alice", "bus-secret", "wss://srv.test:443/nats"))
    c.expect("#405 fetched settings verified before saving", verified,
             [(got["proxy_url"], got["proxy_key"], dest)])
    c.expect("#405 saved key is the server's",
             {k: creds.human_profile(dest)[k] for k in ("https_port", "proxy_url", "proxy_key")},
             {"https_port": "443", "proxy_url": got["proxy_url"], "proxy_key": "server-key"})

    creds.write_connection(dest, "alice", "bus-secret", "443", got["proxy_url"], "old-key")
    with patched(join, ask=ask, _still_valid=lambda *a: True,
                 _login=lambda *a: c.fail("#405 valid bus credentials must be reused"),
                 _probe=lambda *a: None), patched(join.bus, ask_once=request):
        join.complete("srv.test", "alice", dest, "443")
    c.expect("#405 rotated key replaces old key",
             creds.human_profile(dest)["proxy_key"], "server-key")

    old = creds.human_profile(dest)
    with patched(join, _still_valid=lambda *a: True, _probe=lambda *a: None), \
            patched(join.bus, ask_once=lambda *a: {"error": "not authorized"}):
        try:
            join.complete("srv.test", "alice", dest, "443")
            c.fail("#405 refused RPC must fail join")
        except RuntimeError:
            pass
    c.expect("#405 failed RPC leaves previous client config", creds.human_profile(dest), old)
    with patched(join, _still_valid=lambda *a: True,
                 _probe=lambda *a: c.fail("#405 mismatched port must not send key")), \
            patched(join.bus, ask_once=lambda *a: dict(got, https_port="8443")):
        try:
            join.complete("srv.test", "alice", dest, "443")
            c.fail("#405 inconsistent bus/proxy port must refuse")
        except RuntimeError:
            pass
    c.expect("#405 mismatched port leaves previous client config",
             creds.human_profile(dest), old)


# HYPOTHESIS (#427): оба пути входа спрашивают HTTPS-порт даже при обычном
# 443, а явный вход без контекста не использует единственный joined-сервер.
# SOLUTION: явный --https-port, затем сохранённый порт, окружение и 443;
# одинаковый выбор одного joined-сервера, неоднозначность -- отказ.
# RESULT: до правки 11 отказов; после неё 55 проверок входа зелёные.
# STATUS: FIXED — see #427

def check_login_port_and_server_427(c):
    from _lib import patched_env
    def run(argv):
        try:
            return join.main(argv)
        except (SystemExit, ValueError):
            return 1
    for argv, expected in ((["alice", "--https-port", "8443"], (None, "alice", "8443")),
                           (["--https-port=8443", "alice"], (None, "alice", "8443"))):
        try:
            got = join.parse(argv)
        except SystemExit:
            got = None
        c.expect("#427 explicit port syntax", got, expected)

    root = os.path.join(tempfile.mkdtemp(prefix="mop-login-427-"), "servers")
    calls, errors, prompts = [], [], []
    def complete(*args, **kw):
        calls.append(args)
    def ask(prompt, **kw):
        prompts.append(prompt)
        return "new.test" if prompt == "server address: " else "unexpected answer"
    with patched(creds, ROOT=root), patched(join, git=lambda *a: None, serving=lambda *a: [],
                 complete=complete, ask=ask), patched(join.lib, fail=errors.append), \
            context.use(context.resolve({}, {}, {})), patched_env(MOP_HTTPS_PORT="9443"):
        for argv, expected in ((["--server", "srv.test", "alice", "--https-port", "8443"], "8443"),
                               (["--server", "srv.test", "alice", "--https-port=8555"], "8555"),
                               (["--server", "srv.test", "alice"], "9443")):
            calls.clear()
            c.expect("#427 explicit login succeeds", run(argv), 0)
            c.expect("#427 port precedence", calls[-1][3] if calls else None, expected)
        c.expect("#427 no routine port question", prompts, [])
        calls.clear()
        c.expect("#427 invalid port refuses", run(["--server", "srv.test", "alice",
                                                           "--https-port", "65536"]), 1)
        c.expect("#427 invalid port never connects", calls, [])
        c.check("#427 invalid port explained", bool(errors) and "port" in errors[-1].lower())

    with patched(creds, ROOT=root), patched(join, git=lambda *a: None, serving=lambda *a: [],
                 complete=complete, ask=ask), patched(join.lib, fail=errors.append), \
            patched_env(MOP_HTTPS_PORT=None), \
            context.use(context.resolve({}, {}, {})):
        calls.clear()
        c.expect("#427 silent default succeeds", run(["--server", "srv.test", "alice"]), 0)
        c.expect("#427 silent default is 443", calls[-1][3] if calls else None, "443")
        c.expect("#427 still no port question", prompts, [])
        calls.clear()
        c.expect("#427 empty explicit port refuses", run(["--server", "srv.test", "alice",
                                                          "--https-port="]), 1)
        c.expect("#427 empty port never connects", calls, [])
        creds.write_client(os.path.join(root, "srv.test"), "7443",
                           "https://srv.test:7443/llm", "fake-key")
        calls.clear()
        c.expect("#427 saved port succeeds", run(["--server", "srv.test", "alice"]), 0)
        c.expect("#427 saved port reused", calls[-1][3] if calls else None, "7443")
        calls.clear()
        c.expect("#427 explicit port overrides saved", run(["--server", "srv.test", "alice",
                                                              "--https-port", "8555"]), 0)
        c.expect("#427 explicit override selected", calls[-1][3] if calls else None, "8555")
        calls.clear()
        c.expect("#427 one joined server chosen", run(["alice"]), 0)
        c.expect("#427 one joined server address", calls[-1][0] if calls else None, "srv.test")
        creds.write_client(os.path.join(root, "other.test"), "443",
                           "https://other.test/llm", "fake-key")
        calls.clear()
        c.expect("#427 two joined servers refuse", run(["alice"]), 1)
        c.expect("#427 ambiguity never connects", calls, [])
        c.check("#427 ambiguity explained", bool(errors) and "multiple" in errors[-1].lower())

    with patched(creds, ROOT=root), patched(join, git=lambda *a: None, serving=lambda *a: [],
                 complete=complete, ask=ask), patched_env(MOP_HTTPS_PORT="9443"), \
            context.use(context.resolve({}, {}, {})):
        calls.clear()
        c.expect("#427 saved port beats environment", run(["--server", "srv.test", "alice"]), 0)
        c.expect("#427 saved port is preferred", calls[-1][3] if calls else None, "7443")

    with patched(creds, ROOT=os.path.join(root, "new")), patched(
            join, git=lambda *a: None, serving=lambda *a: [], complete=complete, ask=ask), \
            context.use(context.resolve({}, {}, {})), patched_env(MOP_HTTPS_PORT=None):
        prompts.clear()
        calls.clear()
        c.expect("#427 first login may ask address", run(["alice"]), 0)
        c.expect("#427 first login asks only address", prompts, ["server address: "])
        c.expect("#427 first login defaults port", calls[-1][3] if calls else None, "443")


def main():
    c = Checks()
    check_login_port_and_server_427(c)
    check_fetch_proxy_405(c)
    check_complete_395(c)
    check_no_local_env_395(c)
    check_ambiguous_server_395(c)
    check_failed_login_keeps_pin_395(c)
    return c.report("join")


if __name__ == "__main__":
    sys.exit(main())
