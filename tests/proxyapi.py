#!/usr/bin/env python3
"""Проверки команды ротации клиентского ключа: python3 tests/proxyapi.py

#539: библиотека #538 -- ОДИН оркестратор; серверный адаптер даёт ей
настоящие transport-швы (management API петли + клиентский probe), CLI --
тонкий фасад группы глаголов. Граница контроллера -- инвентарь
(как mop server user) плюс явный отказ puppet-контексту, ДО сети и файлов.
Ни одного живого запроса: HTTP-транспорт подменён на границе адаптера,
узлы -- фейковый список, пути -- временный каталог.

HYPOTHESIS (#539): без адаптера библиотечные швы -- NotImplementedError,
а CLI-логика в командлете -- вторая копия оркестратора, которая
разъедется с #538 на первом же изменении контракта.
SOLUTION: mop/server/proxyapi.py -- Effects-адаптер (urllib, только
петля, ProxyHandler({}) без redirect, ключ management из файла) +
граница контроллера + resolve путей + доставка прямым bus-каналом по
ЯВНОМУ списку узлов; mop/cli/server/proxykey/{__init__,start,resume,status}
-- печать safe-проекции, rc 0/1.
STATUS: в работе (#539): прогон до реализации обязан быть красным.
"""
import io
import json
import os
import sys
import tempfile

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched, patched_env  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.server import proxykey  # noqa: E402

SECRETS = ("old-key-539", "new-key-539", "mgmt-key-539")


# ── общий стенд: временные source/copy/mgmt/op + фейковый транспорт ──────
class FakeHTTP:
    """Транспорт адаптера на границе urllib: без сети, с журналом.

    GET api-keys -> список; PUT -> ({"http_status":..,"reason":..} | None);
    /v1/models?key -> 200 | 401. Ответы подставляет проверка."""

    def __init__(self, keys, accepted, put_outcome=None):
        self.keys = list(keys)
        self.accepted = set(accepted)
        self.put_outcome = put_outcome
        self.log = []

    def get_keys(self, bearer):
        self.log.append(("get", bearer))
        return list(self.keys)

    def put_keys(self, bearer, body):
        self.log.append(("put", bearer, list(body)))
        self.keys = list(body)
        return self.put_outcome

    def models(self, key):
        self.log.append(("probe", key))
        return 200 if key in self.accepted else 401


def stand(c, keys=("old-key-539",), accepted=("old-key-539",),
          put_outcome=None):
    """Временный контроллер: source, копия кластера, ключ management,
    состояние операции; (пути, FakeHTTP)."""
    d = tempfile.mkdtemp(prefix="mop-539-")
    src = os.path.join(d, "llm-proxy-client.pass")
    cp = os.path.join(d, "cluster-home", ".config", "mop", "secrets",
                      "llm-proxy-client.pass")
    os.makedirs(os.path.dirname(cp), exist_ok=True)
    mgmt = os.path.join(d, "secrets", "llm-proxy-mgmt.pass")
    os.makedirs(os.path.dirname(mgmt), exist_ok=True)
    for p, k in ((src, keys[0]), (cp, keys[0]), (mgmt, "mgmt-key-539")):
        with open(p, "w", encoding="utf-8") as f:
            f.write(k + "\n")
        os.chmod(p, 0o600)
    op = os.path.join(d, "proxy-rotation.json")
    lock = os.path.join(d, "proxy-rotation.lock")
    inv = os.path.join(d, "inventory.yaml")
    with open(inv, "w", encoding="utf-8") as f:
        f.write("all:\n  hosts: {}\n")
    return {"src": src, "cp": cp, "mgmt": mgmt, "op": op, "lock": lock,
            "inv": inv, "dir": d}, FakeHTTP(keys, accepted, put_outcome)


def effects_of(http, mgmt_path, port="8317"):
    """Адаптер с подсунутым транспортом, портом и файлом ключа management."""
    from mop.server import proxyapi
    return proxyapi.Effects(transport=http, mgmt_key_path=mgmt_path,
                           port=port)


def no_network_log(c, http, what):
    c.check(f"#539 {what}: ни одного касания транспорта",
            not http.log, http.log)


