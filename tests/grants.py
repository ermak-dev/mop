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
определением (#580 landed). STATUS: RED -- 3 поведенческих зелёные,
хранилищная половина заблокирована дефектом #580: soul.binding требует
soul_digest в 32 hex, а формула контракта (soul.digest, docs/SOUL.md)
даёт sha256 в 64 hex -- запись привязки с настоящим дайджестом бандла
невозможна, пока #580 не согласует длину. Отказ воспроизводится на
каждой проверке: «binding soul_digest=...: 32 lower-case hex
characters». Проверки НЕ ослаблены под дефект.
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
                                    "service": "node", "action": "state"})
    c.check("#581 the bootstrap service answers a resolve request "
            "with a verdict, not a missing verb",
            not (anon.get("error") or "").startswith("no such verb"),
            f"сегодня: {anon.get('error')!r}")
    c.check("#581 an anonymous legacy caller gets a denial "
            "that echoes no principal and no role",
            anon.get("allow") is False and "grant-" not in json.dumps(anon)
            and not (anon.get("role") or anon.get("owner")), anon)
    out = bootstrap.answer("mop", {"verb": "resolve", "principal": "grant-x",
                                   "service": "node", "action": "state",
                                   "_caller": "node-pve1"})
    c.check("#581 a node caller gets the fail-closed verdict "
            "for an unknown principal",
            out.get("allow") is False and "unknown principal" in
            (out.get("reason") or ""), out)
    out = bootstrap.answer("mop", {"verb": "resolve_target",
                                   "target": "pu-mop-7"})
    err = out.get("error") or ""
    c.check("#581 the bootstrap service answers resolve_target "
            "from the registry, anonymously fail-closed",
            not err.startswith("no such verb") and out.get("found") is False,
            f"сегодня: {err!r}")


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
                        request_id="r2", job="j2", soul_inc="i2",
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
                "with the source binding facts",
                got.get("allow") is True and got.get("role") == "puppet"
                and got.get("soul_digest") == PUPPET_DIGEST
                and got.get("owner") == "anton"
                and got.get("project") == "mop"
                and got.get("dispatch") is False
                and got.get("receives_dispatch") is True, got)
        got = mod.resolve(rec["principal"], "node", "restart", root=root.name)
        c.check("#581 resolve denies a verb outside the list, naming it",
                got.get("allow") is False and "restart" in (got.get("reason")
                                                            or ""), got)
        got = mod.resolve(rec["principal"], "cluster", "roster", root=root.name)
        c.check("#581 resolve checks cluster verbs against the cluster list",
                got.get("allow") is False, got)
        target = mod.resolve_target("mop", "pu-mop-9", root=root.name)
        c.check("#581 resolve_target answers the target's eligibility "
                "from the registry",
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
        dispatch = busnames.dispatch_subject(
            "mop", "*", soul.grant_id(rec["principal"]))
        cluster = busnames.cluster("mop", login=rec["principal"])
        c.check("#581 the connection ACL is own-token busnames subjects "
                "and replies only",
                sorted(got.get("publish") or []) == sorted(
                    [rpc, dispatch, cluster, busnames.INBOX])
                and (got.get("subscribe") or []) == [busnames.INBOX], got)
        legacy = {busnames.cluster("mop"), busnames.node("mop", "*", "rpc"),
                  busnames.node("mop", "*", "msg")}
        leak = [s for s in (got.get("publish") or []) + (got.get("subscribe")
                or []) if s in legacy or ".msg" in s or "broadcast" in s
                or "server" in s]
        c.check("#581 no legacy anonymous subject reaches the grant ACL",
                not leak, leak)
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n", request_id="r2",
                         job="pu-mop-8", soul_inc=incarnation("eight"), intent="intent-P")
    with root:
        got = mod.permissions(rec["principal"],
                              mod.binding(rec["principal"], root=root.name),
                              root=root.name)
        c.check("#581 a role without dispatch or cluster verbs gets "
                "neither subject",
                busnames.dispatch_subject(
                    "mop", "*", soul.grant_id(rec["principal"]))
                not in (got.get("publish") or [])
                and busnames.cluster("mop", login=rec["principal"])
                not in (got.get("publish") or []), got)


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
               check_resolve_endpoint_today,
               check_prepare, check_request_id, check_immutable_bundle,
               check_activate, check_revoke_cut, check_admit,
               check_password, check_resolve, check_permissions,
               check_provider, check_concurrency, check_parallel_requests):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("grants")


if __name__ == "__main__":
    sys.exit(main())
