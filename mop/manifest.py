"""Манифест проекта: .mop/ в корне проекта, из его origin или из рабочей копии.

Чистый разбор и его проверки живут в config (manifest, manifest_parts);
здесь — дорога за ним: зеркало или каталог, файлы для ansible. Молчит и
печатает вызывающий: библиотека возвращает данные.

Два файла, и имена говорят, когда каждый играется (#61):

    .mop/sandbox.yaml    размеры тела (vars по конвенции PROJECT_SCOPED),
                         конфигурация проекта (остальные vars) и системные
                         пакеты (tasks) — ПЕЧЁТСЯ в образ проекта; на узле,
                         где тело равно узлу, играется прогоном deploy
    .mop/bootstrap.yaml  env-файлы и настройка окружения — играется при
                         КАЖДОМ старте песочницы (#62)

Раньше файлы звались node.yaml и workspace.yaml и обещали обратное тому,
что делал механизм: workspace пёкся в образ, node играл на машине-узле (на
гипервизоре — на самом Proxmox, где папета нет), а при старте не играло
ничто. Старые имена читаются переходно — оба как sandbox, ровно так, как они
и работали, — и возвращаются в `legacy`, чтобы вызывающий сказал об этом
вслух: непереехавший проект не должен молча остаться без манифеста. Порядок
переезда обязателен: mop читает оба имени → переезжают проекты → старое имя
снимается.

Тяжёлое — в sandbox: то, что лежит в bootstrap, платится на каждом подъёме
папета. Файлы, а не значения в аргументах: задачи и конфигурация манифеста —
структура, и пусть её кладёт yaml, а не json в командной строке.
"""
import os
import subprocess

from . import config, puppets

SANDBOX = ".mop/sandbox.yaml"
BOOTSTRAP = ".mop/bootstrap.yaml"
# Переходные имена: читаются как sandbox, пока проекты не переехали.
LEGACY = (".mop/node.yaml", ".mop/workspace.yaml")


def fetch(origin):
    """origin -> словарь манифестов проекта: {'project', 'asks', 'alien',
    'legacy', 'sandbox_vars', 'sandbox_tasks', 'bootstrap_vars',
    'bootstrap_tasks'} (пути или None).

    Нет файла — соответствующая часть None: большинству проектов хватает
    общего. Ошибки — громкие: недоступный origin и кривая форма поднимают
    RuntimeError, их переводит в выход вызывающий.

    Зеркало, а не рабочий клон: манифест принадлежит репозиторию, и origin —
    единственная его правда для deploy, у которого рабочей копии чужого
    проекта нет вовсе. Читается HEAD зеркала, то есть дефолтная ветка.
    """
    project = puppets.project_of(origin)
    import tempfile
    with_dir = tempfile.mkdtemp(prefix=f"mop-mirror-{project}-")
    tmp = os.path.join(with_dir, "mirror.git")
    try:
        r = subprocess.run(["git", "clone", "--mirror", "-q", origin, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"cannot read {project}: "
                               f"{(r.stderr or r.stdout).strip()}")

        def show(path):
            got = subprocess.run(["git", "-C", tmp, "show", f"HEAD:{path}"],
                                 capture_output=True, text=True)
            return got.stdout if got.returncode == 0 else None
        return _collect(project, show)
    finally:
        subprocess.run(["rm", "-rf", with_dir], capture_output=True)


def fetch_tree(root, project):
    """Те же манифесты из рабочей копии root: файлы как лежат, незакоммиченные
    тоже (#46). Ради этого сборка и заводилась в рабочей копии: правку `.mop`
    пробуют образом, а не влитием в master. Новой власти это не даёт — у того,
    кто собирает, и так ssh на узлы; тому, кто нет, `.mop` не поможет."""
    def show(path):
        p = os.path.join(root, path)
        if not os.path.exists(p):
            return None
        with open(p) as f:
            return f.read()
    return _collect(project, show)


def _collect(project, show):
    """Словарь манифестов из источника show: path -> текст или None."""
    # base с PID: вырезка происходит и в deploy, и в сборке, и руками, и
    # один путь на всех однажды столкнул два клона в один tmp_pack (#32).
    # Файлы для ansible живут в base и переживают вызов — /tmp вычищается
    # перезагрузкой, мусор копится только до неё.
    base = f"/tmp/mop-manifest-{project}-{os.getpid()}"
    out = {"project": project, "asks": {}, "alien": [], "legacy": [],
           "sandbox_vars": None, "sandbox_tasks": None,
           "bootstrap_vars": None, "bootstrap_tasks": None, "bootstrap_text": None}

    def note_alien(names):
        out["alien"] += [k for k in names if k not in out["alien"]]

    # Песочница: новое имя, а за ним — старые, слитые в неё в том порядке, в
    # каком они играли: узловое прежде рабочего.
    s_vars, s_tasks = {}, []
    for path in (SANDBOX, *LEGACY):
        got = _parse(show(path), project, path)
        if got is None:
            continue
        if path in LEGACY:
            out["legacy"].append(path)
        mvars, tasks = got
        asks, mine, alien = config.manifest_parts(mvars)
        out["asks"].update(asks)
        s_vars.update(mine)
        note_alien(alien)
        s_tasks += tasks
    if s_vars:
        out["sandbox_vars"] = _write(base, project, "sandbox-vars.yml", None, s_vars)
    out["sandbox_tasks"] = _write(base, project, "sandbox-tasks.yml", s_tasks)

    # Bootstrap: размеров здесь не просят — они дело песочницы, и просьба
    # тут была бы проглочена молча, поэтому идёт в чужие, по имени.
    # Сырой текст -- тоже (#124): сервер принимает bootstrap глаголом `put`
    # текстом, и `mop project add` кладёт его туда сам, без прогона.
    out["bootstrap_text"] = show(BOOTSTRAP)
    got = _parse(out["bootstrap_text"], project, BOOTSTRAP)
    if got is not None:
        bvars, btasks = got
        asks, mine, alien = config.manifest_parts(bvars)
        note_alien(sorted(set(alien) | set(asks)))
        if mine:
            out["bootstrap_vars"] = _write(base, project, "bootstrap-vars.yml", None, mine)
        out["bootstrap_tasks"] = _write(base, project, "bootstrap-tasks.yml", btasks)
    out["alien"].sort()
    return out


def _parse(text, project, path):
    """Один манифест. -> (vars, tasks) или None, если файла нет."""
    if text is None:
        return None
    try:
        return config.manifest(text)
    except ValueError as e:
        raise RuntimeError(f"{project}/{path}: {e}")


def _write(base, project, name, tasks=None, of_vars=None):
    """Секция манифеста во временный файл для ansible; None, если пусто."""
    import yaml
    what = of_vars if of_vars is not None else tasks
    if not what:
        return None
    os.makedirs(base, exist_ok=True)
    path = os.path.join(base, name)
    with open(path, "w") as f:
        yaml.safe_dump(what, f, allow_unicode=True, default_flow_style=False)
    return path
