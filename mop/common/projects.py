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
    # Файл -- общим чтением (#317), записи -- по одной (#337).
    got = fsutil.read_json(path, {})
    if not isinstance(got, dict):
        return {}
    # По записи, не по файлу (#337): потолок защищает, и одна плохая строка
    # (null, список, bool) не должна ни ронять глагол, ни снимать остальные.
    out = {}
    for k, v in got.items():
        if isinstance(v, bool):
            continue
        try:
            out[k] = int(v)
        except (TypeError, ValueError, OverflowError):
            continue
    return out


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


# ─── атомарный реестр: один файл вместо пары (#534) ──────────────────────
# Раньше origin'ы (projects) и лимиты (limits.json) лежали в двух файлах, и
# каждый мутирующий глагол сервиса писал их двумя отдельными атомарными
# записями: обрыв между ними разводил данные -- проект снят, а потолок
# остался. Теперь реестр -- один снимок {version:1, projects:[{origin,
# limit?}]}: ключа limit нет -- без лимита, limit:0 -- заморозка; имя
# нигде не хранится, оно выводится driver.project_of. Один проект на
# basename: второй origin того же имени в v1 не лезет, и миграция такой
# пары отказывает. Всё ниже -- чистые функции от снимка; читает и пишет
# файл один writer -- сервис кластера (mop/server/cluster.py), под своим
# замком, снимок целиком за одну операцию.
SNAPSHOT = paths.local("projects.json")
SNAPSHOT_DIR = os.path.dirname(SNAPSHOT)
SNAPSHOT_VERSION = 1
# Легаси-файлы (projects, limits.json) остаются на диске -- их читает
# deploy до #540 и откат; новый writer их не пишет, правда после
# миграции -- снимок.


def _entry(origin, limit):
    """Запись снимка: без лимита -- без ключа (None и 0 -- разные вещи)."""
    return {"origin": origin} if limit is None else {"origin": origin, "limit": limit}


def snapshot_of(dir_):
    """Снимок реестра каталога: projects.json либо миграция пары. Чистая.

    Функция НИЧЕГО не пишет -- даже миграция здесь только считает снимок,
    записывает его writer сервиса. Порядок:
      * есть projects.json -- он и есть правда; битый JSON, чужая схема
        (version > нашей) или не-объект -- громкий отказ: пустой реестр
        или fallback на легаси-пару маскировали бы поломку;
      * нет снимка, есть пара -- снимок из пары; непредставимое (голое
        имя, два origin'а одного проекта, orphan/чужой лимит) -- отказ с
        именем, без потерь и без выдумывания origin'а;
      * нет ничего -- свежая установка: честный пустой реестр."""
    path = os.path.join(dir_, "projects.json")
    if os.path.exists(path):
        return _snapshot_from_file(path)
    old_registry, old_limits = os.path.join(dir_, "projects"), os.path.join(dir_, "limits.json")
    if os.path.exists(old_registry) or os.path.exists(old_limits):
        return _snapshot_of_pair(dir_)
    return {"version": SNAPSHOT_VERSION, "projects": []}


def _snapshot_from_file(path):
    """Разбор файла снимка с проверкой схемы. Битое -- громкий отказ."""
    try:
        with open(path) as f:
            got = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"the project registry {path} is unreadable: {e}") from None
    if not isinstance(got, dict) or "projects" not in got:
        raise RuntimeError(f"the project registry {path} is not an object with projects")
    version = got.get("version")
    if version != SNAPSHOT_VERSION:
        raise RuntimeError(f"the project registry {path} is version {version!r}, "
                           f"this mop reads version {SNAPSHOT_VERSION}: upgrade mop first")
    if not isinstance(got["projects"], list):
        raise RuntimeError(f"the project registry {path}: projects must be a list")
    return got


def _snapshot_of_pair(dir_):
    """Снимок из легаси-пары каталога. Непредставимое -- отказ, не потеря.

    Голое имя и коллизия basename'ов в v1 не представимы без выдумывания
    origin'а; orphan-лимит (имени нет в реестре) нельзя ни перенести (не
    куда), ни выбросить (потеря потолка) -- всё это отказ оператору с
    конкретным именем, файлы остаются как были."""
    origins, legacy = puppets.project_ids(read(os.path.join(dir_, "projects")))
    if legacy:
        raise RuntimeError("cannot migrate the registry: bare name(s) without an "
                           f"origin: {', '.join(sorted(legacy))}. Add the origin of "
                           "each project (mop project add <git-origin>) or drop "
                           "the line by hand: v1 stores origins only")
    seen = {}
    for o in sorted(origins):
        name = puppets.project_of(o)
        if name in seen:
            # Имя и число, не сырые origin'ы: origin в реестре может нести
            # креды (userinfo, token), и отказ оператору их не печатает.
            raise RuntimeError("cannot migrate the registry: 2 origins of one "
                               f"project {name}. Leave the true one and drop "
                               "the other by hand (mop project list --origins): "
                               "v1 keeps one project per name")
        seen[name] = o
    limits = read_limits(os.path.join(dir_, "limits.json"))
    for name in sorted(limits):
        if name not in seen:
            raise RuntimeError("cannot migrate the registry: limit of an unknown "
                               f"project {name!r} in limits.json. Remove the line "
                               "by hand or register the project first")
    return {"version": SNAPSHOT_VERSION,
            "projects": [_entry(o, limits.get(puppets.project_of(o))) for o in sorted(origins)]}


