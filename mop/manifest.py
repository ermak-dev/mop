"""Манифест шарда: .mop/ в корне проекта, из его origin или из рабочей копии.

Чистый разбор и его проверки живут в config (manifest, manifest_parts);
здесь — дорога за ним: зеркало или каталог, файлы для ansible. Молчит и
печатает вызывающий: библиотека возвращает данные.

Файлы, а не значения в аргументах: задачи и конфигурация манифеста —
структура, и пусть её кладёт yaml, а не json в командной строке.
"""
import os
import subprocess

from . import config

NODE = ".mop/node.yaml"
WORKSPACE = ".mop/workspace.yaml"


def fetch(origin):
    """origin -> словарь манифестов проекта: {'shard', 'asks', 'alien',
    'node_tasks', 'ws_vars', 'ws_tasks'} (пути или None).

    Два манифеста в .mop/ репозитория (#32): node.yaml — что проекту нужно
    от узла (ресурсы в vars по конвенции SHARD_SCOPED, узловые задачи в
    tasks), workspace.yaml — что нужно его телу (конфигурация в vars,
    окружение в tasks; копирование файлов — задачами copy, без отдельных
    конвенций: формат ansible уже декларативен).

    Нет файла — соответствующая часть None: большинству проектов хватает
    общего. Ошибки — громкие: недоступный origin и кривая форма поднимают
    RuntimeError, их переводит в выход вызывающий.

    Зеркало, а не рабочий клон: манифест принадлежит репозиторию, и origin —
    единственная его правда для deploy, у которого рабочей копии чужого
    проекта нет вовсе. Читается HEAD зеркала, то есть дефолтная ветка.
    """
    shard = os.path.basename(origin).removesuffix(".git")
    import tempfile
    with_dir = tempfile.mkdtemp(prefix=f"mop-mirror-{shard}-")
    tmp = os.path.join(with_dir, "mirror.git")
    try:
        r = subprocess.run(["git", "clone", "--mirror", "-q", origin, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"cannot read {shard}: "
                               f"{(r.stderr or r.stdout).strip()}")

        def show(path):
            got = subprocess.run(["git", "-C", tmp, "show", f"HEAD:{path}"],
                                 capture_output=True, text=True)
            return got.stdout if got.returncode == 0 else None
        return _collect(shard, show)
    finally:
        subprocess.run(["rm", "-rf", with_dir], capture_output=True)


def fetch_tree(root, shard):
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
    return _collect(shard, show)


def _collect(shard, show):
    """Словарь манифестов из источника show: path -> текст или None."""
    # base с PID: вырезка происходит и в deploy, и в сборке, и руками, и
    # один путь на всех однажды столкнул два клона в один tmp_pack (#32).
    # Файлы для ansible живут в base и переживают вызов — /tmp вычищается
    # перезагрузкой, мусор копится только до неё.
    base = f"/tmp/mop-manifest-{shard}-{os.getpid()}"
    out = {"shard": shard, "asks": {}, "alien": [],
           "node_tasks": None, "ws_vars": None, "ws_tasks": None}
    node = _parse(show(NODE), shard, NODE)
    if node is not None:
        nvars, ntasks = node
        out["asks"], _, out["alien"] = config.manifest_parts(nvars)
        out["node_tasks"] = _write(base, shard, "node-tasks.yml", ntasks)
    ws = _parse(show(WORKSPACE), shard, WORKSPACE)
    if ws is not None:
        wvars, wtasks = ws
        _, mine, alien = config.manifest_parts(wvars)
        out["alien"] += [k for k in alien if k not in out["alien"]]
        if mine:
            out["ws_vars"] = _write(base, shard, "ws-vars.yml", None, mine)
        out["ws_tasks"] = _write(base, shard, "ws-tasks.yml", wtasks)
    return out


def _parse(text, shard, path):
    """Один манифест. -> (vars, tasks) или None, если файла нет."""
    if text is None:
        return None
    try:
        return config.manifest(text)
    except ValueError as e:
        raise RuntimeError(f"{shard}/{path}: {e}")


def _write(base, shard, name, tasks=None, of_vars=None):
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
