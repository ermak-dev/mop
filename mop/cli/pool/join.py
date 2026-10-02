"""join a pool server: mop join [--server ADDRESS] [LOGIN]

Server and login come from --server/LOGIN, the process environment or the
working copy's binding; if missing, they are asked for. The bus password is
asked without echo (or read from MOP_BUS_PASSWORD) and verified before
saving. A valid previous login is reused.

The HTTPS port defaults to 443. After authenticating to the bus, join asks
the server for its LLM proxy URL and client key over a personal, authenticated
subject. The key is never printed or asked for separately; /v1/models is
checked before saving. Rejoining refreshes a rotated key without replacing
working credentials when verification fails. Neither nodes nor puppets can
request the key, and there is no public HTTP endpoint for it.

The bus uses the server's TLS proxy at /nats. A self-signed certificate is
pinned before sending the bus password; compare its fingerprint on the
controller: openssl x509 -noout -fingerprint -sha256 -in
~/.config/mop/secrets/tls.pem

Bus and proxy credentials stay in separate private files under
~/.config/mop/servers/<server>/. The clone remembers only server and login
in local git config. --user NAME is the old spelling of LOGIN.
"""
import getpass
import glob
import os
import ssl
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request

from mop.cli import lib
from mop.common import bus, busnames, context, creds, paths


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


def ask(prompt, secret=False, default=None):
    """Спросить недостающее до записи; секрет не отзывается в терминал."""
    if not sys.stdin.isatty():
        raise RuntimeError(f"{prompt.rstrip(': ')} is required; no terminal to ask on")
    value = getpass.getpass(prompt) if secret else input(prompt)
    value = value.strip() or default
    if not value:
        raise RuntimeError(f"{prompt.rstrip(': ')} is required")
    return value


def _probe(url, key, directory):
    """Проверить ключ без передачи его по открытому LAN или через redirect."""
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" and parsed.hostname not in ("localhost", "127.0.0.1", "::1"):
        raise RuntimeError("the proxy key needs HTTPS outside localhost")
    pin = creds.cafile(directory) if parsed.hostname == os.path.basename(directory) else None
    tls = ssl.create_default_context(cafile=pin)

    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, request, fp, code, message, headers, newurl):
            raise RuntimeError("proxy verification must not redirect the client key")

    opener = urllib.request.build_opener(NoRedirect(), urllib.request.HTTPSHandler(context=tls))
    request = urllib.request.Request(url.rstrip("/") + "/v1/models",
                                     headers={"Authorization": f"Bearer {key}"})
    try:
        with opener.open(request, timeout=10) as response:
            if response.status != 200:
                raise RuntimeError(f"proxy verification returned HTTP {response.status}")
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"proxy verification returned HTTP {e.code}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"proxy verification failed: {e.reason}") from e


def _restore_pin(dest, der):
    """Вернуть прежний TLS-пин после неуспешного входа."""
    if der is not None:
        creds.write_cert(dest, der)
    else:
        path = creds.cafile(dest)
        if path:
            os.remove(path)


def _fetch(host, login, password, dest, https):
    """Запросить настройки от имени уже проверенного человека, не узла."""
    c = creds.wss_config(host, https, None, password, user=login,
                         cafile=creds.cafile(dest))
    reply = bus.ask_once(c, busnames.join_config(login), "client_config")
    if not isinstance(reply, dict):
        raise RuntimeError("invalid proxy configuration response")
    if reply.get("error"):
        raise RuntimeError(reply["error"])
    try:
        record = creds._client_values(reply["https_port"], reply["proxy_url"],
                                      reply["proxy_key"])
    except (KeyError, TypeError, ValueError) as e:
        raise RuntimeError("invalid proxy configuration from server") from e
    if record["https_port"] != https:
        raise RuntimeError("server proxy port differs from the verified bus port")
    return record


