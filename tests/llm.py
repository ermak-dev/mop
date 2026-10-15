#!/usr/bin/env python3
"""Проверка окружения LLM без пула: python3 tests/llm.py

Профилей больше нет (#390): окружение сессии одно для папетов и мастеров --
адрес прокси из настройки, карта моделей, имя ключа. Всё -- чистые данные.
"""
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, context, creds, llm  # noqa: E402


# HYPOTHESIS (#547): адрес прокси живёт в двух формах -- канонический
# origin сервера (новый издатель) и легаси mop-issued https://host:port/llm;
# потребители клеили /v1/* сами, а origin для ANTHROPIC_BASE_URL не отличали
# ничего: чужой /llm обрезан был бы догадкой, база с /v1 отдала бы claude
# /v1/v1 на первом же запросе.
# SOLUTION: один URL-контракт в llm: canonical() снимает ровно легаси-/llm
# ТОЛЬКО при ЯВНОМ совпадении issuer-authority (выбранный сервер +
# сохранённый порт профиля), scheme/hostname/port сохранены; endpoint()
# надстраивает ровно один /v1/<resource>, уже-v1 база не удваивается;
# origin() для ANTHROPIC_BASE_URL отказывает на /v1-базе. Стандартный
# urllib.parse -- работа с URL, не второй парсер профиля.
# RESULT: RED до появления контракта в модуле.
# STATUS: RED
def check_url_contract_547(c):
    # Управляемая нормализация: authority издателя известен явно.
    c.expect("#547 managed legacy /llm canonicalizes to the issuer origin",
             llm.canonical("https://srv.test:8443/llm", host="srv.test", port="8443"),
             "https://srv.test:8443")
    c.expect("#547 trailing-slash legacy form canonicalizes the same",
             llm.canonical("https://srv.test:8443/llm/", host="srv.test", port="8443"),
             "https://srv.test:8443")
    c.expect("#547 implicit 443 equals the saved port",
             llm.canonical("https://srv.test/llm", host="srv.test", port="443"),
             "https://srv.test")
    c.expect("#547 already canonical origin is a no-op",
             llm.canonical("https://srv.test:8443", host="srv.test", port="8443"),
             "https://srv.test:8443")
    # Чужой authority и произвольный путь -- НЕ обрезать догадкой: /llm на
    # чужом хосте бывает законным кастомным префиксом.
    c.expect("#547 foreign authority with /llm stays untouched",
             llm.canonical("https://foreign.test:8443/llm", host="srv.test", port="8443"),
             "https://foreign.test:8443/llm")
    c.expect("#547 mismatched port stays untouched",
             llm.canonical("https://srv.test:9443/llm", host="srv.test", port="8443"),
             "https://srv.test:9443/llm")
    c.expect("#547 plain http is not the issuer form",
             llm.canonical("http://srv.test:8443/llm", host="srv.test", port="8443"),
             "http://srv.test:8443/llm")
    c.expect("#547 custom base path stays untouched",
             llm.canonical("https://srv.test:8443/anthropic", host="srv.test", port="8443"),
             "https://srv.test:8443/anthropic")
    c.expect("#547 without issuer hints canonical() does not guess",
             llm.canonical("https://any.test:8443/llm"),
             "https://any.test:8443/llm")
    # Authority -- ДВЕ подсказки, не одна (#547, ревью мастера): host без
    # порта снял бы /llm с чужого порта того же имени; порт без хоста --
    # с чужого хоста. Известен только один -- НЕ наш издатель, вход цел.
    c.expect("#547 host alone is not the issuer: a foreign port keeps /llm",
             llm.canonical("https://srv.test:8443/llm", host="srv.test"),
             "https://srv.test:8443/llm")
    c.expect("#547 port alone is not the issuer: a foreign host keeps /llm",
             llm.canonical("https://foreign.test:8443/llm", port="8443"),
             "https://foreign.test:8443/llm")
    # endpoint: ровно один /v1; уже-v1 база -- resource-aware, без /v1/v1.
    c.expect("#547 endpoint over a canonical origin",
             llm.endpoint("https://srv.test:8443", "models"),
             "https://srv.test:8443/v1/models")
    c.expect("#547 endpoint over managed legacy /llm",
             llm.endpoint("https://srv.test:8443/llm", "models",
                          host="srv.test", port="8443"),
             "https://srv.test:8443/v1/models")
    c.expect("#547 custom base path gets its single /v1",
             llm.endpoint("https://x.test/anthropic", "models"),
             "https://x.test/anthropic/v1/models")
    c.expect("#547 already-v1 base never doubles",
             llm.endpoint("https://x.test/v1", "models"), "https://x.test/v1/models")
    c.expect("#547 already-v1 base with trailing slash",
             llm.endpoint("https://x.test/v1/", "models"), "https://x.test/v1/models")
    c.expect("#547 messages rides the same contract",
             llm.endpoint("https://srv.test:8443/llm", "messages",
                          host="srv.test", port="8443"),
             "https://srv.test:8443/v1/messages")
    c.expect("#547 trailing slash on the origin",
             llm.endpoint("https://srv.test:8443/", "models"),
             "https://srv.test:8443/v1/models")
    c.expect("#547 internal root origin passes through",
             llm.endpoint("http://mop.corp.ermak.dev:8317", "models"),
             "http://mop.corp.ermak.dev:8317/v1/models")
    for bad in ("", "x/y", "/abs", ".", "..", "models?x", "models#f", None,
                "http://evil.test"):
        try:
            llm.endpoint("https://srv.test:8443", bad)
            c.fail(f"#547 resource {bad!r} must be refused", repr(bad))
        except ValueError:
            c.check(f"#547 resource {bad!r} refused as a non-segment", True)
    # origin для ANTHROPIC_BASE_URL: /v1-база -- явный отказ, не тихое /v1/v1.
    c.expect("#547 origin of a managed legacy base",
             llm.origin("https://srv.test:8443/llm", host="srv.test", port="8443"),
             "https://srv.test:8443")
    c.expect("#547 origin keeps a custom base path",
             llm.origin("https://x.test/anthropic"), "https://x.test/anthropic")
    try:
        llm.origin("https://x.test/v1")
        c.fail("#547 a /v1 base must refuse for origin-only callers",
               "https://x.test/v1")
    except ValueError as e:
        c.check("#547 a /v1 base refuses with a URL-contract reason",
                "v1" in str(e) and "origin" in str(e), str(e))


