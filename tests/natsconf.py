#!/usr/bin/env python3
"""Проверка пользователей NATS без пула: python3 tests/natsconf.py

Список пользователей шины собирает один код (#116): включить в массив
`users` второй файл NATS не умеет (проверено `nats-server -t`: include внутри
массива -- ошибка разбора), поэтому весь список -- один файл, и пишет его
сервер. Люди, машины и узлы приходят базовым JSON от `mop server deploy`, папеты --
из реестра сервера. Здесь -- текст файла и пароли; reload и подключение под
новым пользователем проверяются только на живом пуле.
"""
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import natsconf, operators  # noqa: E402

BASE = {
    "service": "svc-pass",
    "callout": "co-pass",
    "nodes": {"hyper": "node-pass"},
}

# Снимок users.conf до #144: имена субъектов и пользователей собраны в одно
# определение, и рефакторинг обязан не сдвинуть в файле ни байта. Снят заново
# в #207, #212, #213 (права людей) и в #219 намеренно: людей и папетов в
# файле больше нет -- их пускает auth callout (#206), а статически входят
# только машины, которых callout не спрашивает.
OPERATORS = "anton:admin; ivan:user:rugent,cloudpub; olga:user:*"
USERS_CONF = """users = [
  {
    user: "callout", password: "co-pass"
    permissions: {
      publish:   { allow: ["$SYS._INBOX.>"] }
      subscribe: { allow: ["$SYS.REQ.USER.AUTH"] }
    }
  }
  {
    user: "service", password: "svc-pass"
    permissions: {
      publish:   { allow: ["mop.>", "mopjoin.*.rpc", "_INBOX.>"], deny: ["mop.*.node.*.rpc.*", "mop.*.cluster.rpc.*", "mopjoin.*.rpc"] }
      subscribe: { allow: ["mop.>", "mopjoin.*.rpc", "_INBOX.>"] }
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


def granted(ops, projects=("mop",)):
    """Права всех, кто на шине: машины -- из users.conf, как их прочтёт NATS;
    люди и папеты -- тем, что выдаёт им auth callout (#206, #219): людям --
    operators.permissions, папетам -- natsconf.puppet_permissions. Те же
    функции, что callout кладёт в JWT (tests/callout.py сверяет стороны)."""
    got = rules(natsconf.render({"service": "svc-pass", "callout": "co-pass",
                                 "nodes": {"hyper": "hy-pass"}}))
    for n, o in ops.items():
        p = operators.permissions(o, n)
        got[n] = {"publish": (p["publish"], p["publish_deny"]),
                  "subscribe": (p["allow"], p["deny"])}
    for pr in projects:
        p = natsconf.puppet_permissions(pr)
        got[f"puppet-{pr}"] = {"publish": (p["publish"], []), "subscribe": (p["subscribe"], [])}
    return got


def check_login_in_subject_207(c):
    """HYPOTHESIS (#207): человеку дан весь mop.<p>.> (или mop.> без admin) --
    логин токеном субъекта он подделал бы так же, как полем тела.
    SOLUTION: публикация человека -- явным списком: rpc агента и сервиса
    кластера -- только со СВОИМ логином (плюс прежние субъекты без логина на
    время перехода), остальное, что люди публикуют сегодня, -- как было.
    Подписка -- как была. Машины логин человека не публикуют: у сервиса --
    deny на субъекты с логином, у узлов и папетов их нет в списке.
    STATUS: FIXED — see #207"""
    from mop.common import busnames
    ops = operators.parse("alice:user:mop; bob:user:mop; anton:admin; olga:user:*; "
                          "anton.ermak:user:mop")
    got = granted(ops)

    def may(user, subj, side="publish"):
        allow, deny = got[user][side]
        return permitted(allow, subj, deny)

    def rule(user, subj, want, side="publish"):
        """-> (что, получено, ожидалось) для c.expect."""
        return f"{user} {'may' if want else 'must not'} {side} {subj}", may(user, subj, side), want

    # Свой логин -- да, чужой -- нет; прежние субъекты -- на время перехода.
    c.expect(*rule("alice", "mop.mop.node.hyper.rpc.alice", True))
    c.expect(*rule("alice", "mop.mop.node.hyper.rpc.bob", False))
    c.expect(*rule("alice", "mop.mop.cluster.rpc.alice", True))
    c.expect(*rule("alice", "mop.mop.cluster.rpc.bob", False))
    # #404: вне mop.>, поэтому user:* и admin не получают чужой логин
    # через проектную маску; service слушает, но не публикует запрос.
    for user in ("alice", "anton", "olga", "anton.ermak"):
        c.expect(*rule(user, busnames.join_config(user), True))
        c.expect(*rule(user, busnames.join_config("bob"), False))
    for user in ("service", "node-hyper", "puppet-mop"):
        c.expect(*rule(user, busnames.join_config("alice"), False))
    c.expect(*rule("alice", "mop.mop.node.hyper.rpc", True))
    c.expect(*rule("alice", "mop.mop.cluster.rpc", True))
    # Всё, что человек публикует сегодня, остаётся: инбоксы мастеров и
    # опрос who, события, публичный канал, all, сервер, ответы.
    for subj in ("mop.mop.node.hyper.msg", "mop.mop.all.msg", "mop.mop.master.h-1.inbox",
                 "mop.mop.master.all.inbox", "mop.mop.events", "mop.mop.server.rpc",
                 "_INBOX.abc.1"):
        c.expect(*rule("alice", subj, True))
    # Чужой проект и машинное -- как было: нет.
    for subj in ("mop.rugent.node.hyper.rpc.alice", "mop.admin.node.hyper.rpc.alice",
                 "mop.admin.build.rpc", "mop.rugent.events"):
        c.expect(*rule("alice", subj, False))
    # Подписка: свой инбокс и ответы (список -- #212, check_subscribe_212;
    # инбокс под своим логином -- #213, check_inbox_login_213).
    for subj in ("mop.mop.master.alice.h-1.inbox", "mop.mop.master.all.inbox", "_INBOX.abc.1"):
        c.expect(*rule("alice", subj, True, "subscribe"))
    # admin: весь пул, но логин -- свой.
    for subj in ("mop.rugent.node.hyper.rpc.anton", "mop.admin.node.hyper.rpc.anton",
                 "mop.admin.cluster.rpc.anton", "mop.admin.build.rpc",
                 "mop.admin.master.h-1.inbox", "mop.rugent.events", "mop.admin.node.hyper.rpc"):
        c.expect(*rule("anton", subj, True))
    for subj in ("mop.rugent.node.hyper.rpc.bob", "mop.admin.cluster.rpc.bob"):
        c.expect(*rule("anton", subj, False))
    # user:* -- любой проект, но не admin, и логин свой.
    c.expect(*rule("olga", "mop.rugent.node.hyper.rpc.olga", True))
    c.expect(*rule("olga", "mop.rugent.node.hyper.rpc.bob", False))
    c.expect(*rule("olga", "mop.admin.node.hyper.rpc.olga", False))
    c.expect(*rule("olga", "mop.admin.cluster.rpc.olga", False))
    # Машины не публикуют логин человека.
    for user in ("service", "node-hyper", "puppet-mop"):
        for subj in ("mop.mop.node.hyper.rpc.alice", "mop.mop.cluster.rpc.alice",
                     "mop.admin.node.hyper.rpc.anton", "mop.admin.cluster.rpc.anton"):
            c.expect(*rule(user, subj, False))
    # ...а своё делают как прежде: сервис спрашивает агента без логина
    # (ворота #40), отвечает и пишет события.
    for subj in ("mop.admin.node.hyper.rpc", "mop.mop.events", "_INBOX.abc.1"):
        c.expect(*rule("service", subj, True))
    for subj in busnames.service_subscriptions():
        c.expect(*rule("service", subj, True, "subscribe"))
    for subj in busnames.agent_subscriptions("hyper")["rpc"]:
        c.expect(*rule("node-hyper", subj, True, "subscribe"))
    # Логин с точкой (LDAP/AD, #208) -- закодированным токеном, и только своим.
    c.expect(*rule("anton.ermak", "mop.mop.node.hyper.rpc.anton%2Eermak", True))
    c.expect(*rule("anton.ermak", "mop.mop.cluster.rpc.anton%2Eermak", True))
    for subj in ("mop.mop.node.hyper.rpc.anton", "mop.mop.node.hyper.rpc.bob",
                 "mop.mop.cluster.rpc.anton", "mop.mop.node.hyper.rpc.anton.ermak"):
        c.expect(*rule("anton.ermak", subj, False))
    c.expect(*rule("anton", "mop.mop.node.hyper.rpc.anton%2Eermak", False))
    # Отказ на deploy -- только пустому логину и управляющим символам.
    for bad in ("", "a\tb", "a\nb"):
        try:
            operators.permissions(ops["alice"], bad)
            refused = False
        except ValueError:
            refused = True
        c.check(f"login {bad!r} must be refused", refused)


def check_subscribe_212(c):
    """HYPOTHESIS (#212): после #207 человек публикует только со своим
    логином, но подписан на весь mop.<p>.> (или mop.>): подписавшись на
    rpc агента или сервиса кластера, он отвечает первым вместо них, и
    проверенная личность вызывающего ничего не стоит.
    SOLUTION: подписка человека -- явным списком того, на что подписывается
    клиентский код: инбоксы мастеров (свой и опрос who, mop.<p>.master.>),
    события проекта (дашборд под человеком) и _INBOX (ответы, поток
    сборки). rpc агентов и сервисов, msg, all, server, build -- нет.
    STATUS: FIXED — see #212"""
    ops = operators.parse("alice:user:mop; anton:admin; olga:user:*")
    got = granted(ops)

    def rule(user, subj, want):
        """-> (что, получено, ожидалось) для c.expect."""
        allow, deny = got[user]["subscribe"]
        return (f"{user} {'may' if want else 'must not'} subscribe {subj}",
                permitted(allow, subj, deny), want)

    # Ответить вместо агента или сервиса -- нельзя: их субъекты не слушать.
    for user, p in (("alice", "mop"), ("anton", "mop"), ("anton", "admin"), ("olga", "rugent")):
        for subj in (f"mop.{p}.node.hyper.rpc.*", f"mop.{p}.node.hyper.rpc",
                     f"mop.{p}.node.*.rpc.>", f"mop.{p}.node.hyper.msg",
                     f"mop.{p}.cluster.rpc.*", f"mop.{p}.cluster.rpc",
                     f"mop.{p}.server.rpc", f"mop.{p}.all.msg", f"mop.{p}.>"):
            c.expect(*rule(user, subj, False))
    for subj in ("mop.admin.build.rpc", "mop.>", "mop.*.cluster.rpc.*", "mop.*.node.*.rpc.*"):
        c.expect(*rule("anton", subj, False))
    # Всё, на что клиент подписывается сегодня: свой инбокс (адрес
    # <логин>.<хост>-<pid> с #213, хост бывает с точками), опрос who, ответы
    # и мультиплекс запросов nats-py, поток сборщика (новый _INBOX), события.
    for user, p in (("alice", "mop"), ("anton", "admin"), ("anton", "rugent"),
                    ("olga", "rugent")):
        for subj in (f"mop.{p}.master.{user}.wate-1.inbox", f"mop.{p}.master.all.inbox",
                     f"mop.{p}.master.{user}.host.lan-7.inbox", f"mop.{p}.events",
                     "_INBOX.abc", "_INBOX.abc.*"):
            c.expect(*rule(user, subj, True))
    # Дашборд под admin слушает события всех проектов разом.
    c.expect(*rule("anton", "mop.*.events", True))
    # Чужой проект и admin -- как было.
    c.expect(*rule("alice", "mop.rugent.master.alice.wate-1.inbox", False))
    c.expect(*rule("olga", "mop.admin.master.olga.wate-1.inbox", False))


def check_inbox_login_213(c):
    """HYPOTHESIS (#213): после #207/#212 человек подписан на
    mop.<p>.master.> -- инбоксы всех мастеров проекта: адрес инбокса
    <хост>-<pid> логина не несёт, и второй мастер читает отчёты папетов
    первому.
    SOLUTION: адрес мастера -- <токен логина>.<хост>-<pid>, инбокс
    mop.<p>.master.<токен>.<хост>-<pid>.inbox; человек подписан только на
    mop.<p>.master.<свой токен>.> и опрос who (mop.<p>.master.all.inbox).
    Публикация в инбоксы мастеров -- как была: папеты, узлы, люди.
    STATUS: FIXED — see #213"""
    ops = operators.parse("alice:user:mop; bob:user:mop; anton.ermak:admin; olga:user:*")
    got = granted(ops)

    def sub(user, subj, want):
        """-> (что, получено, ожидалось) для c.expect."""
        allow, deny = got[user]["subscribe"]
        return (f"{user} {'may' if want else 'must not'} subscribe {subj}",
                permitted(allow, subj, deny), want)

    def pub(user, subj, want=True):
        """-> (что, получено, ожидалось) для c.expect."""
        allow, deny = got[user]["publish"]
        return (f"{user} {'may' if want else 'must not'} publish {subj}",
                permitted(allow, subj, deny), want)

    # Адрес: логин токеном впереди, хост с точками -- после.
    from mop.common import busnames
    addr = busnames.master_address("anton.ermak", "wate.lan-7")
    c.expect("master_address", addr, "anton%2Eermak.wate.lan-7")
    c.expect("inbox of the new address", busnames.inbox("mop", addr),
             "mop.mop.master.anton%2Eermak.wate.lan-7.inbox")
    alice = busnames.inbox("mop", busnames.master_address("alice", "wate-1"))
    bob = busnames.inbox("mop", busnames.master_address("bob", "wate-2"))
    who = busnames.inbox("mop", busnames.ALL_MASTERS)
    c.expect(*sub("alice", alice, True))
    c.expect(*sub("alice", who, True))
    c.expect(*sub("alice", bob, False))
    c.expect(*sub("bob", bob, True))
    c.expect(*sub("bob", alice, False))
    # Прежний адрес без логина -- ровно утечка: его не слушает никто из людей.
    for old in ("mop.mop.master.wate-1.inbox", "mop.mop.master.host.lan-7.inbox",
                "mop.mop.master.>", "mop.mop.master.*.>", "mop.mop.master.bob.>"):
        c.expect(*sub("alice", old, False))
    # Логин с точкой -- своим токеном, а не префиксом «anton».
    c.expect(*sub("anton.ermak", busnames.inbox("mop", addr), True))
    c.expect(*sub("anton.ermak", "mop.mop.master.anton.wate.lan-7.inbox", False))
    # user:* -- свой инбокс в любом проекте, чужой -- нигде.
    c.expect(*sub("olga", busnames.inbox("rugent", busnames.master_address("olga", "h-1")), True))
    c.expect(*sub("olga", busnames.inbox("rugent", busnames.master_address("bob", "h-1")), False))
    # Ответы в инбокс нового вида: папет проекта, агент узла, другой мастер.
    c.expect(*pub("puppet-mop", alice))
    c.expect(*pub("node-hyper", alice))
    c.expect(*pub("bob", alice))
    c.expect(*pub("puppet-mop", busnames.inbox("mop", addr)))


def main():
    c = Checks()

    # HYPOTHESIS (#116): пользователей рендерит шаблон ansible на контроллере,
    # и сервис пула не может завести проект, не повторив шаблон второй копией.
    # SOLUTION: один рендерер на Python, им пользуются и deploy, и сервис.
    # STATUS: FIXED — see #116
    # С #219 в файле только машины: людей и папетов пускает callout (#206).
    text = natsconf.render(BASE)
    for want in ('user: "service", password: "svc-pass"',
                 'user: "callout", password: "co-pass"',
                 'user: "node-hyper", password: "node-pass"',
                 '"mop.*.node.hyper.>"'):
        c.check(f"render lacks {want!r}", not (want not in text))
    for gone in ('allowed_connection_types', 'user: "puppet-', 'user: "anton"'):
        c.check(f"users.conf must hold machines only (#219), found {gone!r}",
                not (gone in text))
    c.check("render must be the users array, the file included into authorization {}",
            not (not text.lstrip().startswith("users = [")))
    # Порядок стабилен: иначе каждый прогон менял бы файл и дёргал reload.
    c.check("render must not depend on the order of nodes",
            not (natsconf.render({**BASE, "nodes": {"mini": "m", "hyper": "node-pass"}}) !=
                 natsconf.render({**BASE, "nodes": {"hyper": "node-pass", "mini": "m"}})))
    # Кавычка в пароле не рвёт файл.
    odd = natsconf.render({**BASE, "service": 'a"b'})
    c.check("a quote in a password must be escaped", not ('password: "a\\"b"' not in odd),
            repr(odd[:120]))

    # HYPOTHESIS (#144): субъекты и имена пользователей набраны руками в шести
    # модулях; переименование в одном месте молча разводит права и подписки.
    # SOLUTION: одно определение (mop/common/busnames.py), из него строятся и права,
    # и подписки. Характеризация: файл пользователей тот же байт в байт.
    ops = operators.parse(OPERATORS)
    got = natsconf.render({"service": "svc-pass", "callout": "co-pass",
                           "nodes": {"hyper": "hy-pass", "mini": "mi-pass"}})
    c.check("users.conf drifted from its snapshot", not (got != USERS_CONF), "\n" + got)

    # Свойство (#144): всё, на что подписываются агент и сервисы, разрешено
    # правами их пользователя. Подписки и права строятся из busnames, и
    # проверка ловит расхождение до пула, где отказ прав молчит (error_cb).
    from mop.common import busnames
    c.check("covers() does not follow NATS wildcard semantics",
            not (covers("_INBOX.>", "_INBOX") or not covers("mop.*.node.x.>", "mop.*.node.x.rpc")
                 or covers("mop.a.>", "mop.*.events") or not covers("mop.>", "mop.*.all.msg")))
    reply = "_INBOX.abc.1"          # ответ на request: подписка на свой инбокс
    for node in ("hyper", "mini"):
        perms = natsconf.node_permissions(node)
        subs = busnames.agent_subscriptions(node)
        for subj in subs["rpc"] + subs["msg"] + [reply]:
            c.check(f"{busnames.node_user(node)} may not subscribe to {subj}",
                    not (not permitted(perms["subscribe"], subj)))
        c.check("the agent must listen on rpc and msg",
                not (not subs["rpc"] or not subs["msg"]), subs)
    for subj in busnames.service_subscriptions() + [reply]:
        c.check(f"{busnames.SERVICE} may not subscribe to {subj}",
                not (not permitted(natsconf.SERVICE_PERMISSIONS, subj)))
    for p in ("mop", "rugent"):
        allow = natsconf.puppet_permissions(p)["subscribe"]
        c.check(f"{busnames.puppet_user(p)} may not subscribe to its reply inbox",
                not (not permitted(allow, reply)))
    # Мастер слушает свой инбокс и опрос `who`: оператор с проектом, admin
    # -- за псевдопроект admin, user:* -- за любой проект.
    for name, project in (("ivan", "rugent"), ("anton", busnames.ADMIN), ("olga", "mop")):
        perms = operators.permissions(ops[name], name)
        for subj in (busnames.inbox(project, busnames.master_address(name, "host-1")),
                     busnames.inbox(project, busnames.ALL_MASTERS), reply):
            c.check(f"{name} may not subscribe to {subj}",
                    not (not permitted(perms["allow"], subj, perms["deny"])))
    # STATUS: FIXED — see #144

    check_login_in_subject_207(c)
    check_subscribe_212(c)
    check_inbox_login_213(c)

    # Пароли папетов рождаются на сервере: недостающий заводится, имеющийся
    # не меняется -- иначе живые папеты отвалились бы от шины.
    root = tempfile.mkdtemp(prefix="mop-test-natsconf-")
    with open(os.path.join(root, "nats-puppet-mop.pass"), "w") as f:
        f.write("kept\n")
    got = natsconf.passwords(["mop", "rugent"], root)
    c.check("an existing password must be kept", not (got.get("mop") != "kept"), got)
    new = got.get("rugent") or ""
    c.check("a new password must be 32+ letters and digits",
            not (len(new) < 32 or not new.isalnum()), repr(new))
    c.check("a generated password must be stored and reused",
            not (natsconf.passwords(["rugent"], root).get("rugent") != new))
    mode = os.stat(os.path.join(root, "nats-puppet-rugent.pass")).st_mode & 0o777
    c.check("a password file must be 0600", not (mode != 0o600), f"got {oct(mode)}")

    # HYPOTHESIS (#211): reload -- SIGHUP вслепую. nats отвергает reload молча
    # (только «[ERR] Failed to reload server configuration» в своём журнале) и
    # продолжает на старом конфиге, а `mop server cluster users --reload`, удаление
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
        c.expect("digest_of must read -t's sha256", natsconf.digest_of(ok_t), new)
        c.check("digest_of of a failed -t must be None",
                not (natsconf.digest_of(bad_t) is not None))
        c.check("running == file must be accepted",
                not (natsconf.reload_verdict(ok_t, new) is not None),
                repr(natsconf.reload_verdict(ok_t, new)))
        why = natsconf.reload_verdict(ok_t, old) or ""
        c.check("a kept old digest must be a refusal naming both digests and the cure (restart)",
                old in why and new in why and "restart" in why, repr(why))
        why = natsconf.reload_verdict(bad_t, old) or ""
        c.check("an invalid file must be refused with nats' own words",
                not ("Parse error on line 3" not in why), repr(why))
        why = natsconf.reload_verdict(ok_t, None) or ""
        c.check("no running digest (monitoring silent) must be a refusal",
                not ("varz" not in why), repr(why))
    except AttributeError as e:
        c.fail("verified reload is missing", e)

    # HYPOTHESIS (#206): с auth callout люди и папеты входят через сервис, а
    # в статическом списке остаются только машины, которых callout не
    # спрашивает (auth_users). Два списка, набранные порознь, разойдутся:
    # машина вне auth_users пойдёт в callout и получит отказ, а изменить
    # auth_users reload'ом nats не даёт (стенд #206: «config reload not
    # supported for AuthCallout») -- только рестартом.
    # SOLUTION: статические пользователи и auth_users из одного списка
    # машин; блок callout -- своим файлом callout.conf, и его смена --
    # рестарт, а не reload. С #219 callout всегда включён: людей вне
    # провайдера нет, и выключенный callout не пустил бы никого.
    # STATUS: FIXED — see #206, #219
    import re
    try:
        c.check("#219: the callout is always on -- no off switch in natsconf",
                not (hasattr(natsconf, "callout_on") or hasattr(natsconf, "CALLOUT_OFF")))
        on = natsconf.render(BASE)
        static = re.findall(r'user: "([^"]+)"', on)
        c.expect("static users are the machines only", static,
                 ["callout", "service", "node-hyper"])
        cblock = on[on.index('user: "callout"'):on.index('user: "service"')]
        c.check("the callout user may only answer auth requests",
                not ('"$SYS._INBOX.>"' not in cblock or '"$SYS.REQ.USER.AUTH"' not in cblock
                     or '"mop.>"' in cblock), repr(cblock))
        conf = natsconf.render_callout(BASE, "AISSUER", "XKEY")
        auth_users = re.findall(r'"([^"]+)"', re.search(r"auth_users: \[([^]]*)\]", conf).group(1))
        c.expect("auth_users must be the static users", auth_users, static)
        for want in ("auth_callout {", "issuer: AISSUER", 'account: "$G"', "xkey: XKEY",
                     "timeout: 2"):
            c.check(f"callout.conf lacks {want!r}", not (want not in conf), conf)
        # Не разойтись: новый узел попадает в оба списка сразу.
        grown = dict(BASE, nodes=dict(BASE["nodes"], mini="mi-pass"))
        s2 = re.findall(r'user: "([^"]+)"', natsconf.render(grown))
        a2 = re.findall(r'"([^"]+)"', re.search(r"auth_users: \[([^]]*)\]",
                                                natsconf.render_callout(grown, "A", "X")).group(1))
        c.check("a new node must land in both lists",
                not (s2 != a2 or "node-mini" not in a2), f"{s2} vs {a2}")
        try:
            natsconf.render({k: v for k, v in BASE.items() if k != "callout"})
            refused = False
        except ValueError:
            refused = True
        c.check("no callout password must be refused: nobody could let people in", refused)
        # Рестарт или reload: решает блок callout, а не users.conf.
        c.check("a changed or new callout.conf needs a restart, an unchanged one not",
                not (not natsconf.needs_restart("# old\n", conf) or natsconf.needs_restart(conf, conf)
                     or not natsconf.needs_restart(None, conf)))
    except (AttributeError, TypeError) as e:
        c.fail("auth callout rendering", f"{type(e).__name__}: {e}")

    return c.report("natsconf")


if __name__ == "__main__":
    sys.exit(main())
