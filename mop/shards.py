"""Шарды пула: кто заведён. Данные, без печати (печатает `mop shards`).

Шард — проект: один репозиторий, один срез пула, один мастер. Список
строится из трёх источников всегда объединением, никогда заменой:

  * ростер Nomad — у кого живой джоб;
  * память ~/.config/mop/shards — у кого он БЫЛ;
  * аргументы — новый проект, у которого папета ещё нет.

Память нужна ровно из-за второго пункта: без неё удаление последнего папета
проекта молча вычеркнуло бы его пользователя из конфига NATS на следующем
deploy, и работающий мастер перестал бы подключаться. Память хранит
ORIGIN'Ы, а не имена (#33): имя выводится basename'ом, а имя в origin не
разворачивается; от легаси-времён в ней остаются голые имена.
"""
import os

from . import bus, nomad, puppets

FILE = os.path.expanduser("~/.config/mop/shards")


def remembered():
    try:
        with open(FILE) as f:
            return {l.strip() for l in f if l.strip()}
    except OSError:
        return set()


def collect(extra=()):
    """Все шарды, какие система знает. -> ({origin'ы}, {легаси-имена},
    [предупреждения]).

    Ростер недоступен — работаем по памяти, но говорим об этом строкой:
    молча сузить список значит выписать шард из конфига NATS."""
    origins, legacy = puppets.shard_ids(
        l for l in remembered() if not l.startswith("-"))
    for arg in extra:
        if arg.startswith("-"):
            continue
        if "/" in arg or ":" in arg:
            origins.add(arg)
        else:
            legacy.add(arg)
    warnings = []
    try:
        origins |= {j["Meta"]["origin"] for j in puppets.jobs(shard=bus.ADMIN)
                    if (j.get("Meta") or {}).get("origin")}
    except Exception as e:
        warnings.append(f"Nomad roster unavailable ({nomad.describe_error(e)}), "
                        f"using only remembered shards")
    return origins, legacy, warnings


def names(origins, legacy):
    """Имена шардов для плейбука: basename origin'ов плюс легаси, по порядку."""
    return sorted({puppets.shard_of(o) for o in origins} | set(legacy))


def remember(lines):
    os.makedirs(os.path.dirname(FILE), exist_ok=True)
    with open(FILE, "w") as f:
        f.write("".join(f"{s}\n" for s in sorted(lines)))
