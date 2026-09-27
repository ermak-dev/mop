#!/usr/bin/env python3
"""Драйвер логина claude без pty и без браузера: python3 tests/credlogin.py

Логин claude.ai для реестра кредитов (#282, эпик #281) ведёт сам клиент
`claude auth login` в pty на сервере: он печатает адрес авторизации и ждёт
код. Чистая часть -- разбор его экрана -- проверяется здесь на захваченном
выводе; сам pty и обмен кода на токен -- только живьём, с браузером
оператора.

HYPOTHESIS (#282): адрес в выводе клиента -- гиперссылка OSC 8, а видимый
текст терминал переносит по строкам вперемешку с управляющими
последовательностями; разбор по тексту ловит обрывок (первый прогон
26.09 упал на этом). SOLUTION: credlogin.authorize_url берёт адрес из
самой гиперссылки (оба терминатора, BEL и ST, с id и без), откат -- на
склеенный текст без ANSI. Исход хода -- по `claude auth status --json`
и по токену setup-token в выводе; секреты наружу только через .result,
в логах -- маска. STATUS: FIXED — see #282
"""
import json
import os
import stat
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import credlogin  # noqa: E402

URL = ("https://claude.com/cai/oauth/authorize?code=true"
       "&client_id=9d1c250a-e61b-44d9-88ed-5944d1962f5e&response_type=code"
       "&redirect_uri=https%3A%2F%2Fplatform.claude.com%2Foauth%2Fcode%2Fcallback"
       "&scope=org%3Acreate_api_key+user%3Aprofile+user%3Ainference"
       "&code_challenge=PKCE&code_challenge_method=S256&state=STATE")

# Экран `claude auth login` 2.1.283 (захвачено 27.09, PKCE и state
# подменены): гиперссылка без id, терминатор BEL, затем тот же адрес
# текстом в цвете и приглашение кода.
AUTH_LOGIN = ("Opening browser to sign in…\r\nIf the browser didn't open, visit: "
              "\x1b]8;;" + URL + "\x07\x1b[94m" + URL + "\x1b[39m\x1b]8;;\x07\r\n"
              "Paste code here if prompted > ").encode()

# Экран `claude setup-token`: гиперссылка с id и терминатором ST, видимый
# текст порезан переносами и курсором -- в нём адрес не собрать.
SETUP_TOKEN = ("Browser didn't open? Use the url below to sign in (c to copy)\r\n"
               "\x1b]8;id=gzxvvn;" + URL + "\x1b\\" + URL[:60] + "\r\n\x1b[2K"
               + URL[60:130] + "\r\n" + URL[130:] + "\x1b]8;;\x1b\\\r\n"
               "Paste code here if prompted > ").encode()

# Без гиперссылки вовсе (старый клиент, другой терминал): адрес текстом,
# порезанный переносом строки.
PLAIN = ("If the browser didn't open, visit: " + URL[:90] + "\r\n" + URL[90:]
         + "\r\nPaste code here if prompted > ").encode()


def main():
    c = Checks()
    for name, raw in (("auth login", AUTH_LOGIN), ("setup-token", SETUP_TOKEN),
                      ("plain text", PLAIN)):
        c.expect(f"authorize_url({name})", credlogin.authorize_url(raw), URL)
    c.expect("authorize_url(no url)", credlogin.authorize_url(b"Opening browser\r\n"), None)
    c.expect("scopes_of", credlogin.scopes_of(URL),
             ["org:create_api_key", "user:profile", "user:inference"])

    # Исход: токен setup-token, ошибка, ожидание.
    tok = "sk-ant-oat01-" + "x" * 40
    c.expect("outcome(token)", credlogin.outcome("done\r\n" + tok + "\r\n"), "token")
    c.expect("token_of", credlogin.token_of("> " + tok + " <"), tok)
    c.expect("outcome(error)", credlogin.outcome("Error: invalid code"), "error")
    c.expect("outcome(pending)", credlogin.outcome("Paste code here if prompted > ***"), "pending")
    # Маска: ни токена, ни кода в логах.
    masked = credlogin.mask("got " + tok + " and sk-ant-api03-abc")
    c.check("mask hides every sk-ant secret", "sk-ant-" not in masked, masked)
    c.check("mask keeps the rest", masked.startswith("got "), masked)

    # Дом кредита: флаги первого запуска те же, что пишет врапер (spec.py),
    # чтобы `claude -p` в этом доме не встал на онбординге; каталог 0700.
    with tempfile.TemporaryDirectory() as d:
        home = os.path.join(d, "anton")
        credlogin.prepare_home(home)
        c.check("prepare_home: 0700", stat.S_IMODE(os.stat(home).st_mode) == 0o700,
                oct(os.stat(home).st_mode))
        with open(os.path.join(home, ".claude.json")) as f:
            flags = json.load(f)
        for k in ("hasCompletedOnboarding", "resumeReturnDismissed"):
            c.check(f"prepare_home: {k}", flags.get(k) is True, flags)
        credlogin.prepare_home(home)          # повторно -- без потери своего
        c.check("prepare_home is idempotent", os.path.exists(os.path.join(home, ".claude.json")))
        c.expect("credentials_path", credlogin.credentials_path(home),
                 os.path.join(home, ".claude", ".credentials.json"))

    # Итог `claude auth status --json` -> строка для человека, без секретов.
    c.expect("status_line(logged in)",
             credlogin.status_line({"loggedIn": True, "email": "a@b.c", "subscriptionType": "max"}),
             "logged in as a@b.c (max)")
    c.expect("status_line(not logged in)", credlogin.status_line({"loggedIn": False}), None)
    return c.report("credlogin")


if __name__ == "__main__":
    sys.exit(main())
