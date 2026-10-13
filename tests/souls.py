#!/usr/bin/env python3
"""Границы делегированных личностей #581 без пула: python3 tests/souls.py

Делегированная личность папета (#578) -- новый род принципала на шине: не
человек и не машинный пользователь. Контракт #579 (имя пространства,
уникальный ID гранта) и модель #580 (ключи, дайджесты) ещё не приняты, поэтому
здесь -- только то, что от их выбора не зависит: резервирование пространства
имён и зачистка доверенных полей на границе транспорта.

HYPOTHESIS: (1) новое пространство имён пользователей, не зарезервированное
в busnames/операторах сразу, даст коллизию логина человека с именем
делегированной личности -- права сложатся в одного пользователя, а разбор
`puppet-<проект>` примет чужое имя за проект. (2) Каркас сервиса и агент
узла снимают с тела только `_caller` (и агент -- `_project`): новое
доверенное поле шапки (`_grant`, `_actor`) проедет в обработчик как выданное
сервером, а тело пишет кто угодно.
SOLUTION: (1) когда контракт #579 назовёт префикс, он обязан приехать в
busnames одним определением и сразу в RESERVED_PREFIXES, is_machine и отказ
operators.parse -- проверка ниже включается с появлением константы;
провайдер папетов не принимает чужие имена и сегодня. (2) Граница снимает с
тела весь префикс доверенных полей, а не избранные имена: список имён
разошёлся бы с контрактом при каждом его изменении. Свои доверенные поля
(`_caller` из субъекта, `_project` у агента) граница ставит сама, поверх
тела.
STATUS: FIXED — see #581 (зачистка границы); резервирование пространства --
красная половина, ждёт контракта #579.
"""
import asyncio
import os
import sys

import hermetic  # noqa: F401,E402 -- изоляция раньше mop
from _lib import Checks, Msg  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import busnames  # noqa: E402


def check_namespace(c):
    """Пространство имён делегированных личностей зарезервировано.

    До контракта #579 константы нет -- проверка стоит на страже: в день,
    когда префикс появится в busnames, он обязан быть уже в RESERVED_PREFIXES,
    в is_machine и под отказом operators.parse. Коллизию ловят не после
    первого входа, а на первой же ветке с константой."""
    from mop.server import operators

    prefix = getattr(busnames, "SOUL_PREFIX", None)
    if prefix is None:
        c.check("#581 no delegated-actor namespace exists yet "
                "(the contract #579 names it)", True)
        return
    name = f"{prefix}pu"
    c.check(f"#581 the {prefix}* namespace is in RESERVED_PREFIXES",
            name.startswith(busnames.RESERVED_PREFIXES),
            f"{name!r} vs RESERVED_PREFIXES={busnames.RESERVED_PREFIXES}")
    c.check(f"#581 is_machine recognizes a {prefix}* principal",
            busnames.is_machine(name),
            f"is_machine({name!r}) is False: делегированная личность -- не "
            f"человек, гейт callout обязан отличать её до проверки пароля")
    try:
        operators.parse(f"{name}:user:mop")
    except ValueError:
        c.check(f"#581 a human login {name}:… is refused by operators.parse",
                True)
    else:
        c.check(f"#581 a human login {name}:… is refused by operators.parse",
                False, "parse принял логин из пространства делегированных "
                "личностей: права сложились бы в одного пользователя")


def check_gate(c):
    """Провайдер папетов не принимает чужие имена -- и сегодня.

    Разбор `puppet-<проект>` -- единственный разбор префикса в провайдерах:
    принять `node-x` или будущее имя делегированной личности значило бы
    искать файл пароля по имени из чужого пространства."""
    import tempfile

    from mop.server import callout, identity

    with tempfile.TemporaryDirectory() as root:
        provider = callout.PuppetProvider(root=root)
        for user in ("node-hyper", "service", "callout"):
            try:
                provider.authenticate(user, "x")
            except identity.Refused:
                c.check(f"#581 the puppet provider refuses {user!r}",
                        True)
            except Exception as e:
                c.check(f"#581 the puppet provider refuses {user!r}",
                        False, f"{type(e).__name__}: {e}")
            else:
                c.check(f"#581 the puppet provider refuses {user!r}",
                        False, "имя из чужого пространства принято за проект")


def check_service_boundary(c):
    """Каркас сервиса снимает с тела весь доверенный префикс.

    `_caller` после зачистки ставит сам каркас -- из субъекта; всё остальное
    с префиксом `_` тело доставить не может. Обработчик видит ровно то, что
    граница решила ему отдать."""
    from mop.common import service

    def handler(project, req, send):
        return {"seen": {k: v for k, v in req.items() if k.startswith("_")}}

    forged = ('{"verb": "ping", "_caller": "bob", "_project": "admin",'
              ' "_grant": {"roles": ["admin"]}, "_actor": "delegated-pu",'
              ' "note": "x"}').encode()
    for caller, want in (("alice", {"_caller": "alice"}), (None, {})):
        out = asyncio.run(service.answer("t", "mop", forged, handler,
                                         lambda *a: [], lambda line: None, None,
                                         caller=caller))
        seen = (out or {}).get("seen")
        c.check(f"#581 service.answer with caller {caller!r} leaves only "
                f"the subject's trusted field", seen == want,
                f"обработчик увидел {seen!r}: тело доставляет доверенные "
                f"поля, хотя вызывающий -- {caller!r}")


def check_agent_boundary(c):
    """Агент узла -- та же граница: доверенные поля ставит субъект.

    `_project` и `_caller` агент берёт из субъекта (права NATS), публичный
    канал `owner` снимает. Новый доверенный префикс не должен проезжать и
    здесь -- глаголы узла читали бы `_grant` как выданный сервером."""
    import dataclasses

    from mop.node import agent

    seen = []

    def spy(conn, req):
        seen.append({k: v for k, v in req.items() if k.startswith("_")})
        return {"ok": True}

    verb = dataclasses.replace(agent.VERBS["state"], fn=spy)
    keep = agent.VERBS["state"]
    agent.VERBS["state"] = verb
    try:
        forged = {"verb": "state", "name": "pu-mop-1", "_caller": "bob",
                  "_grant": {"roles": ["admin"]}, "_actor": "delegated-pu"}
        msg = Msg("mop.mop.node.hyper.rpc.alice", body=forged)
        asyncio.run(agent.handle(None, msg, public=False))
        # rpc-канал: субъект назвал alice и проект mop -- только их глагол
        # и должен видеть, всё остальное тело написало само.
        c.check("#581 agent.handle leaves only the subject's trusted fields",
                seen == [{"_project": "mop", "_caller": "alice"}],
                f"глагол увидел {seen!r}: доверенные поля приехали телом")
    finally:
        agent.VERBS["state"] = keep


def main():
    c = Checks()
    for fn in (check_namespace, check_gate, check_service_boundary,
               check_agent_boundary):
        try:
            fn(c)
        except Exception as e:
            c.fail(fn.__name__, f"{type(e).__name__}: {e}")
    return c.report("souls")


if __name__ == "__main__":
    sys.exit(main())
