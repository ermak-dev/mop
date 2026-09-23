"""server credentials for this operator: mop join [ssh-host]

Brings ~/.config/mop/servers/<MOP_SERVER_LAN>/ from the server: the
operator's bus password, one master password per shard and the Nomad
management token — exactly what the server's own `mop deploy` collected for
itself, nothing about nodes or puppets. After this, `mop list` and
`mop master` work here with no ansible run on this machine.

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
            if name not in creds.pick([name]) and name != creds.TOKEN_FILE:
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
    for n in dropped:
        os.remove(os.path.join(dest, n))
    print(f"{dest}: {', '.join(sorted(names))}")
    if dropped:
        print(f"  gone from the server, removed here: {', '.join(dropped)}")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
