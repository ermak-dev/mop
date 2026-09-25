"""Реестр проектов пула: кто заведён. Данные, без печати (печатает `mop project`).

Проект (он же проект) — один репозиторий, один срез пула, один мастер. По
этому списку плейбук заводит пользователей NATS `puppet-<проект>` (люди
ходят своими именами, #106): без записи здесь папет поднимется и будет
читаться как «агент молчит» на пустом месте.

Раньше списка как сущности не было (#79). Он собирался при каждом прогоне
объединением трёх источников — ростера Nomad, памяти контроллера и
аргументов `mop server deploy <origin>`, — и завод проекта был побочным эффектом
прогона всей установки. Отсюда же росли оба костыля: память нужна была
затем, что удаление последнего папета вычёркивало проект из конфига NATS, а
объединение никогда не заменой — затем, что недоступный ростер сузил бы
список молча. Снять проект было нельзя вовсе.

Теперь список — реестр (#79), и живёт он на сервере (#117): правят его
глаголы сервиса кластера `project_add|delete`, `mop server deploy` спрашивает его
глаголом `projects`. Это единственный ответ на вопрос «какие проекты
заведены».

Реестр хранит ORIGIN'Ы (#33): имя выводится basename'ом, а имя в origin не
разворачивается — таблицы имён нет и заводить нельзя. От легаси-времён в нём
остаются голые имена, origin которых уже не узнать; завести новую такую
строку нельзя, а старую не теряем.
"""
import json
import os

from .. import driver
from . import fsutil, paths, puppets

FILE = paths.local(paths.PROJECTS)


# ─── чистое: что реестр принимает, что теряет ────────────────────────────
def with_origin(origin, known):
    """Реестр после завода проекта. -> (реестр, добавлен?).

    Голое имя отвергаем: origin — единственная правда о проекте, из него
    режется манифест `.mop` и им же клонируется папет. Приняв имя, реестр
    завёл бы пользователя на шине для проекта, которого не достать."""
    origin = (origin or "").strip()
    if not puppets.looks_like_origin(origin):
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
                     if l == name or puppets.project_of(l) == name)
    return known - set(dropped), dropped


def names(origins, legacy):
    """Имена проектов для плейбука: basename origin'ов плюс легаси, по порядку."""
    return sorted({puppets.project_of(o) for o in origins} | set(legacy))


# Схемы, которые git везёт по ssh; scp-форму parse_origin сам зовёт ssh.
SSH_SCHEMES = ("ssh", "git+ssh", "ssh+git")


def git_hosts(origins, default):
    """Хосты, ключам которых узел доверяет в known_hosts. Чистая функция.

    MOP_GIT_HOST установки плюс ssh-хосты origin'ов реестра (#121): проект
    бывает и с чужого форжа, ключ пула там пускают, а ключа хоста узел не
    знал -- и папет падал на клоне. https и локальный путь ключа хоста не
    требуют. Порт у ssh-схем -- в записи known_hosts как [host]:port."""
    found = []
    for origin in origins:
        scheme, _, host, port, _ = driver.parse_origin(origin) or (None,) * 5
        # Только ssh: ssh://, scp-форма (разбор общий, #154) и git+ssh:// /
        # ssh+git:// -- git клонирует их тем же ssh (#166).
        if scheme in SSH_SCHEMES and host:
            found.append(f"[{host}]:{port}" if port and port != "22" else host)
    return [default] + sorted(set(found) - {default})


# ─── лимиты: потолок папетов проекта (#107) ─────────────────────────────
# Политика, а не состав: реестр отвечает «какие проекты заведены», лимиты --
# «сколько папетов проекту можно». Отдельный файл, а не суффикс в строке
# origin'а: иначе поменялся бы разбор реестра всюду, где читают origin.
# Правит его `mop project limit` на контроллере, на сервер его кладёт прогон
# (deploy/roles/cluster), а сверяет сервис кластера в глаголе `add`.
LIMITS = paths.local("limits.json")


def parse_limit(text):
    """Строка команды -> потолок: число >= 0 или None («без лимита»).

    Мусор -- отказ, а не «без лимита»: опечатка снимала бы потолок молча.
    0 законен -- заморозка: новых папетов не заводить."""
    text = (text or "").strip()
    if text == "none":
        return None
    if not text.isdigit():
        raise ValueError(f"limit must be a whole number or none, got {text!r}")
    return int(text)


def with_limit(limits, name, value):
    """Лимиты после установки или снятия (value=None). -> новый словарь."""
    out = dict(limits)
    if value is None:
        out.pop(name, None)
    else:
        out[name] = value
    return out


def read_limits(path=LIMITS):
    """{проект: потолок}; нет файла -- лимитов нет."""
    try:
        with open(path) as f:
            return {k: int(v) for k, v in json.load(f).items()}
    except (OSError, ValueError, AttributeError):
        return {}


def write_limits(limits, path=LIMITS):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fsutil.write_atomic(path, json.dumps(limits, sort_keys=True))


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
    fsutil.write_atomic(path, "".join(f"{s}\n" for s in sorted(lines)))


def for_deploy(answer, local):
    """Реестр для `mop server deploy`. -> (строки, примечание|None). Чистая функция.

    Правда -- реестр сервера (#117): его правят глаголы сервиса кластера.
    Сервер не ответил -- это чистая установка (шины ещё нет, её поднимает
    этот же прогон) или упавший сервис; тогда -- копия этой машины, и об
    этом говорится. Пустой ответ сервера -- правда, а не повод взять копию."""
    if answer.get("ok"):
        return set(answer.get("lines") or []), None
    return set(local), (f"the server's project registry is unavailable "
                        f"({answer.get('error')}): using this machine's copy, "
                        f"{len(local)} line(s)")
