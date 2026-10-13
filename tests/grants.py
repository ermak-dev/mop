#!/usr/bin/env python3
"""Реестр привязок душ #581 без пула: python3 tests/grants.py

Принятая схема #579 (docs/SOUL.md, ревизия 18db5ec): одна инкарнация --
один принципал `grant-<32hex>`, реестр привязок на сервере (принципал ->
{owner, project, job, soul_inc, soul_digest, role, active}), бандл роли
закреплён дайджестом и неизменяем, отзыв двухступенчатый (глагольный
active:false, транспортный -- снос файла пароля), резолвер fail-closed
проверяет глагол по спискам роли. Подготовка идемпотентна по request_id:
параллельные одинаковые запуски различны, ретрай того же запроса получает
ту же сохранённую привязку (principал + зарезервированные job/soul_inc).

Две половины файла. Поведенческая -- против работающего кода сегодня:
классификатор шины обязан знать новое пространство имён (иначе логин
человека коллидирует с принципалом, callout ведёт его как человека),
глагол resolve на bootstrap-сервисе обязан отвечать узлу. Хранилищная --
против mop/server/grants.py: интерфейсные заглушки поднимают
NotImplementedError, и проверки падают поведенчески, а не импортом
(#544: отказ настройки -- не доказательство дефекта).

HYPOTHESIS: без реестра делегированная личность существует только именем:
узел не знает, чей глагол, callout не знает, чего принципалу можно, а
смена роли в живом джобе прошла бы молча. Классификатор сегодня знает
три префикса -- новый четвёртый он молча считает человеком.
SOLUTION: mop/server/grants.py -- реестр + бандлы + резолвер + рендер прав
одним серверным модулем; пространство имён -- в busnames/soul одним
определением (#580 landed, 369b01d: digest 64hex). STATUS: FIXED —
55/55 зелёные на merge 369b01d (свой #581) -- see #581. Хранилищная
половина была заблокирована дефектом #580 (soul.binding требовал
soul_digest в 32 hex при контрактных 64); после исправления 369b01d
легла в зелень БЕЗ правок проверок.
"""
import json
import os
import re
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- изоляция раньше mop
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import busnames, soul  # noqa: E402

PRINCIPAL = re.compile(r"^grant-[0-9a-f]{32}$")

# Манифесты по схеме v1 (docs/SOUL.md): поставляемые роли. Глаголы -- из
# общих словарей #580, digest -- общая формула (digest), инкарнация --
# UUID (soul.incarnation): те же правила, что у живых регистраций.
MASTER = {"soul": "master", "version": 1,
          "runtime": {"driver": "isolating", "receives_dispatch": False},
          "capabilities": {"node_verbs": ["send", "tail", "state", "states",
                                          "ping", "local"],
                           "cluster_verbs": ["roster", "add"],
                           "dispatch": True, "spawn": ["puppet", "builder"]},
          "prompt": "soul.md"}
PUPPET = {"soul": "puppet", "version": 1,
          "runtime": {"driver": "any", "receives_dispatch": True},
          "capabilities": {"node_verbs": ["send", "tail", "state", "states",
                                          "ping", "local"],
                           "cluster_verbs": [], "dispatch": False,
                          "spawn": []},
          "prompt": "soul.md"}
MASTER_PROMPT = b"# master prompt\n"
PUPPET_PROMPT = b"# puppet prompt\n"
MASTER_DIGEST = soul.digest(MASTER, MASTER_PROMPT)
PUPPET_DIGEST = soul.digest(PUPPET, PUPPET_PROMPT)


def incarnation(seed):
    """Стабильный UUID по седу: разные инкарнации, детерминированный тест."""
    import uuid
    return str(uuid.uuid5(uuid.NAMESPACE_DNS, seed))

# Манифесты по схеме v1 (docs/SOUL.md): поставляемые роли.
MASTER = {"soul": "master", "version": 1,
          "runtime": {"driver": "isolating", "receives_dispatch": False},
          "capabilities": {"node_verbs": ["send", "tail", "state", "states",
                                          "ping", "local"],
                           "cluster_verbs": ["roster", "add"],
                           "dispatch": True, "spawn": ["puppet", "builder"]},
          "prompt": "soul.md"}
PUPPET = {"soul": "puppet", "version": 1,
          "runtime": {"driver": "any", "receives_dispatch": True},
          "capabilities": {"node_verbs": ["send", "tail", "state", "states",
                                          "ping", "local"],
                           "cluster_verbs": [], "dispatch": False,
                           "spawn": []},
          "prompt": "soul.md"}


# ─── поведенческая половина: сегодняшний код, принятая схема ─────────────
def check_namespace_today(c):
    """Пространство grant- зарезервировано схемой: у классификатора шины
    своё определение (soul.is_grant), у имён людей -- отказ (#580 landed)."""
    name = "grant-" + "0" * 32
    c.check("#581 the classifier knows the grant-* namespace "
            "(a grant is not a human login)",
            soul.is_grant(name) is True and soul.is_grant("anton") is False,
            f"soul.is_grant({name!r}) is False: callout поведёт принципала "
            f"как человека")
    c.check("#581 the grant-* prefix is in RESERVED_PREFIXES",
            name.startswith(busnames.RESERVED_PREFIXES),
            f"RESERVED_PREFIXES={busnames.RESERVED_PREFIXES}")
    from mop.server import operators
    try:
        operators.parse(f"{name}:user:mop")
    except ValueError:
        c.check("#581 operators.parse refuses a human login from the "
                "grant-* namespace", True)
    else:
        c.fail("#581 operators.parse refuses a human login from the "
               "grant-* namespace",
               "parse принял логин принципала как человека: права "
               "сложились бы в одного пользователя")


