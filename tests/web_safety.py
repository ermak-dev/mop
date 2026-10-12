#!/usr/bin/env python3
"""Собранный веб-исполнитель под обязательным ops.safety.

HYPOTHESIS: _execute в mop/cli/server/web.py зовёт Worker.run без
safety-предиката: инвариант #460 («веб работу не берёт») жил только у
send (его проверяет _wrap_send внутри эффекта), а рестарт, снос и
обновление доходили до эффекта, ни разу не спросив клон, -- ворота
сервиса (#40) пускают ничейный клон и своего владельца, и клик по
кнопке сносил грязную работу так же, как чистое место.
SOLUTION: production-_execute под подменой ТОЛЬКО шины (глаголы сервиса
кластера и узла): ops.safety на свежих фактах ДО эффекта -- dirty
(включая ничейный), unknown, чужая аренда и busy-disruptive отказывают
без единой мутации, чистый разрешённый случай доходит до эффекта;
проект цели -- из origin спеки, а не из имени, владелец -- логин
HTTP-принципала, а не тело запроса; add -- без цели (его ворота --
реестр #470), send -- за своими воротами _wrap_send.
"""
import os
import sys
import time

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus  # noqa: E402
from mop.cli.server import web as webmod  # noqa: E402

ORIGIN = "git@example.test:g/actual.git"
CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
# Глаголы-мутации: чтения (spec/alloc/projects/roster/state) не считаются --
# ворота обязаны спрашивать, но не трогать.
MUTATIONS = frozenset(("restart", "delete", "wipe", "recycle", "update",
                       "add", "stop", "send", "type", "write"))


def mutations_of(cluster, node):
    """Мутации, дошедшие до кластера и узла: [(источник, глагол, поля)]."""
    out = [("cluster", v, f) for v, f in cluster if v in MUTATIONS]
    out += [("node", v, f) for _, v, f in node if v in MUTATIONS]
    return out


def facts(owner=None, dirty=0, session="idle 1 1"):
    """Ответ узлового state: факты клона и статус сессии, как их отдаёт агент."""
    return {"present": True, "session": session,
            "clone": dict(CLEAN, dirty=dirty, owner=owner)}


def execute(action, target, params, who=("anton", "admin"), state=None,
            state_error=None, spec_error=None, origin=ORIGIN):
    """Вердикт собранного исполнителя #566, подменена только шина.

    call_cluster -- глаголы сервиса кластера: spec/alloc/projects --
    чтения (alloc несёт счётчик рестартов, который bump'ит только эффект,
    -- наблюдение #464 сходится без единого sleep), restart -- эффект.
    request -- глаголы узла: state -- факты клона, send -- доставка.
    -> (вердикт, кластерные вызовы [(verb, fields)], узловые
    [(node, verb, fields)])."""
    cluster, node, restarts = [], [], [7]

    def call_cluster(verb, project=None, **fields):
        cluster.append((verb, fields))
        if verb == "spec":
            if spec_error is not None:
                raise spec_error
            return {"meta": {"origin": origin}}
        if verb == "alloc":
            return {"alloc": {"ID": "a1", "JobID": fields.get("name"),
                              "NodeName": "node-a", "ClientStatus": "running",
                              "task": {"state": "running",
                                       "restarts": restarts[0],
                                       "exit": None, "failed": False}}}
        if verb == "restart":
            restarts[0] += 1
            return {"ok": True}
        if verb == "projects":
            return {"names": []}
        return {}

    def request(node_name, verb, **fields):
        node.append((node_name, verb, fields))
        if verb == "state":
            if state_error is not None:
                raise state_error
            return state
        if verb == "send":
            return {"msg_id": "message-1"}
        raise AssertionError(verb)

    with patched(bus, call_cluster=call_cluster, request=request):
        try:
            # Дошедший эффект с незаглушенным глаголом -- исключение: в
            # адаптере это failed, здесь -- красная метка с причиной, а не
            # обрыв файла проверки на полдороге.
            got = webmod._execute(action, target, params, who)()
        except Exception as e:  # noqa: BLE001 -- метка RED, причина на виду
            got = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    return got, cluster, node


