#!/usr/bin/env python3
"""Каталог моделей выбранного прокси без живой сети: python3 tests/models.py."""
import os
import sys
import tempfile
import urllib.request

import hermetic  # noqa: F401,E402
from _lib import Checks, patched, patched_env, run_command  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import config, context, creds  # noqa: E402


# HYPOTHESIS: проверка /v1/models при login доказывает доступность прокси,
# но мастер не получает каталог моделей выбранного сервера.
# SOLUTION: клиентская операция возвращает проверенные ID, командлет печатает
# их и объявляется MCP-инструментом только для мастера и оператора.
# RESULT: ожидаем проверку формы, TLS, запрет перенаправлений и выбор сервера.
# STATUS: FIXED — see #448

def check_model_ids_448(c, models):
    c.expect("#448 IDs from proxy data", models.ids({"object": "list", "data": [
        {"id": "claude-opus-5-5", "object": "model"},
        {"id": "glm-5.3", "object": "model"}]}),
        ["claude-opus-5-5", "glm-5.3"])
    c.expect("#448 empty catalog is not a failure", models.ids({"data": []}), [])
    for payload in ({}, {"data": None}, {"data": [None]},
                    {"data": [{"id": ""}]}, {"data": [{"name": "opus"}]}):
        try:
            models.ids(payload)
            c.fail("#448 malformed catalog must be rejected", repr(payload))
        except ValueError:
            c.check("#448 invalid model catalog rejected", True)


def check_selected_server_448(c, models):
    root = os.path.join(tempfile.mkdtemp(prefix="mop-models-448-"), "servers")
    seen = []

    class Response:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *_):
            pass

        def read(self):
            return b'{"data":[{"id":"model-a"},{"id":"model-b"}]}'

    class Opener:
        def open(self, req, timeout):
            seen.append((req.full_url, req.get_header("Authorization"), timeout))
            return Response()

    def opener(*handlers):
        c.check("#448 redirects disabled", any(isinstance(h, urllib.request.HTTPRedirectHandler)
                and type(h) is not urllib.request.HTTPRedirectHandler for h in handlers))
        return Opener()

    with patched(creds, ROOT=root), patched_env(MOP_SERVER_LAN=None), \
            patched(urllib.request, build_opener=opener), config.client_sources():
        for host in ("a.test", "b.test"):
            creds.write_connection(os.path.join(root, host), "user", "bus-pw", "443",
                                   f"https://{host}/llm", host + "-key")
        with context.use(context.resolve({"server": "b.test"}, {}, {})):
            c.expect("#448 chosen proxy models", models.available(), ["model-a", "model-b"])
        c.expect("#448 request uses chosen URL and key", seen,
                 [("https://b.test/llm/v1/models", "Bearer b.test-key", 10)])
        with context.use(context.resolve({}, {}, {})):
            try:
                models.available()
                c.fail("#448 ambiguous server must be rejected")
            except (RuntimeError, ValueError):
                pass
    c.expect("#448 no network request after ambiguous server", len(seen), 1)


def check_command_448(c):
    from mop.cli.core import models as command
    from mop.client import models
    from mop import cli
    with patched(models, available=lambda: ["model-a", "model-b"]):
        out, err, code = run_command(command.main, via_cli=True)
    c.expect("#448 command prints model IDs", (out, err, code), ("model-a\nmodel-b\n", "", 0))
    with patched(models, available=lambda: []):
        out, err, code = run_command(command.main, via_cli=True)
    c.expect("#448 empty catalog is distinguishable from failure", (out, err, code),
             ("no models\n", "", 0))
    with patched(models, available=lambda: (_ for _ in ()).throw(
            ValueError("invalid proxy model catalog: expected data list"))):
        out, err, code = run_command(command.main, via_cli=True)
    c.expect("#448 malformed proxy response is a readable CLI error", (out, err, code),
             ("", "invalid proxy model catalog: expected data list\n", 1))
    declared = cli.declared(os.path.join(cli.PACKAGE, "core", "models.py"))
    c.expect("#448 models is read-only MCP command", declared,
             {"annotations": "readonly", "args": []})


def main():
    c = Checks()
    try:
        from mop.client import models
    except ImportError as exc:
        c.fail("#448 model catalog client exists", str(exc))
        return c.report("models")
    check_model_ids_448(c, models)
    check_selected_server_448(c, models)
    check_command_448(c)
    return c.report("models")


if __name__ == "__main__":
    sys.exit(main())
