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
from _lib import Checks, patched, run_command  # noqa: E402
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

    # Клиент на сервере (#292). Юниты зовут `claude` голым именем, а на
    # контроллерах его нет: дочерний процесс pty умирал на execvp, родитель
    # читал закрытую трубу, и страница показывала «[Errno 2] No such file
    # or directory» -- на mop и rumop 27.09. HYPOTHESIS: нет ни поиска
    # клиента, ни отказа до fork'а. SOLUTION: client_path -- PATH процесса,
    # затем ~/.local/bin/claude (туда его ставит install.sh); client()
    # отказывает RuntimeError с именем пользователя до fork'а, и все три
    # вызова клиента идут по абсолютному пути. STATUS: FIXED — see #292
    have = {"/opt/bin/claude", "/home/pool/.local/bin/claude"}
    exists = lambda p: p in have
    c.expect("client_path: the first PATH entry that has the client",
             credlogin.client_path("/usr/bin:/opt/bin", "/home/pool", exists), "/opt/bin/claude")
    c.expect("client_path: ~/.local/bin when PATH has none",
             credlogin.client_path("/usr/bin", "/home/pool", exists),
             "/home/pool/.local/bin/claude")
    c.expect("client_path: None when nothing exists",
             credlogin.client_path("/usr/bin", "/home/nobody", exists), None)
    c.expect("client_path: empty PATH still finds ~/.local/bin",
             credlogin.client_path("", "/home/pool", exists), "/home/pool/.local/bin/claude")
    with patched(credlogin, client_path=lambda path_env, home, exists=None: None), \
            patched(credlogin.pty, fork=lambda: c.fail("Login.start forked without a client")):
        try:
            credlogin.Login.start("/nonexistent/home", "login")
            c.fail("Login.start without a client must refuse")
        except RuntimeError as e:
            c.check("Login.start refuses before forking, naming deploy",
                    "not installed" in str(e) and "mop server deploy" in str(e), e)
        try:
            credlogin.client()
            c.fail("client() without a client must refuse")
        except RuntimeError as e:
            c.check("client() names the user", " for " in str(e), e)
    with patched(credlogin, client_path=lambda path_env, home, exists=None: "/opt/bin/claude"):
        c.expect("client() returns the found path", credlogin.client(), "/opt/bin/claude")
    check_register_307(c)
    check_name_first_328(c)
    return c.report("credlogin")


def check_register_307(c):
    """HYPOTHESIS (#307): `mop cred login <имя>` кладёт секрет в дом кредита,
    но записи cred.json не пишет: реестр видит дом, а цикл сервиса,
    probe_all, `mop cred status`, `--cred` и кнопка страницы (login_start
    требует запись) его пропускают или отвергают; владелец не сохраняется.
    SOLUTION: после удачного входа командлет зовёт credreg.register_login:
    вид -- login или token, владелец -- email из `claude auth status` для
    login; у setup-token (user:inference, без профиля) владельца нет.
    Запись, что уже есть (переавторизация), сохраняется и дополняется.
    STATUS: FIXED — see #307"""
    import importlib
    from mop.server import credreg
    cmd = importlib.import_module("mop.cli.cred.login")
    reg = getattr(cmd, "registration", None)
    if c.check("#307 login.registration exists", reg is not None):
        c.expect("#307 login: the email is the owner", reg("login", {"email": "anton@example.dev"}),
                 {"kind": "login", "owner": "anton@example.dev"})
        c.expect("#307 login without an email: no owner", reg("login", {}),
                 {"kind": "login", "owner": ""})
        c.expect("#307 setup-token: a token, no owner (no profile scope)",
                 reg("setup-token", {"email": "anton@example.dev"}), {"kind": "token", "owner": ""})

    calls = []

    class FakeLogin:
        ok = True

        def __init__(self, home, mode):
            os.makedirs(home, exist_ok=True)
            self.url, self.error, self.result = "https://claude.ai/oauth/authorize?scope=x", "bad code", \
                "sk-ant-oat01-" + "y" * 40

        @classmethod
        def start(cls, home, mode):
            return cls(home, mode)

        def submit(self, code):
            return FakeLogin.ok

    def register(name, profile="claude", kind="login", owner="", now=None):
        calls.append((name, kind, owner))
        return {"name": name}
    with patched(credlogin, Login=FakeLogin, auth_status=lambda home: {"email": "anton@example.dev"},
                 status_line=lambda st: "logged in as anton@example.dev"), \
            patched(credreg, register_login=register):
        out, err, code = run_command(cmd.main, ["alice"], stdin="the-code\n")
        c.check("#307 a login registers the credential with its owner",
                code == 0 and calls == [("alice", "login", "anton@example.dev")], (code, calls, err))
        calls.clear()
        out, err, code = run_command(cmd.main, ["bob", "--setup-token"], stdin="the-code\n")
        c.check("#307 a setup-token registers a token without an owner",
                code == 0 and calls == [("bob", "token", "")], (code, calls, err))
        calls.clear()
        FakeLogin.ok = False
        out, err, code = run_command(cmd.main, ["carol"], stdin="the-code\n")
        c.check("#307 a failed login registers nothing", code == 1 and calls == [], (code, calls))


def check_name_first_328(c):
    """HYPOTHESIS (#328): `mop cred login "my acct"` проверяет только `/`
    и строит дом сам (paths.local), а правило имени реестра
    (credreg.check_name) срабатывает лишь в register_login -- после входа:
    одноразовый код потрачен, сессия лежит в доме, которого реестр не знает.
    SOLUTION: дом берётся из credreg.home(name), и его ValueError --
    отказ в одну строку до Login.start: ни адреса, ни pty.
    STATUS: FIXED — see #328"""
    import importlib
    from mop.server import credreg
    cmd = importlib.import_module("mop.cli.cred.login")
    homes = []

    class Refused(Exception):
        pass

    def start(home, mode):
        homes.append(home)
        raise Refused()
    with patched(credlogin, Login=type("L", (), {"start": staticmethod(start)})):
        for bad in ("bad name", "a:b", "имя", "a/b"):
            homes.clear()
            try:
                out, err, code = run_command(cmd.main, [bad], stdin="the-code\n")
            except Refused:
                out, err, code = "", "", 0
            c.check(f"#328 {bad!r} is refused before Login.start",
                    homes == [] and code == 1 and "credential name" in err
                    and len(err.strip().splitlines()) == 1 and out == "", (homes, code, out, err))
        homes.clear()
        try:
            run_command(cmd.main, ["good-name"], stdin="the-code\n")
        except Refused:
            pass
        c.expect("#328 a valid name reaches Login.start with the registry's home",
                 homes, [credreg.home("good-name")])


if __name__ == "__main__":
    sys.exit(main())
