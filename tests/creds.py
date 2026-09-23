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

    # Что едет мастеру из secrets/ сервера: только своё. Пароли узлов и
    # папетов мастеру не положены — с ними он мог бы представиться узлом.
    # HYPOTHESIS (#52): контроллер и `mop join` должны отбирать одни и те же
    # файлы, иначе на одной машине оператор увидит больше, чем на другой.
    cases += 1
    listing = ["nats-node-mate.pass", "nats-master-rugent.pass", "junk",
               "nats-puppet-rugent.pass", "nats-admin.pass", "nats-master-mop.pass"]
    got = creds.pick(listing)
    want = ["nats-admin.pass", "nats-master-mop.pass", "nats-master-rugent.pass"]
    if got != want:
        bad += 1
        print(f"FAILED  pick -> {got}, wanted {want}")

    # Токен Nomad оператору НЕ положен (#82): управление уехало за шину, и
    # management-токен на каждой машине мастера — полный контроль над
    # кластером в обход всякой проверки проекта. Отбор — единственное место,
    # где это записано, поэтому проверяется отдельной строкой.
    cases += 1
    if creds.TOKEN_FILE in creds.pick(listing + [creds.TOKEN_FILE]):
        bad += 1
        print("FAILED  pick must never hand the Nomad token to an operator")

    # Контроллер собирает свой каталог сервера из secrets/ и bootstrap.json:
    # он тоже машина оператора, и после deploy на нём всё работает без join.
    secrets = tempfile.mkdtemp()
    for name in listing:
        with open(os.path.join(secrets, name), "w") as f:
            f.write(name + "\n")
    # Токен в каталог кладёт fetch игры сервера, с правами по umask; collect
    # обязан закрыть и его (#54): на свежем контроллере он лежал бы 0644.
    dest = os.path.join(tempfile.mkdtemp(), "10.0.0.5")
    os.makedirs(dest)
    with open(os.path.join(dest, "bootstrap.json"), "w") as f:
        json.dump({"SecretID": "tok-123"}, f)
    os.chmod(os.path.join(dest, "bootstrap.json"), 0o644)
    cases += 1
    copied = creds.collect(secrets, dest)
    if sorted(os.listdir(dest)) != sorted(want + ["bootstrap.json"]) or copied != want:
        bad += 1
        print(f"FAILED  collect -> {copied}, dir {sorted(os.listdir(dest))}")
    cases += 1
    if creds.password(dest, "mop") != "nats-master-mop.pass" or creds.token(dest) != "tok-123":
        bad += 1
        print("FAILED  collected files must read back through password()/token()")
    cases += 1
    modes = {n: os.stat(os.path.join(dest, n)).st_mode & 0o777 for n in os.listdir(dest)}
    if (os.stat(dest).st_mode & 0o777) != 0o700 or any(m != 0o600 for m in modes.values()):
        bad += 1
        print(f"FAILED  the server directory holds secrets: dir "
              f"{oct(os.stat(dest).st_mode & 0o777)}, files {modes}")

    # Что каталог сервера ТЕРЯЕТ. Проект сняли (#79) — его пароль мастера
    # у оператора остаётся, и lib.shard_ready по нему отвечает «проект на
    # шине есть»: `mop master` поднимется, чтобы не подключиться. Отбор —
    # строго пароли мастеров: админский и токен к проектам отношения не
    # имеют, а чужое в каталоге не наше дело.
    cases += 1
    local = ["nats-admin.pass", "nats-master-mop.pass", "nats-master-gone.pass",
             "bootstrap.json", "junk"]
    got = creds.stale(local, ["nats-master-mop.pass", "nats-admin.pass"])
    if got != ["nats-master-gone.pass"]:
        bad += 1
        print(f"FAILED  stale -> {got}, wanted ['nats-master-gone.pass']")
    cases += 1
    # Пустой ответ сервера — не повод снести всё: tar пришёл пустым, сеть
    # моргнула, отбор на той стороне поменялся. Чистим только при ответе.
    if creds.stale(local, []) != []:
        bad += 1
        print(f"FAILED  stale on an empty listing must keep everything: "
              f"{creds.stale(local, [])}")

    # forget: пароли снятого проекта уходят и с контроллера, и из каталога
    # сервера — обоими путями их читает один и тот же shard_ready.
    cases += 1
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    for d in (a, b):
        for n in ("nats-master-gone.pass", "nats-puppet-gone.pass",
                  "nats-master-stay.pass"):
            with open(os.path.join(d, n), "w") as f:
                f.write("x\n")
    removed = creds.forget("gone", a, b)
    left = sorted(set(os.listdir(a)) | set(os.listdir(b)))
    if left != ["nats-master-stay.pass"] or len(removed) != 4:
        bad += 1
        print(f"FAILED  forget -> {removed}, left {left}")

    print(f"{cases - bad}/{cases} matched")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
