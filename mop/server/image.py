"""Образ проекта: манифест -> прогон плейбука сборки -> объявление узлам.

Дорога у сборки одна, а фронтендов два — `mop driver build` в терминале и
инструмент `build` в MCP мастера (#49), — поэтому она живёт здесь.
Библиотека молчит: вывод ansible уходит туда, куда скажет вызывающий
(`out`: файл или None, то есть наследование), а итог возвращается данными.

Только для управляющей машины: здесь нужны токен Nomad (объявление) и
инвентарь (плейбук), и ни того ни другого на узлах нет.
"""
import json
import os
import subprocess

from ..common import config, llm, manifest, puppets, state
from ..common.domain import Alloc, JobMeta
from .. import driver
from . import nomad, playvars, spec

PLAYBOOK = os.path.join(config.PROJECT, "deploy", "pve-build.yml")


def prepare(origin, root=None):
    """Манифесты проекта: из рабочей копии root, если она названа, иначе из
    origin (дефолтная ветка). -> словарь manifest.fetch плюс 'origin'.
    Отказ — RuntimeError, как у manifest."""
    project = puppets.project_of(origin)
    got = manifest.fetch_tree(root, project) if root else manifest.fetch(origin)
    got["origin"] = origin
    return got


def extra_vars(origin, got):
    """Что уезжает плейбуку. None-половины не передаём вообще, а не как
    null: `default('')` в условиях плейбука не подменяет определённый None,
    и len(None) ронял сборку проекта без vars (rugent: только задачи). Поймано
    живой сборкой (#26), держится tests/image.py."""
    extra = {"mop_project": got["project"], "mop_origin": origin,
             "mop_project_asks": got["asks"]}
    # В образ едет только песочница (#61): bootstrap играется при старте.
    if got["sandbox_vars"]:
        extra["mop_project_vars"] = got["sandbox_vars"]
    if got["sandbox_tasks"]:
        extra["mop_project_tasks"] = got["sandbox_tasks"]
    return extra


def play_extra(origin, got, fresh=False, node=None):
    """Переменные прогона: манифест плюс то, что сказали ключами. Чистая
    половина bake (#280): mop_fresh и mop_node едут только когда названы --
    плейбук спрашивает `is defined`, и null ему не то же, что отсутствие."""
    extra = extra_vars(origin, got)
    if fresh:
        extra["mop_fresh"] = True
    if node:
        extra["mop_node"] = node
    return extra


def bake(origin, got, out=None, fresh=False, on_line=None, node=None):
    """Прогнать плейбук сборки. -> код возврата ansible.

    out — куда писать вывод плейбука: файл (MCP пишет в журнал и присылает
    хвост вестью) либо None (терминал видит прогон живьём). Инвентарь — из
    окружения, его ставит диспетчер `mop` для всех командлетов.

    fresh — начисто, с базового образа. Умолчание — инкремент (#60): шаблон
    клонируется в сборочное тело, плейбук играется там и качает только
    новое, и лишь потом образ подменяется. Инкремент не даёт чистоты: то,
    чего в плейбуке уже нет, в образе останется — за этим и остаётся fresh.

    on_line — вместо out: каждая строка вывода отдаётся вызывающему (сборщик
    на сервере показывает по ним шаг, #123).

    node — только этот узел (#280): остальные выходят из игры сами
    (end_host по mop_node), а не через --limit."""
    settings = json.dumps(playvars.playbook_vars(), ensure_ascii=False)
    extra = play_extra(origin, got, fresh, node)
    argv = ["ansible-playbook", "-i", os.environ["INVENTORY"], PLAYBOOK,
            "--extra-vars", settings, "--extra-vars", json.dumps(extra)]
    if on_line is None:
        return subprocess.call(argv, stdout=out,
                               stderr=subprocess.STDOUT if out else None)
    p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                         text=True, bufsize=1)
    for line in p.stdout:
        on_line(line.rstrip("\n"))
    return p.wait()


