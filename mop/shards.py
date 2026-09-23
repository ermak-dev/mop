"""Реестр проектов пула: кто заведён. Данные, без печати (печатает `mop project`).

Проект (он же шард) — один репозиторий, один срез пула, один мастер. По
этому списку плейбук заводит пользователей NATS `master-<проект>` и
`puppet-<проект>`: без записи здесь мастер не подключится к шине, а папет
поднимется и будет читаться как «агент молчит» на пустом месте.

Раньше списка как сущности не было (#79). Он собирался при каждом прогоне
объединением трёх источников — ростера Nomad, памяти контроллера и
аргументов `mop deploy <origin>`, — и завод проекта был побочным эффектом
прогона всей установки. Отсюда же росли оба костыля: память нужна была
затем, что удаление последнего папета вычёркивало проект из конфига NATS, а
объединение никогда не заменой — затем, что недоступный ростер сузил бы
список молча. Снять проект было нельзя вовсе.

Теперь список — реестр: файл контроллера, который правят `mop project add` и
`mop project delete`, и единственный ответ на вопрос «какие проекты
заведены». Ростер и аргументы в него больше не вливаются; память переносится
в него один раз (merged) и дальше не читается.

Реестр хранит ORIGIN'Ы (#33): имя выводится basename'ом, а имя в origin не
разворачивается — таблицы имён нет и заводить нельзя. От легаси-времён в нём
остаются голые имена, origin которых уже не узнать; завести новую такую
строку нельзя, а старую не теряем.
"""
import os

from . import bus, nomad, puppets

FILE = os.path.expanduser("~/.config/mop/projects")
# Память шардов до #79. Читается ровно один раз — при переносе в реестр.
MEMORY = os.path.expanduser("~/.config/mop/shards")


# ─── чистое: что реестр принимает, что теряет ────────────────────────────
def with_origin(origin, known):
    """Реестр после завода проекта. -> (реестр, добавлен?).

    Голое имя отвергаем: origin — единственная правда о проекте, из него
    режется манифест `.mop` и им же клонируется папет. Приняв имя, реестр
    завёл бы пользователя на шине для проекта, которого не достать."""
    origin = (origin or "").strip()
    if not any(c in origin for c in ":/"):
        raise RuntimeError(f"{origin!r} doesn't look like a git-origin: "
                           f"a project is registered by its origin, not by its name")
    return known | {origin}, origin not in known


def without_project(name, known):
    """Реестр после снятия проекта по ИМЕНИ. -> (реестр, [снятые строки]).

    Имя проекта — basename origin'а, и оно же имя пользователя NATS: два
    origin'а с одним basename — один проект, и снимаются оба. Пустой
    вердикт означает «такого в реестре не было» и печатается как отказ:
    опечатка в имени иначе читалась бы как успешное снятие."""
    dropped = sorted(l for l in known
                     if l == name or puppets.shard_of(l) == name)
    return known - set(dropped), dropped


def merged(memory, roster):
    """Реестр из памяти контроллера и ростера Nomad — разовый перенос.

    Объединение, а не выбор: потерять строку значит выписать проект из
    конфига NATS на следующем прогоне, а вернуть его потом некому — память
    после переноса не читается."""
    return set(memory) | set(roster)


def names(origins, legacy):
    """Имена проектов для плейбука: basename origin'ов плюс легаси, по порядку."""
    return sorted({puppets.shard_of(o) for o in origins} | set(legacy))


# ─── файл реестра ────────────────────────────────────────────────────────
def read(path=FILE):
    """Строки файла множеством; нет файла — пусто."""
    try:
        with open(path) as f:
            return {l.strip() for l in f if l.strip()}
    except OSError:
        return set()


def write(lines, path=FILE):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write("".join(f"{s}\n" for s in sorted(lines)))


def roster_origins():
    """Origin'ы живых папетов. Нужны один раз — при переносе памяти."""
    return {j["Meta"]["origin"] for j in puppets.jobs(shard=bus.ADMIN)
            if (j.get("Meta") or {}).get("origin")}


def registry():
    """Реестр: -> ({origin'ы}, {легаси-имена}, примечание|None).

    Нет файла — переносим память и ростер, пишем и говорим об этом строкой
    (печатает командлет). Недоступный ростер здесь — отказ, а не
    предупреждение: перенос разовый, и пропущенный проект потом неоткуда
    взять."""
    note = None
    if not os.path.exists(FILE):
        memory = read(MEMORY)
        try:
            live = roster_origins()
        except Exception as e:
            raise RuntimeError(
                f"the project registry {FILE} doesn't exist yet and the Nomad "
                f"roster is unavailable ({nomad.describe_error(e)}): "
                f"a project missing from the first registry loses its bus user. "
                f"Fix the roster, or write {FILE} by hand — one origin per line")
        lines = merged(memory, live)
        write(lines)
        note = (f"{FILE}: registry created from {len(memory)} remembered and "
                f"{len(live)} running project(s); {MEMORY} is no longer read")
    return (*puppets.shard_ids(read()), note)