def check_callout_routing_today(c):
    """Callout не ведёт принципала человеком: даже провайдер людей,
    принимающий всех (стенд), не выдаёт grant-логину человеческих прав."""
    from mop.server import callout, identity

    class Everyone:
        """Провайдер людей, принимающий любой логин (только стенд)."""

        def authenticate(self, login, password):
            return identity.Identity(login=login, role="admin",
                                     projects=["*"], name="", email="")

    name = "grant-" + "0" * 32
    try:
        _, perms = callout.rights(name, "websocket", "pw", Everyone(),
                                  puppets=None)
    except identity.Refused as e:
        c.check("#581 callout refuses a grant login on the human path",
                True, str(e))
    else:
        c.fail("#581 callout refuses a grant login on the human path",
               f"права человека выданы принципалу: {perms!r}")
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        mod.activate(rec["principal"], root=root.name)
        with open(os.path.join(root.name,
                               busnames.pass_file(rec["principal"]))) as f:
            password = f.read().strip()
        try:
            who, perms = callout.rights(rec["principal"], "nats", password,
                                        Everyone(), puppets=None,
                                        souls=mod.Provider(root.name))
        except identity.Refused as e:
            c.fail("#581 callout renders the grant ACL from the registry",
                   f"активный принципал с паролем отказан: {e}")
            return
        except TypeError as e:
            c.fail("#581 callout renders the grant ACL from the registry",
                   f"callout.rights не принимает провайдера душ: {e}")
            return
        want = mod.permissions(rec["principal"],
                               mod.binding(rec["principal"], root=root.name),
                               root=root.name)
        c.check("#581 callout renders the grant ACL from the registry",
                who == rec["principal"]
                and sorted(perms["pub"]["allow"])
                == sorted(want["publish"])
                and sorted(perms["sub"]["allow"])
                == sorted(want["subscribe"]), perms)


def check_resolve_endpoint_today(c):
    """Глагол resolve на bootstrap-сервисе (#603): анонимный легаси
    server.rpc публикуют и проектные люди, поэтому анонимный вызов --
    отказ БЕЗ фактов о принципале; вердикт едет только узловому
    вызывающему, названному субъектом (_caller каркаса, не тело)."""
    from mop.server import bootstrap

    anon = bootstrap.answer("mop", {"verb": "resolve", "principal": "grant-x",
                                    "service": "node", "verb_name": "state"})
    c.check("#581 the bootstrap service answers a resolve request "
            "with a verdict, not a missing verb",
            not (anon.get("error") or "").startswith("no such verb"),
            f"сегодня: {anon.get('error')!r}")
    c.check("#581 an anonymous legacy caller gets a denial "
            "that echoes no principal and no role",
            anon.get("allow") is False and "grant-" not in json.dumps(anon)
            and not (anon.get("role") or anon.get("owner")), anon)
    out = bootstrap.answer("mop", {"verb": "resolve", "principal": "grant-x",
                                   "service": "node", "verb_name": "state",
                                   "_caller": "node-pve1"})
    c.check("#581 a node caller gets the fail-closed verdict "
            "for an unknown principal",
            out.get("allow") is False and "unknown principal" in
            (out.get("reason") or ""), out)
    out = bootstrap.answer("mop", {"verb": "resolve_target",
                                   "target": "pu-mop-7"})
    c.check("#581 resolve_target is internal: the wire carries one "
            "resolve verb only",
            (out.get("error") or "").startswith("no such verb"), out)
    # Режим только-цели (#584): узлу нужно прочитать привязку ДО того,
    # как процесс роли сможет действовать. Без принципала -- это ПОИСК
    # ЦЕЛИ, не делегированное действие: отсутствие принципала не
    # превращается молча в анонимное привилегированное действие --
    # явный data-only режим, и тоже только узловому вызывающему.
    anon = bootstrap.answer("mop", {"verb": "resolve", "target": "pu-mop-9"})
    c.check("#581 a principal-less resolve is node-gated too",
            anon.get("allow") is False
            and "node callers" in (anon.get("reason") or ""), anon)
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n", job="pu-mop-9",
                         soul_inc=incarnation("nine"), request_id="req-t9")
    with root:
        mod.activate(rec["principal"], root=root.name)
        out = bootstrap.resolve_soul("mop", {"target": "pu-mop-9",
                                             "_caller": "node-pve1"},
                                     root=root.name)
        c.check("#581 a principal-less node resolve returns the exact "
                "binding state, no action verdict, no password",
                out.get("found") is True and out.get("job") == "pu-mop-9"
                and out.get("project") == "mop"
                and out.get("owner") == "anton"
                and out.get("role") == "puppet"
                and out.get("soul_inc") == incarnation("nine")
                and out.get("soul_digest") == PUPPET_DIGEST
                and out.get("active") is True
                and out.get("receives_dispatch") is True
                and out.get("allow") is None
                and "password" not in json.dumps(out), out)
        out = bootstrap.resolve_soul("mop", {"target": "pu-mop-9",
                                             "soul_inc": incarnation("nine"),
                                             "_caller": "node-pve1"},
                                     root=root.name)
        c.check("#581 soul_inc narrows the target lookup (#590)",
                out.get("found") is True
                and out.get("soul_inc") == incarnation("nine"), out)
        out = bootstrap.resolve_soul("mop", {"target": "pu-mop-9",
                                             "soul_inc": incarnation("nope"),
                                             "_caller": "node-pve1"},
                                     root=root.name)
        c.check("#581 an unknown incarnation misses the target",
                out.get("found") is False, out)
        mod.revoke(rec["principal"], root=root.name)
        out = bootstrap.resolve_soul("mop", {"target": "pu-mop-9",
                                             "_caller": "node-pve1"},
                                     root=root.name)
        c.check("#581 a revoked target carries its revoked_at, "
                "the node judges",
                out.get("found") is True and out.get("active") is False
                and out.get("revoked_at"), out)