# ─── тела проекта до и после сборки (#60) ───────────────────────────────────
# Пересборка — операция над ШАРДОМ, а не над образом: образ с живым клоном
# не заменить, значит тела проекта на контейнерных узлах сносятся до сборки и
# папеты поднимаются заново после — уже клонами нового образа. Попутно это
# чинит ловушку, пойманную 22.09: `ensure` переиспользует стоящее тело, и
# «пересозданный» папет приходил с прежними пакетами. Цена названа: снос
# теряет прогретые target (35 ГБ и 329 с на папета), поэтому занятый папет
# — отказ, если не сказано force.
def plan_clear(rows, force=False, node=None):
    """Кого сносить перед сборкой. rows: [{name, node, container, kind}]
    -> [имя]; RuntimeError с именами, если кто-то занят и не force.

    node -- только тела этого узла (#280): чужой занятый папет не блокирует
    и не снимается, его образ не трогают.

    Тела на узлах, где тело равно узлу, сборки не касаются. Свободен —
    только тот, о ком это сказано прямо (state.is_free): молчащий агент
    и папет без состояния читаются как занятые, потому что снос под живой
    работой хуже отказа."""
    mine = [r for r in rows if r.get("container")
            and (node is None or r.get("node") == node)]
    busy = [r["name"] for r in mine if not state.is_free(r.get("kind"))]
    if busy and not force:
        raise RuntimeError(
            f"rebuilding the image destroys the project's bodies, and these are "
            f"not free: {', '.join(busy)} — wait, or mop driver build --force")
    return [r["name"] for r in mine]


def project_rows(project, api=None):
    """Папеты проекта, как их видит plan_clear: [{name, node, container,
    state, kind, job}]. Не размещённые (без аллокации) не считаются: тела у них
    нет, снимать нечего."""
    meta = (api or nomad).nodes_meta()
    rows = []
    for item in puppets.roster():
        job = item["job"]
        if JobMeta.from_job(job).project != project:
            continue
        alloc = Alloc.from_dict(item["alloc"])
        node = alloc.node if alloc else None
        if not node:
            continue
        drv = driver.of_node(meta.get(node), node)
        rows.append({"name": job["ID"], "node": node, "job": job,
                     "container": driver.is_container(drv),
                     "state": item["state"], "kind": item["kind"]})
    return rows


def clear(project, force=False, api=None, node=None):
    """Остановить папетов проекта на контейнерных узлах и снести их тела.
    -> [{name, origin, llm, node}] — кого поднять заново после сборки.
    Отказ по занятым — RuntimeError из plan_clear, до первого останова.
    node -- только на этом узле (#280)."""
    api = api or nomad
    rows = project_rows(project, api=api)
    jobs = {r["name"]: (r["job"], r["node"]) for r in rows}
    gone = []
    for name in plan_clear(rows, force, node):
        job, node = jobs[name]
        m = JobMeta.from_job(job)
        api.deregister(name, purge=False)
        puppets._wait_stopped(name)
        puppets.wipe(node, name)
        # Ветка мастера (#256) едет с остальной метой (#265): без неё папет
        # после сборки поднимался на origin/HEAD, а не на своей ветке.
        gone.append({"name": name, "origin": m.origin, "llm": llm.of_meta(m),
                     "branch": m.branch, "cred": m.cred, "node": node})
    return gone


def restore(gone, api=None):
    """Поднять снесённых заново — той же спекой, из нового образа. Зовётся
    и после неудачной сборки: старый образ на месте, папетам есть из чего
    клонироваться.

    Каждого, а не до первого отказа (#188): остановка на первом оставляла
    остальных снятых лежать, и об этом не говорил никто. Отказы -- одним
    исключением, по строке на папета, с именем (#182): голый отказ Nomad не
    говорил, кого из снятых не подняли."""
    failed = []
    for p in gone:
        try:
            (api or nomad).register(spec.respec(p["name"], JobMeta(p["origin"], p["llm"],
                                                          p.get("branch"), cred=p.get("cred"))))
        except Exception as e:
            failed.append(f"{p['name']} on {p.get('node') or '?'}: "
                          f"not raised again: {e}")
    if failed:
        raise RuntimeError("\n".join(failed))