# HYPOTHESIS: сессия и проверка прокси берут URL/ключ из .env, а без
# привязки сервер угадывается даже если join есть на двух установках.
# SOLUTION: выбранный сервер и его client.json — единственный источник для
# клиентских команд; без выбора при двух входах явный отказ.
# RESULT: два сервера разделены, один выбирается без подсказки, два отказывают.
# STATUS: FIXED — see #397
def check_joined_proxy_397(c):
    from mop.cli.core import _common
    root = os.path.join(tempfile.mkdtemp(prefix="mop-llm-397-"), "servers")
    with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN=None,
                                                MOP_PROXY_URL="http://wrong.test",
                                                MOP_PROXY_KEY="wrong-key"):
        # #533: целый вход -- одна connection.json (пароль шины + прокси вместе),
        # прежняя пара больше не пишется. #547: адреса -- фактическая легаси-
        # форма издателя (порт прописан явно, /llm на конце), читатели
        # нормализуют её в канонический origin.
        for host, url, key, port in (("a.test", "https://a.test:8443/llm", "a-key", "8443"),
                                     ("b.test", "https://b.test:443/llm", "b-key", "443")):
            creds.write_connection(os.path.join(root, host), "user", "bus-pw", port, url, key)
        with config.client_sources():
            with context.use(context.resolve({"server": "a.test"}, {}, {})):
                result = _common.session_env()
                c.expect("#397/#547 a legacy mop-issued URL canonicalizes for the session",
                         result["ANTHROPIC_BASE_URL"], "https://a.test:8443")
                c.expect("#397 selected server supplies session key", result[llm.AUTH_VAR],
                         "a-key")
            with context.use(context.resolve({"server": "b.test"}, {}, {})):
                c.expect("#397 second server is independent", _common.session_env()[llm.AUTH_VAR],
                         "b-key")
            with context.use(context.resolve({}, {}, {})):
                try:
                    _common.session_env()
                    c.fail("#397 two joined servers require an explicit selection")
                except (RuntimeError, ValueError) as e:
                    c.check("#397 ambiguity identifies server choice", "server" in str(e).lower(),
                            str(e))
                try:
                    config.get("MOP_SERVER_LAN")
                    c.fail("#397 roster must not guess a server")
                except (RuntimeError, ValueError):
                    pass
        os.remove(os.path.join(root, "b.test", creds.CONNECTION_FILE))
        with config.client_sources(), context.use(context.resolve({}, {}, {})):
            c.expect("#397 one joined server is the default", config.get("MOP_SERVER_LAN"),
                     "a.test")
            c.expect("#397 mop code can use the only server", _common.session_env()[llm.AUTH_VAR],
                     "a-key")


