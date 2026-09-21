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
    """origin -> (имя шарда, просьбы, путь vars, путь tasks, чужие имена).

    Нет mop.yaml — (имя, {}, None, None, []): большинству проектов хватает
    общего, и отсутствие манифеста — штатный случай, а не ошибка. Ошибки —
    громкие: недоступный origin и кривая форма поднимают RuntimeError, их
    переводит в выход вызывающий.

    Зеркало, а не рабочий клон: манифест принадлежит РЕПОЗИТОРИЮ, и origin —
    единственная его правда; рабочая копия на управляющей машине может быть
    грязной или вчерашней.
    """
    shard = os.path.basename(origin).removesuffix(".git")
    base = f"/tmp/mop-manifest-{shard}"
    tmp = f"{base}.git"
    subprocess.run(["rm", "-rf", tmp], capture_output=True)
    try:
        r = subprocess.run(["git", "clone", "--mirror", "-q", origin, tmp],
                           capture_output=True, text=True)
        if r.returncode != 0:
            raise RuntimeError(f"cannot read {shard}: "
                               f"{(r.stderr or r.stdout).strip()}")
        got = subprocess.run(["git", "-C", tmp, "show", "HEAD:mop.yaml"],
                             capture_output=True, text=True)
        if got.returncode != 0:
            return shard, {}, None, None, []
        try:
            mvars, tasks = config.manifest(got.stdout)
        except ValueError as e:
            raise RuntimeError(f"{shard}/mop.yaml: {e}")
        asks, mine, alien = config.manifest_parts(mvars)
        import yaml
        os.makedirs(base, exist_ok=True)
        vars_path = tasks_path = None
        if mine:
            vars_path = os.path.join(base, "vars.yml")
            with open(vars_path, "w") as f:
                yaml.safe_dump(mine, f, allow_unicode=True)
        if tasks:
            tasks_path = os.path.join(base, "tasks.yml")
            with open(tasks_path, "w") as f:
                yaml.safe_dump(tasks, f, allow_unicode=True,
                               default_flow_style=False)
        return shard, asks, vars_path, tasks_path, alien
    finally:
        subprocess.run(["rm", "-rf", tmp], capture_output=True)
