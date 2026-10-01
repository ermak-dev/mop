#!/usr/bin/env python3
"""Проверка окружения LLM без пула: python3 tests/llm.py

Профилей больше нет (#390): окружение сессии одно для папетов и мастеров --
адрес прокси из настройки, карта моделей, имя ключа. Всё -- чистые данные.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, llm  # noqa: E402


def main():
    c = Checks()
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
    from _lib import patched
    with patched(config, get=lambda name, _d=config.get: {"MOP_PROXY_KEY": "k3"}.get(name, _d(name))):
        session = _common.session_env()
    c.check("the local session carries the proxy key (#390)",
            session.get("ANTHROPIC_AUTH_TOKEN") == "k3"
            and session.get("ANTHROPIC_BASE_URL") == llm.env()["ANTHROPIC_BASE_URL"],
            {k: v for k, v in session.items() if "TOKEN" in k})
    return c.report("llm")


if __name__ == "__main__":
    sys.exit(main())