def check_session_ca_412(c):
    # HYPOTHESIS: session_env передаёт URL/ключ, но Node.js не получает
    # сохранённый сертификат; с ним та же сессия проходит TLS.
    # SOLUTION: для HTTPS передать tls.pem выбранного сервера через
    # NODE_EXTRA_CA_CERTS, не заменяя явный выбор пользователя.
    # RESULT: до исправления оба сервера давали None вместо пути CA.
    # STATUS: FIXED — see #412
    from mop.cli.core import _common
    with tempfile.TemporaryDirectory(prefix="mop-ca-412-") as root, \
            patched(creds, ROOT=root), patched_env(NODE_EXTRA_CA_CERTS=None), \
            config.client_sources():
        for host in ("a.test", "b.test"):
            directory = os.path.join(root, host)
            creds.write_connection(directory, "user", "bus-pw", "443",
                                   f"https://{host}:443/llm", "key")
            cert = os.path.join(directory, creds.CERT_FILE)
            with open(cert, "w") as f:
                f.write("test certificate")
            with context.use(context.resolve({"server": host}, {}, {})):
                c.expect(f"#412 Claude trusts the selected server {host}",
                         _common.session_env().get("NODE_EXTRA_CA_CERTS"), cert)
        with context.use(context.resolve({"server": "a.test"}, {}, {})):
            with patched_env(NODE_EXTRA_CA_CERTS="/custom/ca.pem"):
                env = dict(os.environ, **_common.session_env())
                c.expect("#412 explicit user CA is preserved", env.get("NODE_EXTRA_CA_CERTS"),
                         "/custom/ca.pem")
            creds.write_connection(os.path.join(root, "a.test"), "user", "bus-pw", "443",
                                   "http://a.test:443/llm", "key")
            c.check("#412 HTTP needs no extra CA", "NODE_EXTRA_CA_CERTS" not in _common.session_env())
            creds.write_connection(os.path.join(root, "a.test"), "user", "bus-pw", "443",
                                   "https://a.test:443/llm", "key")
            os.remove(os.path.join(root, "a.test", creds.CERT_FILE))
            c.check("#412 no nonexistent CA path", "NODE_EXTRA_CA_CERTS" not in _common.session_env())



# HYPOTHESIS (#426): клиент переносит статическую GLM-карту папета даже
# на сервер, где GLM нет; Claude без gateway discovery не видит каталог.
# SOLUTION: клиентский запуск убирает модельные алиасы (включая унаследованные)
# и включает discovery, оставляя папетскую спеку и выбранный gateway без перемен.
# RESULT: до правки восемь отказов на двух серверах; теперь 75 проверок
# llm и 228 проверок спеки зелёные без запуска claude и настоящих ключей.
# После #449 «нет перемен у папета» значит форму #449: без карты моделей,
# с каталогом; проверка переформулирована под новый контракт.
# STATUS: FIXED — see #426

def check_client_model_discovery_426(c):
    from mop.cli.core import _common, code, master
    from mop.server import spec
    slots = ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
             "ANTHROPIC_DEFAULT_HAIKU_MODEL")
    captured = []

    def fake_exec(binary, args, env):
        captured.append((binary, args, env))

    with tempfile.TemporaryDirectory(prefix="mop-426-") as root, \
            patched(creds, ROOT=root), \
            patched_env(MOP_SERVER_LAN=None,
                        ANTHROPIC_DEFAULT_OPUS_MODEL="old-parent-glm",
                        ANTHROPIC_DEFAULT_SONNET_MODEL="old-parent-glm",
                        ANTHROPIC_DEFAULT_HAIKU_MODEL="old-parent-glm",
                        CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY="0",
                        MOP_426_UNRELATED="kept"), \
            config.client_sources(), \
            patched(code.login, ensure=lambda: None), \
            patched(_common, feedback_fd=lambda *a: ("/proc/self/fd/3", 3)), \
            patched(code.os, execvpe=fake_exec):
        for host in ("a.test", "b.test"):
            creds.write_connection(os.path.join(root, host), "user", "bus-pw", "443",
                                   f"https://{host}:443/llm", f"{host}-fake-key")
            with context.use(context.resolve({"server": host}, {}, {})):
                code.main(["--model", "gpt-example"])
            for binary, args, env in captured[-1:]:
                c.expect(f"#426/#547 {host} gateway is the canonical origin",
                         env.get("ANTHROPIC_BASE_URL"), f"https://{host}:443")
                c.expect(f"#426 {host} selected client key", env.get(llm.AUTH_VAR),
                         f"{host}-fake-key")
                c.expect("#426 native gateway discovery enabled",
                         env.get("CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"), "1")
                c.check("#426 no mop or inherited model aliases",
                        all(slot not in env for slot in slots))
                c.expect("#426 unrelated environment kept", env.get("MOP_426_UNRELATED"),
                         "kept")
                c.check("#426 explicit model argument kept", "gpt-example" in args)
                c.expect("#426 claude binary selected", binary, "claude")
    # С #449 контракт папета сменился: спека не квасит карту моделей, а
    # включает каталог (tests/spec.py, check_llm_env_449). Смысл #426 жив --
    # клиентская мутация (снятие алиасов для СВОЕЙ сессии) не трогает спеку:
    # папетское окружение не зависит от клиентского и имеет форму #449.
    baked = dict(kv.split("=", 1) for kv in __import__("base64").b64decode(
        spec.task_env("pu-x-1", "git@h:g/x.git")["PU_LLM_ENV"]).decode().splitlines())
    c.check("#426 client-only change leaves puppet model environment intact",
            all(slot not in baked for slot in slots))
    c.expect("#426 puppet env has the #449 shape regardless of the client",
             baked, {k: v for k, v in llm.env().items()
                     if not k.startswith("ANTHROPIC_DEFAULT_")}
             | {"CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"})


