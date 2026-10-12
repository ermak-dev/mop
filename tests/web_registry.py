#!/usr/bin/env python3
"""Ворота add производственного воркера: источник реестра (#571).

HYPOTHESIS: mop/cli/server/web.py поднимал WORKER = opworker.Worker()
без registry: ворота add (#470) fail closed на None и отказывали каждый
origin -- веб-add на живом дашборде не работал вовсе, хотя форму список
проектов кормил её собственный читатель _registry_projects, который
после #534 читает замороженную легаси-пару, а не снимок v1 (writer пары
больше не пишет). Проверки #470 собирали воркера руками с registry=fake
-- производственная сборка источника так и не получила его.
SOLUTION: один источник _registry_projects(): origin'ы реестра сервера,
снимок v1 глазами читателя (projects.view_of, #534). Callable уходит и
воротам воркера (Worker(registry=...), #470), и списку формы: маршрут
передаёт функцию в существующую границу Api.projects (битый реестр --
503 фразой, не пустой список «проектов нет»). Зарегистрированный origin
проходит ворота и доезжает до общего workflow (#458), незнакомый
отказывает с подсказкой до эффекта, битый реестр поднимает причину
наружу (у адаптера это failed с причиной, #506), пустым множеством не
становясь: проглоченная ошибка читателя выдала бы битый реестр за
«проекта нет в реестре» без причины, а пустой реестр отказывает origin
(fail closed) -- разрешением пустота не бывает, терялась бы причина, не
граница. Подменяются только
внешние вводы-выводы -- manifest.fetch и шина; WORKER, ворота и run --
производственные, руками не собираются.
STATUS: FIXED — see #571
"""
import io
import os
import sys

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus, manifest, projects  # noqa: E402
from mop.server import identity, websession  # noqa: E402
from mop.cli.server import web as webmod  # noqa: E402

# Снимок держит один проект; второй origin не заведён нигде.
REGISTERED = "git@example.test:g/mop.git"
UNKNOWN = "git@example.test:g/ghost.git"
# Валидный один play без задач: remote_workspace разбирает его по-настоящему.
BOOT = "- name: play\n  tasks: []\n"


def seed_registry(origins):
    """Снять реестр v1 в герметичный дом процесса: пишет писателем
    (write_snapshot), читают его производственные читатели."""
    projects.write_snapshot({"version": 1,
                             "projects": [{"origin": o} for o in origins]})


def break_registry():
    """Испортить снимок: читатель view_of обязан поднять причину."""
    with open(projects.SNAPSHOT, "w") as f:
        f.write("{oops")


def execute_add(origin):
    """Производственный _execute на add; подменена только шина.

    -> вердикт воркера либо {"status": "raised", "error": ...}:
    исключение из ворот едет наружу (у адаптера это failed с причиной),
    и проверка обязана его видеть, а не глотать."""
    webmod._ROSTER_CACHE[0] = 0.0      # кэш резолвера -- модульный
    webmod._ROSTER_CACHE[1] = {}
    try:
        return webmod._execute("add", None, {"origin": origin},
                               ("anton", "admin"))()
    except Exception as e:  # noqa: BLE001 -- причина и есть предмет проверки
        return {"status": "raised", "error": str(e)}


def cluster_bus(calls):
    """Шина-подмена: глаголы сервиса кластера считаются с полями.

    add сразу ставит папета на узел (alloc running, без метки
    bootstrap): общий workflow доходит до вердикта без ожиданий.
    Узловых глаголов путь add не зовёт -- потому request поднимает
    AssertionError, а не отвечает выдумкой."""
    def call_cluster(verb, timeout=20, project=None, **fields):
        calls.append((verb, fields))
        if verb == "add":
            return {"ok": True, "name": "pu-mop-9"}
        if verb == "alloc":
            return {"alloc": {"ID": "a1", "JobID": fields.get("name"),
                              "NodeName": "node-a",
                              "ClientStatus": "running"}}
        if verb == "projects":
            return {"names": []}       # резолвер ростера: нового имени нет
        return {}

    def request(node, verb, **fields):
        raise AssertionError(f"add must not reach node verb {verb}")

    return call_cluster, request


