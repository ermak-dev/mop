"""log in to a server's bus as yourself: mop join [--server ADDRESS] [LOGIN]

What is not named comes from the command's context: the working
copy's binding (git config mop.server, mop.user), over it the environment
(MOP_SERVER_LAN, MOP_BUS_USER), over it the command line (--server, LOGIN).
With no server in any of them: the one server you are already logged in to
whose registry has this clone's origin, else MOP_SERVER_LAN from .env. With
no login: the one kept for that server, else $USER.

The password is asked for (or read from MOP_BUS_PASSWORD) and checked by
connecting before anything is written; if you are already logged in to that
server under that login, nothing is asked. Who you are and what you may
reach is decided by the server's identity provider: its operators file
(mop server user) or its directory.

The bus is reached through the server's TLS proxy (wss://<server>/nats); a
self-signed certificate is pinned on first login, before the password is
sent, and its fingerprint printed — compare it on the controller:
openssl x509 -noout -fingerprint -sha256 -in ~/.config/mop/secrets/tls.pem

The password stays in ~/.config/mop/servers/<server>/: one login per person
per server, for every project. The working copy remembers only the server
and the login (git config mop.server, mop.user, not committed), and every
mop command run in it goes there. --user NAME is the old spelling of LOGIN.
"""
import getpass
import glob
import os
import subprocess
import sys

from mop.cli import lib
from mop.common import bus, config, context, creds, paths


def parse(argv):
    """-> (сервер|None, логин|None). Отказ -- usage."""
    server, login, rest = None, None, list(argv)
    while rest:
        a = rest.pop(0)
        if a in ("--server", "--user"):
            if not rest:
                lib.usage(__doc__)
            v = rest.pop(0)
        elif a.startswith("--server=") or a.startswith("--user="):
            a, v = a.split("=", 1)
        elif a.startswith("-") or login is not None:
            lib.usage(__doc__)
        else:
            login = a
            continue
        if a == "--server":
            server = v
        else:
            login = v
    return server, login


def git(*args):
    r = subprocess.run(["git", *args], capture_output=True, text=True)
    return r.stdout.strip() if r.returncode == 0 else None


def serving(origin):
    """Серверы, где у машины уже есть вход и чей реестр знает origin."""
    out = []
    for d in sorted(glob.glob(os.path.join(creds.ROOT, "*"))):
        host = os.path.basename(d)
        try:
            c = bus.server_config(host)
            # Логин -- тех кредов, которыми спрашиваем (#207), а не этой машины.
            ans = bus.ask_once(c, bus.cluster_subject(bus.ADMIN, as_login=c.get("user")),
                               "projects")
        except Exception:
            continue
        if origin in (ans.get("origins") or []):
            out.append(host)
    return out


def main(argv):
    """Сервер и логин -- из контекста команды (#131): клон < окружение <
    командная строка; `--server` снимает диспетчер, логин -- здесь."""
    explicit_server, login = parse(argv)
    ctx = context.current()
    if explicit_server:                       # `mop join --server` мимо диспетчера
        ctx = context.resolve({"server": explicit_server}, os.environ,
                              context.clone_binding())
    in_clone = git("rev-parse", "--show-toplevel") is not None
    origin = lib.cwd_origin() if in_clone else None
    src = ctx.sources.get("server")
    explicit = ctx.server if src in ("cli", "env") else None
    bound = ctx.server if src == "clone" else None
    found = serving(origin) if origin and not ctx.server else []
    default = config._node().get("MOP_SERVER_LAN") or config._load().get("MOP_SERVER_LAN")
    try:
        host = creds.pick_server(explicit, bound, found, default)
    except ValueError as e:
        lib.usage(str(e))
    # Дальше весь процесс -- про этот сервер: каталог кредов, TLS-прокси.
    with context.use(context.resolve({"server": host}, {}, {})):
        dest = creds.server_dir(host)
        stored = creds.operator(dest)
        login = creds.pick_login(login or ctx.user, (stored or {}).get("user"),
                                 os.environ.get("USER"))
        try:
            if not (stored and stored.get("user") == login and _still_valid(host)):
                _login(host, login, dest)
        except RuntimeError as e:
            lib.fail(str(e))
            return 1
    # Клон запоминает свой сервер и логин (#125, #131); пароль -- нет.
    if in_clone:
        clone = context.clone_binding()
        for name, value in (("server", host), ("user", login)):
            if clone.get(name) != value:
                git("config", "--local", context.FIELDS[name][0], value)
    return 0


def _still_valid(host):
    """Пускает ли шина по уже лежащим кредам: тогда пароль не спрашиваем."""
    try:
        bus.check(bus.server_config(host))
        return True
    except Exception:
        return False


def _pinned(path):
    """DER закреплённого сертификата, либо None."""
    if not path:
        return None
    import ssl
    try:
        with open(path) as f:
            return ssl.PEM_cert_to_DER_cert(f.read())
    except (OSError, ValueError):
        return None


def _login(host, user, dest):
    """Вход своим именем: пароль спрашиваем, проверяем соединением, кладём.

    Проверка соединением обязательна: молча положенный неверный пароль
    читается потом как «агент не отвечает» через двадцать секунд таймаута —
    самый дорогой из возможных способов узнать об опечатке."""
    https = config.get("MOP_HTTPS_PORT")
    # Шина -- через TLS-прокси (#97). Самоподписанный сертификат закрепляем
    # при первом входе, до пароля: пароль уходит только туда, чей сертификат
    # уже закреплён. Настоящий не закрепляем, прежний пин снимаем.
    try:
        der = creds.untrusted_cert(host, https)
    except OSError as e:
        raise RuntimeError(f"no TLS proxy at {host}:{https}: {e}")
    pin = creds.cafile(dest)
    if der is None and pin:
        os.remove(pin)
    if der is not None:
        new = _pinned(pin) != der
        pin = creds.write_cert(dest, der)
        # Единственная строка успешного входа: сверить отпечаток -- дело
        # человека, и молча закреплённый сертификат этого не даёт.
        if new:
            print(f"{host}: self-signed certificate {creds.fingerprint(der)}")
    # Пароль -- после прокси: опечатка в адресе сервера отказывает сразу, а
    # не после вопроса о пароле.
    password = os.environ.get("MOP_BUS_PASSWORD")
    if not password:
        if not sys.stdin.isatty():
            raise RuntimeError("no MOP_BUS_PASSWORD and no terminal to ask on")
        password = getpass.getpass(f"password for {user} on {host}: ")
    if not password:
        raise RuntimeError("empty password")
    # Сначала проверяем, потом кладём: каталог не должен запомнить того, кого
    # шина не пустила.
    c = creds.wss_config(host, https, None, password, user=user,
                         cafile=pin if der is not None else None)
    try:
        bus.check(c)
    except Exception as e:
        raise RuntimeError(str(e))
    creds.write_operator(dest, user, password)
    # Ролевые пароли прежних join'ов (#106) и токен Nomad (#82): шина их не
    # знает или они здесь не нужны, а лежащие -- это секрет без пользы.
    gone = creds.legacy(os.listdir(dest))
    if os.path.exists(os.path.join(dest, creds.TOKEN_FILE)) and \
            not os.path.isdir(paths.local(paths.SECRETS)):
        gone.append(creds.TOKEN_FILE)
    for n in gone:
        os.remove(os.path.join(dest, n))
