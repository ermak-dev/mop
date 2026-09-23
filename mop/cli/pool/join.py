"""server credentials for this operator: mop join --user <name> | [ssh-host]

Two ways in, and the first is the one to use.

  mop join --user anton     log in as yourself: the password is asked for
                            (or read from MOP_BUS_PASSWORD) and checked by
                            connecting; nothing is copied and no ssh is needed.
                            The bus is reached through the server's TLS proxy
                            (wss://<MOP_SERVER_LAN>/nats); a self-signed
                            certificate is pinned on first login, before the
                            password is sent, and its fingerprint printed

  mop join <ssh-host>       the old way: tar the server's credential
                            directory over ssh. Kept until every installation
                            has moved its operators onto their own names (#84).

Brings ~/.config/mop/servers/<MOP_SERVER_LAN>/ from the server: the
operator's bus password and one master password per project — nothing about
nodes or puppets. After this, `mop list` and `mop master` work here with no
ansible run on this machine.

The Nomad management token is NOT brought any more (#82). It used to travel
here because the master talked to Nomad itself; that talk moved onto the bus
(#80, #81), and a management token on every master's machine is full control
over the cluster past every project check. A copy left from an earlier join
is removed.

ssh-host is the ssh alias of the server; without it, MOP_SERVER_LAN is used
as the address. The directory is keyed by MOP_SERVER_LAN, so a second server
is a second directory, and MOP_SERVER_LAN=<address> mop master retargets the
master at it.

Run again after `mop project add` on the server added a project: a new
master password appears there, not here. A project taken off the pool with
`mop project delete` takes its password out of this directory too.
"""
import getpass
import io
import os
import subprocess
import sys
import tarfile

from mop.cli import lib
from mop import bus, config, creds


def as_operator(argv):
    """Имя оператора из аргументов либо окружения; None — прежний путь."""
    for i, a in enumerate(argv):
        if a == "--user":
            return argv[i + 1] if i + 1 < len(argv) else ""
        if a.startswith("--user="):
            return a.split("=", 1)[1]
    return os.environ.get("MOP_BUS_USER") or None


def login(user):
    """Вход своим именем: пароль спрашиваем, проверяем соединением, кладём.

    Проверка соединением обязательна: молча положенный неверный пароль
    читается потом как «агент не отвечает» через двадцать секунд таймаута —
    самый дорогой из возможных способов узнать об опечатке."""
    if not user:
        lib.usage("mop join --user <name>: name required")
    password = os.environ.get("MOP_BUS_PASSWORD")
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
    print("  what you may reach is decided by the bus, not by this file: "
          "MOP_OPERATORS on the server")
    return 0


def main(argv):
    user = as_operator(argv)
    if user is not None:
        return login(user)
    if len(argv) > 1 or any(a.startswith("-") for a in argv):
        lib.usage(__doc__)
    # На контроллере join бессмыслен и вреден: свой каталог он собирает сам в
    # конце `mop deploy` (creds.collect), и ТОКЕН ему нужен — им работают
    # сервис кластера, deploy и сборка образов. Признак контроллера — его
    # secrets/: lookup('password') заводит этот каталог только там.
    if os.path.isdir(os.path.expanduser("~/.config/mop/secrets")):
        raise RuntimeError(
            "this machine is the controller: it collects its own credentials "
            "at the end of mop deploy, and it keeps the Nomad token that "
            "join must not touch")
    host = argv[0] if argv else config.get("MOP_SERVER_LAN")
    dest = creds.server_dir()
    # tar, а не scp: файлы едут одним потоком с правами, а каталог на той
    # стороне называет сервер сам -- его MOP_SERVER_LAN и наш обязаны совпадать.
    remote = f"~/.config/mop/servers/{config.get('MOP_SERVER_LAN')}"
    run = subprocess.run(["ssh", host, f"tar -C {remote} -cf - ."],
                         capture_output=True)
    if run.returncode != 0:
        raise RuntimeError(f"ssh {host}: {run.stderr.decode().strip() or 'failed'}\n"
                           f"the server keeps its credentials in {remote}; "
                           f"run mop deploy there first")
    creds.make_dir(dest)
    names = []
    with tarfile.open(fileobj=io.BytesIO(run.stdout)) as tar:
        for m in tar.getmembers():
            if not m.isfile():
                continue
            name = os.path.basename(m.name)
            if name not in creds.pick([name]):
                continue          # чужое не берём, даже если сервер положил
            with open(os.path.join(dest, name), "wb") as f:
                f.write(tar.extractfile(m).read())
            os.chmod(os.path.join(dest, name), 0o600)
            names.append(name)
    if not names:
        raise RuntimeError(f"{host}:{remote} holds nothing for a master")
    # Снятый проект (#79) уносит и свой пароль: оставленный, он отвечает
    # `mop master`, что проект на шине есть, — и мастер поднимется, чтобы
    # не подключиться. Чистим только по непустому ответу сервера.
    dropped = creds.stale(os.listdir(dest), names)
    # Токен от прежних join'ов — туда же: он больше не нужен здесь ни одной
    # команде, а пока лежит, остаётся полным доступом к кластеру.
    if os.path.exists(os.path.join(dest, creds.TOKEN_FILE)):
        dropped.append(creds.TOKEN_FILE)
    for n in dropped:
        os.remove(os.path.join(dest, n))
    print(f"{dest}: {', '.join(sorted(names))}")
    if dropped:
        print(f"  gone from the server, removed here: {', '.join(dropped)}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
