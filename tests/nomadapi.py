#!/usr/bin/env python3
"""Разбор ответов Nomad без пула: python3 tests/nomadapi.py

Файл не tests/nomad.py: каталог tests/ стоит первым в sys.path, и такое имя
заслонило бы библиотеку python-nomad, которую импортирует mop.server.nomad.
"""
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

import requests  # noqa: E402

from mop.common import state  # noqa: E402
from mop.server import nomad  # noqa: E402

# Строка врапера #333 -- та, что на rumop пришла как «Â«…Â»».
LINE = ("bootstrap of pu-rudesktop-8 failed at task "
        "«Python environment of the site (uv sync)»: non-zero return code")


def _response(body):
    """Ответ /v1/client/fs/logs?plain=true: байты без charset, как у Nomad."""
    r = requests.Response()
    r.status_code = 200
    r.headers["Content-Type"] = "text/plain"
    r._content = body
    # Кодировку ставит адаптер requests (build_response) по заголовкам: для
    # text/* без charset это ISO-8859-1. Без этой строки .text угадывал бы
    # кодировку по байтам и прятал дефект.
    r.encoding = requests.utils.get_encoding_from_headers(r.headers)
    return r


def _stderr(body):
    with patched(nomad, _raw=lambda *a, **kw: _response(body)):
        return nomad.alloc_stderr("a1b2", "puppet")


def check_stderr_utf8_343(c):
    """HYPOTHESIS (#343): alloc_stderr отдаёт r.text; у plain-логов Nomad нет
    charset, и requests для text/* без него берёт ISO-8859-1 -- «» приходят
    как «Â«…Â»», и state.failure_reason не узнаёт строку врапера #333.
    SOLUTION: декодировать байты UTF-8 с errors="replace": хвост берётся
    смещением от конца и может начаться посреди символа.
    RESULT: первая версия проверки прошла на старом коде -- ручной Response
    без encoding, и .text угадывал кодировку по байтам; фейк теперь ставит
    её, как адаптер requests, и проверка падала на «Â«».
    STATUS: FIXED — see #343"""
    got = _stderr((LINE + "\n").encode())
    c.check("#343 UTF-8 stderr without a charset keeps its «»",
            "«Python environment of the site (uv sync)»" in got, repr(got))
    c.expect("#343 failure_reason recognises #333's line read through alloc_stderr",
             state.failure_reason(got),
             "bootstrap task «Python environment of the site (uv sync)» failed: "
             "non-zero return code")

    # Смещение от конца разрезало «» пополам: первый байт -- хвост символа.
    cut = "«x».\n".encode()[1:]
    try:
        got = _stderr(cut)
    except UnicodeDecodeError as e:
        got = e
    c.expect("#343 a tail cut mid-character decodes with a replacement char",
             got, "�x».\n")


def main():
    c = Checks()
    check_stderr_utf8_343(c)
    return c.report("nomadapi")


if __name__ == "__main__":
    sys.exit(main())