# #662: локальный запуск мастера снят; контракт полного argv с именем
# claude у единственного локального exec (mop code) проверяет свой блок
# ниже (#440-инвариант сохранён там, где exec ещё есть).
def check_master_exec_argv_440(c):
    from mop.cli.core import code
    called = []

    def fake_exec(binary, args, env):
        called.append((binary, args))

    with patched(code.login, ensure=lambda: None), \
            patched(code._common, launch_env=lambda: {},
                    feedback_fd=lambda *a: ("/proc/self/fd/3", 3)), \
            patched(code.os, execvpe=fake_exec):
        code.main([])

    binary, args = called[0]
    c.expect("#440 code executes claude", binary, "claude")
    c.expect("#440 executable occupies argv[0]", args[0], "claude")
    c.check("#440 bypass is parsed as a flag",
            "--dangerously-skip-permissions" in args, args)


# HYPOTHESIS (#547): окружение папета строится из MOP_PROXY_URL как есть:
# легаси-форма самой установки (/llm на её же TLS-прокси) ехала в
# ANTHROPIC_BASE_URL префиксом, а чужой кастомный путь -- оставаться чужим
# выбором. #557 добавляет третий случай: СОБСТВЕННЫЙ внутренний корень
# установки (её authority плюс MOP_LLM_PROXY_PORT, путь пуст) -- это не
# «чужой выбор», а адрес впрямую к порту прокси мимо TLS, и он
# канонизируется в публичный origin.
# SOLUTION: env() гоняет настройку через тот же контракт origin() с
# подсказками собственного authority установки (MOP_SERVER_LAN +
# MOP_HTTPS_PORT), а до него -- to_public_origin(): свой внутренний корень
# становится публичным origin, чужой адрес проходит насквозь.
# RESULT: RED -- env() пока отдаёт внутренний корень без канонизации.
# STATUS: RED
def check_setting_origin_547(c):
    def settings(url):
        return patched(config, get=lambda name: {
            "MOP_PROXY_URL": url, "MOP_SERVER_LAN": "srv.test",
            "MOP_HTTPS_PORT": "8443", "MOP_LLM_PROXY_PORT": "8317"}.get(name, ""))
    with settings("https://srv.test:8443/llm"):
        c.expect("#547 the puppet env carries the canonical origin",
                 llm.env()["ANTHROPIC_BASE_URL"], "https://srv.test:8443")
    # #557: свой внутренний корень (srv.test -- MOP_SERVER_LAN этой установки,
    # 8317 -- MOP_LLM_PROXY_PORT) канонизируется в публичный origin, scheme
    # внутреннего адреса не важен.
    with settings("http://srv.test:8317"):
        c.expect("#557 the own internal root becomes the public origin",
                 llm.env()["ANTHROPIC_BASE_URL"], "https://srv.test:8443")
    with settings("https://srv.test:8317"):
        c.expect("#557 the own internal root over https canonicalizes too",
                 llm.env()["ANTHROPIC_BASE_URL"], "https://srv.test:8443")
    # Тот же хост, но чужой порт -- не внутренний корень установки: чужой
    # сервис на соседнем порте проходит насквозь, как и раньше.
    with settings("http://srv.test:8318"):
        c.expect("#557 a foreign port of the same host stays untouched",
                 llm.env()["ANTHROPIC_BASE_URL"], "http://srv.test:8318")
    with settings("http://mop.corp.ermak.dev:8317"):
        c.expect("#547/#557 a foreign host with the internal port passes through",
                 llm.env()["ANTHROPIC_BASE_URL"], "http://mop.corp.ermak.dev:8317")
    with settings("https://proxy.vendor.dev/anthropic"):
        c.expect("#547 a foreign custom path stays the operator's choice",
                 llm.env()["ANTHROPIC_BASE_URL"], "https://proxy.vendor.dev/anthropic")
    # #557: пустая настройка -- не пустой адрес: дефолт -- публичный origin
    # установки, литерал внутреннего корня из config исчез.
    with settings(""):
        c.expect("#557 an unset MOP_PROXY_URL means the public origin",
                 llm.env()["ANTHROPIC_BASE_URL"], "https://srv.test:8443")


