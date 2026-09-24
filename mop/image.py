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

from . import config, driver, llm, manifest, nomad, playvars, puppets, spec, state

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


def bake(origin, got, out=None, fresh=False, on_line=None):
    """Прогнать плейбук сборки. -> код возврата ansible.

    out — куда писать вывод плейбука: файл (MCP пишет в журнал и присылает
    хвост вестью) либо None (терминал видит прогон живьём). Инвентарь — из
    окружения, его ставит диспетчер `mop` для всех командлетов.

    fresh — начисто, с базового образа. Умолчание — инкремент (#60): шаблон
    клонируется в сборочное тело, плейбук играется там и качает только
    новое, и лишь потом образ подменяется. Инкремент не даёт чистоты: то,
    чего в плейбуке уже нет, в образе останется — за этим и остаётся fresh.

    on_line — вместо out: каждая строка вывода отдаётся вызывающему (сборщик
    на сервере показывает по ним шаг, #123)."""
    settings = json.dumps(playvars.playbook_vars(), ensure_ascii=False)
    extra = extra_vars(origin, got)
    if fresh:
        extra["mop_fresh"] = True
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
def plan_clear(rows, force=False):
    """Кого сносить перед сборкой. rows: [{name, node, container, kind}]
    -> [имя]; RuntimeError с именами, если кто-то занят и не force.

    Тела на узлах, где тело равно узлу, сборки не касаются. Свободен —
    только тот, о ком это сказано прямо (state.is_free): молчащий агент
    и папет без состояния читаются как занятые, потому что снос под живой
    работой хуже отказа."""
    mine = [r for r in rows if r.get("container")]
    busy = [r["name"] for r in mine if not state.is_free(r.get("kind"))]
    if busy and not force:
        raise RuntimeError(
            f"rebuilding the image destroys the project's bodies, and these are "
            f"not free: {', '.join(busy)} — wait, or mop driver build --force")
    return [r["name"] for r in mine]


def project_rows(project):
    """Папеты проекта, как их видит plan_clear: [{name, node, container,
    state, kind, job}]. Не размещённые (без аллокации) не считаются: тела у них
    нет, снимать нечего."""
    meta = nomad.nodes_meta()
    rows = []
    for item in puppets.roster():
        job = item["job"]
        if puppets.project_of((job.get("Meta") or {}).get("origin") or "") != project:
            continue
        node = item["alloc"]["NodeName"] if item["alloc"] else None
        if not node:
            continue
        drv = driver.of_node(meta.get(node), node)
        rows.append({"name": job["ID"], "node": node, "job": job,
                     "container": driver.is_container(drv),
                     "state": item["state"], "kind": item["kind"]})
    return rows


def clear(project, force=False):
    """Остановить папетов проекта на контейнерных узлах и снести их тела.
    -> [{name, origin, llm, node}] — кого поднять заново после сборки.
    Отказ по занятым — RuntimeError из plan_clear, до первого останова."""
    rows = project_rows(project)
    jobs = {r["name"]: (r["job"], r["node"]) for r in rows}
    gone = []
    for name in plan_clear(rows, force):
        job, node = jobs[name]
        m = job.get("Meta") or {}
        nomad.deregister(name, purge=False)
        puppets._wait_stopped(name)
        puppets.wipe(node, name)
        gone.append({"name": name, "origin": m.get("origin"),
                     "llm": llm.resolve(m.get("llm")),
                     "node": node})
    return gone


def restore(gone):
    """Поднять снесённых заново — той же спекой, из нового образа. Зовётся
    и после неудачной сборки: старый образ на месте, папетам есть из чего
    клонироваться."""
    for p in gone:
        try:
            nomad.register(spec.job_spec(p["name"], p["origin"], p["llm"]))
        except Exception as e:
            # С именем папета (#182): голый отказ Nomad не говорил, кого из
            # снятых не подняли.
            raise RuntimeError(f"{p['name']} on {p.get('node') or '?'}: "
                               f"not raised again: {e}") from e


def build(origin, got, out=None, fresh=False, force=False, on_line=None,
          on_step=None):
    """Вся сборка как операция над проектом: снять тела → плейбук → поднять
    папетов заново → объявить образ. -> {rc, gone, announced}.

    Одна дорога на оба фронтенда (`mop driver build`, инструмент build в
    MCP). Папеты поднимаются заново при ЛЮБОМ исходе плейбука: при отказе
    старый образ на месте, и оставить их снятыми значило бы наказать проект
    за неудачную сборку дважды. Объявление — только после успеха.

    on_step — имя этапа вызывающему (сборщик шлёт его просителю, #123)."""
    step = on_step or (lambda _s: None)
    step("stopping the project's bodies")
    gone = clear(got["project"], force)
    try:
        rc = bake(origin, got, out, fresh, on_line)
    finally:
        if gone:
            step("raising the project's puppets again")
        restore(gone)
    if rc == 0:
        step("announcing the image to the nodes")
    announced = announce(got["project"]) if rc == 0 else []
    return {"rc": rc, "gone": gone, "announced": announced}


def announce(project):
    """Сказать кластеру, что образ этого проекта на узлах собран.
    -> [(узел, 'announced'|'already announced'|'not a container node')].

    Без этого планировщик про образ не знает, и папет проекта на узел не
    сядет — ограничение в спеке смотрит именно на этот перечень. Отдельным
    шагом после плейбука, а не задачей в нём: токен Nomad есть только у
    управляющей машины, и тащить его в плейбук, который ходит на узлы,
    значит вернуть то, ради чего заводили шину.

    Только после успешной сборки: объявить образ, которого нет, — это
    папет, висящий pending, и ровно тот отказ, от которого ограничение и
    заводилось."""
    out = []
    for name, meta in sorted(nomad.nodes_meta().items()):
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
        have = [s for s in (nomad.node_dynamic_meta(name).get("mop_projects") or "").split(",") if s]
        if project in have:
            out.append((name, "already announced"))
            continue
        nomad.set_node_meta(name, {"mop_projects": ",".join(sorted(have + [project]))})
        out.append((name, f"announced, serves {', '.join(sorted(have + [project]))}"))
    return out