# ── граница контроллера: puppet-контекст и не-контроллер отказ ДО сети ──
# HYPOTHESIS: без границы мастер-шелл/узел/пул-сессия дошли бы до
# management API с файлами контроллера. UID -- НЕ граница: папет может
# жить под тем же UID. Граница -- инвентарь установки (как mop server
# user) плюс явный отказ puppet-окружению (MOP_PROJECT/MOP_BUS_CONFIG,
# их сеет spec.py) ДО сети и файлов.
def check_controller_guard_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])

    # Инвентарь есть, puppet-окружения нет -- это контроллер.
    with patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                     MOP_BUS_CONFIG=None):
        why = proxyapi.refusal_if_not_controller()
    c.check("#539 the controller passes the boundary", why is None, why)

    # Машина без инвентаря -- не контроллер (как mop server user).
    with patched_env(INVENTORY=os.path.join(box["dir"], "nope.yaml")):
        why = proxyapi.refusal_if_not_controller()
    c.check("#539 a machine without the inventory is refused",
            isinstance(why, str), why)

    # Puppet-контекст на самой машине контроллера -- отказ: папет может
    # жить под тем же UID и тем же домом, env-признак решает.
    for marker in ("MOP_PROJECT", "MOP_BUS_CONFIG"):
        with patched_env(INVENTORY=box["inv"], **{marker: "proj"}):
            why = proxyapi.refusal_if_not_controller()
        c.check(f"#539 a puppet context ({marker}) is refused "
                "even on the controller",
                isinstance(why, str) and "puppet" in why.lower(), why)
    no_network_log(c, http, "the boundary refusals")


# ── адаптер: pinned v8 маршруты, только петля, ключ из файла ────────────
def check_adapter_pinned_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])
    got = eff.fetch_list()
    c.expect("#539 fetch returns the pinned GET list", got, ["old-key-539"])
    c.check("#539 fetch hit the pinned api-keys route",
            http.log == [("get", "mgmt-key-539")], http.log)

    http.log.clear()
    verdict = eff.put_list(["old-key-539", "new-key-539"])
    c.expect("#539 a clean PUT is applied", verdict, ("applied", None))
    c.check("#539 PUT sent the whole list",
            http.log == [("put", "mgmt-key-539",
                          ["old-key-539", "new-key-539"])], http.log)

    http.log.clear()
    c.check("#539 the client probe is a client-key check",
            eff.probe("old-key-539") is True
            and http.log == [("probe", "old-key-539")], http.log)
    # Bearer management читается из файла и едет только в транспорт.
    c.check("#539 the management key rides only inside the transport calls",
            all(g[1] == "mgmt-key-539" for g in http.log
                if g[0] in ("get", "put")), http.log)
    c.check("#539 an unaccepted client key probes False",
            eff.probe("nope") is False, None)

    url = proxyapi.base_url()
    c.check("#539 the base URL is loopback with the settings port",
            url == "http://127.0.0.1:8317", url)
    # Порт -- из настройки, валидируется 1..65535: не клиентские данные.
    for bad in ("0", "65536", "x", "-1"):
        with patched_env(MOP_LLM_PROXY_PORT=bad):
            why = proxyapi.port_refusal()
        c.check(f"#539 port {bad!r} is refused before the URL",
                isinstance(why, str), why)
    with patched_env(MOP_LLM_PROXY_PORT="9317"):
        c.check("#539 a valid settings port builds the URL",
                proxyapi.base_url() == "http://127.0.0.1:9317",
                proxyapi.base_url())
    # Никаких клиентских URL: базу строит адаптер, только 127.0.0.1.
    c.check("#539 no client-controlled URL enters the adapter",
            "base_url" in dir(proxyapi) and "url" not in
            inspect_signature(eff), None)


def inspect_signature(eff):
    """Имена параметров Effects: URL пользователем не задаётся."""
    return [k for k in vars(eff) if not k.startswith("_")] + \
           [k for k in type(eff).__dict__ if not k.startswith("_")]


