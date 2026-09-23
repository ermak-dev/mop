"""make a project ready: mop project add [--update|--rebuild] [git-origin]

Without origin, the origin of the current working copy is used.

Registers the project on the server (operator's verb project_add, #117)
and builds its image from .mop/sandbox.yaml on the container nodes through the server's builder (#123) when it is missing on
any of them. Afterwards `mop add` can place a puppet right away.

  --update    build the image even if it exists, incrementally
  --rebuild   build it from the base image

Idempotent: run it again after changing the project's .mop. Silent when all
goes well; on a terminal it shows the current step.
"""
import sys

from mop.cli import lib
from mop import bus, manifest, puppets


def main(argv):
    mode = "missing"
    args = []
    for a in argv:
        if a in ("--update", "--rebuild"):
            if mode != "missing":
                lib.usage(__doc__)
            mode = a[2:]
        elif a.startswith("-"):
            lib.usage(__doc__)
        else:
            args.append(a)
    if len(args) > 1:
        lib.usage(__doc__)
    origin = lib.origin(args[0] if args else None, __doc__)
    name = puppets.project_of(origin)
    p = lib.Progress(name)
    try:
        return _add(origin, name, mode, p)
    finally:
        p.clear()


def _add(origin, name, mode, p):
    # Недоступный origin валит команду до сервера: заведённый проект обязан
    # существовать. Читает его машина оператора -- у неё и есть доступ.
    p.step("reading .mop")
    got = manifest.fetch(origin)

    p.step("registering on the bus")
    ans = bus.ask_cluster("project_add", project=bus.ADMIN, origin=origin)
    if ans.get("error"):
        p.clear()
        lib.fail(f"{name}: {ans['error']}")
        return 1

    p.step("image")
    for ev in bus.ask_stream(bus.build_subject(), "image builder", "build",
                             origin=origin, mode=mode):
        if ev.get("done"):
            if ev.get("error") or not ev.get("ok"):
                p.clear()
                for line in ev.get("tail") or []:
                    print(line, file=sys.stderr)
                lib.fail(f"{name}: {ev.get('error') or 'image build failed'}")
                return 1
            return 0
        p.step(f"image: {ev.get('step', '')}")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
