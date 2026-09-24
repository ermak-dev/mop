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

from mop import natsconf, operators  # noqa: E402

BASE = {
    "service": "svc-pass",
    "operators": {"anton": {"password": "op-pass", "allow": ["mop.>", "_INBOX.>"],
                            "deny": ["mop.admin.>"]},
                  "boss": {"password": "boss-pass", "allow": ["mop.>", "_INBOX.>"],
                           "deny": []}},
    "nodes": {"hyper": "node-pass"},
}

# Снимок users.conf до #144: имена субъектов и пользователей собраны в одно
# определение, и рефакторинг обязан не сдвинуть в файле ни байта.
OPERATORS = "anton:admin; ivan:user:rugent,cloudpub; olga:user:*"
USERS_CONF = """users = [
  {
    user: "service", password: "svc-pass"
    permissions: {
      publish:   { allow: ["mop.>", "_INBOX.>"] }
      subscribe: { allow: ["mop.>", "_INBOX.>"] }
    }
  }
  {
    user: "anton", password: "anton-pass"
    allowed_connection_types: ["WEBSOCKET"]
    permissions: {
      publish:   { allow: ["mop.>", "_INBOX.>"] }
      subscribe: { allow: ["mop.>", "_INBOX.>"] }
    }
  }
  {
    user: "ivan", password: "ivan-pass"
    allowed_connection_types: ["WEBSOCKET"]
    permissions: {
      publish:   { allow: ["mop.cloudpub.>", "mop.rugent.>", "_INBOX.>"] }
      subscribe: { allow: ["mop.cloudpub.>", "mop.rugent.>", "_INBOX.>"] }
    }
  }
  {
    user: "olga", password: "olga-pass"
    allowed_connection_types: ["WEBSOCKET"]
    permissions: {
      publish:   { allow: ["mop.>", "_INBOX.>"], deny: ["mop.admin.>"] }
      subscribe: { allow: ["mop.>", "_INBOX.>"], deny: ["mop.admin.>"] }
    }
  }
  {
    user: "puppet-mop", password: "pu-pass"
    permissions: {
      publish:   { allow: ["mop.mop.node.*.msg", "mop.mop.all.msg", "mop.mop.master.>", "mop.mop.events", "_INBOX.>"] }
      subscribe: { allow: ["_INBOX.>"] }
    }
  }
  {
    user: "puppet-rugent", password: "ru-pass"
    permissions: {
      publish:   { allow: ["mop.rugent.node.*.msg", "mop.rugent.all.msg", "mop.rugent.master.>", "mop.rugent.events", "_INBOX.>"] }
      subscribe: { allow: ["_INBOX.>"] }
    }
  }
  {
    user: "node-hyper", password: "hy-pass"
    permissions: {
      publish:   { allow: ["mop.*.master.>", "mop.*.events", "mop.*.node.hyper.msg", "mop.*.server.rpc", "_INBOX.>"] }
      subscribe: { allow: ["mop.*.node.hyper.>", "mop.*.all.msg", "_INBOX.>"] }
    }
  }
  {
    user: "node-mini", password: "mi-pass"
    permissions: {
      publish:   { allow: ["mop.*.master.>", "mop.*.events", "mop.*.node.mini.msg", "mop.*.server.rpc", "_INBOX.>"] }
      subscribe: { allow: ["mop.*.node.mini.>", "mop.*.all.msg", "_INBOX.>"] }
    }
  }
]
"""


def covers(allow, subject):
    """Разрешает ли маска прав allow подписку subject (сама может быть маской).

    Семантика NATS: `*` -- ровно один токен, `>` -- один и больше в хвосте.
    Подписка на маску разрешена, только если маска прав накрывает её целиком."""
    a, s = allow.split("."), subject.split(".")
    for i, tok in enumerate(a):
        if tok == ">":
            return len(s) > i
        if i >= len(s) or s[i] == ">":
            return False
        if tok != "*" and (tok != s[i]):
            return False
    return len(a) == len(s)


