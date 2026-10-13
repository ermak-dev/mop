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
одним серверным модулем; пространство имён -- в busnames одним
определением. STATUS: RED -- поведение подтверждено на текущем коде,
реализация #581 ждёт общего модуля кодировок #580.
"""
import os
import re
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- изоляция раньше mop
from _lib import Checks  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import busnames  # noqa: E402

PRINCIPAL = re.compile(r"^grant-[0-9a-f]{32}$")

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
MASTER_DIGEST = "a" * 64
PUPPET_DIGEST = "b" * 64


# ─── поведенческая половина: сегодняшний код, принятая схема ─────────────
def check_namespace_today(c):
    """Пространство grant- зарезервировано схемой -- классификатор обязан
    его знать сегодня, а не в день первого входа."""
    name = "grant-" + "0" * 32
    c.check("#581 busnames.is_machine recognizes the accepted grant-* "
            "namespace (a grant is not a human login)",
            busnames.is_machine(name) is True,
            f"is_machine({name!r}) is False: callout поведёт принципала "
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
        return
    except TypeError as e:
        c.fail("#581 callout.rights must accept today's providers",
               f"{type(e).__name__}: {e}")
        return
    c.fail("#581 callout refuses a grant login on the human path",
           f"права человека выданы принципалу: {perms!r}")


def check_resolve_endpoint_today(c):
    """Глагол resolve на bootstrap-сервисе: узел зовёт его узловым кредом,
    сегодня ответ «no such verb» -- эндпоинта нет."""
    from mop.server import bootstrap

    out = bootstrap.answer("mop", {"verb": "resolve", "principal": "grant-x",
                                   "service": "node", "verb_name": "state"})
    err = out.get("error") or ""
    c.check("#581 the bootstrap service answers a resolve request "
            "with a verdict, not a missing verb",
            not err.startswith("no such verb"), f"сегодня: {err!r}")


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
             request_id="req-1", job="pu-mop-7", soul_inc="inc-1",
             intent="intent-A"):
    """Подготовленный грант в своём временном корне: (root, запись)."""
    root = tempfile.TemporaryDirectory()
    rec = mod.prepare(root.name, project=project, role=role["soul"],
                      digest=digest, manifest=role, prompt=prompt,
                      owner=owner, request_id=request_id, job=job,
                      soul_inc=soul_inc, intent=intent)
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
                and rec.get("soul_inc") == "inc-1", rec)
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
        got = mod.bundle(root.name, MASTER_DIGEST)
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
        first = mod.prepare(root.name, project="mop", role="master",
                            digest=MASTER_DIGEST, manifest=MASTER,
                            prompt=b"# master prompt\n", owner="anton",
                            request_id="launch-1", job="pu-mop-7",
                            soul_inc="inc-1", intent="intent-A")
        retry = mod.prepare(root.name, project="mop", role="master",
                            digest=MASTER_DIGEST, manifest=MASTER,
                            prompt=b"# master prompt\n", owner="anton",
                            request_id="launch-1", job="pu-mop-7",
                            soul_inc="inc-1", intent="intent-A")
        c.check("#581 a retried request gets its saved binding",
                retry.get("principal") == first.get("principal"),
                (first, retry))
        other = mod.prepare(root.name, project="mop", role="master",
                            digest=MASTER_DIGEST, manifest=MASTER,
                            prompt=b"# master prompt\n", owner="anton",
                            request_id="launch-2", job="pu-mop-8",
                            soul_inc="inc-2", intent="intent-A")
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
                 {"job": "pu-mop-8", "soul_inc": "inc-8"})):
            try:
                mod.prepare(root.name, project="mop", role="master",
                            digest=MASTER_DIGEST, manifest=MASTER,
                            prompt=b"# master prompt\n", owner="anton",
                            request_id="launch-1", job="pu-mop-7",
                            soul_inc="inc-1", intent="intent-A", **kwargs)
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
        mod.prepare(root.name, project="mop", role="master",
                    digest=MASTER_DIGEST, manifest=MASTER,
                    prompt=b"# master prompt\n", owner="anton",
                    request_id="r", job="j", soul_inc="i", intent="intent-A")
        try:
            mod.prepare(root.name, project="mop", role="puppet",
                        digest=MASTER_DIGEST, manifest=PUPPET,
                        prompt=b"# other prompt\n", owner="anton",
                        request_id="r2", job="j2", soul_inc="i2",
                        intent="intent-A")
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
        got = mod.activate(root.name, rec["principal"])
        c.check("#581 activate confirms the reserved binding and goes live",
                got.get("job") == "pu-mop-7" and got.get("soul_inc") == "inc-1"
                and got.get("active") is True, got)
        for name, principal in (
                ("an unknown principal", "grant-" + "0" * 32),
                ("a revoked principal", rec["principal"])):
            if name == "a revoked principal":
                mod.activate(root.name, rec["principal"])
                mod.revoke(root.name, rec["principal"])
            try:
                mod.activate(root.name, principal)
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
        mod.activate(root.name, rec["principal"])
        got = mod.revoke(root.name, rec["principal"])
        c.check("#581 revoke is the verb-level revocation with a timestamp",
                got.get("active") is False and bool(got.get("revoked_at")),
                got)
        again = mod.revoke(root.name, rec["principal"])
        c.check("#581 revoke is idempotent",
                again.get("active") is False, again)
        with open(os.path.join(root.name,
                               busnames.pass_file(rec["principal"]))) as f:
            password = f.read().strip()
        mod.cut(root.name, rec["principal"])
        c.check("#581 cut removes the password file, the binding survives",
                not os.path.exists(os.path.join(
                    root.name, busnames.pass_file(rec["principal"])))
                and mod.binding(root.name, rec["principal"]) is not None,
                "файл пароля жив или запись пропала")
        c.check("#581 password_ok denies a cut principal",
                mod.password_ok(root.name, rec["principal"], password)
                is False, "отрезанный принципал проходит по паролю")
        c.check("#581 admit denies an inactive principal on new connections",
                mod.admit(root.name, rec["principal"])
                == (False, c and "inactive"), "отозванный допущен")


def check_admit(c):
    """Вход -- не пароль: неактивная привязка в callout не пускается,
    живые соединения это не рвёт (их callout и не видит)."""
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        c.check("#581 admit refuses an unactivated principal",
                mod.admit(root.name, rec["principal"])[0] is False,
                "неактивированный допущен: сырые инбоксы до регистрации")
        mod.activate(root.name, rec["principal"])
        c.check("#581 admit accepts a bound active principal",
                mod.admit(root.name, rec["principal"])[0] is True,
                "активная привязка не пущена")
        c.check("#581 admit refuses an unknown principal",
                mod.admit(root.name, "grant-" + "0" * 32)[0] is False,
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
                mod.password_ok(root.name, rec["principal"], password)
                and not mod.password_ok(root.name, rec["principal"], "wrong")
                and not mod.password_ok(root.name, "grant-" + "0" * 32,
                                        password),
                "верный/неверный/чужой принципал")


def check_resolve(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n", job="pu-mop-9",
                         soul_inc="inc-9")
    with root:
        mod.activate(root.name, rec["principal"])
        got = mod.resolve(root.name, rec["principal"], "node", "state")
        c.check("#581 resolve allows a verb of the role's node list "
                "with the source binding facts",
                got.get("allow") is True and got.get("role") == "puppet"
                and got.get("soul_digest") == PUPPET_DIGEST
                and got.get("owner") == "anton"
                and got.get("project") == "mop"
                and got.get("dispatch") is False
                and got.get("receives_dispatch") is True, got)
        got = mod.resolve(root.name, rec["principal"], "node", "restart")
        c.check("#581 resolve denies a verb outside the list, naming it",
                got.get("allow") is False and "restart" in (got.get("reason")
                                                            or ""), got)
        got = mod.resolve(root.name, rec["principal"], "cluster", "roster")
        c.check("#581 resolve checks cluster verbs against the cluster list",
                got.get("allow") is False, got)
        target = mod.resolve_target(root.name, "mop", "pu-mop-9")
        c.check("#581 resolve_target answers the target's eligibility "
                "from the registry",
                target.get("receives_dispatch") is True
                and target.get("role") == "puppet"
                and target.get("job") == "pu-mop-9", target)
        c.check("#581 resolve_target fails closed on an unbound target",
                (mod.resolve_target(root.name, "mop", "pu-mop-1")
                 .get("found")) is False, "чужая цель «найдена»")
        mod.revoke(root.name, rec["principal"])
        got = mod.resolve(root.name, rec["principal"], "node", "state")
        c.check("#581 resolve fails closed on a revoked principal",
                got.get("allow") is False and (got.get("reason") or ""), got)
        got = mod.resolve(root.name, "grant-" + "0" * 32, "node", "state")
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
                              mod.binding(root.name, rec["principal"]))
        rpc = busnames.node("mop", "*", "rpc", login=rec["principal"])
        dispatch = busnames.node("mop", "*", "dispatch",
                                 login=rec["principal"])
        cluster = busnames.cluster("mop", login=rec["principal"])
        c.check("#581 the connection ACL is own-token busnames subjects "
                "and replies only",
                sorted(got.get("publish") or []) == sorted(
                    [rpc, dispatch, cluster, busnames.INBOX])
                and (got.get("subscribe") or []) == [busnames.INBOX], got)
        leak = [s for s in (got.get("publish") or []) + (got.get("subscribe")
                or []) if ".msg" in s or "broadcast" in s
                or "server" in s or s.startswith("mop.mop.cluster.")]
        c.check("#581 no legacy anonymous subject reaches the grant ACL",
                not leak, leak)
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n", request_id="r2",
                         job="pu-mop-8", soul_inc="inc-8", intent="intent-P")
    with root:
        got = mod.permissions(rec["principal"],
                              mod.binding(root.name, rec["principal"]))
        c.check("#581 a role without dispatch or cluster verbs gets "
                "neither subject",
                busnames.node("mop", "*", "dispatch",
                              login=rec["principal"])
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
        mod.activate(root.name, rec["principal"])
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


def main():
    c = Checks()
    for fn in (check_namespace_today, check_callout_routing_today,
               check_resolve_endpoint_today,
               check_prepare, check_request_id, check_immutable_bundle,
               check_activate, check_revoke_cut, check_admit,
               check_password, check_resolve, check_permissions,
               check_provider):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("grants")


if __name__ == "__main__":
    sys.exit(main())