def snapshot_origins(snapshot):
    """Origin'ы снимка по порядку."""
    return [p["origin"] for p in snapshot["projects"]]


def snapshot_limits(snapshot):
    """{имя: потолок} снимка: только записи с лимитом."""
    return {puppets.project_of(p["origin"]): p["limit"]
            for p in snapshot["projects"] if p.get("limit") is not None}


def snapshot_names(snapshot):
    """Имена проектов снимка (basename'ы, без дублей), по порядку."""
    return sorted({puppets.project_of(p["origin"]) for p in snapshot["projects"]})


def snapshot_with_origin(snapshot, origin):
    """Снимок после завода проекта. -> снимок. Чистая.

    Голое имя -- отказ, как и в легаси-реестре: origin -- единственная
    правда о проекте. Второй origin того же имени -- тоже отказ: в v1
    один проект на имя, и выбрать «правильный» origin молча нельзя.
    Заведённость глагол видит, сравнив снимки: здесь только данные."""
    origin = (origin or "").strip()
    if not puppets.looks_like_origin(origin):
        raise RuntimeError(f"{origin!r} doesn't look like a git-origin: "
                           f"a project is registered by its origin, not by its name")
    if origin in snapshot_origins(snapshot):
        return {"version": snapshot["version"], "projects": list(snapshot["projects"])}
    name = puppets.project_of(origin)
    if name in snapshot_names(snapshot):
        raise RuntimeError(f"project {name} is already registered with another "
                           f"origin: v1 keeps one origin per project")
    return {"version": snapshot["version"],
            "projects": snapshot["projects"] + [_entry(origin, None)]}


def snapshot_without(snapshot, name):
    """Снимок после снятия проекта по имени. -> снимок.

    Имя -- basename origin'а: проект снимается весь, вместе с лимитом --
    в одном снимке, разводить их больше нечем. Снятые строки глагол
    выводит сравнением снимков; пустой результат значит «проекта нет»,
    и это отказ вызывающего (опечатка иначе читалась бы снятием)."""
    return {"version": snapshot["version"],
            "projects": [p for p in snapshot["projects"]
                         if puppets.project_of(p["origin"]) != name]}


def snapshot_with_limit(snapshot, name, value):
    """Снимок после установки лимита (value=None -- снять). -> снимок."""
    out = []
    for p in snapshot["projects"]:
        if puppets.project_of(p["origin"]) == name:
            p = _entry(p["origin"], value)
        out.append(p)
    return {"version": snapshot["version"], "projects": out}


def snapshot_lines(snapshot):
    """Строки реестра для ответа глагола `projects` (поле lines): origin'ы
    и голые имена снимка -- как их печатает и читает легаси-реестр."""
    return snapshot_origins(snapshot)


def write_snapshot(snapshot, path=SNAPSHOT):
    """Запись снимка атомарно. Единственный writer -- сервис кластера."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"version": snapshot["version"],
               "projects": [{"origin": p["origin"], **({"limit": p["limit"]}
                                                       if p.get("limit") is not None else {})}
                            for p in snapshot["projects"]]}
    fsutil.write_atomic(path, json.dumps(payload, indent=1, sort_keys=True) + "\n")


def legacy_pair_of(snapshot):
    """Обратное преобразование снимка в легаси-пару. -> (строки, лимиты).

    Чистая, без записи: даунгрейд -- операторская процедура (остановить
    writers, выгрузить пару, переключить), и её шаги видны явно. После
    новых мутаций v1 roundtrip-стабильна: миграция той же пары даёт тот
    же снимок (проверено tests/projects.py)."""
    lines = snapshot_origins(snapshot)
    limits = {puppets.project_of(p["origin"]): p["limit"]
              for p in snapshot["projects"] if p.get("limit") is not None}
    return lines, limits


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
