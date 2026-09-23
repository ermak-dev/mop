"""mop driver build [project|origin] [--fresh] [--force]: bake the project's image

On the hypervisors, from the control machine. No argument: this working
copy, .mop as it lies (#46). Incremental by default: a copy of the image
takes what changed; --fresh builds from the base image anew. The project's
puppets on container nodes are stopped, their bodies destroyed and raised
again from the new image; a busy puppet refuses unless --force (#60).
"""
import sys

from mop.cli import lib
from mop import bus, image, puppets

def main(argv):
    """Собрать образ проекта на узлах с контейнерным драйвером.

    Сборкой, а не подъёмом из базового образа на месте: падение плейбука видно
    при сборке, а не в три часа ночи внутри врапера. Дальше каждое тело —
    клон шаблона, на lvm-thin связанный и мгновенный.

    Без аргумента — эта рабочая копия, и `.mop` берётся из её файлов как
    лежат, без коммита и без push (#46): правку манифеста пробуют образом, а
    не влитием в master. Зеркало в образ по-прежнему снимается с origin —
    тела клонируют репозиторий, не копию. С аргументом — дефолтная ветка
    origin, как в deploy: там нет рабочей копии, которая сказала бы иначе."""
    fresh, force = "--fresh" in argv, "--force" in argv
    argv = [a for a in argv if a not in ("--fresh", "--force")]
    if len(argv) > 1:
        lib.usage(__doc__)
    if argv:
        origin, root = origin_of(argv[0]), None
    else:
        origin = lib.origin(None, __doc__)
        root = lib.git("rev-parse", "--show-toplevel")
    # Дорога сборки живёт в библиотеке (mop/image.py): у неё двое
    # вызывающих, этот командлет и инструмент build в MCP. Здесь печать.
    got = image.prepare(origin, root)
    project = got["project"]
    print(f"  {project}/.mop read from "
          f"{'this working copy' if root else 'origin, default branch'}")
    for k, v in sorted(got["asks"].items()):
        print(f"  {project}/.mop/sandbox.yaml asks for {k}={v}")
    for k in got["alien"]:
        # Громко: проглоченный ключ -- это либо настройка, которая не
        # сработала, либо чужая, которая сработала.
        print(f"  {project}/.mop: {k} is not a project's to set — ignored")
    for old in got["legacy"]:
        print(f"  {project}/{old}: read as .mop/sandbox.yaml for the transition "
              f"(#61) — rename it, the old name will stop being read")
    # Пересборка — операция над проектом (#60): тела проекта на контейнерных
    # узлах снимаются до плейбука и поднимаются заново после, при любом
    # исходе. Занятый папет — отказ до первого останова, если не --force.
    r = image.build(origin, got, fresh=fresh, force=force)
    for p in r["gone"]:
        print(f"  {p['name']} on {p['node']}: body destroyed, puppet raised again "
              f"{'from the new image' if r['rc'] == 0 else 'from the old image'}")
    for node, what in r["announced"]:
        print(f"  {node}: {what}")
    return r["rc"]


def origin_of(arg):
    """ORIGIN проекта: им же и сказали, либо ищем по имени. Громко, если нет.

    Образу нужен origin, а не имя: из зеркала репозитория растут клоны всех
    тел проекта. Обратного отображения «имя -> origin» в системе нет и заводить
    его нельзя — проект определяется ровно одним способом, basename origin без
    .git, и таблица имён рядом однажды разошлась бы с ним. Поэтому не таблица,
    а поиск по тому, что уже есть: джобы проекта и рабочая копия под рукой."""
    if puppets.looks_like_origin(arg):
        return arg                      # это и есть origin
    for job in puppets.jobs(project=bus.ADMIN):
        o = (job.get("Meta") or {}).get("origin") or ""
        if o and puppets.project_of(o) == arg:
            return o
    here = lib.cwd_origin()
    if here and puppets.project_of(here) == arg:
        return here
    sys.exit(f"don't know where project {arg} comes from: it has no puppets and "
             f"this working copy is a different project.\n"
             f"Name it outright: mop driver build <git-origin>")
