#!/usr/bin/env python3
"""Проверка пользователей NATS без пула: python3 tests/natsconf.py

Список пользователей шины собирает один код (#116): включить в массив
`users` второй файл NATS не умеет (проверено `nats-server -t`: include внутри
массива -- ошибка разбора), поэтому весь список -- один файл, и пишет его
сервер. Люди, машины и узлы приходят базовым JSON от `mop deploy`, папеты --
из реестра сервера. Здесь -- текст файла и пароли; reload и подключение под
новым пользователем проверяются только на живом пуле.
"""
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop import natsconf  # noqa: E402

BASE = {
    "service": "svc-pass",
    "operators": {"anton": {"password": "op-pass", "allow": ["mop.>", "_INBOX.>"],
                            "deny": ["mop.admin.>"]},
                  "boss": {"password": "boss-pass", "allow": ["mop.>", "_INBOX.>"],
                           "deny": []}},
    "nodes": {"hyper": "node-pass"},
}


def main():
    failed = []

    # HYPOTHESIS (#116): пользователей рендерит шаблон ansible на контроллере,
    # и сервис пула не может завести проект, не повторив шаблон второй копией.
    # SOLUTION: один рендерер на Python, им пользуются и deploy, и сервис.
    # STATUS: FIXED — see #116
    text = natsconf.render(BASE, {"mop": "pu-pass", "rugent": "ru-pass"})
    for want in ('user: "service", password: "svc-pass"',
                 'user: "anton", password: "op-pass"',
                 'allowed_connection_types: ["WEBSOCKET"]',
                 '"mop.admin.>"',
                 'user: "node-hyper", password: "node-pass"',
                 '"mop.*.node.hyper.>"',
                 'user: "puppet-mop", password: "pu-pass"',
                 '"mop.mop.node.*.msg"',
                 'user: "puppet-rugent", password: "ru-pass"'):
        if want not in text:
            failed.append(f"render lacks {want!r}")
    if not text.lstrip().startswith("users = ["):
        failed.append("render must be the users array, the file included "
                      "into authorization {}")
    # Пустой deny не пишется: NATS принял бы и пустой, но `deny: []` рядом с
    # оператором-админом читается как недосмотр.
    boss = text[text.index('user: "boss"'):text.index('user: "node-hyper"')]
    if "deny" in boss:
        failed.append("an operator without deny must not get a deny list")
    # Папет чужого проекта не видит: права папета -- только его проект.
    pu = text[text.index('user: "puppet-mop"'):text.index('user: "puppet-rugent"')]
    if "rugent" in pu:
        failed.append("puppet-mop's permissions mention another project")
    # Порядок стабилен: иначе каждый прогон менял бы файл и дёргал reload.
    if natsconf.render(BASE, {"rugent": "ru-pass", "mop": "pu-pass"}) != text:
        failed.append("render must not depend on the order of projects")
    # Кавычка в пароле не рвёт файл.
    odd = natsconf.render({**BASE, "service": 'a"b'}, {})
    if 'password: "a\\"b"' not in odd:
        failed.append(f"a quote in a password must be escaped: {odd[:120]!r}")
    # Проект без пароля -- отказ, а не пользователь без пароля.
    try:
        natsconf.render(BASE, {"mop": ""})
        failed.append("a project without a password must be refused")
    except ValueError as e:
        if "mop" not in str(e):
            failed.append(f"the refusal must name the project: {e}")

    # Пароли папетов рождаются на сервере: недостающий заводится, имеющийся
    # не меняется -- иначе живые папеты отвалились бы от шины.
    root = tempfile.mkdtemp(prefix="mop-test-natsconf-")
    with open(os.path.join(root, "nats-puppet-mop.pass"), "w") as f:
        f.write("kept\n")
    got = natsconf.passwords(["mop", "rugent"], root)
    if got.get("mop") != "kept":
        failed.append(f"an existing password must be kept: {got}")
    new = got.get("rugent") or ""
    if len(new) < 32 or not new.isalnum():
        failed.append(f"a new password must be 32+ letters and digits: {new!r}")
    if natsconf.passwords(["rugent"], root).get("rugent") != new:
        failed.append("a generated password must be stored and reused")
    mode = os.stat(os.path.join(root, "nats-puppet-rugent.pass")).st_mode & 0o777
    if mode != 0o600:
        failed.append(f"a password file must be 0600, got {oct(mode)}")

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("natsconf: FAILED" if failed else "natsconf: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
