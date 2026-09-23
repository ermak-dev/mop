"""log in to the server's bus as yourself: mop join --user <name>

The password is asked for (or read from MOP_BUS_PASSWORD; on the controller,
from its own secrets/nats-op-<name>.pass) and checked by connecting before
anything is written. What you may reach is decided by your role in
MOP_OPERATORS on the server, not by files here:

  anton:admin                 the whole pool plus the machines
  ivan:user:rugent,cloudpub   the named projects
  olga:user:*                 every project, not the machines

The bus is reached through the server's TLS proxy
(wss://<MOP_SERVER_LAN>/nats); a self-signed certificate is pinned on first
login, before the password is sent, and its fingerprint printed.

Writes ~/.config/mop/servers/<MOP_SERVER_LAN>/operator.json. The directory is
keyed by MOP_SERVER_LAN, so a second server is a second login, and
MOP_SERVER_LAN=<address> mop master retargets the master at it.

The shared role passwords (admin, master-<project>) and the old way of
copying them over ssh are gone (#106): one person, one login. Copies of them
left from an earlier join are removed. The Nomad token never travels here
(#82): pool control goes through verbs on the bus.
"""
import getpass
import os
import sys

from mop.cli import lib
from mop import bus, config, creds, operators


def as_operator(argv):
    """Имя оператора из аргументов либо окружения; None -- не названо."""
    for i, a in enumerate(argv):
        if a == "--user":
            return argv[i + 1] if i + 1 < len(argv) else ""
        if a.startswith("--user="):
            return a.split("=", 1)[1]
    return os.environ.get("MOP_BUS_USER") or None


def own_password(user):
    """Пароль с самого контроллера: lookup('password') завёл его в secrets/.
    Больше войти контроллеру не с чего -- спрашивать у человека пароль,
    который лежит тут же, значит заставить его читать файл руками."""
    path = os.path.expanduser(f"~/.config/mop/secrets/{operators.pass_file(user)}")
    try:
        with open(path) as f:
            return f.read().strip() or None
    except OSError:
        return None


def login(user):
    """Вход своим именем: пароль спрашиваем, проверяем соединением, кладём.

    Проверка соединением обязательна: молча положенный неверный пароль
    читается потом как «агент не отвечает» через двадцать секунд таймаута —
    самый дорогой из возможных способов узнать об опечатке."""
    if not user:
        lib.usage(__doc__)
    password = os.environ.get("MOP_BUS_PASSWORD") or own_password(user)
    if not password:
        if not sys.stdin.isatty():
            raise RuntimeError("no MOP_BUS_PASSWORD and no terminal to ask on")
        password = getpass.getpass(f"password for {user} on "
                                   f"{config.get('MOP_SERVER_LAN')}: ")
    if not password:
        raise RuntimeError("empty password")
    dest = creds.server_dir()
    host, https = config.get("MOP_SERVER_LAN"), config.get("MOP_HTTPS_PORT")
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
        pin = creds.write_cert(dest, der)
        print(f"{pin}: self-signed certificate {creds.fingerprint(der)}")
        print("  compare on the controller: openssl x509 -noout -fingerprint "
              "-sha256 -in ~/.config/mop/secrets/tls.pem")
    # Сначала проверяем, потом кладём: каталог не должен запомнить того, кого
    # шина не пустила.
    c = creds.wss_config(host, https, None, password, user=user,
                         cafile=pin if der is not None else None)
    try:
        bus.check(c)
    except Exception as e:
        raise RuntimeError(f"the bus did not take {user}: {e}")
    path = creds.write_operator(dest, user, password)
    print(f"{path}: {user}")
    # Ролевые пароли прежних join'ов (#106) и токен Nomad (#82): шина их не
    # знает или они здесь не нужны, а лежащие -- это секрет без пользы.
    gone = creds.legacy(os.listdir(dest))
    if os.path.exists(os.path.join(dest, creds.TOKEN_FILE)) and \
            not os.path.isdir(os.path.expanduser("~/.config/mop/secrets")):
        gone.append(creds.TOKEN_FILE)
    for n in gone:
        os.remove(os.path.join(dest, n))
    if gone:
        print(f"  removed here: {', '.join(gone)}")
    print("  what you may reach is decided by the bus, not by this file: "
          "your role in MOP_OPERATORS on the server")
    return 0


def main(argv):
    user = as_operator(argv)
    if user is None or [a for a in argv if a != "--user" and not a.startswith("--user=")
                        and a != user]:
        lib.usage(__doc__)
    return login(user)


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
