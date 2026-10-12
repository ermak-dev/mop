#!/usr/bin/env python3
"""Собранный веб-исполнитель под обязательным ops.safety.

HYPOTHESIS: _execute в mop/cli/server/web.py зовёт Worker.run без
safety-предиката: инвариант #460 («веб работу не берёт») жил только у
send (его проверяет _wrap_send внутри эффекта), а рестарт, снос и
обновление доходили до эффекта, ни разу не спросив клон, -- ворота
сервиса (#40) пускают ничейный клон и своего владельца, и клик по
кнопке сносил грязную работу так же, как чистое место. Первый фикс
(ace086b) закрыл матрицу отказов, но с тремя дырами (#566, ревью):
занятость считалась списком «занятых» слов -- диалог, ожидание и
незнакомое слово статуса проходили как безопасные; узел брался только
работающей аллокации -- легитимный wipe остановленного (#377)
отказывался; префетч ехал субъектом процесса (#498) -- на проектном
веб-процессе чужая цель ловила belongs-отказ до того, как известен
origin.
SOLUTION: production-_execute под подменой ТОЛЬКО шины (глаголы сервиса
кластера и узла): ops.safety на свежих фактах ДО эффекта -- dirty
(включая ничейный), unknown, чужая аренда, живая НЕ-idle сессия и
неразрешимый проект отказывают без единой мутации, чистый разрешённый
случай доходит до эффекта. Занятость -- общим вердиктом
(_state_from_session, #501): всё, что не free и не hung, -- живая
работа, и незнакомое слово тоже; hung/offline -- сессии нет, рестарт
им и лечится. Узел -- последней аллокации в любом статусе (#377), а
факты клона -- глаголом clone узла (жив и при мёртвой сессии, #40, как
_clone_of сервиса): state фактов клона без tmux не даёт. Проект -- из
origin спеки, а спека и аллокация спрашиваются транспортом проекта
цели (#498, резолвер ростера), не субъектом процесса; origin
отсутствует -- отказ ДО узла и эффекта. Владелец -- логин
HTTP-принципала. Add -- без цели (его ворота -- реестр #470), send --
за своими воротами _wrap_send. Фейк шины держит субъекты: чтения --
проекту цели или оператору (как настоящая служба), мутации -- ТОЛЬКО
проекту цели (строже службы нарочно: веб обязан ехать проектом цели,
субъект процесса -- то самое окно #498).
"""
import os
import sys
import time

import hermetic  # noqa: F401,E402 -- настройки не с этой машины (#209)
from _lib import Checks, patched  # noqa: E402
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from mop.common import bus, puppets  # noqa: E402
from mop.cli.server import web as webmod  # noqa: E402

ORIGIN = "git@example.test:g/actual.git"
TARGET_PROJECT = puppets.project_of(ORIGIN)   # "actual": проект решает origin
CLEAN = {"cur": "master", "def": "master", "dirty": 0, "ahead": 0}
# Глаголы-мутации: чтения (spec/alloc/projects/roster/state/clone) не
# считаются -- ворота обязаны спрашивать, но не трогать.
MUTATIONS = frozenset(("restart", "delete", "wipe", "recycle", "update",
                       "add", "stop", "send", "type", "write"))
# Чтения службы кластера: оператору положены, как настоящей службе.
READS = frozenset(("spec", "alloc", "roster", "projects", "pool", "stderr"))


def mutations_of(cluster, node):
    """Мутации, дошедшие до кластера и узла: [(источник, глагол, поля)]."""
    out = [("cluster", v, f) for v, f in cluster if v in MUTATIONS]
    out += [("node", v, f) for _, v, f in node if v in MUTATIONS]
    return out


def facts(owner=None, dirty=0, session="idle 1 1", present=True):
    """Ответ узлового state: факты клона и статус сессии, как их отдаёт агент."""
    return {"present": present, "session": session,
            "clone": dict(CLEAN, dirty=dirty, owner=owner)}