# HYPOTHESIS (#557): публичный origin установки -- https://<MOP_SERVER_LAN>:
# <MOP_HTTPS_PORT> -- собирали два потребителя по-разному (_client_config
# inline f-строкой, дефолт настройки -- литералом внутреннего корня 8317 из
# config.SETTINGS), и установка без явного MOP_PROXY_URL ездила на
# внутренний порт мимо TLS.
# SOLUTION: llm.public_origin() -- единственная формула; серверный читатель
# каталога ходит не через публичный origin, а петлёй 127.0.0.1:
# MOP_LLM_PROXY_PORT -- он потребитель на самом сервере, TLS прокси ему
# не нужен.
# RESULT: RED -- функций нет.
# STATUS: RED
def check_public_origin_557(c):
    def settings(lan, https, llm_port):
        return patched(config, get=lambda name: {
            "MOP_SERVER_LAN": lan, "MOP_HTTPS_PORT": https,
            "MOP_LLM_PROXY_PORT": llm_port}.get(name, ""))
    with settings("srv.test", "8443", "8317"):
        c.check("#557 RED public_origin exists",
                hasattr(llm, "public_origin"), "no llm.public_origin")
        c.expect("#557 public origin from the installation settings",
                 getattr(llm, "public_origin", lambda: None)(),
                 "https://srv.test:8443")
        c.check("#557 RED internal_loop exists",
                hasattr(llm, "internal_loop"), "no llm.internal_loop")
        loop = getattr(llm, "internal_loop", lambda: None)()
        c.expect("#557 the server catalog reader rides the loop, not TLS",
                 llm.endpoint(loop, "models") if loop else "no llm.internal_loop",
                 "http://127.0.0.1:8317/v1/models")
    with settings("mop.corp.ermak.dev", "443", "8317"):
        c.expect("#557 implicit 443 stays explicit in the formula",
                 getattr(llm, "public_origin", lambda: None)(),
                 "https://mop.corp.ermak.dev:443")
    # to_public_origin: голый внутренний корень СВОЕГО authority заменяется;
    # с путём, юзером, чужим хостом или чужим портом -- вход цел.
    with settings("srv.test", "8443", "8317"):
        to_public = getattr(llm, "to_public_origin",
                            lambda *a, **k: a[0] if a else None)
        for url, want in (
                ("http://srv.test:8317", "https://srv.test:8443"),
                ("https://srv.test:8317/", "https://srv.test:8443"),
                ("http://srv.test:8317/llm", "http://srv.test:8317/llm"),
                ("http://user:pw@srv.test:8317", "http://user:pw@srv.test:8317"),
                ("http://foreign.test:8317", "http://foreign.test:8317"),
                ("http://srv.test:8318", "http://srv.test:8318"),
                ("https://srv.test:8443", "https://srv.test:8443")):
            c.expect(f"#557 to_public_origin({url!r})",
                     to_public(url, host="srv.test", port="8443",
                               internal_port="8317"), want)


# HYPOTHESIS (#547 → #557): серверный читатель каталога (adminapi, юнит
# mop-web) ходил по MOP_PROXY_URL с пином CA, как клиент, -- хотя он
# потребитель на самом сервере, и прокси стоит у него на петле. Ходить
# публичным origin через nginx ему незачем, а пин -- вторая копия
# TLS-правила, которая разъехалась бы с #517.
# SOLUTION: база -- петля http://127.0.0.1:MOP_LLM_PROXY_PORT (llm.
# internal_loop), без TLS и без пина; endpoint надстраивает /v1/models.
# RESULT: RED -- читатель пока зовёт endpoint с подсказками издателя и
# строит ssl-контекст с пином.
# STATUS: RED
def check_catalog_reader_loop_557(c):
    from mop.server import adminapi
    seen = {}

    def fake_endpoint(url, resource, host=None, port=None):
        seen.update(url=url, resource=resource, host=host, port=port)
        return "http://127.0.0.1:8317/v1/models"

    import urllib.request
    from mop.common import llm as llm_mod

    class Ok:
        status = 200

        def read(self):
            return b'{"data":[{"id":"glm-5.3"}]}'

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    settings = {"MOP_LLM_PROXY_PORT": "8317", "MOP_PROXY_URL": "http://x.test:1"}

    def fake_open(req, timeout=10):
        seen["opened"] = req.full_url
        return Ok()

    from mop.server import proxykey

    with patched(llm_mod, endpoint=fake_endpoint), \
            patched(adminapi.config, get=lambda name: settings.get(name, "")), \
            patched(urllib.request, urlopen=fake_open), \
            patched(proxykey, source=lambda *a, **k: "file-key"):
        try:
            got = adminapi.catalog_read()
        except Exception as e:  # noqa: BLE001 -- RED: читатель ещё не на петле
            c.fail(f"#557 RED the catalog reader must not refuse on settings: {e}")
            return
    c.expect("#557 the catalog ids ride the shared parser", got, ["glm-5.3"])
    c.expect("#557 the catalog reader walks the loop base",
             seen.get("opened"), "http://127.0.0.1:8317/v1/models")
    c.check("#557 the reader carries no issuer hints any more",
            (seen.get("host"), seen.get("port")) == (None, None),
            (seen.get("host"), seen.get("port")))


# HYPOTHESIS (#547, ревью мастера): серверный читатель каталога звал
# контракт без порта установки -- host-only подсказка, а с ней (до
# adb998d) canonical снимал /llm с любого порта того же имени.
# #557 убрал подсказки вовсе: читатель ходит петлёй (проверка выше,
# check_catalog_reader_loop_557), и host/port в вызове не участвуют.
# Осталась часть проверки про сам канонический контракт -- в
# check_url_contract_547. STATUS: устранена #557 -- заменена петлёй.