def _fake_handler(method, path, headers=None):
    """Минимальный Handler без сети, как в tests/web.py (#486)."""
    h = webmod.Handler.__new__(webmod.Handler)
    h.command, h.path = method, path
    h.headers = dict(headers or {})
    h.rfile, h.wfile, h.sent = io.BytesIO(), io.BytesIO(), []
    h.client_address = ("127.0.0.1", 0)
    h.send_response = lambda code, msg=None: h.sent.append(("status", code))
    h.send_header = lambda k, v: h.sent.append((k, v))
    h.end_headers = lambda: None
    getattr(h, "do_" + method)()
    return h.sent, h.wfile.getvalue()


def form_projects():
    """GET /api/admin/projects производственным маршрутом.

    -> (код, тело). Личность -- своя сессия (админ), настройка установки
    -- подменённой конфигурацией, как в проверке #486."""
    class Provider:
        def lookup(self, login):
            return identity.Identity(login, "admin", ())

    store = websession.SessionStore(now=lambda: 1000.0)
    with patched(webmod.config,
                 get=lambda name, default=None:
                 {"MOP_SERVER_LAN": "10.0.0.1",
                  "MOP_HTTPS_PORT": "443"}.get(name, default)), \
            patched(webmod, SESSIONS=store), \
            patched(websession, provider=lambda: Provider()):
        token = store.open(identity.Identity("anton", "admin", ()))
        sent, out = _fake_handler("GET", "/api/admin/projects",
                                  headers={"Cookie": f"mop_session={token}"})
    code = next((v for k, v in sent if k == "status"), None)
    return code, out


def main():
    c = Checks()
    calls = []
    call_cluster, request = cluster_bus(calls)

    # Снимок с одним проектом: ворота и форма читают один источник.
    seed_registry([REGISTERED])
    with patched(bus, call_cluster=call_cluster, request=request), \
            patched(manifest, fetch=lambda origin: {"bootstrap_text": BOOT,
                                                    "commit": "c0"}):

        # Зарегистрированный origin проходит ворота производственного
        # воркера и доезжает до общего workflow одним add.
        got = execute_add(REGISTERED)
        c.check("#571 a registered origin passes the production gate",
                got.get("status") == "succeeded" and got.get("name") == "pu-mop-9",
                got)
        adds = [f for v, f in calls if v == "add"]
        c.check("#571 the add rides the shared workflow exactly once",
                len(adds) == 1 and adds[0].get("origin") == REGISTERED, calls)
        c.check("#571 the workspace travels from the remote source",
                bool(adds) and adds[0].get("workspace") == BOOT
                and (adds[0].get("bootstrap_sent") or {}).get("source") == "origin",
                adds)

        # Незнакомый origin: отказ ВЕТКИ ПОДКЛЮЧЁННОГО реестра -- с
        # подсказкой списка (у не подключённого подсказки нет), до
        # эффекта ни одного глагола-мутации.
        calls.clear()
        got = execute_add(UNKNOWN)
        c.check("#571 an unknown origin is refused with the list hint",
                got.get("status") == "refused"
                and "registry" in (got.get("error") or "")
                and "mop project list" in (got.get("error") or ""), got)
        c.check("#571 an unknown origin reaches no cluster mutation",
                not [v for v, _ in calls if v == "add"], calls)

        # Форма видит тот же источник: заведённый origin -- в списке.
        code, out = form_projects()
        c.check("#571 the form lists the same registry source",
                code == 200 and REGISTERED.encode() in out, (code, out[:200]))

    # Битый реестр: причина читателя едет наружу, ворота не молчат и не
    # пускают; форма отвечает 503 своей существующей границей, а не
    # пустым списком.
    break_registry()
    calls.clear()
    with patched(bus, call_cluster=call_cluster, request=request), \
            patched(manifest, fetch=lambda origin: {"bootstrap_text": BOOT}):
        got = execute_add(REGISTERED)
        c.check("#571 a broken registry never becomes a permission",
                got.get("status") == "raised"
                and not [v for v, _ in calls if v == "add"], (got, calls))
        c.check("#571 the reader's reason travels to the verdict boundary",
                "registry" in (got.get("error") or "")
                and "unreadable" in (got.get("error") or ""), got)
    code, out = form_projects()
    c.check("#571 the form answers 503 on a broken registry, not an empty list",
            code == 503 and b"error" in out, (code, out[:200]))

    # За процессом не оставлять снимка даже в герметичном доме.
    if os.path.exists(projects.SNAPSHOT):
        os.remove(projects.SNAPSHOT)
    return c.report("web_registry")


if __name__ == "__main__":
    sys.exit(main())