def execute(action, target, params, who=("anton", "admin"), state=None,
            clone=None, state_error=None, spec_error=None, origin=ORIGIN,
            alloc="running", allocate=True):
    """Вердикт собранного исполнителя #566, подменена только шина.

    call_cluster -- глаголы сервиса кластера, и она держит субъекты
    (#498): чтения пускают проект цели и оператора (субъект процесса в
    проверках -- оператор), мутации -- только проект цели. Субъект
    каждого разрешённого вызова пишется в журнал полем _subject.
    alloc -- ClientStatus последней аллокации (complete -- остановленный
    папет, #377), allocate=False -- аллокации нет. request -- глаголы
    узла: clone -- факты клона (жив и при мёртвой сессии), state --
    сессия, send -- доставка, wipe -- эффект.
    -> (вердикт, кластерные вызовы, узловые [(node, verb, fields)])."""
    cluster, node, restarts = [], [], [7]
    # Кэш резолвера -- модульный: без сброса один прогон файла решал бы
    # имена для всех остальных по своей первой картинке.
    webmod._ROSTER_CACHE[0] = 0.0
    webmod._ROSTER_CACHE[1] = {}

    def call_cluster(verb, project=None, **fields):
        # Субъект на проводе: None -- субъект процесса (в проверках это
        # оператор: hermetic снимает MOP_PROJECT).
        subj = project if project is not None else bus.PROJECT
        named = fields.get("name") or fields.get("origin")
        if verb in READS:
            if subj not in (TARGET_PROJECT, bus.ADMIN):
                raise bus.Refused(
                    f"{named} belongs to project {TARGET_PROJECT}, not {subj}")
        elif subj != TARGET_PROJECT:
            # Строже настоящей службы (оператора та пустила бы) -- нарочно:
            # мутация веба обязана ехать проектом цели, субъект процесса и
            # есть окно #498.
            raise bus.Refused(
                f"{named} belongs to project {TARGET_PROJECT}, not {subj}")
        cluster.append((verb, {**fields, "_subject": subj}))
        if verb == "spec":
            if spec_error is not None:
                raise spec_error
            return {"meta": {"origin": origin}}
        if verb == "alloc":
            if not allocate:
                return {"alloc": None}
            return {"alloc": {"ID": "a1", "JobID": fields.get("name"),
                              "NodeName": "node-a",
                              "ClientStatus": alloc,
                              "task": {"state": "running",
                                       "restarts": restarts[0],
                                       "exit": None, "failed": False}}}
        if verb == "restart":
            restarts[0] += 1
            return {"ok": True}
        if verb == "projects":
            return {"names": [TARGET_PROJECT]}
        if verb == "roster":
            return {"items": [{"job": {"ID": target,
                                       "Meta": {"origin": ORIGIN},
                                       "Type": "service"}}]}
        return {}

    def clone_answer():
        # Глагол clone отвечает фактами клона и при мёртвой сессии (#40):
        # остановленной цели их даёт он, не state.
        if clone is not None:
            return clone
        if state_error is not None:
            raise state_error
        if isinstance(state, dict) and "clone" in state:
            return {"clone": state["clone"]}
        return None

    def request(node_name, verb, **fields):
        node.append((node_name, verb, fields))
        if verb in ("state", "clone"):
            if state_error is not None:
                raise state_error
            return state if verb == "state" else clone_answer()
        if verb == "send":
            return {"msg_id": "message-1"}
        if verb == "wipe":
            return {"reset": True, "target": "target"}
        raise AssertionError(verb)

    with patched(bus, call_cluster=call_cluster, request=request):
        try:
            # Дошедший эффект с незаглушенным глаголом -- исключение: в
            # адаптере это failed, здесь -- красная метка с причиной, а не
            # обрыв файла проверки на полдороги.
            got = webmod._execute(action, target, params, who)()
        except Exception as e:  # noqa: BLE001 -- метка RED, причина на виду
            got = {"status": "failed", "error": f"{type(e).__name__}: {e}"}
    return got, cluster, node


