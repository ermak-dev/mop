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

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
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
# определение, и рефакторинг обязан не сдвинуть в файле ни байта. Снят заново
# в #207 намеренно: публикация людей -- явным списком с логином в rpc, у
# сервиса -- deny на субъекты с логином; подписка, папеты и узлы -- прежние.
OPERATORS = "anton:admin; ivan:user:rugent,cloudpub; olga:user:*"
USERS_CONF = """users = [
  {
    user: "service", password: "svc-pass"
    permissions: {
      publish:   { allow: ["mop.>", "_INBOX.>"], deny: ["mop.*.node.*.rpc.*", "mop.*.cluster.rpc.*"] }
      subscribe: { allow: ["mop.>", "_INBOX.>"] }
    }
  }
  {
    user: "anton", password: "anton-pass"
    allowed_connection_types: ["WEBSOCKET"]
    permissions: {
      publish:   { allow: ["mop.*.node.*.rpc.anton", "mop.*.cluster.rpc.anton", "mop.*.node.*.rpc", "mop.*.cluster.rpc", "mop.*.node.*.msg", "mop.*.all.msg", "mop.*.master.>", "mop.*.events", "mop.*.server.rpc", "mop.admin.build.rpc", "_INBOX.>"] }
      subscribe: { allow: ["mop.>", "_INBOX.>"] }
    }
  }
  {
    user: "ivan", password: "ivan-pass"
    allowed_connection_types: ["WEBSOCKET"]
    permissions: {
      publish:   { allow: ["mop.cloudpub.node.*.rpc.ivan", "mop.cloudpub.cluster.rpc.ivan", "mop.cloudpub.node.*.rpc", "mop.cloudpub.cluster.rpc", "mop.cloudpub.node.*.msg", "mop.cloudpub.all.msg", "mop.cloudpub.master.>", "mop.cloudpub.events", "mop.cloudpub.server.rpc", "mop.rugent.node.*.rpc.ivan", "mop.rugent.cluster.rpc.ivan", "mop.rugent.node.*.rpc", "mop.rugent.cluster.rpc", "mop.rugent.node.*.msg", "mop.rugent.all.msg", "mop.rugent.master.>", "mop.rugent.events", "mop.rugent.server.rpc", "_INBOX.>"] }
      subscribe: { allow: ["mop.cloudpub.>", "mop.rugent.>", "_INBOX.>"] }
    }
  }
  {
    user: "olga", password: "olga-pass"
    allowed_connection_types: ["WEBSOCKET"]
    permissions: {
      publish:   { allow: ["mop.*.node.*.rpc.olga", "mop.*.cluster.rpc.olga", "mop.*.node.*.rpc", "mop.*.cluster.rpc", "mop.*.node.*.msg", "mop.*.all.msg", "mop.*.master.>", "mop.*.events", "mop.*.server.rpc", "_INBOX.>"], deny: ["mop.admin.>"] }
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


def rules(text):
    """Отрендеренный users.conf -> {пользователь: {publish|subscribe: (allow, deny)}}:
    проверяются правила файла, который прочтёт NATS, а не словарь до него."""
    import json
    import re
    out = {}
    for block in text.split("\n  {\n")[1:]:
        user = re.search(r'user: "([^"]+)"', block).group(1)
        sides = {}
        for side in ("publish", "subscribe"):
            m = re.search(side + r':\s*\{ allow: (\[.*?\])(?:, deny: (\[.*?\]))? \}', block)
            sides[side] = (json.loads(m.group(1)), json.loads(m.group(2) or "[]"))
        out[user] = sides
    return out


def check_login_in_subject_207():
    """HYPOTHESIS (#207): человеку дан весь mop.<p>.> (или mop.> без admin) --
    логин токеном субъекта он подделал бы так же, как полем тела.
    SOLUTION: публикация человека -- явным списком: rpc агента и сервиса
    кластера -- только со СВОИМ логином (плюс прежние субъекты без логина на
    время перехода), остальное, что люди публикуют сегодня, -- как было.
    Подписка -- как была. Машины логин человека не публикуют: у сервиса --
    deny на субъекты с логином, у узлов и папетов их нет в списке.
    STATUS: FIXED — see #207"""
    from mop import busnames
    out = []
    ops = operators.parse("alice:user:mop; bob:user:mop; anton:admin; olga:user:*; "
                          "anton.ermak:user:mop")
    try:
        base = {"service": "svc-pass",
                "operators": {n: {"password": "p", **operators.permissions(o, n)}
                              for n, o in ops.items()},
                "nodes": {"hyper": "hy-pass"}}
    except TypeError as e:
        return [f"operators.permissions(op, login) is missing: {e}"]
    got = rules(natsconf.render(base, {"mop": "pu-pass"}))

    def may(user, subj, side="publish"):
        allow, deny = got[user][side]
        return permitted(allow, subj, deny)

    def expect(user, subj, want, side="publish"):
        if may(user, subj, side) != want:
            out.append(f"{user} {'may' if want else 'must not'} {side} {subj}")

    # Свой логин -- да, чужой -- нет; прежние субъекты -- на время перехода.
    expect("alice", "mop.mop.node.hyper.rpc.alice", True)
    expect("alice", "mop.mop.node.hyper.rpc.bob", False)
    expect("alice", "mop.mop.cluster.rpc.alice", True)
    expect("alice", "mop.mop.cluster.rpc.bob", False)
    expect("alice", "mop.mop.node.hyper.rpc", True)
    expect("alice", "mop.mop.cluster.rpc", True)
    # Всё, что человек публикует сегодня, остаётся: инбоксы мастеров и
    # опрос who, события, публичный канал, all, сервер, ответы.
    for subj in ("mop.mop.node.hyper.msg", "mop.mop.all.msg", "mop.mop.master.h-1.inbox",
                 "mop.mop.master.all.inbox", "mop.mop.events", "mop.mop.server.rpc",
                 "_INBOX.abc.1"):
        expect("alice", subj, True)
    # Чужой проект и машинное -- как было: нет.
    for subj in ("mop.rugent.node.hyper.rpc.alice", "mop.admin.node.hyper.rpc.alice",
                 "mop.admin.build.rpc", "mop.rugent.events"):
        expect("alice", subj, False)
    # Подписка не сужалась: свой инбокс и ответы.
    for subj in ("mop.mop.master.h-1.inbox", "mop.mop.master.all.inbox", "_INBOX.abc.1"):
        expect("alice", subj, True, "subscribe")
    # admin: весь пул, но логин -- свой.
    for subj in ("mop.rugent.node.hyper.rpc.anton", "mop.admin.node.hyper.rpc.anton",
                 "mop.admin.cluster.rpc.anton", "mop.admin.build.rpc",
                 "mop.admin.master.h-1.inbox", "mop.rugent.events", "mop.admin.node.hyper.rpc"):
        expect("anton", subj, True)
    for subj in ("mop.rugent.node.hyper.rpc.bob", "mop.admin.cluster.rpc.bob"):
        expect("anton", subj, False)
    # user:* -- любой проект, но не admin, и логин свой.
    expect("olga", "mop.rugent.node.hyper.rpc.olga", True)
    expect("olga", "mop.rugent.node.hyper.rpc.bob", False)
    expect("olga", "mop.admin.node.hyper.rpc.olga", False)
    expect("olga", "mop.admin.cluster.rpc.olga", False)
    # Машины не публикуют логин человека.
    for user in ("service", "node-hyper", "puppet-mop"):
        for subj in ("mop.mop.node.hyper.rpc.alice", "mop.mop.cluster.rpc.alice",
                     "mop.admin.node.hyper.rpc.anton", "mop.admin.cluster.rpc.anton"):
            expect(user, subj, False)
    # ...а своё делают как прежде: сервис спрашивает агента без логина
    # (ворота #40), отвечает и пишет события.
    for subj in ("mop.admin.node.hyper.rpc", "mop.mop.events", "_INBOX.abc.1"):
        expect("service", subj, True)
    for subj in busnames.service_subscriptions():
        expect("service", subj, True, "subscribe")
    for subj in busnames.agent_subscriptions("hyper")["rpc"]:
        expect("node-hyper", subj, True, "subscribe")
    # Логин с точкой (LDAP/AD, #208) -- закодированным токеном, и только своим.
    expect("anton.ermak", "mop.mop.node.hyper.rpc.anton%2Eermak", True)
    expect("anton.ermak", "mop.mop.cluster.rpc.anton%2Eermak", True)
    for subj in ("mop.mop.node.hyper.rpc.anton", "mop.mop.node.hyper.rpc.bob",
                 "mop.mop.cluster.rpc.anton", "mop.mop.node.hyper.rpc.anton.ermak"):
        expect("anton.ermak", subj, False)
    expect("anton", "mop.mop.node.hyper.rpc.anton%2Eermak", False)
    # Отказ на deploy -- только пустому логину и управляющим символам.
    for bad in ("", "a\tb", "a\nb"):
        try:
            operators.permissions(ops["alice"], bad)
            out.append(f"login {bad!r} must be refused")
        except ValueError:
            pass
    return out


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
            "operators": {n: {"password": f"{n}-pass", **operators.permissions(o, n)}
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
        perms = operators.permissions(ops[name], name)
        for subj in (busnames.inbox(project, "host-1"),
                     busnames.inbox(project, busnames.ALL_MASTERS), reply):
            if not permitted(perms["allow"], subj, perms["deny"]):
                failed.append(f"{name} may not subscribe to {subj}")
    # STATUS: FIXED — see #144

    failed += check_login_in_subject_207()

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

    # HYPOTHESIS (#206): с auth callout люди и папеты входят через сервис, а
    # в статическом списке остаются только машины, которых callout не
    # спрашивает (auth_users). Два списка, набранные порознь, разойдутся:
    # машина вне auth_users пойдёт в callout и получит отказ, а изменить
    # auth_users reload'ом nats не даёт (стенд #206: «config reload not
    # supported for AuthCallout») -- только рестартом.
    # SOLUTION: MOP_AUTH_CALLOUT (по умолчанию off -- users.conf байт в байт
    # как был); on -- статические пользователи и auth_users из одного списка
    # машин; блок callout -- своим файлом callout.conf, и его смена --
    # рестарт, а не reload.
    # STATUS: FIXED — see #206
    import re
    base = dict(BASE, callout="co-pass")
    projects = {"mop": "pu-pass", "rugent": "ru-pass"}
    try:
        check_on = natsconf.callout_on
        for value, want in (("on", True), ("off", False), ("", False)):
            if check_on(value) is not want:
                failed.append(f"callout_on({value!r}) must be {want}")
        try:
            check_on("yes")
            failed.append("MOP_AUTH_CALLOUT=yes must be refused: on or off, nothing else")
        except ValueError:
            pass
        if natsconf.render(base, projects, callout=False) != natsconf.render(BASE, projects):
            failed.append("callout off: users.conf must be byte-identical to today's")
        on = natsconf.render(base, projects, callout=True)
        static = re.findall(r'user: "([^"]+)"', on)
        if static != ["callout", "service", "node-hyper"]:
            failed.append(f"callout on: static users are the machines only: {static}")
        if 'allowed_connection_types' in on:
            failed.append("callout on: no human entry may stay in users.conf")
        cblock = on[on.index('user: "callout"'):on.index('user: "service"')]
        if '"$SYS._INBOX.>"' not in cblock or '"$SYS.REQ.USER.AUTH"' not in cblock \
                or '"mop.>"' in cblock:
            failed.append(f"the callout user may only answer auth requests: {cblock!r}")
        conf = natsconf.render_callout(base, "AISSUER", "XKEY")
        auth_users = re.findall(r'"([^"]+)"', re.search(r"auth_users: \[([^]]*)\]", conf).group(1))
        if auth_users != static:
            failed.append(f"auth_users {auth_users} must be the static users {static}")
        for want in ("auth_callout {", "issuer: AISSUER", 'account: "$G"', "xkey: XKEY",
                     "timeout: 2"):
            if want not in conf:
                failed.append(f"callout.conf lacks {want!r}: {conf}")
        # Не разойтись: новый узел попадает в оба списка сразу.
        grown = dict(base, nodes=dict(base["nodes"], mini="mi-pass"))
        s2 = re.findall(r'user: "([^"]+)"', natsconf.render(grown, projects, callout=True))
        a2 = re.findall(r'"([^"]+)"', re.search(r"auth_users: \[([^]]*)\]",
                                                natsconf.render_callout(grown, "A", "X")).group(1))
        if s2 != a2 or "node-mini" not in a2:
            failed.append(f"a new node must land in both lists: {s2} vs {a2}")
        off = natsconf.render_callout(base, None, None, callout=False)
        if "auth_callout" in off or "timeout" in off:
            failed.append(f"callout off: callout.conf holds nothing for nats: {off!r}")
        try:
            natsconf.render(BASE, projects, callout=True)
            failed.append("callout on without a callout password must be refused")
        except ValueError:
            pass
        # Рестарт или reload: решает блок callout, а не users.conf.
        if not natsconf.needs_restart(off, conf) or natsconf.needs_restart(conf, conf) \
                or not natsconf.needs_restart(None, conf):
            failed.append("a changed or new callout.conf needs a restart, an unchanged one not")
    except AttributeError as e:
        failed.append(f"auth callout rendering is missing: {e}")

    print("\n".join(f"FAIL {l}" for l in failed) if failed else "", end="\n" if failed else "")
    print("natsconf: FAILED" if failed else "natsconf: ok")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
