"""create a puppet: mop add [--llm PROFILE] [git-origin]

Without origin, the origin of the current working copy is used. The name is
picked automatically: <project>-<number>. Nomad decides placement — a puppet
reserves 8 GB from the pool.
"""
import os
import time

from mop.cli import lib
from mop import bootstrap, bus, config, puppets


def main(argv):
    profile, args = lib.parse_llm(argv)
    if len(args) > 1:
        lib.usage(__doc__)
    profile = profile or config.get("MOP_DEFAULT_LLM")
    origin = args[0] if args else lib.cwd_origin()
    shard = puppets.shard_of(origin)
    # Курица и яйцо: у нового проекта ещё нет пользователя в конфиге NATS, и
    # папет поднимется, но к шине не подключится — прочитается как «агент
    # молчит» на пустом месте. Лучше отказать здесь, чем разбираться там.
    if not lib.shard_ready(shard):
        lib.usage(f"project {shard} isn't on the bus yet.\n"
                  f"Register it on the server: mop project add {origin}")
    lib.push_llm_keys(profile)
    # bootstrap.yaml рабочей копии — на сервер ДО регистрации (#62): первый
    # подъём обязан увидеть его. Из рабочей копии, потому что решает мастер;
    # нет файла — сервер держит то, что положил deploy из origin.
    if not args and os.path.exists(bootstrap.FILE):
        try:
            got = bootstrap.push_from(os.getcwd(), shard)
            print(f"  {bootstrap.FILE}: on the server, {got.get('tasks', 0)} task(s)"
                  + (f"; ignored: {', '.join(got['alien'])}" if got.get("alien") else ""))
        except bus.BusError as e:
            print(f"  {bootstrap.FILE} did not reach the server: {e}")
    # Имя выбирает сервис кластера вместе с регистрацией: спека собирается
    # там же (#80), а выбор имени и есть первая её строка.
    got = bus.ask_cluster("add", origin=origin, profile=profile, timeout=30)
    if got.get("error"):
        lib.fail(got["error"])
        return 1
    name = got["name"]
    print(f"{name}: {origin} [{profile}]")

    node = None
    for _ in range(120):
        a, _ = lib.alloc_of(name)
        if a:
            if node is None:
                node = a["NodeName"]
                print(f"  → {node}")
            if a["ClientStatus"] == "running":
                print("  → running")
                return
            if a["ClientStatus"] == "failed":
                print("  → FAILED, check: mop list / nomad UI")
                return
        time.sleep(1)
    print(f"  → still {'pending — no free slots?' if node is None else 'not running'}, "
          f"check mop list")



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
