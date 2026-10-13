#!/usr/bin/env python3
"""Реестр привязок душ #581 без пула: python3 tests/grants.py

Принятая схема #579 (docs/SOUL.md, ревизия 18db5ec): одна инкарнация --
один принципал `grant-<32hex>`, реестр привязок на сервере (принципал ->
{owner, project, job, soul_inc, soul_digest, role, active}), бандл роли
закреплён дайджестом и неизменяем, отзыв двухступенчатый (глагольный
active:false, транспортный -- снос файла пароля), резолверfail-closed
проверяет глагол по спискам роли. Здесь -- красные проверки серверного
слоя #581: хранилище, жизненный цикл, резолвер, права соединения.

HYPOTHESIS: без реестра делегированная личность существует только именем:
узел не знает, чей глагол, callout не знает, чего princip'алу можно, а
смена роли в живом джобе прошла бы молча. SOLUTION: mop/server/grants.py --
реестр + бандлы + резолвер + рендер прав одним серверным модулем;
кодировки принципала и дайджеста -- общий модуль #580, здесь только
серверная часть. STATUS: RED -- ждёт реализации #581 (общие кодировки
#580 не блокируют форму записей: формат принципала и дайджеста фиксирован
схемой #579).
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


def grants(c):
    """Модуль серверного слоя или громкое красное имя."""
    try:
        from mop.server import grants  # noqa: E402
    except ImportError as e:
        c.fail("#581 mop/server/grants.py must exist: the registry, "
               "bundles, resolver and connection rights of delegated "
               "principals (#579 rev 3)", f"ImportError: {e}")
        return None
    return grants


def prepared(c, mod, role=MASTER, digest=MASTER_DIGEST,
             prompt=b"# master prompt\n", project="mop", owner="anton"):
    """Подготовленный грант в своём временном корне: (root, запись)."""
    root = tempfile.TemporaryDirectory()
    rec = mod.prepare(root.name, project=project, role=role["soul"],
                      digest=digest, manifest=role, prompt=prompt,
                      owner=owner)
    return root, rec


def check_prepare(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        c.check("#581 prepare issues a grant-<32hex> principal "
                "with the schema binding fields",
                bool(PRINCIPAL.match(rec.get("principal") or ""))
                and rec.get("owner") == "anton" and rec.get("project") == "mop"
                and rec.get("role") == "master"
                and rec.get("soul_digest") == MASTER_DIGEST
                and rec.get("active") is False
                and rec.get("job") is None and rec.get("soul_inc") is None,
                rec)
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


def check_immutable_bundle(c):
    mod = grants(c)
    if mod is None:
        return
    root = tempfile.TemporaryDirectory()
    with root:
        mod.prepare(root.name, project="mop", role="master",
                    digest=MASTER_DIGEST, manifest=MASTER,
                    prompt=b"# master prompt\n", owner="anton")
        try:
            mod.prepare(root.name, project="mop", role="puppet",
                        digest=MASTER_DIGEST, manifest=PUPPET,
                        prompt=b"# other prompt\n", owner="anton")
        except ValueError as e:
            c.check("#581 a different bundle under a known digest "
                    "is a loud refusal", MASTER_DIGEST[:8] in str(e)
                    or "digest" in str(e), str(e))
        else:
            c.fail("#581 a different bundle under a known digest "
                   "is a loud refusal", "повторный prepare прошёл молча")


def check_activate(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod)
    with root:
        got = mod.activate(root.name, rec["principal"], "pu-mop-7",
                           "inc-1111")
        c.check("#581 activate binds job and soul_inc and goes live",
                got.get("job") == "pu-mop-7" and got.get("soul_inc") == "inc-1111"
                and got.get("active") is True, got)
        for name, args, why in (
                ("an unknown principal", ("grant-" + "0" * 32, "j", "i"), ""),
                ("rebinding to another job", (rec["principal"], "pu-mop-8",
                                              "inc-2222"), ""),
                ("a revoked principal", (rec["principal"], "j2", "i2"), "revoke")):
            if name == "a revoked principal":
                mod.revoke(root.name, rec["principal"])
            try:
                mod.activate(root.name, *args)
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
        mod.activate(root.name, rec["principal"], "pu-mop-7", "inc-1")
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
                and not mod.password_ok(root.name, "grant-" + "0" * 32, password),
                "верный/неверный/чужой принципал")


def check_resolve(c):
    mod = grants(c)
    if mod is None:
        return
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n")
    with root:
        mod.activate(root.name, rec["principal"], "pu-mop-9", "inc-9")
        got = mod.resolve(root.name, rec["principal"], "node", "state")
        c.check("#581 resolve allows a verb of the role's node list "
                "with the binding facts",
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
        mod.revoke(root.name, rec["principal"])
        got = mod.resolve(root.name, rec["principal"], "node", "state")
        c.check("#581 resolve fails closed on a revoked principal",
                got.get("allow") is False and (got.get("reason") or ""),
                got)
        got = mod.resolve(root.name, "grant-" + "0" * 32, "node", "state")
        c.check("#581 resolve fails closed on an unknown principal",
                got.get("allow") is False and (got.get("reason") or ""), got)


def check_permissions(c):
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
        c.check("#581 the connection ACL is own-token subjects "
                "and replies only",
                sorted(got.get("publish") or []) == sorted(
                    [rpc, dispatch, busnames.INBOX])
                and (got.get("subscribe") or []) == [busnames.INBOX], got)
        leak = [s for s in (got.get("publish") or []) + (got.get("subscribe")
                or []) if ".msg" in s or "broadcast" in s
                or "server" in s or "cluster" in s]
        c.check("#581 no legacy anonymous subject reaches the grant ACL",
                not leak, leak)
    root, rec = prepared(c, mod, role=PUPPET, digest=PUPPET_DIGEST,
                         prompt=b"# puppet prompt\n")
    with root:
        got = mod.permissions(rec["principal"],
                              mod.binding(root.name, rec["principal"]))
        c.check("#581 a role without dispatch gets no dispatch subject",
                busnames.node("mop", "*", "dispatch",
                              login=rec["principal"])
                not in (got.get("publish") or []), got)


def check_provider(c):
    """Провайдер для callout: пароль принципала -- constant-time."""
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
            got = provider.authenticate(rec["principal"], password)
        except Exception as e:
            c.fail("#581 the provider passes a valid grant password",
                   f"{type(e).__name__}: {e}")
            return
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
    for fn in (check_prepare, check_immutable_bundle, check_activate,
               check_revoke_cut, check_password, check_resolve,
               check_permissions, check_provider):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("grants")


if __name__ == "__main__":
    sys.exit(main())