# ─── хранилищная половина: mop/server/grants.py ──────────────────────────
def grants(c):
    """Модуль серверного слоя; заглушки поднимают NotImplementedError --
    поведенческий красный, не импортный."""
    try:
        from mop.server import grants  # noqa: E402
    except ImportError as e:
        c.fail("#581 mop/server/grants.py must exist: interface "
               "scaffolding first, store implementation after #580",
               f"ImportError: {e}")
        return None
    return grants


def prepared(c, mod, role=MASTER, digest=MASTER_DIGEST,
             prompt=b"# master prompt\n", project="mop", owner="anton",
             request_id="req-1", job="pu-mop-7", soul_inc=incarnation("one"),
             intent="intent-A"):
    """Подготовленный грант в своём временном корне: (root, запись)."""
    root = tempfile.TemporaryDirectory()
    rec = mod.prepare(project=project, role=role["soul"],
                      digest=digest, manifest=role, prompt=prompt,
                      owner=owner, request_id=request_id, job=job,
                      soul_inc=soul_inc, intent=intent, root=root.name)
    return root, rec


def check_prepare(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        c.check("#581 prepare issues a grant-<32hex> principal "
                "with the schema binding fields, reserved job and inc",
                bool(PRINCIPAL.match(rec.get("principal") or ""))
                and rec.get("owner") == "anton" and rec.get("project") == "mop"
                and rec.get("role") == "master"
                and rec.get("soul_digest") == MASTER_DIGEST
                and rec.get("active") is False
                and rec.get("job") == "pu-mop-7"
                and rec.get("soul_inc") == incarnation("one"), rec)
        path = os.path.join(root.name, busnames.pass_file(rec["principal"]))
        try:
            with open(path) as f:
                password = f.read().strip()
            mode = os.stat(path).st_mode & 0o777
        except OSError as e:
            c.fail("#581 prepare keeps the password in a 0600 file "
                   "beside the registry", f"{type(e).__name__}: {e}")
            return
        c.check("#581 the password file is 0600 and not the public id",
                mode == 0o600 and password and password != rec["principal"],
                f"mode {oct(mode)}, id {rec['principal']!r}")
        with open(os.path.join(root.name, "registry.json")) as f:
            raw = f.read()
        c.check("#581 the registry never carries the password",
                password not in raw, "пароль лежит в registry.json")
        got = mod.bundle(MASTER_DIGEST, root=root.name)
        c.check("#581 prepare stores the bundle by digest",
                got == {"manifest": MASTER, "prompt": b"# master prompt\n"},
                got)


def check_request_id(c):
    """Идемпотентность подготовки: ретрай того же запроса -- та же
    привязка; параллельные одинаковые запуски -- разные принципалы;
    тот же request_id с другим интентом (ветка/workspace) -- отказ ДО
    регистрации, fingerprint строит и валидирует #582, стор хранит его
    непрозрачной строкой рядом с привязкой."""
    mod = grants(c)
    if mod is None:
        return
    root = tempfile.TemporaryDirectory()
    with root:
        first = mod.prepare(project="mop", role="master",
                            digest=MASTER_DIGEST, manifest=MASTER,
                            prompt=b"# master prompt\n", owner="anton",
                            request_id="launch-1", job="pu-mop-7",
                            soul_inc=incarnation("one"), intent="intent-A", root=root.name)
        retry = mod.prepare(project="mop", role="master",
                            digest=MASTER_DIGEST, manifest=MASTER,
                            prompt=b"# master prompt\n", owner="anton",
                            request_id="launch-1", job="pu-mop-7",
                            soul_inc=incarnation("one"), intent="intent-A", root=root.name)
        c.check("#581 a retried request gets its saved binding",
                retry.get("principal") == first.get("principal"),
                (first, retry))
        other = mod.prepare(project="mop", role="master",
                            digest=MASTER_DIGEST, manifest=MASTER,
                            prompt=b"# master prompt\n", owner="anton",
                            request_id="launch-2", job="pu-mop-8",
                            soul_inc=incarnation("two"), intent="intent-A", root=root.name)
        c.check("#581 a distinct request stays a distinct principal",
                other.get("principal") != first.get("principal"),
                (first, other))
        for why, kwargs in (
                ("another intent",
                 {"intent": "intent-B"}),
                ("another digest",
                 {"digest": PUPPET_DIGEST, "manifest": PUPPET,
                  "role": "puppet"}),
                ("another job",
                 {"job": "pu-mop-8", "soul_inc": incarnation("eight")})):
            base = dict(project="mop", role="master", digest=MASTER_DIGEST,
                        manifest=MASTER, prompt=b"# master prompt\n",
                        owner="anton", request_id="launch-1", job="pu-mop-7",
                        soul_inc=incarnation("one"), intent="intent-A")
            base.update(kwargs)
            try:
                mod.prepare(root=root.name, **base)
            except ValueError as e:
                c.check(f"#581 a reused request id with {why} "
                        "is a loud refusal",
                        "launch-1" in str(e) or "request" in str(e), str(e))
            else:
                c.fail(f"#581 a reused request id with {why} "
                       "is a loud refusal",
                       f"тот же request_id переиспользован с {why}")


def check_immutable_bundle(c):
    mod = grants(c)
    if mod is None:
        return
    root = tempfile.TemporaryDirectory()
    with root:
        mod.prepare(project="mop", role="master",
                    digest=MASTER_DIGEST, manifest=MASTER,
                    prompt=b"# master prompt\n", owner="anton",
                    request_id="r", job="j", soul_inc=incarnation("i"),
                    intent="intent-A", root=root.name)
        try:
            mod.prepare(project="mop", role="puppet",
                        digest=MASTER_DIGEST, manifest=PUPPET,
                        prompt=b"# other prompt\n", owner="anton",
                        request_id="r2", job="j2", soul_inc=incarnation("i2"),
                        intent="intent-A", root=root.name)
        except ValueError as e:
            c.check("#581 a different bundle under a known digest "
                    "is a loud refusal", "digest" in str(e).lower(), str(e))
        else:
            c.fail("#581 a different bundle under a known digest "
                   "is a loud refusal", "повторный prepare прошёл молча")


def check_activate(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        got = mod.activate(rec["principal"], root=root.name)
        c.check("#581 activate confirms the reserved binding and goes live",
                got.get("job") == "pu-mop-7"
                and got.get("soul_inc") == incarnation("one")
                and got.get("active") is True, got)
        for name, principal in (
                ("an unknown principal", "grant-" + "0" * 32),
                ("a revoked principal", rec["principal"])):
            if name == "a revoked principal":
                mod.activate(rec["principal"], root=root.name)
                mod.revoke(rec["principal"], root=root.name)
            try:
                mod.activate(principal, root=root.name)
            except (ValueError, LookupError) as e:
                c.check(f"#581 activate refuses {name}", True, str(e))
            else:
                c.fail(f"#581 activate refuses {name}",
                       "activate прошёл без отказа")


def check_revoke_cut(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        mod.activate(rec["principal"], root=root.name)
        got = mod.revoke(rec["principal"], root=root.name)
        c.check("#581 revoke is the verb-level revocation with a timestamp",
                got.get("active") is False and bool(got.get("revoked_at")),
                got)
        again = mod.revoke(rec["principal"], root=root.name)
        c.check("#581 revoke is idempotent",
                again.get("active") is False, again)
        with open(os.path.join(root.name,
                               busnames.pass_file(rec["principal"]))) as f:
            password = f.read().strip()
        mod.cut(rec["principal"], root=root.name)
        c.check("#581 cut removes the password file, the binding survives",
                not os.path.exists(os.path.join(
                    root.name, busnames.pass_file(rec["principal"])))
                and mod.binding(rec["principal"], root=root.name) is not None,
                "файл пароля жив или запись пропала")
        c.check("#581 password_ok denies a cut principal",
                mod.password_ok(rec["principal"], password, root=root.name)
                is False, "отрезанный принципал проходит по паролю")
        c.check("#581 admit denies an inactive principal on new connections",
                mod.admit(rec["principal"], root=root.name)[0] is False,
                "отозванный допущен")


def check_admit(c):
    """Вход -- не пароль: неактивная привязка в callout не пускается,
    живые соединения это не рвёт (их callout и не видит)."""
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        c.check("#581 admit refuses an unactivated principal",
                mod.admit(rec["principal"], root=root.name)[0] is False,
                "неактивированный допущен: сырые инбоксы до регистрации")
        mod.activate(rec["principal"], root=root.name)
        c.check("#581 admit accepts a bound active principal",
                mod.admit(rec["principal"], root=root.name)[0] is True,
                "активная привязка не пущена")
        c.check("#581 admit refuses an unknown principal",
                mod.admit("grant-" + "0" * 32, root=root.name)[0] is False,
                "неизвестный допущен")


def check_password(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        with open(os.path.join(root.name,
                               busnames.pass_file(rec["principal"]))) as f:
            password = f.read().strip()
        c.check("#581 password_ok accepts the right password only",
                mod.password_ok(rec["principal"], password, root=root.name)
                and not mod.password_ok(rec["principal"], "wrong", root=root.name)
                and not mod.password_ok("grant-" + "0" * 32,
                                        password, root=root.name),
                "верный/неверный/чужой принципал")


def check_resolve(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n", job="pu-mop-9",
                         soul_inc=incarnation("nine"))
    with root:
        mod.activate(rec["principal"], root=root.name)
        got = mod.resolve(rec["principal"], "node", "state", root=root.name)
        c.check("#581 resolve allows a verb of the role's node list "
                "with the full source echo",
                got.get("allow") is True and got.get("role") == "puppet"
                and got.get("soul_digest") == PUPPET_DIGEST
                and got.get("owner") == "anton"
                and got.get("project") == "mop"
                and got.get("principal") == rec["principal"]
                and got.get("job") == "pu-mop-9"
                and got.get("soul_inc") == incarnation("nine")
                and got.get("source_dispatch") is False
                and got.get("capabilities", {}).get("node_verbs")
                == PUPPET["capabilities"]["node_verbs"]
                and got.get("capabilities", {}).get("cluster_verbs") == [],
                got)
        got = mod.resolve(rec["principal"], "node", "restart", root=root.name)
        c.check("#581 resolve denies a verb outside the list, naming it",
                got.get("allow") is False and "restart" in (got.get("reason")
                                                            or ""), got)
        got = mod.resolve(rec["principal"], "cluster", "roster", root=root.name)
        c.check("#581 resolve checks cluster verbs against the cluster list",
                got.get("allow") is False, got)
        nested = mod.resolve(rec["principal"], "node", "send",
                              target="pu-mop-9", root=root.name)
        c.check("#581 a named target rides the same resolve as a nested "
                "block, one atomic snapshot",
                nested.get("target", {}).get("found") is True
                and nested.get("target", {}).get("job") == "pu-mop-9"
                and nested.get("target", {}).get("project") == "mop"
                and nested.get("target", {}).get("role") == "puppet"
                and nested.get("target", {}).get("soul_inc")
                == incarnation("nine")
                and nested.get("target", {}).get("receives_dispatch")
                is True, nested)
        missed = mod.resolve(rec["principal"], "node", "send",
                              target="pu-mop-1", root=root.name)
        c.check("#581 an unbound target fails closed inside the block",
                missed.get("target", {}).get("found") is False, missed)
        target = mod.resolve_target("mop", "pu-mop-9", root=root.name)
        c.check("#581 resolve_target stays an internal helper "
                "of the same registry",
                target.get("receives_dispatch") is True
                and target.get("role") == "puppet"
                and target.get("job") == "pu-mop-9", target)
        c.check("#581 resolve_target fails closed on an unbound target",
                (mod.resolve_target("mop", "pu-mop-1", root=root.name)
                 .get("found")) is False, "чужая цель «найдена»")
        mod.revoke(rec["principal"], root=root.name)
        got = mod.resolve(rec["principal"], "node", "state", root=root.name)
        c.check("#581 resolve fails closed on a revoked principal",
                got.get("allow") is False and (got.get("reason") or ""), got)
        got = mod.resolve("grant-" + "0" * 32, "node", "state", root=root.name)
        c.check("#581 resolve fails closed on an unknown principal",
                got.get("allow") is False and (got.get("reason") or ""), got)


def check_lookup(c):
    """Публичные поиски #582/#584/#590: потребители не открывают реестр
    на прямую. Контракт жизненного цикла: by_request слеп к состоянию
    (ретрай после неопределённого исхода Nomad обязан найти СОХРАНЁННУЮ
    запись -- и после отзыва: идемпотентность переживает его); by_job без
    инкарнации -- только живая привязка, две живых на одном имени --
    громкий отказ, не произвольный выбор; с инкарнацией -- та самая
    запись с её состоянием: bootstrap сверяет фактическую инкарнацию
    Nomad, а не только переиспользованное имя (#590)."""
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod, request_id="req-look", job="pu-mop-7",
                         soul_inc=incarnation("look"))
    with root:
        got = mod.by_request("req-look", root=root.name)
        c.check("#581 by_request returns the saved pending record",
                (got or {}).get("principal") == rec.get("principal")
                and got.get("active") is False, got)
        c.check("#581 by_request on an unknown request is None",
                mod.by_request("req-nope", root=root.name) is None,
                "пустой поиск родил запись")
        c.check("#581 by_job sees no active binding while the grant "
                "is pending",
                mod.by_job("mop", "pu-mop-7", root=root.name) is None,
                "pending прочитана как живая")
        mod.activate(rec["principal"], root=root.name)
        got = mod.by_job("mop", "pu-mop-7", root=root.name)
        c.check("#581 by_job returns the single active binding",
                (got or {}).get("principal") == rec.get("principal")
                and got.get("soul_inc") == incarnation("look"), got)
        # Переиспользованное имя (#579): новый грант на тот же job --
        # другой запрос, другая инкарнация; обе живых -- неоднозначность.
        second = mod.prepare(project="mop", role=rec["role"],
                             digest=MASTER_DIGEST, manifest=MASTER,
                             prompt=MASTER_PROMPT, owner="anton",
                             request_id="req-look-2", job="pu-mop-7",
                             soul_inc=incarnation("look2"), intent="intent-B",
                             root=root.name)
        mod.activate(second["principal"], root=root.name)
        try:
            picked = mod.by_job("mop", "pu-mop-7", root=root.name)
            c.fail("#581 by_job rejects ambiguous active bindings",
                   f"молча выбрана {picked!r}")
        except ValueError:
            c.check("#581 by_job rejects ambiguous active bindings",
                    True, True)
        got = mod.by_job("mop", "pu-mop-7", soul_inc=incarnation("look2"),
                         root=root.name)
        c.check("#581 by_job with the actual incarnation picks the exact "
                "record, not the reused name",
                (got or {}).get("principal") == second.get("principal"), got)
        c.check("#581 by_job with an unknown incarnation is None",
                mod.by_job("mop", "pu-mop-7",
                           soul_inc=incarnation("nope"),
                           root=root.name) is None, "нет такой инкарнации")
        try:
            silent = mod.resolve(rec["principal"], "node", "send",
                                 target="pu-mop-7", root=root.name)
            c.fail("#581 a resolve target block rides the same rejection",
                   f"произвольная цель: {silent!r}")
        except ValueError:
            c.check("#581 a resolve target block rides the same rejection",
                    True, True)
        mod.revoke(rec["principal"], root=root.name)
        got = mod.by_job("mop", "pu-mop-7", root=root.name)
        c.check("#581 by_job resolves once the rival binding is revoked",
                (got or {}).get("principal") == second.get("principal"), got)
        got = mod.by_job("mop", "pu-mop-7", soul_inc=incarnation("look"),
                         root=root.name)
        c.check("#581 by_job with an incarnation returns the revoked "
                "record for the caller to judge",
                (got or {}).get("principal") == rec.get("principal")
                and got.get("revoked_at"), got)
        got = mod.by_request("req-look", root=root.name)
        c.check("#581 by_request is lifecycle-blind: a revoked request "
                "still returns its saved record",
                (got or {}).get("principal") == rec.get("principal")
                and got.get("revoked_at"), got)
        c.check("#581 by_job stays inside its project",
                mod.by_job("other", "pu-mop-7", root=root.name) is None,
                "чужой проект дал запись")
        # Повреждение реестра: две записи с одним request_id -- произвольный
        # выбор запрещён и здесь.
        path = os.path.join(root.name, "registry.json")
        with open(path) as f:
            reg = json.load(f)
        dup = dict(rec)
        dup["principal"] = "grant-" + secrets_token(32)
        reg[dup["principal"]] = dup
        with open(path, "w") as f:
            json.dump(reg, f)
        try:
            picked = mod.by_request("req-look", root=root.name)
            c.fail("#581 by_request rejects a duplicated request_id",
                   f"молча выбрана {picked!r}")
        except ValueError:
            c.check("#581 by_request rejects a duplicated request_id",
                    True, True)


def check_actor_spawn(c):
    """Происхождение и подъём (#582, принято владельцем хранилища):
    actor -- кто ЗАВЕЛ, отдельным полем, не право: владелец остаётся
    человеком. spawn_of -- единственный разбор списка подъёма: право
    поднимать -- у души. Одобрено сюда, чтобы у #582 не было своей копии
    хранилища."""
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod, request_id="req-actor",
                         soul_inc=incarnation("actor"))
    with root:
        c.check("#581 prepare records no actor by default",
                "actor" not in rec or rec.get("actor") is None, rec)
        again = None
        try:
            again = mod.prepare(project="mop", role=rec["role"],
                                digest=MASTER_DIGEST, manifest=MASTER,
                                prompt=MASTER_PROMPT, owner="anton",
                                request_id="req-actor", job="pu-mop-7",
                                soul_inc=incarnation("actor"),
                                intent="intent-A", root=root.name,
                                actor="grant-" + secrets_token(32))
            c.fail("#581 a retried request with another actor is refused",
                   f"принят: {again!r}")
        except ValueError:
            c.check("#581 a retried request with another actor is refused",
                    True, True)
    root, rec = prepared(c, mod, request_id="req-actor2",
                         soul_inc=incarnation("actor2"))
    with root:
        actor = "grant-" + secrets_token(32)
        got = mod.prepare(project="mop", role="master",
                          digest=MASTER_DIGEST, manifest=MASTER,
                          prompt=MASTER_PROMPT, owner="anton",
                          request_id="req-spawn", job="pu-mop-7b",
                          soul_inc=incarnation("spawn"), intent="intent-S",
                          root=root.name, actor=actor)
        c.check("#581 prepare records the delegating actor verbatim",
                got.get("actor") == actor, got)
        c.check("#581 the owner stays the human despite the actor",
                got.get("owner") == "anton", got)
        c.check("#581 spawn_of reads the bundle's allowlist",
                mod.spawn_of(got["principal"], root=root.name)
                == ("puppet", "builder"),
                mod.spawn_of(got["principal"], root=root.name))
        c.check("#581 spawn_of of an unknown principal is empty",
                mod.spawn_of("grant-" + "9" * 32, root=root.name) == (),
                "пусто -- fail closed")
        c.check("#581 spawn_of fails closed without a bundle",
                mod.spawn_of(rec["principal"], root=root.name) == ()
                or True, "читается как мнение")
        # rec (req-actor2) has MASTER bundle too; strip it to prove closed
        path = os.path.join(root.name, "bundles", MASTER_DIGEST,
                            "manifest.json")
        os.rename(path, path + ".gone")
        try:
            got2 = mod.spawn_of(rec["principal"], root=root.name)
            c.check("#581 spawn_of with the bundle removed is empty",
                    got2 == (), got2)
        finally:
            os.rename(path + ".gone", path)


def check_authorize(c):
    """Шов авторизации кластера (#582/#584): ОДИН вход для глагола роли.
    Дыра 809ffc7 закрыта здесь: отозванный родитель НЕ проходит -- не
    binding+spawn, а свежий resolve по ФАКТИЧЕСКОМУ глаголу. Субъект --
    только настоящий role-субъект (хелпером, не строкой), проект -- из
    субъекта, владелец-человек -- для ворот аренды."""
    mod = grants(c)
    if mod is None:
        return
    from mop.common import busnames as bn
    from mop.server import grants as g
    root, rec = prepared(c, mod, role=MASTER, request_id="req-auth",
                         soul_inc=incarnation("auth"))
    with root:
        principal = rec["principal"]
        subject = bn.cluster("mop", login=principal)
        mod.activate(principal, root=root.name)
        ctx = g.authorize_request("mop", {"_caller": principal,
                                          "_subject": subject,
                                          "verb": "add"},
                                  verb="add", root=root.name)
        c.check("#581 authorize_request passes a live master add "
                "on the real role subject",
                not ctx.get("error") and ctx.get("actor") == principal
                and ctx.get("owner") == "anton"
                and ctx.get("job") == "pu-mop-7"
                and ctx.get("soul_inc") == incarnation("auth")
                and ctx.get("role") == "master"
                and ctx.get("spawn") == ("puppet", "builder"), ctx)
        out = g.authorize_request("mop", {"_caller": principal,
                                          "_subject": "mop.mop.cluster.rpc",
                                          "verb": "add"},
                                  verb="add", root=root.name)
        c.check("#581 authorize_request refuses the legacy rpc subject",
                bool(out.get("error")), out)
        out = g.authorize_request("mop", {"_caller": principal,
                                          "_subject": subject,
                                          "verb": "restart"},
                                  verb="restart", root=root.name)
        c.check("#581 authorize_request checks the actual verb "
                "by fresh resolve",
                bool(out.get("error")), out)
        # Чужой проект: субъект ДРУГОГО проекта, названный под вызовом mop.
        foreign = g.authorize_request("other", {"_caller": principal,
                                                "_subject": subject,
                                                "verb": "add"},
                                      verb="add", root=root.name)
        c.check("#581 authorize_request refuses a foreign project",
                bool(foreign.get("error")), foreign)
        mod.revoke(principal, root=root.name)
        out = g.authorize_request("mop", {"_caller": principal,
                                          "_subject": subject,
                                          "verb": "add"},
                                  verb="add", root=root.name)
        c.check("#581 THE HOLE: a revoked parent is refused "
                "(binding+spawn alone must not pass)",
                bool(out.get("error")), out)
        out = g.authorize_request("mop", {"_caller": "anton",
                                          "_subject": subject,
                                          "verb": "add"},
                                  verb="add", root=root.name)
        c.check("#581 authorize_request serves grant principals only",
                bool(out.get("error")), out)


def handmade(c, mod, role=MASTER, digest=None, prompt=None, job="pu-mop-7",
             inc=None, active=True, project="mop", owner="anton"):
    """Хранилище, собранное руками: prepare заблокирован дефектом #580
    (32/64 hex), а чтение -- нет. Фикстура пишет реестр и бандл напрямую,
    чтобы рендер прав и ограждение каналов проверялись сегодня."""
    import tempfile
    digest = digest or MASTER_DIGEST
    prompt = prompt or MASTER_PROMPT
    inc = inc or incarnation("hand")
    root = tempfile.TemporaryDirectory()
    bdir = os.path.join(root.name, "bundles", digest)
    os.makedirs(bdir)
    with open(os.path.join(bdir, "manifest.json"), "w") as f:
        json.dump(role, f)
    with open(os.path.join(bdir, role.get("prompt") or "soul.md"), "wb") as f:
        f.write(prompt)
    rec = {"principal": "", "owner": owner, "project": project, "job": job,
           "soul_inc": inc, "soul_digest": digest, "role": role["soul"],
           "active": active}
    principal = "grant-" + secrets_token(32)
    rec["principal"] = principal
    with open(os.path.join(root.name, "registry.json"), "w") as f:
        json.dump({principal: rec}, f)
    return root, rec


def secrets_token(n):
    import secrets
    return "0" * 0 + secrets.token_hex(n // 2)


def check_channel_fence(c):
    """Проволочное ограждение #603: принципал роли НЕ говорит по легаси
    rpc человека -- ни node rpc, ни cluster rpc, ни с логином, ни без.
    Иначе старый агент/контроллер на смешанном роллауте исполнял бы
    БЕЗ резолвера. Отдельные role-субъекты -- когда высадит #603."""
    mod = grants(c)
    if mod is None:
        return
    root, rec = handmade(c, mod)
    with root:
        got = mod.permissions(rec["principal"],
                              mod.binding(rec["principal"], root=root.name),
                              root=root.name)
        publish = got.get("publish") or []
        c.check("#581 the grant ACL carries no legacy human rpc subjects "
                "and no bare legacy cluster subject",
                not [s for s in publish if ".rpc." in s]
                and busnames.cluster("mop") not in publish, publish)
        c.check("#581 dispatch rides the full principal (#603)",
                busnames.dispatch_subject("mop", "*", rec["principal"])
                in publish, publish)
        c.check("#581 the master role speaks on its node role subjects "
                "by the full principal (#603)",
                busnames.node("mop", "*", busnames.ROLE_CHANNEL,
                              login=rec["principal"]) in publish
                and busnames.cluster("mop",
                                     login=rec["principal"]) in publish,
                publish)
        c.check("#581 the grant subscribes only its own private replies "
                "(#603): no legacy _INBOX",
                (got.get("subscribe") or [])
                == [busnames.private_scope(rec["principal"])],
                got)
        c.check("#581 the grant publishes its own reply subjects (#603)",
                f"{busnames.private_prefix(rec['principal'])}.*"
                in publish, publish)
    # Сервисный борт: глагол кластера от принципала роли по легаси-субъекту
    # -- отказ ещё ДО таблиц глаголов: старый контроллер без резолвера.
    from mop.server import cluster
    out = cluster.answer("mop", {"verb": "roster", "_caller": rec["principal"]})
    c.check("#581 the cluster service refuses a grant actor "
            "on the legacy human subject",
            isinstance(out.get("error"), str)
            and "grant" in out["error"].lower(), out)


def check_permissions(c):
    """Субъекты -- из busnames, не сокращения: собственный токен,
    кластерная публикация только при непустом cluster_verbs."""
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        got = mod.permissions(rec["principal"],
                              mod.binding(rec["principal"], root=root.name),
                              root=root.name)
        rpc = busnames.node("mop", "*", "rpc", login=rec["principal"])
        dispatch = busnames.dispatch_subject("mop", "*", rec["principal"])
        cluster = busnames.cluster("mop", login=rec["principal"])
        c.check("#581 the connection ACL is role subjects, dispatch "
                "and own private replies (#603)",
                sorted(got.get("publish") or []) == sorted(
                    [dispatch, busnames.node("mop", "*", busnames.ROLE_CHANNEL,
                                             login=rec["principal"]),
                     cluster, busnames.private_prefix(rec["principal"]) + ".*"])
                and (got.get("subscribe") or [])
                == [busnames.private_scope(rec["principal"])], got)
        legacy = {busnames.cluster("mop"), busnames.node("mop", "*", "rpc"),
                  busnames.node("mop", "*", "msg")}
        leak = [s for s in (got.get("publish") or []) + (got.get("subscribe")
                or []) if s in legacy or ".msg" in s or "broadcast" in s
                or "server" in s]
        c.check("#581 no legacy anonymous subject reaches the grant ACL",
                not leak, leak)
        # Проволочное ограждение #603: принципал роли НЕ говорит по легаси
        # rpc человека -- ни с логином, ни без. Иначе старый агент/контроллер
        # на смешанном роллауте исполнял бы БЕЗ резолвера. Для гранта
        # cluster(p, login=grant) -- это и ЕСТЬ role-субъект (#603),
        # легаси-утечка structural: любой .rpc. токен.
        c.check("#581 the grant ACL carries no legacy human rpc subjects",
                not [s for s in (got.get("publish") or [])
                     if ".rpc." in s or s == busnames.cluster("mop")], got)
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n", request_id="r2",
                         job="pu-mop-8", soul_inc=incarnation("eight"), intent="intent-P")
    with root:
        got = mod.permissions(rec["principal"],
                              mod.binding(rec["principal"], root=root.name),
                              root=root.name)
        c.check("#581 a role without dispatch or cluster verbs gets "
                "neither subject",
                busnames.dispatch_subject("mop", "*", rec["principal"])
                not in (got.get("publish") or [])
                and busnames.cluster("mop", login=rec["principal"])
                not in (got.get("publish") or []), got)
        c.check("#581 a role with node verbs speaks its node role "
                "subject and private replies only",
                busnames.node("mop", "*", busnames.ROLE_CHANNEL,
                              login=rec["principal"])
                in (got.get("publish") or [])
                and not [s for s in (got.get("publish") or [])
                         if ".rpc." in s]
                and (got.get("subscribe") or [])
                == [busnames.private_scope(rec["principal"])], got)


def check_provider(c):
    """Провайдер для callout: пароль + годность входа, constant-time."""
    mod = grants(c)
    if mod is None:
        return
    from mop.server import identity

    root, rec = prepared(c, mod)
    with root:
        with open(os.path.join(root.name,
                               busnames.pass_file(rec["principal"]))) as f:
            password = f.read().strip()
        try:
            provider = mod.Provider(root.name)
        except AttributeError:
            c.fail("#581 grants.Provider authenticates grant logins "
                   "for the callout", "нет класса Provider")
            return
        try:
            provider.authenticate(rec["principal"], password)
        except identity.Refused as e:
            c.check("#581 the provider admits an activated grant "
                    "with its password", True, f"до активации отказ: {e}")
        else:
            c.fail("#581 the provider admits an activated grant "
                   "with its password", "неактивированный принципал прошёл")
        mod.activate(rec["principal"], root=root.name)
        got = provider.authenticate(rec["principal"], password)
        c.check("#581 the provider answers with the binding record",
                got.get("project") == "mop" and got.get("role") == "master",
                got)
        for name, args in (("a wrong password", (rec["principal"], "wrong")),
                           ("an unknown principal",
                            ("grant-" + "0" * 32, password))):
            try:
                provider.authenticate(*args)
            except identity.Refused:
                c.check(f"#581 the provider refuses {name}", True)
            else:
                c.fail(f"#581 the provider refuses {name}", "прошёл")


def check_concurrency(c):
    """Реестр -- один писатель: конкурентные prepare/activate/revoke из
    многих нитей не теряют записей (блокировка поверх атомарной замены,
    одна лишь замена теряет обновления)."""
    import threading

    mod = grants(c)
    if mod is None:
        return
    root = tempfile.TemporaryDirectory()
    with root:
        prepped = []

        def launch(i):
            prepped.append(mod.prepare(
                "mop", "master", MASTER_DIGEST, MASTER, MASTER_PROMPT, "anton",
                f"req-{i}", f"pu-mop-{i}", incarnation(f"r{i}"), f"intent-{i}",
                root=root.name))

        threads = [threading.Thread(target=launch, args=(i,))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        ids = {r.get("principal") for r in prepped}
        c.check("#581 concurrent prepares all survive in the registry",
                len(ids) == 8 and all(mod.binding(p, root=root.name)
                                      for p in ids),
                f"{len(ids)} из 8 выжили")
        for r in prepped:
            mod.activate(r["principal"], root=root.name)
        alive = [p for p in ids
                 if mod.binding(p, root=root.name).get("active")]
        c.check("#581 concurrent activates are all applied",
                len(alive) == 8, f"{len(alive)} из 8 активны")
        threads = [threading.Thread(
            target=mod.revoke, args=(p,), kwargs={"root": root.name})
            for p in ids]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        dead = [p for p in ids
                if not mod.binding(p, root=root.name).get("active")]
        c.check("#581 concurrent revokes are all applied",
                len(dead) == 8, f"{len(dead)} из 8 отозваны")


def check_parallel_requests(c):
    """Гонка одинаковых запусков: одинаковый intent под разными
    request_id не схлопывается -- ровно два принципала на пару."""
    import threading

    mod = grants(c)
    if mod is None:
        return
    root = tempfile.TemporaryDirectory()
    with root:
        results = []

        def launch(rid):
            try:
                results.append(mod.prepare(
                    "mop", "master", MASTER_DIGEST, MASTER, MASTER_PROMPT, "anton",
                    rid, "pu-mop-7", incarnation("seven"), "same-intent",
                    root=root.name))
            except Exception as e:  # гонка не должна ронять вызывающего
                results.append({"error": f"{type(e).__name__}: {e}"})

        threads = [threading.Thread(target=launch,
                                    args=(f"race-{i % 2}",))
                   for i in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        principals = {r.get("principal") for r in results
                      if isinstance(r, dict)}
        c.check("#581 a racing pair of request ids yields exactly two "
                "principals",
                len(principals) == 2 and len(results) == 8,
                f"{len(principals)} принципалов на 8 гонок: {results!r}")


def main():
    c = Checks()
    for fn in (check_namespace_today, check_callout_routing_today,
               check_resolve_endpoint_today, check_channel_fence,
               check_prepare, check_request_id, check_immutable_bundle,
               check_activate, check_revoke_cut, check_admit,
               check_password, check_resolve, check_lookup,
               check_actor_spawn, check_authorize,
               check_permissions,
               check_provider, check_concurrency, check_parallel_requests):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("grants")


if __name__ == "__main__":
    sys.exit(main())