def subjects_of(cluster, verb):
    """Субъекты проекта, под которыми звали глагол: [project]."""
    return [f.get("_subject") for v, f in cluster if v == verb]


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

    # Занятость -- общий вердикт (#501), не список «занятых» слов: диалог,
    # ожидание и НЕЗНАКОМОЕ слово статуса -- сессия жива и не idle,
    # disruptive над ней отказывает в словах про живую сессию.
    for what, st in (
            ("a dialog session", facts(owner=mine, session="requires_action 1 1")),
            ("a waiting session", facts(owner=mine, session="waiting 1 1")),
            ("an unknown status word", facts(owner=mine, session="new-status 1 1"))):
        got, cluster, node = execute("restart", target, {}, state=st)
        c.check(f"#566 restart over {what} is refused as a live session",
                got.get("status") == "refused"
                and "live" in (got.get("error") or "")
                and not mutations_of(cluster, node), (got, cluster, node))

    # Мёртвая сессия (listen=0) на чистом клоне -- disruption безопасен:
    # hung/offline -- «не отвечает», рестарт им и лечится, это не работа.
    for word in ("offline 1 0", "hung 1 0"):
        got, cluster, node = execute("restart", target, {},
                                     state=facts(owner=mine, session=word))
        c.check(f"#566 a dead session ({word.split()[0]}) on a clean clone restarts",
                got.get("status") == "succeeded"
                and sum(v == "restart" for v, _ in cluster) == 1, (got, cluster))

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

    # Контекст не собирается (спека молчит, узел молчит, аллокации нет) --
    # отказ, не пропуск и не подстановка «значит, свободен».
    for what, kw in (("no spec answer", {"spec_error": bus.BusError("nomad down")}),
                     ("a silent node", {"state_error": bus.BusError("agent silent")}),
                     ("no allocation", {"allocate": False})):
        got, cluster, node = execute("restart", target, {}, **kw)
        c.check(f"#566 restart refuses on {what}",
                got.get("status") == "refused"
                and not mutations_of(cluster, node), (got, cluster, node))
    # Origin отсутствует -- отказ ДО узла и эффекта (#498): чья цель,
    # неизвестно, и спрашивать узел о чужом нельзя.
    got, cluster, node = execute("restart", target, {}, origin=None)
    c.check("#566 a spec without an origin refuses before the node and the effect",
            got.get("status") == "refused" and node == []
            and not any(v == "alloc" for v, _ in cluster)
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

    # ── остановленная цель: wipe спрашивает последнюю аллокацию (#377) ──
    # Джоб остановлен (complete), сессии нет, факты клона -- глаголом
    # clone: рабочий кейс wipe, и ворота ДОЛЖНЫ его пускать.
    stopped_clean = {"clone": dict(CLEAN, owner=None)}
    got, cluster, node = execute("wipe", target, {}, alloc="complete",
                                 state={"present": False}, clone=stopped_clean)
    c.check("#566 wipe on a stopped target with a clean clone passes to the effect",
            got.get("status") in ("succeeded", "partial")
            and any(v == "wipe" for _, v, _ in node), (got, cluster, node))
    # Остановленный, но факты неизвестны -- по-прежнему отказ: останов
    # аллокации не доказывает пустоту клона.
    got, cluster, node = execute("wipe", target, {}, alloc="complete",
                                 state_error=bus.BusError("agent silent"))
    c.check("#566 a stopped target with unknown facts is still refused",
            got.get("status") == "refused"
            and not mutations_of(cluster, node), (got, cluster, node))

    # ── чистый разрешённый случай доходит до эффекта ────────────────────
    got, cluster, node = execute("restart", target, {}, state=facts(owner=mine))
    c.check("#566 the owner's clean idle clone restarts",
            got.get("status") == "succeeded"
            and sum(v == "restart" for v, _ in cluster) == 1, (got, cluster))
    asks = [f for _, v, f in node if v in ("state", "clone")]
    c.check("#566 the clone is inspected before the effect",
            bool(asks), node)
    if asks:
        c.check("#566 the node asks ride the target's project",
                all(f.get("project") == TARGET_PROJECT for f in asks), asks)
        c.check("#566 the asks name the target",
                all(f.get("name") == target for f in asks), asks)

    # ── субъект: префетч и эффект едут проектом цели (#498) ─────────────
    # Первый spec и первый alloc -- preflight ворот; мутация -- транспорт
    # воркера. Субъект процесса (оператор в проверках) у префетча -- то
    # самое окно: на проектном веб-процессе он получил бы belongs-отказ
    # до того, как известен origin.
    got, cluster, node = execute("restart", target, {}, state=facts(owner=mine))
    c.check("#566 the preflight spec rides the target's project",
            subjects_of(cluster, "spec")[:1] == [TARGET_PROJECT],
            subjects_of(cluster, "spec"))
    c.check("#566 the preflight alloc rides the target's project",
            subjects_of(cluster, "alloc")[:1] == [TARGET_PROJECT],
            subjects_of(cluster, "alloc"))
    c.check("#566 the mutation rides the target's project",
            subjects_of(cluster, "restart") == [TARGET_PROJECT],
            subjects_of(cluster, "restart"))

    # ── владелец -- HTTP-принципал, не тело запроса ─────────────────────
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