def main():
    c = Checks()
    # Имя цели врёт про проект: проект решает origin спеки, не префикс.
    target = "pu-untrusted-1"
    now = time.time()
    mine = {"user": "anton", "at": now - 5}
    olga = {"user": "olga", "at": now - 5}

    # ── отказы ДО эффекта: ни одной мутации ─────────────────────────────
    for what, st in (
            ("a nobody's dirty clone", facts(dirty=3)),
            ("the owner's dirty clone", facts(owner=mine, dirty=3)),
            ("a foreign live lease", facts(owner=olga)),
            ("a busy session over the owner's clean clone",
             facts(owner=mine, session="busy 1 1")),
            ("an agent error answer", {"error": "agent silent"}),
            ("no facts at all", None)):
        got, cluster, node = execute("restart", target, {}, state=st)
        c.check(f"#566 restart refuses {what}",
                got.get("status") == "refused" and bool(got.get("error")),
                (got, cluster))
        c.check(f"#566 restart on {what} fires no mutation",
                not mutations_of(cluster, node), (cluster, node))

    got, cluster, _ = execute("restart", target, {}, state=facts(dirty=3))
    c.check("#566 the nobody's-work refusal names nobody",
            "nobody" in (got.get("error") or ""), got)
    got, cluster, _ = execute("restart", target, {}, state=facts(owner=olga))
    c.check("#566 the foreign-lease refusal names the holder",
            "olga" in (got.get("error") or ""), got)
    got, cluster, _ = execute("restart", target, {},
                              state=facts(owner=mine, session="busy 1 1"))
    c.check("#566 the busy refusal says the session is live",
            "live" in (got.get("error") or ""), got)
    got, cluster, _ = execute("restart", target, {}, state={"error": "silent"})
    c.check("#566 unknown facts refuse with cannot tell",
            "cannot tell" in (got.get("error") or ""), got)

    # Контекст не собирается (спека, узел молчит) -- отказ, не пропуск.
    for what, kw in (("no spec answer", {"spec_error": bus.BusError("nomad down")}),
                     ("a silent node", {"state_error": bus.BusError("agent silent")})):
        got, cluster, node = execute("restart", target, {}, **kw)
        c.check(f"#566 restart refuses on {what}",
                got.get("status") == "refused"
                and not mutations_of(cluster, node), (got, cluster, node))

    # Каждое действие с целью проходит те же ворота: снос, рецикл и
    # обновление мутируют не рестартом, но отказ обязан стоять ДО эффекта
    # и у них.
    for action in ("recycle", "delete", "wipe", "update", "model"):
        params = ({"model": "glm-5.3"} if action == "model"
                  else {"fresh": False} if action == "update" else {})
        got, cluster, node = execute(action, target, params, state=facts(dirty=3))
        c.check(f"#566 {action} refuses a nobody's dirty clone",
                got.get("status") == "refused" and bool(got.get("error")),
                (got, cluster))
        c.check(f"#566 {action} on a dirty clone fires no mutation",
                not mutations_of(cluster, node), (action, cluster, node))

    # ── чистый разрешённый случай доходит до эффекта ────────────────────
    got, cluster, node = execute("restart", target, {}, state=facts(owner=mine))
    c.check("#566 the owner's clean idle clone restarts",
            got.get("status") == "succeeded"
            and sum(v == "restart" for v, _ in cluster) == 1, (got, cluster))
    asked = [f for _, v, f in node if v == "state"]
    c.check("#566 the clone is inspected before the effect",
            bool(asked), node)
    if asked:
        c.expect("#566 the target project comes from the origin",
                 asked[0].get("project"), "actual")
        c.expect("#566 the facts ask names the target",
                 asked[0].get("name"), target)

    # Владелец -- HTTP-принципал: аренда антона чужая ольге, хоть в теле
    # запроса ничего о владельце нет вовсе.
    got, cluster, node = execute("restart", target, {}, state=facts(owner=mine),
                                 who=("olga", "admin"))
    c.check("#566 the owner is the HTTP principal, not the payload",
            got.get("status") == "refused" and "anton" in (got.get("error") or "")
            and not mutations_of(cluster, node), got)

    # ── add -- targetless: ворота реестра, не факты ─────────────────────
    got, cluster, node = execute("add", None, {"origin": ORIGIN})
    c.check("#566 add is targetless: the registry gate answers, not facts",
            got.get("status") == "refused"
            and "registry" in (got.get("error") or "")
            and not mutations_of(cluster, node), (got, cluster, node))

    # ── send -- за своими воротами _wrap_send, как раньше ───────────────
    got, cluster, node = execute("send", target, {"message": "hi"},
                                 state=facts(dirty=3))
    c.check("#566 send keeps its own guard: a dirty clone refuses delivery",
            got.get("status") == "refused"
            and not mutations_of(cluster, node), (got, node))
    got, cluster, node = execute("send", target, {"message": "hi"},
                                 state=facts(owner=mine))
    c.check("#566 a clean send still delivers",
            got.get("status") == "succeeded"
            and [v for _, v, _ in node] == ["state", "send"], (got, node))
    return c.report("web_safety")


if __name__ == "__main__":
    sys.exit(main())