def complete(host, login, dest, https):
    """Один проверенный вход: старый пароль можно использовать, новые файлы
    появляются только после обеих сетевых проверок."""
    if not https.isascii() or not https.isdecimal() or not 1 <= int(https) <= 65535:
        raise ValueError("server HTTPS port must be between 1 and 65535")
    pin = _pinned(creds.cafile(dest))
    try:
        stored = creds.operator(dest)
        valid = stored and stored["user"] == login and _still_valid(host, https, dest, stored)
        password = stored["password"] if valid else _login(host, login, dest, https)
        record = _fetch(host, login, password, dest, https)
        _probe(record["proxy_url"], record["proxy_key"], dest)
        creds.write_client(dest, **record)
        if not valid:
            creds.write_operator(dest, login, password)
        # Старые ролевые пароли и токен Nomad удаляем только после полного входа.
        gone = creds.legacy(os.listdir(dest))
        if os.path.exists(os.path.join(dest, creds.TOKEN_FILE)) and \
                not os.path.isdir(paths.local(paths.SECRETS)):
            gone.append(creds.TOKEN_FILE)
        for name in gone:
            os.remove(os.path.join(dest, name))
    except Exception:
        _restore_pin(dest, pin)
        raise


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
    try:
        try:
            host = creds.pick_server(explicit, bound, found, None)
        except ValueError:
            if len(found) > 1:
                raise
            host = ask("server address: ")
        # Дальше весь процесс -- про этот сервер: каталог кредов, TLS-прокси.
        with context.use(context.resolve({"server": host}, {}, {})):
            dest = creds.server_dir(host)
            stored = creds.operator(dest)
            login = creds.pick_login(login or ctx.user, (stored or {}).get("user"), None)
            if not login:
                login = ask(f"login on {host}: ")
            saved = creds.client(dest)
            https = (saved or {}).get("https_port") or os.environ.get("MOP_HTTPS_PORT") or "443"
            if not saved and sys.stdin.isatty():
                https = ask(f"HTTPS port for {host} [{https}]: ", default=https)
            complete(host, login, dest, https)
    except (RuntimeError, ValueError, OSError) as e:
        lib.fail(str(e))
        return 1
    # Клон запоминает свой сервер и логин (#125, #131); пароль -- нет.
    if in_clone:
        clone = context.clone_binding()
        for name, value in (("server", host), ("user", login)):
            if clone.get(name) != value:
                git("config", "--local", context.FIELDS[name][0], value)
    return 0


def _still_valid(host, https, dest, stored):
    """Пускает ли шина по уже лежащим кредам: тогда пароль не спрашиваем."""
    try:
        bus.check(creds.wss_config(host, https, None, stored["password"],
                                   user=stored["user"], cafile=creds.cafile(dest)))
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


def _login(host, user, dest, https):
    """Проверить пароль шиной; сохранение -- после проверки прокси в complete.

    Молча положенный неверный пароль читался бы потом как «агент не отвечает»
    через двадцать секунд таймаута."""
    # Шина -- через TLS-прокси (#97). Самоподписанный сертификат закрепляем
    # при первом входе, до пароля: пароль уходит только туда, чей сертификат
    # уже закреплён. Настоящий не закрепляем, прежний пин снимаем.
    try:
        der = creds.untrusted_cert(host, https)
    except OSError as e:
        raise RuntimeError(f"no TLS proxy at {host}:{https}: {e}")
    pin = creds.cafile(dest)
    previous = _pinned(pin)
    if der is None and pin:
        os.remove(pin)
    if der is not None:
        new = _pinned(pin) != der
        pin = creds.write_cert(dest, der)
        # Единственная строка успешного входа: сверить отпечаток -- дело
        # человека, и молча закреплённый сертификат этого не даёт.
        if new:
            print(f"{host}: self-signed certificate {creds.fingerprint(der)}")
    try:
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
        bus.check(c)
        return password
    except Exception as e:
        _restore_pin(dest, previous)
        raise RuntimeError(str(e)) from e