# ── адаптер: исходы PUT по контракту #537/#538 ──────────────────────────
def check_adapter_outcomes_539(c):
    from mop.server import proxyapi
    # Подтверждённый отказ ДО применения: 403 + enum reason.
    box, http = stand(c, put_outcome={"http_status": 403,
                                      "reason": "forbidden"})
    eff = effects_of(http, box["mgmt"])
    tag, why = eff.put_list(["old-key-539"])
    c.expect("#539 a pre-apply refusal carries the tag", tag, "refused")
    c.expect("#539 the refusal carries the http status",
             why["http_status"], 403)
    c.expect("#539 the refusal carries the enum reason", why["reason"],
             "forbidden")

    # Неясные исходы (не 401/403, не.handler-proven) -- unknown-путь
    # библиотеки: исключение наружу, решает оркестратор.
    for code in (500, 200, 301):
        box, http = stand(c, put_outcome={"http_status": code})
        eff = effects_of(http, box["mgmt"])
        try:
            eff.put_list(["old-key-539"])
            c.fail(f"#539 HTTP {code} on PUT must not be a clean verdict")
        except Exception as e:  # noqa: BLE001 -- исход неясен, не отказ
            c.check(f"#539 HTTP {code} on PUT raises to the orchestrator",
                    type(e).__name__ in ("RuntimeError", "OSError",
                                         "TimeoutError"), repr(e))


# ── resolve путей: источник и копия раздельны, один путь разрешён ────────
def check_paths_539(c):
    from mop.server import proxyapi
    box, http = stand(c)
    # Копия стоит под cluster-home/.config/mop/secrets: MOP_HOME -- сам
    # cluster-home (paths.under дом + .config/mop/secrets/файл).
    mophome = os.path.dirname(os.path.dirname(os.path.dirname(
            os.path.dirname(box["cp"]))))
    with patched_env(HOME=box["dir"], MOP_HOME=mophome):
        src, cp = proxyapi.resolve_paths()
    c.check("#539 the source resolves under the controller HOME",
            src == os.path.join(box["dir"], ".config", "mop", "secrets",
                                "llm-proxy-client.pass"), src)
    c.check("#539 the copy resolves under the MOP_HOME setting",
            cp == box["cp"], (cp, box["cp"]))

    # Один и тот же физический путь разрешён: маленькая установка.
    with patched_env(HOME=box["dir"]):
        src2, cp2 = proxyapi.resolve_paths(cluster_copy=box["src"])
    c.expect("#539 an explicit matching physical path is allowed",
             (src2, cp2), (os.path.join(box["dir"], ".config", "mop",
                                        "secrets",
                                        "llm-proxy-client.pass"),
              box["src"]))

    # Режим копии -- до эффекта, ДО remote: resume-гuard проверяет
    # пригодность обоих путей (режим/владелец) перед КАЖДОЙ мутирующей
    # фазой, и отказ едет ДО первого сетевого касания. begin только
    # записывает приватный чекпоинт -- это его контракт (#538).
    http.accepted.add("new-key-539")
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]):
        proxykey.begin(box["src"], box["cp"], ["node1"], exclusive=True,
                       effects=effects_of(http, box["mgmt"]))
    os.chmod(box["cp"], 0o644)
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]):
        out = proxykey.resume(exclusive=True,
                              effects=effects_of(http, box["mgmt"]))
    c.check("#539 a non-private copy refuses resume before any effect",
            not out.get("ok") and "private" in str(out.get("blocker", ""))
            and not http.log, (out, http.log))
    os.chmod(box["cp"], 0o600)


# ── получатели: явный список, замороженный, без ready-фильтра ───────────
def check_recipients_539(c):
    from mop.server import proxyapi
    box, http = stand(c)

    # Полный известный состав -- БЕЗ ready-фильтра: offline-узел тоже
    # получатель, его «NOT REACHED» держит операцию открытой.
    fake_pool = [{"name": "node1", "status": "ready", "placeable": True},
                 {"name": "node2", "status": "down", "placeable": True},
                 {"name": "node3", "status": "drained", "placeable": False}]

    class Row:
        def __init__(self, n):
            self.name = n["name"]

    from mop.common import puppets as puppets_mod
    with patched(puppets_mod, pool=lambda: [Row(n) for n in fake_pool]):
        got = proxyapi.all_nodes()
    c.expect("#539 the recipient set is the full pool, no ready filter",
             sorted(got), ["node1", "node2", "node3"])

    # Недоступный список узлов -- блокер ДО эффекта, не пустой набор:
    def boom():
        raise RuntimeError("cluster unreachable")

    with patched(puppets_mod, pool=boom):
        why = proxyapi.nodes_refusal()
    c.check("#539 an unreachable node listing blocks, not empties",
            isinstance(why, str), why)