def build(origin, got, out=None, fresh=False, force=False, on_line=None,
          on_step=None, api=None, node=None):
    """Вся сборка как операция над проектом: снять тела → плейбук → поднять
    папетов заново → объявить образ. -> {rc, gone, announced}.

    node -- сборка на одном узле (#280): снимаются тела проекта только там,
    плейбук играет только там, образ объявляется только ему. Узел, которого
    в пуле нет или чьи тела не контейнеры, -- RuntimeError до первого
    останова.

    Одна дорога на оба фронтенда (`mop driver build`, инструмент build в
    MCP). Папеты поднимаются заново при ЛЮБОМ исходе плейбука: при отказе
    старый образ на месте, и оставить их снятыми значило бы наказать проект
    за неудачную сборку дважды. Объявление — только после успеха.

    on_step — имя этапа вызывающему (сборщик шлёт его просителю, #123).
    api — Nomad (nomad.NomadApi, #275), по умолчанию живой."""
    step = on_step or (lambda _s: None)
    # Проверка узла -- один раз (#321): её же итог объявляет образ ниже.
    nodes = container_nodes(api or nomad, node) if node else None
    step("stopping the project's bodies")
    gone = clear(got["project"], force, api=api, node=node)
    try:
        rc = bake(origin, got, out, fresh, on_line, node=node)
    finally:
        if gone:
            step("raising the project's puppets again")
        restore(gone, api=api)
    if rc == 0:
        step("announcing the image to the nodes")
    announced = announce(got["project"], api=api, node=node, nodes=nodes) if rc == 0 else []
    return {"rc": rc, "gone": gone, "announced": announced}


def container_nodes(api, node=None):
    """Узлы пула по мете Nomad -> {узел: мета}: без node -- все (announce
    отбирает сам и называет прочие строкой). С node -- только он, и
    RuntimeError, если такого узла нет или его тела не контейнеры (#280):
    образ строится и объявляется только там, где тело -- клон шаблона.

    Единственная проверка «контейнерный ли узел пула» (#321): её зовут и
    build, и сборщик (`mop project add --node`) -- у него была своя, с
    другими словами и ValueError."""
    meta = api.nodes_meta()
    if node is not None:
        if node not in meta:
            raise RuntimeError(f"no node {node} in the pool: "
                               f"{', '.join(sorted(meta)) or 'none'}")
        if not driver.is_container(driver.of_node(meta[node], node)):
            raise RuntimeError(f"{node}: its bodies are not containers, "
                               f"there is no image to build there")
        return {node: meta[node]}
    return meta


def announce(project, api=None, node=None, nodes=None):
    """Сказать кластеру, что образ этого проекта на узлах собран.
    -> [(узел, 'announced'|'already announced'|'not a container node')].
    node -- только ему (#280): на остальных образ не собирали; nodes --
    итог container_nodes, если он уже есть.

    Без этого планировщик про образ не знает, и папет проекта на узел не
    сядет — ограничение в спеке смотрит именно на этот перечень. Отдельным
    шагом после плейбука, а не задачей в нём: токен Nomad есть только у
    управляющей машины, и тащить его в плейбук, который ходит на узлы,
    значит вернуть то, ради чего заводили шину.

    Только после успешной сборки: объявить образ, которого нет, — это
    папет, висящий pending, и ровно тот отказ, от которого ограничение и
    заводилось."""
    api = api or nomad
    out = []
    # nodes -- уже проверенные build'ом (#321): второй раз не спрашиваем.
    for name, meta in sorted((nodes if nodes is not None
                              else container_nodes(api, node)).items()):
        # Неизвестный драйвер -- строка с отказом, остальные узлы дальше (#175).
        try:
            container = driver.is_container(driver.of_node(meta, name))
        except RuntimeError as e:
            out.append((name, str(e)))
            continue
        if not container:
            out.append((name, "not a container node"))  # объявляет any и так
            continue
        # Перечень берём с узла: серверная копия отстаёт на секунды, и
        # дописать к устаревшей значит стереть ранее объявленные образы.
        have = [s for s in (api.node_dynamic_meta(name).get(spec.META_PROJECTS) or "").split(",") if s]
        if project in have:
            out.append((name, "already announced"))
            continue
        api.set_node_meta(name, {spec.META_PROJECTS: ",".join(sorted(have + [project]))})
        out.append((name, f"announced, serves {', '.join(sorted(have + [project]))}"))
    return out
