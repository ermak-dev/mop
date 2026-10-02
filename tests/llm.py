#!/usr/bin/env python3
"""Проверка окружения LLM без пула: python3 tests/llm.py

Профилей больше нет (#390): окружение сессии одно для папетов и мастеров --
адрес прокси из настройки, карта моделей, имя ключа. Всё -- чистые данные.
"""
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, context, creds, llm  # noqa: E402


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
        for host, url, key, port in (("a.test", "https://a.test/llm", "a-key", "8443"),
                                     ("b.test", "https://b.test/llm", "b-key", "443")):
            creds.write_client(os.path.join(root, host), port, url, key)
        with config.client_sources():
            with context.use(context.resolve({"server": "a.test"}, {}, {})):
                result = _common.session_env()
                c.expect("#397 selected server supplies session URL", result["ANTHROPIC_BASE_URL"],
                         "https://a.test/llm")
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
        os.remove(os.path.join(root, "b.test", creds.CLIENT_FILE))
        with config.client_sources(), context.use(context.resolve({}, {}, {})):
            c.expect("#397 one joined server is the default", config.get("MOP_SERVER_LAN"),
                     "a.test")
            c.expect("#397 mop code can use the only server", _common.session_env()[llm.AUTH_VAR],
                     "a-key")


def main():
    c = Checks()
    check_joined_proxy_397(c)
    # HYPOTHESIS (#390): реестр плагинов-профилей умер вместе с провайдерами:
    # держать его ради одного URL -- против KISS, а ENV профилей перестал
    # быть статикой плагина. SOLUTION: один модуль с env() и именем ключа;
    # адрес -- настройка установки, карта моделей -- константа до переезда
    # алиасов в прокси. STATUS: FIXED — see #390
    env = llm.env()
    c.check("the proxy URL comes from the setting (#390)",
            env["ANTHROPIC_BASE_URL"] == config.get("MOP_PROXY_URL")
            and bool(env["ANTHROPIC_BASE_URL"]), env.get("ANTHROPIC_BASE_URL"))
    c.check("the key rides under its own name (#390)",
            (llm.KEY, llm.AUTH_VAR), ("MOP_PROXY_KEY", "ANTHROPIC_AUTH_TOKEN"))
    c.check("every model slot is named (#390)",
            all(env.get(k) for k in
                ("ANTHROPIC_DEFAULT_OPUS_MODEL", "ANTHROPIC_DEFAULT_SONNET_MODEL",
                 "ANTHROPIC_DEFAULT_HAIKU_MODEL")), env)
    c.check("long turns get a long client timeout (#382)",
            env.get("API_TIMEOUT_MS") == "3000000", env.get("API_TIMEOUT_MS"))
    # Спека и локальная сессия обязаны собирать окружение из одного места:
    # иначе врапер и мастер расходились бы молча.
    from mop.server import spec
    from mop.cli.core import _common
    with_normal = spec.task_env("pu-x-1", "git@h:g/x.git")
    baked = dict(kv.split("=", 1) for kv in
                 __import__("base64").b64decode(with_normal["PU_LLM_ENV"]).decode().splitlines())
    c.check("the spec bakes exactly llm.env() (#390)", baked, dict(llm.env()))
    c.check("the spec reads the key by llm.KEY (#390)",
            with_normal["PU_LLM_KEY_VAR"], llm.KEY)
    c.check("no profile names remain in the tree (#390)",
            not os.path.isdir(os.path.join(os.path.dirname(config.__file__), "llm")))
    # Клиентская сессия из joined-конфига проверяется выше (#397);
    # llm.env() остаётся серверным шаблоном для спеки папета.
    return c.report("llm")


if __name__ == "__main__":
    sys.exit(main())
