"""make a project ready: mop project add [--update|--rebuild] [-v] [git-origin]

Without origin, the origin of the current working copy is used.

Registers the project on the server (operator's verb project_add)
and builds its image from .mop/sandbox.yaml on the container nodes through the server's builder when it is missing on
any of them. Afterwards `mop add` can place a puppet right away.

  --update    build the image even if it exists, incrementally
  --rebuild   build it from the base image
  -v          print every line of the build's output, not only on a terminal

Idempotent: run it again after changing the project's .mop. Silent when all
goes well; on a terminal it shows the current step under the last lines of
the build's output, and erases both when done.
"""
import sys

from mop.cli import lib
from mop import bus, manifest, puppets


# Инструмент MCP (#160): описание -- докстринг выше, вызов -- эта команда.
MCP = {"annotations": "destructive", "background": True, "args": [
    {"name": "origin", "type": "string", "help": "git origin; without it, the origin of the master's working copy"},
    {"name": "update", "type": "boolean", "flag": "--update", "help": "build the image even if it exists, incrementally"},
    {"name": "rebuild", "type": "boolean", "flag": "--rebuild", "help": "build the image from the base image"}]}


def main(argv):
    mode = "missing"
    verbose = False
    args = []
    for a in argv:
        if a in ("-v", "--verbose"):
            verbose = True
        elif a in ("--update", "--rebuild"):
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
    p = lib.Progress(name, verbose)
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
    try:
        bus.call_cluster("project_add", project=bus.ADMIN, origin=origin)
    except bus.Refused as e:
        raise bus.Refused(f"{name}: {e}")

    p.step("image")
    for ev in bus.ask_stream(bus.build_subject(), "image builder", "build",
                             origin=origin, mode=mode):
        if ev.get("done"):
            if ev.get("error") or not ev.get("ok"):
                p.clear()
                # С -v строки уже напечатаны все, хвост повторил бы их.
                for line in [] if p.verbose else ev.get("tail") or []:
                    print(line, file=sys.stderr)
                lib.fail(f"{name}: {ev.get('error') or 'image build failed'}")
                return 1
            return 0
        if ev.get("lines"):
            p.log(ev["lines"])
        if "step" in ev:
            p.step(f"image: {ev['step']}")
    return 0


# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