def permitted(allow, subject, deny=()):
    return (any(covers(a, subject) for a in allow)
            and not any(covers(d, subject) or covers(subject, d) for d in deny))


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

    # HYPOTHESIS (#144): субъекты и имена пользователей набраны руками в шести
    # модулях; переименование в одном месте молча разводит права и подписки.
    # SOLUTION: одно определение (mop/busnames.py), из него строятся и права,
    # и подписки. Характеризация: файл пользователей тот же байт в байт.
    ops = operators.parse(OPERATORS)
    base = {"service": "svc-pass",
            "operators": {n: {"password": f"{n}-pass", **operators.permissions(o)}
                          for n, o in ops.items()},
            "nodes": {"hyper": "hy-pass", "mini": "mi-pass"}}
    got = natsconf.render(base, {"mop": "pu-pass", "rugent": "ru-pass"})
    if got != USERS_CONF:
        failed.append("users.conf drifted from its snapshot:\n" + got)

    # Свойство (#144): всё, на что подписываются агент и сервисы, разрешено
    # правами их пользователя. Подписки и права строятся из busnames, и
    # проверка ловит расхождение до пула, где отказ прав молчит (error_cb).
    from mop import busnames
    if covers("_INBOX.>", "_INBOX") or not covers("mop.*.node.x.>", "mop.*.node.x.rpc") \
            or covers("mop.a.>", "mop.*.events") or not covers("mop.>", "mop.*.all.msg"):
        failed.append("covers() does not follow NATS wildcard semantics")
    reply = "_INBOX.abc.1"          # ответ на request: подписка на свой инбокс
    for node in ("hyper", "mini"):
        perms = natsconf.node_permissions(node)
        subs = busnames.agent_subscriptions(node)
        for subj in subs["rpc"] + subs["msg"] + [reply]:
            if not permitted(perms["subscribe"], subj):
                failed.append(f"{busnames.node_user(node)} may not subscribe to {subj}")
        if not subs["rpc"] or not subs["msg"]:
            failed.append(f"the agent must listen on rpc and msg: {subs}")
    for subj in busnames.service_subscriptions() + [reply]:
        if not permitted(natsconf.SERVICE_PERMISSIONS, subj):
            failed.append(f"{busnames.SERVICE} may not subscribe to {subj}")
    for p in ("mop", "rugent"):
        allow = natsconf.puppet_permissions(p)["subscribe"]
        if not permitted(allow, reply):
            failed.append(f"{busnames.puppet_user(p)} may not subscribe to its reply inbox")
    # Мастер слушает свой инбокс и опрос `who`: оператор с проектом, admin
    # -- за псевдопроект admin, user:* -- за любой проект.
    for name, project in (("ivan", "rugent"), ("anton", busnames.ADMIN), ("olga", "mop")):
        perms = operators.permissions(ops[name])
        for subj in (busnames.inbox(project, "host-1"),
                     busnames.inbox(project, busnames.ALL_MASTERS), reply):
            if not permitted(perms["allow"], subj, perms["deny"]):
                failed.append(f"{name} may not subscribe to {subj}")
    # STATUS: FIXED — see #144

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

    # HYPOTHESIS (#211): reload -- SIGHUP вслепую. nats отвергает reload молча
    # (только «[ERR] Failed to reload server configuration» в своём журнале) и
    # продолжает на старом конфиге, а `mop cluster users --reload`, удаление
    # проекта и хендлер deploy докладывают успех.
    # SOLUTION: до сигнала -- `nats-server -t` по файлу (битый файл -- отказ
    # без сигнала, с текстом nats); после -- дайджест работающего конфига из
    # /varz против дайджеста файла. Стенд (2.14.6): config_digest в varz равен
    # sha256, который печатает -t; принятый reload его меняет, отвергнутый --
    # и синтаксис, и непереносимая перезагрузкой правка (listen,
    # auth_callout) -- оставляет старый.
    # STATUS: FIXED — see #211
    ok_t = ("nats-server: configuration file /etc/nats/nats-server.conf is valid "
            "(sha256:0c42260ccf0e03c4f86507064f9726ef60037061b20006365f0cf12336b7cbbf)")
    bad_t = ("nats-server: error parsing include file './users.conf', Parse error on line 3: "
             "'Expected a map value terminator \",\" or a map terminator \"}\", but got 'p' instead.'")
    new = "sha256:0c42260ccf0e03c4f86507064f9726ef60037061b20006365f0cf12336b7cbbf"
    old = "sha256:e5d8e7ce4948cb97ef4cafe4384402fa24223d6c2f2a2334ddeae2b684be7192"
    try:
        if natsconf.digest_of(ok_t) != new:
            failed.append(f"digest_of must read -t's sha256: {natsconf.digest_of(ok_t)!r}")
        if natsconf.digest_of(bad_t) is not None:
            failed.append("digest_of of a failed -t must be None")
        if natsconf.reload_verdict(ok_t, new) is not None:
            failed.append(f"running == file must be accepted: {natsconf.reload_verdict(ok_t, new)!r}")
        why = natsconf.reload_verdict(ok_t, old) or ""
        if not (old in why and new in why and "restart" in why):
            failed.append(f"a kept old digest must be a refusal naming both digests and "
                          f"the cure (restart): {why!r}")
        why = natsconf.reload_verdict(bad_t, old) or ""
        if "Parse error on line 3" not in why:
            failed.append(f"an invalid file must be refused with nats' own words: {why!r}")
        why = natsconf.reload_verdict(ok_t, None) or ""
        if "varz" not in why:
            failed.append(f"no running digest (monitoring silent) must be a refusal: {why!r}")
    except AttributeError as e:
        failed.append(f"verified reload is missing: {e}")

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("natsconf: FAILED" if failed else "natsconf: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