def main():
    c = Checks()
    check_url_contract_547(c)
    check_setting_origin_547(c)
    check_public_origin_557(c)
    check_catalog_reader_loop_557(c)
    check_joined_proxy_397(c)
    check_session_ca_412(c)
    check_client_model_discovery_426(c)
    check_master_exec_argv_440(c)
    # HYPOTHESIS (#390): реестр плагинов-профилей умер вместе с провайдерами:
    # держать его ради одного URL -- против KISS, а ENV профилей перестал
    # быть статикой плагина. SOLUTION: один модуль с env() и именем ключа;
    # адрес -- настройка установки, карта моделей -- константа до переезда
    # алиасов в прокси. STATUS: FIXED — see #390
    # #557 уточняет первый пункт: значение происходит из настройки через
    # контракт (to_public_origin + origin), а не равно ей буквально; здесь
    # -- что адрес непуст и растёт из настройки этой машины (подробно --
    # check_setting_origin_547 и check_public_origin_557 выше).
    env = llm.env()
    c.check("the proxy URL comes from the setting (#390/#557)",
            bool(env["ANTHROPIC_BASE_URL"])
            and env["ANTHROPIC_BASE_URL"].startswith(("http://", "https://")),
            env.get("ANTHROPIC_BASE_URL"))
    c.check("the key rides under its own name (#390)",
            (llm.KEY, llm.AUTH_VAR), ("MOP_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN"))
    c.check("every model slot is named (#390)",
            all(env.get(k) for k in
                ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL")), env)
    c.check("long turns get a long client timeout (#382)",
            env.get("API_TIMEOUT_MS") == "3000000", env.get("API_TIMEOUT_MS"))
    # Спека и локальная сессия обязаны собирать окружение из одного места:
    # иначе врапер и мастер расходились бы молча. С #449 спека собирает
    # окружение папета из llm.env() не буквально: слоты моделей сбрасывает,
    # каталог включает (подробно -- tests/spec.py, check_llm_env_449);
    # здесь -- что едет папету, растёт из этого же модуля. Прежние две
    # проверки звали c.check со значением вместо булева -- и проходили
    # всегда; теперь это настоящие сравнения.
    from mop.server import spec
    from mop.cli.core import _common
    with_normal = spec.task_env("pu-x-1", "git@h:g/x.git")
    baked = dict(kv.split("=", 1) for kv in
                 __import__("base64").b64decode(with_normal["PU_LLM_ENV"]).decode().splitlines())
    c.expect("what rides to the puppet grows from llm.env() (#449)",
             baked, {**{k: v for k, v in llm.env().items()
                        if not k.startswith("ANTHROPIC_DEFAULT_")},
                     "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY": "1"})
    c.expect("the spec reads the key by llm.KEY (#390)",
             with_normal["PU_LLM_KEY_VAR"], llm.KEY)
    c.check("no profile names remain in the tree (#390)",
            not os.path.isdir(os.path.join(os.path.dirname(config.__file__), "llm")))
    # Клиентская сессия из joined-конфига проверяется выше (#397);
    # llm.env() остаётся серверным шаблоном для спеки папета.
    # HYPOTHESIS (#419): у сессий `mop master` и `mop code` инструмент
    # черновиков отзыва и команда /feedback живут своей жизнью: репозиторий
    # их не затыкал, и сессия отправляла черновики/команду от имени машины.
    # SOLUTION: session_env отдаёт и это -- настройкой feedbackDrafts=off
    # (узко: инструмент и очередь черновиков, не зонтик NONESSENTIAL_TRAFFIC,
    # который резал бы обновления плагинов) и переменную DISABLE_FEEDBACK_COMMAND
    # (команда /feedback). Оба значения -- код строки, контракт той же версии
    # claude, что и остальной спека-стенд.
    # RESULT: до исправления -- ни переменной, ни --settings в argv.
    # STATUS: FIXED — see #419
    from mop.cli.core import code, master
    want_args = ('--settings', '{"feedbackDrafts":"off"}')

    # Пара (флаг, значение) должна идти подряд: разрыв -- уже чужой аргумент.
    def carries(argv):
        return any(argv[i:i + 2] == list(want_args) for i in range(len(argv)))

    # session_env требует joined-сервер (#397) -- поднимем его в temp-каталоге,
    # как проверки #397/#412 выше.
    with tempfile.TemporaryDirectory(prefix="mop-419-") as root, \
            patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN=None), \
            config.client_sources():
        creds.write_connection(os.path.join(root, "s.test"), "user", "bus-pw", "443",
                               "https://s.test:443/llm", "key")
        with context.use(context.resolve({}, {}, {})):
            session = _common.session_env()
    c.check("the session env carries the feedback kill switch (#419)",
            session.get("DISABLE_FEEDBACK_COMMAND") == "1", session)
    c.check("mop code passes feedbackDrafts=off to claude (#419)",
            carries(code.claude_argv(["--continue"])), code.claude_argv(["--continue"]))
    # HYPOTHESIS (правка #419, край пасстру): claude 2.1.287 не мержит
    # повторные --settings -- побеждает последний (проверено на живом
    # бинарнике: env из первого значения пропадает), а всё после `--`
    # парсер считает позиционными и флагом уже не читает. Значит append
    # нашего --settings В КОНЕЦ ломит обе края: чужие настройки он
    # затирает, а за `--` вообще не применяется. SOLUTION: вставлять пару
    # ДО пасстру (сразу за постоянными флагами) -- тогда наше значение
    # стоит раньше пользовательского. (Слияние внутрь его значения --
    # ниже, feedback_pair: требование «везде».) Хвост узнаём по
    # последнему элементу, а не по первому (флаг может совпасть с нашим).
    def carries_before_passthru(argv, passthru):
        # Пара подряд, и её конец -- до начала хвоста пасстру: хвост
        # узнаём по последнему элементу (уникальное значение), а не по
        # первому (флаг может совпасть с нашим).
        if not passthru:
            return any(argv[i:i + 2] == list(want_args) for i in range(len(argv)))
        tail = argv[-len(passthru):] if len(passthru) <= len(argv) else []
        if tail != passthru:
            return False
        head = argv[:-len(passthru)] if passthru else argv
        return any(head[i:i + 2] == list(want_args) for i in range(len(head)))

    user = ["--settings", '{"model":"opus"}']

    # Слияние пользовательского значения -- в feedback_settings (ниже);
    # командлетный слой проверяется с готовым memfd-путём.
    dash = ["--", "--settings", "custom.json"]
    c.check("mop code feedback settings stay effective before -- (#419)",
            carries(code.claude_argv(dash))
            and code.claude_argv(dash).index("--") > code.claude_argv(dash).index(want_args[0]),
            code.claude_argv(dash))
    # HYPOTHESIS (правка #419, требование «везде» + законный пасстру):
    # claude повторные --settings не мержит (последний побеждает целиком),
    # и наш off ДО пользовательского значения молча включал черновики,
    # когда у того ключа не было; отказ от его файла ломал пасстру
    # (hooks пользователя живут именно файлами). SOLUTION: последний
    # пользовательский --settings (файл или inline, до первого `--`,
    # форма через пробел и через «=») читается, feedbackDrafts=off
    # накладывается, итог едет ОДНИМ --settings memfd-путём -- без
    # argv-содержимого и без диска (подтверждено живым claude 2.1.287:
    # унаследованный /proc/self/fd/N читается). Свой --settings
    # отсутствует -- своя пара. Недоступный файл, не-JSON, не-объект --
    # ValueError (fail closed), без значения в тексте ошибки.
    # RESULT: слияние держит и off, и чужие env/hooks; края отказывают
    # явно и молча про значения.
    from mop.cli.core._common import feedback_fd, feedback_settings

    def merges(src, read=None):
        value, argv = feedback_settings(list(src), read=read)
        return value, argv, json.loads(value)

    files = {"custom.json": '{"env":{"X":"1"},"hooks":{"PreToolUse":[]}}'}
    read = files.__getitem__

    # Inline: off вписан, чужие env/hooks целы, своего --settings не осталось.
    value, argv, got = merges(["--settings", '{"env":{"X":"1"}}'])
    c.check("inline settings keep keys and gain off (#419)",
            got == {"env": {"X": "1"}, "feedbackDrafts": "off",
                    "permissions": {"deny": ["SendFeedback"]}}
            and "--settings" not in argv, argv)
    # Файл: прочитан, off наложен, путь из argv убран.
    value, argv, got = merges(["--settings", "custom.json"], read=read)
    c.check("settings file is read, merged, path removed (#419)",
            got == {"env": {"X": "1"}, "hooks": {"PreToolUse": []},
                    "feedbackDrafts": "off",
                    "permissions": {"deny": ["SendFeedback"]}}
            and "custom.json" not in argv, argv)
    # =-форма.
    value, argv, got = merges(["--settings=custom.json"], read=read)
    c.check("=form file merged the same way (#419)",
            got.get("feedbackDrafts") == "off"
            and got["permissions"]["deny"] == ["SendFeedback"]
            and "--settings" not in argv,
            argv)
    # Повторные --settings: ровно ОДНА пара в итоге -- merged с последнего;
    # ранние, останься они в пасстру, встали бы ПОСЛЕ нашей пары и вытеснили
    # off (claude повторов не мержит, побеждает последний). Все снимаются.
    value, argv, got = merges(["--settings", '{"env":{"A":"1"}}',
                              "--settings", '{"env":{"B":"2"}}'])
    c.check("repeated settings collapse into one merged pair (#419)",
            got == {"env": {"B": "2"}, "feedbackDrafts": "off",
                    "permissions": {"deny": ["SendFeedback"]}}
            and "--settings" not in argv
            and '{"env": {"A": "1"}}' not in argv, argv)
    # Дефект из ревью: пара не может встать в середину чужого флага с
    # значением. Точный argv: [claude, пара, *пасстру] -- при
    # пользовательском skip-флаге в середине набора.
    argv = code.claude_argv(["--model", "opus", "--dangerously-skip-permissions"],
                            "/proc/self/fd/3")
    c.check("pair never splits a user flag from its value (#419)",
            argv == ["claude", "--settings", "/proc/self/fd/3",
                     "--model", "opus", "--dangerously-skip-permissions"], argv)
    # `--settings` без значения (bare и пустое =) -- ясный отказ до
    # exec, не битый флаг: пустое иначе молча оставалось в пасстру ПОСЛЕ
    # нашей пары и вытесняло off. Inline-JSON с ведущим пробелом --
    # валидный для claude JSON, это не путь: мерджится как inline.
    for bad in (["--settings"], ["--continue", "--settings"],
                ["--settings="], ["--settings", ""]):
        try:
            feedback_settings(bad)
            c.check("dangling --settings must refuse (#419)", False, bad)
        except ValueError as e:
            c.check("dangling --settings must refuse (#419)",
                    "feedbackDrafts" in str(e), str(e))
    try:
        value, argv, got = merges(["--settings", ' {"env":{"X":"1"}}'])
        c.check("leading-space inline JSON merges as inline (#419)",
                got == {"env": {"X": "1"}, "feedbackDrafts": "off",
                        "permissions": {"deny": ["SendFeedback"]}}
                and "--settings" not in argv, argv)
    except ValueError as e:
        c.check("leading-space inline JSON merges as inline (#419)", False, str(e))
    # Требование #419 -- невозможность отправки через tool, а не только
    # очередь черновиков: в merged всегда permissions.deny с SendFeedback
    # (как wrapper #420). Чужой deny сохраняется, добавление идемпотентно,
    # пользовательское allow того же имени не снимает наш deny.
    value, argv, got = merges(["--settings",
                               '{"permissions":{"deny":["AskUserQuestion"]},'
                               '"env":{"X":"1"}}'])
    c.check("user deny kept, SendFeedback added (#419)",
            got["permissions"]["deny"] == ["AskUserQuestion", "SendFeedback"],
            value)
    value, argv, got = merges(["--settings",
                               '{"permissions":{"deny":["SendFeedback"]}}'])
    c.check("SendFeedback deny is idempotent (#419)",
            got["permissions"]["deny"] == ["SendFeedback"], value)
    value, argv, got = merges(["--settings",
                               '{"permissions":{"allow":["SendFeedback"],'
                               '"ask":true}}'])
    c.check("user allow does not lift our deny, other keys intact (#419)",
            got["permissions"]["deny"] == ["SendFeedback"]
            and got["permissions"]["allow"] == ["SendFeedback"]
            and got["permissions"]["ask"] is True, value)
    value, argv, got = merges(["--continue"])
    c.check("own settings carry the deny too (#419)",
            got == {"feedbackDrafts": "off",
                    "permissions": {"deny": ["SendFeedback"]}}, value)
    # Своего --settings нет: своя пара, пасстру нетронут.
    value, argv, got = merges(["--continue"])
    c.check("no user settings -> own value, passthru intact (#419)",
            json.loads(value) == {"feedbackDrafts": "off",
                                  "permissions": {"deny": ["SendFeedback"]}}
            and argv == ["--continue"], (value, argv))
    # За `--` флагов нет: пользовательское значение за разделителем не
    # трогается, ours ставится командлетом в зоне флагов.
    value, argv, got = merges(["--", "--settings", '{"env":{"X":"1"}}'])
    c.check("beyond -- untouched, own value returned (#419)",
            json.loads(value) == {"feedbackDrafts": "off",
                                  "permissions": {"deny": ["SendFeedback"]}}
            and argv == ["--", "--settings", '{"env":{"X":"1"}}'], argv)
    # Fail closed: недоступный файл, не-JSON, не-объект -- ValueError без
    # значения в тексте (секреты не эхом).
    def missing(path):
        raise FileNotFoundError(path)

    for bad, what, reader in (
            (["--settings", "custom.json"], "unreadable file", missing),
            (["--settings", "bad.json"], "not json",
             {"bad.json": "not json"}.__getitem__),
            (["--settings", "arr.json"], "not an object",
             {"arr.json": "[1,2]"}.__getitem__)):
        try:
            feedback_settings(bad, read=reader)
            c.check(f"{what} must refuse (#419)", False, bad)
        except ValueError as e:
            c.check(f"{what} must refuse, no value echoed (#419)",
                    "feedbackDrafts" in str(e) and "custom.json" not in str(e)
                    and "not json" not in str(e) and "[1,2]" not in str(e),
                    str(e))
    # memfd: путь вида /proc/self/fd/N, содержимое читается и равно значению.
    path, fd = feedback_fd(value)
    with open(path, encoding="utf-8") as f:
        c.check("feedback_fd carries the value unread via the fd path (#419)",
                path.startswith("/proc/self/fd/") and f.read() == value, path)
    os.close(fd)
    # Командлеты: один --settings в зоне флагов, до пасстру и до `--`,
    # пара не рвётся даже когда пользователь передал свой skip-флаг.
    argv = code.claude_argv(["--continue"], "/proc/self/fd/3")
    c.check("mop code: one settings flag before the passthru (#419)",
            argv[argv.index("--settings"):argv.index("--settings") + 2]
            == ["--settings", "/proc/self/fd/3"]
            and argv.index("--settings") < argv.index("--continue")
            and argv.count("--settings") == 1, argv)
    argv = code.claude_argv(["--dangerously-skip-permissions", "--continue"],
                            "/proc/self/fd/3")
    c.check("pair intact next to the user skip flag (#419)",
            any(argv[i:i + 2] == ["--settings", "/proc/self/fd/3"]
                for i in range(len(argv))), argv)
    return c.report("llm")


if __name__ == "__main__":
    sys.exit(main())
