"""server credentials for this operator: mop join [ssh-host]

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
import io
import os
import subprocess
import tarfile

from mop.cli import lib
from mop import config, creds


def main(argv):
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
