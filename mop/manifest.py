"""Манифест шарда: mop.yaml в корне проекта, прочитанный из его origin.

Чистый разбор и его проверки живут в config (manifest, manifest_parts);
здесь — дорога за ним: зеркало, git show, файлы для ansible. Молчит и
печатает вызывающий: библиотека возвращает данные.

Файлы, а не значения в аргументах: задачи и конфигурация манифеста —
СТРУКТУРА, и пусть её кладёт yaml, а не json в командной строке.
"""
import os
import subprocess

from . import config


def fetch(origin):
    """origin -> словарь манифестов проекта: {'shard', 'asks', 'alien',
    'node_tasks', 'ws_vars', 'ws_tasks'} (пути или None).

    Два манифеста в .mop/ репозитория (#32): node.yaml — что проекту нужно
    от УЗЛА (ресурсы в vars по конвенции SHARD_SCOPED, узловые задачи в
    tasks), workspace.yaml — что нужно его ТЕЛУ (конфигурация в vars,
    окружение в tasks; копирование файлов — задачами copy, БЕЗ отдельных
    конвенций: формат ansible уже декларативен).

    Нет файла — соответствующая часть None: большинству проектов хватает
    общего. Ошибки — громкие: недоступный origin и кривая форма поднимают
    RuntimeError, их переводит в выход вызывающий.

    Зеркало, а не рабочий клон: манифест принадлежит РЕПОЗИТОРИЮ, и origin —
    единственная его правда; рабочая копия на управляющей машине может быть
    грязной или вчерашней.
    """
    shard = os.path.basename(origin).removesuffix(".git")
    # base с PID: вырезка происходит и в deploy, и в сборке, и руками, и
    # ОДИН путь на всех однажды столкнул два клона в один tmp_pack (#32).
    # Файлы для ansible живут в base и переживают вызов — /tmp вычищается
    # перезагрузкой, мусор копится только до неё.
    base = f"/tmp/mop-manifest-{shard}-{os.getpid()}"
    import tempfile
    with_dir = tempfile.mkdtemp(prefix=f"mop-mirror-{shard}-")
    tmp = os.path.join(with_dir, "mirror.git")
    try:
        r = subprocess.run(["git", "clone", "--mirror", "-q", origin, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"cannot read {shard}: "
                               f"{(r.stderr or r.stdout).strip()}")
        out = {"shard": shard, "asks": {}, "alien": [],
               "node_tasks": None, "ws_vars": None, "ws_tasks": None}
        node = _read(tmp, shard, ".mop/node.yaml")
        if node is not None:
            nvars, ntasks = node
            out["asks"], _, out["alien"] = config.manifest_parts(nvars)
            out["node_tasks"] = _write(base, shard, "node-tasks.yml", ntasks)
        ws = _read(tmp, shard, ".mop/workspace.yaml")
        if ws is not None:
            wvars, wtasks = ws
            _, mine, alien = config.manifest_parts(wvars)
            out["alien"] += [k for k in alien if k not in out["alien"]]
            if mine:
                out["ws_vars"] = _write(base, shard, "ws-vars.yml", None, mine)
            out["ws_tasks"] = _write(base, shard, "ws-tasks.yml", wtasks)
        return out
    finally:
        subprocess.run(["rm", "-rf", with_dir], capture_output=True)


def _read(tmp, shard, path):
    """Один манифест из зеркала. -> (vars, tasks) или None, если файла нет."""
    got = subprocess.run(["git", "-C", tmp, "show", f"HEAD:{path}"],
                         capture_output=True, text=True)
    if got.returncode != 0:
        return None
    try:
        return config.manifest(got.stdout)
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
