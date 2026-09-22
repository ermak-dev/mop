#!/usr/bin/env python3
"""Креды сервера на машине оператора без пула: python3 tests/creds.py

Проверяется то, что раньше делал шаблон ansible и что теперь собирает сам
mop: конфиг шины из адреса, порта, шарда и пароля, имена файлов в каталоге
сервера и чтение токена. Ошибка тут молчит особенно охотно: не тот
пользователь NATS — это не отказ, а «агент не отвечает» через двадцать
секунд таймаута.

HYPOTHESIS (#51): креды мастера запечены плейбуком в bus-master-*.json одной
управляющей машины, и мастер вне неё либо со вторым сервером не поднимается.
SOLUTION: mop/creds.py собирает конфиг на лету из ~/.config/mop/servers/<адрес>/.
STATUS: FIXED — see #51
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import creds  # noqa: E402

# (шард, пользователь NATS, файл пароля). Имена файлов совпадают с тем, что
# lookup('password') заводит в secrets/ сервера: копия без переименования.
USERS = [
    (None, "admin", "nats-admin.pass"),
    ("admin", "admin", "nats-admin.pass"),
    ("rugent", "master-rugent", "nats-master-rugent.pass"),
]


def main():
    bad = 0
    cases = 0

    for shard, user, fname in USERS:
        cases += 1
        got = creds.bus_config("10.0.0.5", "4222", shard, "s3cret")
        want = {"url": "nats://10.0.0.5:4222", "user": user, "password": "s3cret"}
        if got != want:
            bad += 1
            print(f"FAILED  bus_config({shard!r}) -> {got}, wanted {want}")
        cases += 1
        if creds.pass_file(shard) != fname:
            bad += 1
            print(f"FAILED  pass_file({shard!r}) -> {creds.pass_file(shard)!r}, "
                  f"wanted {fname!r}")

    # Каталог сервера — по адресу, и только по нему: два сервера — два
    # каталога, и переменная окружения переключает оба вместе с NOMAD_ADDR.
    cases += 1
    if creds.server_dir("10.0.0.5") != os.path.join(creds.ROOT, "10.0.0.5"):
        bad += 1
        print(f"FAILED  server_dir must key by address: {creds.server_dir('10.0.0.5')!r}")

    d = tempfile.mkdtemp()
    with open(os.path.join(d, "nats-master-rugent.pass"), "w") as f:
        f.write("pw-with-newline\n")   # lookup('password') пишет с переводом строки
    with open(os.path.join(d, "bootstrap.json"), "w") as f:
        json.dump({"AccessorID": "x", "SecretID": "tok-123"}, f)

    cases += 1
    if creds.password(d, "rugent") != "pw-with-newline":
        bad += 1
        print(f"FAILED  password must be the file stripped: {creds.password(d, 'rugent')!r}")
    cases += 1
    if creds.password(d, "nobody") is not None:
        bad += 1
        print("FAILED  a missing password is None, not an exception: the caller "
              "has a legacy file to try next")
    cases += 1
    if creds.token(d) != "tok-123":
        bad += 1
        print(f"FAILED  token must be the bootstrap's SecretID: {creds.token(d)!r}")
    cases += 1
    if creds.token(os.path.join(d, "missing")) is not None:
        bad += 1
        print("FAILED  a missing token is None: the profile of mop mcp hangs on it")

    # Что `mop join` везёт с сервера: только своё. Пароли узлов и папетов
    # мастеру не положены — с ними он мог бы представиться узлом.
    cases += 1
    got = creds.files_for(["rugent", "mop"])
    want = ["nats-admin.pass", "nats-master-rugent.pass", "nats-master-mop.pass",
            "bootstrap.json"]
    if got != want:
        bad += 1
        print(f"FAILED  files_for -> {got}, wanted {want}")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