# ── доставка: прямой bus-канал по ЯВНОМУ списку, не proxykey.push ───────
# HYPOTHESIS: old push() решает по sha-гейту и settled -- резюмируемая
# ротация с FAILED/NOT REACHED «завершилась» бы или молча пропустила.
def check_delivery_539(c):
    from mop.server import proxyapi
    from mop.common import bus
    box, http = stand(c, accepted=("old-key-539",))
    eff = effects_of(http, box["mgmt"])
    sent = []

    def fake_request_many(verb, nodes, timeout=None, channel=None,
                          project=None, files=None, **fields):
        sent.append((verb, list(nodes), files))
        # Ответ агента write -- словарь written (#135), не строка:
        # строки сводит bus.results_from, агент их не говорит.
        return {n: {"written": ["secrets.env"]} for n in nodes}

    with patched(bus, request_many=fake_request_many):
        out = eff.deliver("new-key-539", ["node1", "node2"])
    c.check("#539 delivery goes through the write channel with the "
            "explicit saved nodes",
            sent and sent[0][0] == "write" and sent[0][1] == ["node1", "node2"],
            sent)
    files = sent[0][2] if sent else []
    c.check("#539 delivery carries the secrets.env file payload",
            files and files[0][0].endswith("secrets.env"), files)
    from mop.common import bus as bus_mod
    content = bus_mod.file_data(files[0][1]) if files else b""
    c.expect("#539 the payload is the one-key secrets.env",
             content, b"MOP_PROXY_KEY=new-key-539\n")
    c.expect("#539 full OK answers settle", out, {"node1": "OK", "node2": "OK"})

    # FAILED ответ -- обычный итог шины, НЕ перехват: опера держит открытой.
    def failing(verb, nodes, timeout=None, channel=None, project=None,
                files=None, **fields):
        return {"node1": {"written": ["secrets.env"]},
                "node2": {"error": "refused"}}

    with patched(bus, request_many=failing):
        out = eff.deliver("new-key-539", ["node1", "node2"])
    c.check("#539 a raw agent reply rides the bus reducer, not a fake OK",
            out.get("node2", "").startswith("FAILED"), out)
    no_network_log(c, http, "delivery stays off the management channel")


# ── CLI: rc, safe-JSON, --exclusive, группа глаголов ─────────────────────
def check_cli_539(c):
    from mop.cli.server.proxykey import start, status
    box, http = stand(c)
    eff = effects_of(http, box["mgmt"])

    # status: нет операции -- тихий ноль.
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                        MOP_BUS_CONFIG=None), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        rc = status.main([])
        out_text, err_text = sys.stdout.getvalue(), sys.stderr.getvalue()
    c.check("#539 status with no operation is a quiet zero",
            rc == 0 and not out_text.strip(), (rc, out_text))

    # start без --exclusive -- usage-отказ: писатель подтверждается явно.
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=box["inv"], MOP_PROJECT=None,
                        MOP_BUS_CONFIG=None), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        rc = start.main(["--nodes", "node1"])
        out_text, err_text = sys.stdout.getvalue(), sys.stderr.getvalue()
    c.check("#539 start without --exclusive refuses with rc 1",
            rc == 1 and "exclusive" in (err_text + out_text).lower(),
            (rc, err_text))

    # Не-контроллер: rc 1, ДО транспорта.
    with patched(proxykey, OP=box["op"], LOCK=box["lock"]), \
            patched_env(INVENTORY=os.path.join(box["dir"], "nope.yaml")), \
            patched(sys, stdout=io.StringIO(), stderr=io.StringIO()):
        rc = start.main(["--nodes", "node1", "--exclusive"])
    c.check("#539 a non-controller start is refused with rc 1",
            rc == 1, rc)
    no_network_log(c, http, "the CLI refusal")


def main():
    c = Checks()
    check_controller_guard_539(c)
    check_adapter_pinned_539(c)
    check_adapter_outcomes_539(c)
    check_paths_539(c)
    check_recipients_539(c)
    check_delivery_539(c)
    check_cli_539(c)
    return c.report("proxyapi")


if __name__ == "__main__":
    sys.exit(main())
