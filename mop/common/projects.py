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
import hashlib
import json
import os
import secrets
import time

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
      * нет ничего -- свежая установка: честный пустой реестр.

    Это взгляд writer'а: отказ здесь блокирует мутацию. Читателю
    (глаголы-списки, лимит в add, users прогонов), которому старые данные
    обязаны оставаться видимыми, -- view_of: та же пара, но без отказов
    представимости."""
    path = os.path.join(dir_, "projects.json")
    if _present(path):
        return _snapshot_from_file(path)
    old_registry, old_limits = os.path.join(dir_, "projects"), os.path.join(dir_, "limits.json")
    if os.path.exists(old_registry) or os.path.exists(old_limits):
        return _snapshot_of_pair(dir_)
    return {"version": SNAPSHOT_VERSION, "projects": []}


def view_of(dir_):
    """Снимок для читателя: прежние видимые данные, отказов представимости
    нет. Чистая, ничего не пишет.

    Непредставимое в v1 (голое имя, два origin'а одного проекта, orphan-
    лимит) не теряется из виду: миграция -- отказ writer'а (snapshot_of),
    а читатель видит то же, что показывал прежний ответ глагола projects
    по этой паре. Для этого снимок несёт необязательные legacy-строки и
    legacy-лимиты -- их печатают snapshot_lines/names/limits, но writer
    их не сохраняет: первый успешный мутирующий глагол миграцию потребует,
    и там отказ будет услышан."""
    path = os.path.join(dir_, "projects.json")
    if _present(path):
        return _snapshot_from_file(path)
    old_registry, old_limits = os.path.join(dir_, "projects"), os.path.join(dir_, "limits.json")
    if not (os.path.exists(old_registry) or os.path.exists(old_limits)):
        return {"version": SNAPSHOT_VERSION, "projects": []}
    origins, legacy = puppets.project_ids(read(os.path.join(dir_, "projects")))
    limits = read_limits(os.path.join(dir_, "limits.json"))
    snap = {"version": SNAPSHOT_VERSION,
            "projects": [_entry(o, limits.get(puppets.project_of(o))) for o in sorted(origins)]}
    if legacy:
        snap["legacy"] = sorted(legacy)
    orphans = {k: v for k, v in limits.items()
               if k not in {puppets.project_of(o) for o in origins}}
    if orphans:
        snap["legacy_limits"] = orphans
    return snap


def _present(path):
    """Есть ли файл, отличив «нет» от «есть, но не достать» (#534).

    os.path.exists глотает причину недоступности: битый symlink или
    недоступный каталог выглядели бы «файла нет», и читатель молча ушёл
    бы в fallback на легаси-пару, маскируя поломку старым реестром.
    «Не достать» -- громкий отказ, а не пустое место."""
    try:
        os.lstat(path)
        return True
    except FileNotFoundError:
        return False
    except OSError as e:
        raise RuntimeError(f"cannot tell the registry state: {path}: {e}") from None


def _checked(snapshot, where):
    """Проверка схемы v1 одним местом. -> те же данные, битое -- отказ.

    Её зовут reader файла (_snapshot_from_file), writer (write_snapshot)
    и export (legacy_pair_of): одни и те же данные обязаны проходить или
    отказывать одинаково везде, иначе writer «нормализовал» бы то, что
    reader сочёл битым. Схема: точные ключи {version, projects}; version
    -- строго int (type is int: True и 1.0 проходят сравнение == 1, но
    схемой не являются); каждая запись -- {origin, limit?} без чужих
    полей, origin валиден по правилам origin'ов, limit -- строго целое
    >= 0 (bool -- не int для лимита, null -- не отсутствие ключа); один
    origin на имя проекта. Отказ называет место, имя и ТИП -- не сырое
    значение: в испорченном файле может лежать секрет (строка в version,
    чужой токен в limit, секрет и в НАЗВАНИИ неизвестного поля), а текст
    отказа уезжает в журналы и ответы RPC.
    """
    if not isinstance(snapshot, dict):
        raise RuntimeError(f"{where} is {type(snapshot).__name__}, not an object "
                           "with version and projects")
    if set(snapshot) != {"version", "projects"}:
        missing = {"version", "projects"} - set(snapshot)
        if missing:
            raise RuntimeError(f"{where} carries no {', '.join(sorted(missing))}")
        extra = set(snapshot) - {"version", "projects"}
        raise RuntimeError(f"{where} carries {len(extra)} unknown field(s) outside "
                           "version and projects")
    version = snapshot["version"]
    if type(version) is not int or version != SNAPSHOT_VERSION:
        if type(version) is int:
            # Число -- не секрет: назвать его точнее, чем «не наша схема».
            raise RuntimeError(f"{where} is version {version}, this mop reads version "
                               f"{SNAPSHOT_VERSION}: upgrade mop first")
        raise RuntimeError(f"{where} is version of type {type(version).__name__}, not "
                           f"an integer: this mop reads version {SNAPSHOT_VERSION}")
    if not isinstance(snapshot["projects"], list):
        raise RuntimeError(f"{where}: projects must be a list")
    seen = set()
    for n, p in enumerate(snapshot["projects"], start=1):
        if not isinstance(p, dict) or "origin" not in p or set(p) - {"origin", "limit"}:
            if not isinstance(p, dict):
                raise RuntimeError(f"{where}: project #{n} is {type(p).__name__}, "
                                   "not an object with an origin")
            if "origin" not in p:
                raise RuntimeError(f"{where}: project #{n} carries no origin")
            extra = set(p) - {"origin", "limit"}
            raise RuntimeError(f"{where}: project #{n} carries {len(extra)} unknown "
                               "field(s) outside origin and limit")
        origin = p["origin"]
        if not isinstance(origin, str) or not puppets.looks_like_origin(origin):
            raise RuntimeError(f"{where}: origin of project #{n} is "
                               f"{type(origin).__name__}, not a git-origin")
        name = puppets.project_of(origin)
        if name in seen:
            raise RuntimeError(f"{where}: 2 origins of one project {name}; v1 keeps "
                               "one origin per project")
        if "limit" in p:
            value = p["limit"]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise RuntimeError(f"{where}: limit of {name} is a {type(value).__name__}, "
                                   "not a whole number >= 0; null is not the absence "
                                   "of a limit")
        seen.add(name)
    return snapshot


def _snapshot_from_file(path):
    """Разбор файла снимка с проверкой схемы (_checked). Битое -- отказ."""
    try:
        with open(path) as f:
            got = json.load(f)
    except (OSError, ValueError) as e:
        raise RuntimeError(f"the project registry {path} is unreadable: {e}") from None
    return _checked(got, f"the project registry {path}")


def _strict_lines(path):
    """Строки легаси-реестра для МИГРАЦИИ: без файла -- пусто, ошибка чтения
    -- громкий отказ. Прощающий read() глотает OSError, и недоступный
    файл выглядел бы пустым реестром: writer такое не читает."""
    try:
        with open(path) as f:
            return {l.strip() for l in f if l.strip()}
    except FileNotFoundError:
        return set()
    except OSError as e:
        raise RuntimeError(f"cannot migrate the registry: {path} is unreadable: {e}") from None


def _strict_limits(path):
    """Лимиты пары для МИГРАЦИИ: каждая запись -- целое >= 0 или отказ.

    Прощающий read_limits (#337) отбрасывает null/bool/строки молча: глагол
    переживал одну плохую строку. Миграция -- writer: усечённые потолки
    она сохраняла бы навсегда, поэтому невалидная запись -- отказ с именем
    и ТИПОМ (не сырым значением: в испорченном файле может лежать секрет),
    файлы не тронуты."""
    try:
        with open(path) as f:
            got = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as e:
        raise RuntimeError(f"cannot migrate the registry: {path} is unreadable: {e}") from None
    if not isinstance(got, dict):
        raise RuntimeError(f"cannot migrate the registry: {path} is "
                           f"{type(got).__name__}, not an object")
    out = {}
    for name, value in got.items():
        # bool -- это int в питоне, проверяем первым; отрицательное -- не
        # потолок: миграция сохраняла бы его навсегда.
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(f"cannot migrate the registry: limit of {name} in "
                               f"{path} is a {type(value).__name__}, not a whole "
                               "number >= 0. The files are intact, and what to do "
                               "with it is an operator decision")
        out[name] = value
    return out


def _snapshot_of_pair(dir_):
    """Снимок из легаси-пары каталога. Непредставимое -- отказ, не потеря.

    Голое имя и коллизия basename'ов в v1 не представимы без выдумывания
    origin'а; orphan-лимит (имени нет в реестре) нельзя ни перенести (не
    куда), ни выбросить (потеря потолка) -- всё это отказ оператору с
    именем, файлы остаются как были. Отказ называет причину и сохранность,
    но не способ исправления: правка реестра идёт теми же глаголами
    (первый мутирующий потребует ту же миграцию и откажет тем же
    отказом), а редактирование файлов руками -- не путь пула."""
    origins, legacy = puppets.project_ids(_strict_lines(os.path.join(dir_, "projects")))
    if legacy:
        raise RuntimeError("cannot migrate the registry: bare name(s) without an "
                           f"origin: {', '.join(sorted(legacy))}. v1 stores origins "
                           "only; the files are intact, and fixing them is an "
                           "operator decision")
    seen = {}
    for o in sorted(origins):
        name = puppets.project_of(o)
        if name in seen:
            # Имя и число, не сырые origin'ы: origin в реестре может нести
            # креды (userinfo, token), и отказ оператору их не печатает.
            raise RuntimeError("cannot migrate the registry: 2 origins of one "
                               f"project {name}. v1 keeps one origin per project; "
                               "the files are intact, and choosing the true one "
                               "is an operator decision")
        seen[name] = o
    limits = _strict_limits(os.path.join(dir_, "limits.json"))
    for name in sorted(limits):
        if name not in seen:
            raise RuntimeError("cannot migrate the registry: limit of an unknown "
                               f"project {name!r} in limits.json. The files are "
                               "intact, and what to do with the limit is an "
                               "operator decision")
    return {"version": SNAPSHOT_VERSION,
            "projects": [_entry(o, limits.get(puppets.project_of(o))) for o in sorted(origins)]}


def snapshot_origins(snapshot):
    """Origin'ы снимка по порядку."""
    return [p["origin"] for p in snapshot["projects"]]


def snapshot_limits(snapshot):
    """{имя: потолок} снимка: записи с лимитом плюс legacy-потолки вида --
    прежний ответ глагола projects печатал limits целиком."""
    out = {puppets.project_of(p["origin"]): p["limit"]
           for p in snapshot["projects"] if p.get("limit") is not None}
    out.update(snapshot.get("legacy_limits") or {})
    return out


def snapshot_names(snapshot):
    """Имена проектов снимка (basename'ы, без дублей), по порядку."""
    return sorted({puppets.project_of(p["origin"]) for p in snapshot["projects"]}
                  | set(snapshot.get("legacy") or []))


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
    return snapshot_origins(snapshot) + list(snapshot.get("legacy") or [])


def view_lines(view):
    """Строки взгляда для локального читателя без сервера (fallback deploy,
    `mop cluster users`): те же lines, что у глагола-списка. Чистая."""
    return snapshot_lines(view)


def write_snapshot(snapshot, path=SNAPSHOT):
    """Запись снимка атомарно. Единственный writer -- сервис кластера.

    Схема -- та же проверка _checked, что у reader'а, ДО всякого mkdir и
    записи: writer не «нормализует» битое (явный null limit, чужие поля)
    в усечённый файл. Legacy-поля вида (голые имена, orphan-потолки) --
    только читателю, и writer такой view ОТКАЗЫВАЕТСЯ писать: молча
    выбросить их и сохранить усечённое состояние -- та же потеря, что
    обрыв между двумя файлами. Мутаторы зовут strict snapshot_of, но и
    сам writer держит гейт: непредставимое не должно доехать до диска
    ни одним путём."""
    if not isinstance(snapshot, dict):
        _checked(snapshot, "cannot write the registry: the snapshot")  # отказ по типу
    if snapshot.get("legacy") or snapshot.get("legacy_limits"):
        raise RuntimeError("cannot write the registry: the view carries unrepresentable "
                           "legacy data (bare name(s) "
                           f"{', '.join(sorted(snapshot.get('legacy') or ()))}, limit(s) of "
                           f"{', '.join(sorted((snapshot.get('legacy_limits') or {}).keys()))}); "
                           "migrate the pair by an operator decision first")
    _checked(snapshot, "cannot write the registry: the snapshot")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {"version": snapshot["version"],
               "projects": [dict(p) for p in snapshot["projects"]]}
    fsutil.write_atomic(path, json.dumps(payload, indent=1, sort_keys=True) + "\n")


def legacy_pair_of(snapshot):
    """Обратное преобразование снимка в легаси-пару. -> (строки, лимиты).

    Чистая, без записи: даунгрейд -- операторская процедура (остановить
    writers, выгрузить пару, переключить), и её шаги видны явно. После
    новых мутаций v1 roundtrip-стабильна: миграция той же пары даёт тот
    же снимок (проверено tests/projects.py). View читателя (голые имена,
    orphan-потолки) непредставим в паре без потерь -- отказ, не потеря."""
    if not isinstance(snapshot, dict):
        _checked(snapshot, "cannot export the snapshot: it")  # отказ по типу
    if snapshot.get("legacy") or snapshot.get("legacy_limits"):
        raise RuntimeError("cannot export a view with unrepresentable legacy data "
                           "(bare name(s) "
                           f"{', '.join(sorted(snapshot.get('legacy') or ()))}, limit(s) of "
                           f"{', '.join(sorted((snapshot.get('legacy_limits') or {}).keys()))}); "
                           "migrate the pair by an operator decision first")
    _checked(snapshot, "cannot export the snapshot: it")
    lines = snapshot_origins(snapshot)
    limits = {puppets.project_of(p["origin"]): p["limit"]
              for p in snapshot["projects"] if "limit" in p}
    return lines, limits


# ─── выгрузка для отката: пара из одного снимка (#542) ────────────────────
# После миграции старые projects/limits.json лежат на диске устаревшими, а
# git revert снимка не сохраняет add/delete/limit, случившиеся позже.
# Обратный экспорт -- это один валидированный снимок, разложенный в пару в
# НОВОМ каталоге: живой реестр не тронут, настоящий переключение -- за
# оператором (остановить writers, снять финальный экспорт, сверить digest,
# переключить). Resume v1 после правок старым writer'ом обязан перечитать
# свежий снимок, а не доверять устаревшему; лечить голые имена/коллизии/
# orphan-потолки автоматически команда не берётся -- это отдельные решения.
def _write_claimed(dirfd, name, data):
    """Файл пары В СОЗДАННОМ каталоге, по fd, без имени пути (#542).

    Атомарно (временный + fsync + rename по dir_fd), 0600: origin'ы несут
    креды. Запись идёт в каталог, на который указывает fd, -- подмена пути
    (rmtree + чужой mkdir) её не перенаправляет: каталог отвязан от имени,
    и создание в нём ломается ENOENT, а не пишет чужому.
    """
    tmp = f".{name}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600, dir_fd=dirfd)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, name, src_dir_fd=dirfd, dst_dir_fd=dirfd)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp, dir_fd=dirfd)
        except FileNotFoundError:
            pass  # уже нет -- и ладно
        raise


def export_pair(dest, snapshot_path=None):
    """Выгрузить реестр парой в НОВЫЙ каталог. -> сводка.

    Источник не трогает: читает ОДИН раз, единственная запись -- сама
    выгрузка в dest. Функция меняет ФС, это не pure function: «чистота»
    здесь -- отсутствие побочных эффектов на живом реестре, не чистота
    записи.

    Порядок и гарантии (#542):
      * снимок читается ОДНИМ запросом ДО всякого mkdir -- байты, digest,
        разбор валидатором #534; нет файла или битый -- отказ, БЕЗ
        fallback на устаревшую легаси-пару;
      * каталог заявляется атомарным mkdir (0700) и сразу привязывается
        fd (O_NOFOLLOW): записи, сверка и cleanup идут по fd СОЗДАННОГО
        каталога, identity (dev, ino) даёт сам fd. Непустой каталог на
        привязке -- подмена между заявкой и привязкой: отказ без записи и
        без удаления -- чужой каталог не получает ни секретов, ни unlink;
      * файлы пишутся 0600 атомарно (origin'ы несут креды) и
        ПЕРЕЧИТЫВАЮТСЯ по fd: фактические байты и права сверяются с
        ожидаемыми ДО маркера; маркер export.json -- ПОСЛЕДНИМ и тоже
        перечитан: его нет или он не сошёлся -- экспорт не состоялся.
        Это защита от обрыва процесса, не обещание сохранности при потере
        питания;
      * провал чистит только созданное ЭТОЙ попыткой: файлы -- unlink'ом
        по fd (ENOENT -- уже убрали, прощаем), каталог -- атомарной
        пере-заявкой rename(dest -> соль-имя) со сверкой identity по fd
        ПЕРЕД rmdir: «проверить имя, затем удалить» -- окно подмены, а
        rename двигает то, что лежит на имени СЕЙЧАС; не наше -- вернуть
        на место и назвать, чужое не удаляется;
      * сводка -- счётчик, источник и digest: креды origin'ов живут в
        закрытых файлах пары, не в stdout/metadata/ошибках.
    """
    path = SNAPSHOT if snapshot_path is None else snapshot_path
    try:
        with open(path, "rb") as f:
            raw = f.read()
    except FileNotFoundError:
        raise RuntimeError(f"nothing to export: {path} does not exist; a legacy "
                           "pair is not a rollback source -- migrate first") from None
    except OSError as e:
        raise RuntimeError(f"cannot export: {path} is unreadable: {e}") from None
    try:
        parsed = json.loads(raw)
    except ValueError as e:
        raise RuntimeError(f"cannot export: {path} is not valid JSON: {e}") from None
    _checked(parsed, f"cannot export: {path}")
    lines, limits = legacy_pair_of(parsed)
    digest = hashlib.sha256(raw).hexdigest()
    pair = {
        "projects": "".join(f"{s}\n" for s in lines).encode(),
        "limits.json": (json.dumps(limits, sort_keys=True) + "\n").encode(),
    }
    marker = {"what": "mop registry rollback export", "version": 1,
              "schema": "legacy-pair", "source": path, "snapshot_sha256": digest,
              "projects": len(lines),
              "files": {n: hashlib.sha256(d).hexdigest() for n, d in pair.items()},
              "exported_at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())}
    marker_body = (json.dumps(marker, indent=1, sort_keys=True) + "\n").encode()
    # Заявка имени: mkdir атомарен, EEXIST -- отказ. Родителя создаёт
    # оператор: экспорт не расползается по чужим путям.
    try:
        os.mkdir(dest, 0o700)
    except FileExistsError:
        raise RuntimeError(f"cannot export: {dest} already exists; refusing to "
                           "overwrite -- name a new directory") from None
    except OSError as e:
        raise RuntimeError(f"cannot export: cannot create {dest}: {e}") from None
    # Привязка: fd созданного каталога, без следования symlink'ам. С этого
    # момента путь -- не истина: записи, сверка и cleanup идут по fd.
    try:
        dirfd = os.open(dest, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    except OSError as e:
        raise RuntimeError(f"cannot export: cannot bind {dest} as the directory "
                           f"just created: {e}") from None
    try:
        fresh = not os.listdir(dirfd)
    except OSError as e:
        os.close(dirfd)
        raise RuntimeError(f"cannot export: cannot look into {dest}: {e}") from None
    if not fresh:
        # Подмена между заявкой и привязкой: это может быть чужой каталог --
        # ни записи, ни удаления, только отказ.
        os.close(dirfd)
        raise RuntimeError(f"cannot export: {dest} is no longer a fresh empty "
                           "directory we just created; nothing was written, "
                           "nothing was removed") from None
    try:
        for name, data in pair.items():
            _write_claimed(dirfd, name, data)
        # Сверка фактического: перечитать записанное ПО FD и сверить байты
        # и права ДО маркера. Усечённая запись -- отказ: готовой публикации
        # без целой пары не бывает.
        for name, data in pair.items():
            try:
                fdo = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
            except OSError as e:
                raise RuntimeError(f"cannot export: {name} is missing from the "
                                   f"claimed directory: {e}") from None
            with os.fdopen(fdo, "rb") as f:
                if f.read() != data:
                    raise RuntimeError(f"cannot export: {name} was written "
                                       "truncated or corrupted: the read-back "
                                       "bytes do not match what was to be "
                                       "exported")
            st = os.stat(name, dir_fd=dirfd, follow_symlinks=False)
            if (st.st_mode & 0o777) != 0o600:
                raise RuntimeError(f"cannot export: {name} did not keep the "
                                   "private mode 0600")
        _write_claimed(dirfd, "export.json", marker_body)
        # Маркер тоже перечитан: публикация подтверждена фактом, не планом.
        fdo = os.open("export.json", os.O_RDONLY | os.O_NOFOLLOW, dir_fd=dirfd)
        with os.fdopen(fdo, "rb") as f:
            if f.read() != marker_body:
                raise RuntimeError("cannot export: export.json was written "
                                   "truncated or corrupted: the read-back "
                                   "bytes do not match")
    except Exception as e:
        # Публикация не состоялась: убрать своё, но ТОЛЬКО своё -- по fd
        # созданного каталога, имя каталога -- атомарной заявкой.
        msg = str(e)
        why = [msg if msg.startswith("cannot export") else f"cannot export: {e}"]
        why.extend(_drop_claimed_dir(dirfd, dest))
        raise RuntimeError("; ".join(why)) from None
    finally:
        os.close(dirfd)
    return {"what": "mop registry rollback export", "projects": len(lines),
            "source": path, "sha256": digest, "dest": dest}


def _drop_claimed_dir(dirfd, dest):
    """Уборка созданного каталога: файлы по fd, имя -- атомарной заявкой (#542).

    rmdir принимает только путь, а «проверить имя, затем удалить» -- окно
    подмены (между проверкой и rmtree каталог занимает чужой). Поэтому имя
    каталога трогает только АТОМАРНЫЙ rename на соль-имя: двигается то,
    что лежит на имени сейчас; identity сверяется с fd ПОСЛЕ -- совпало,
    значит убираем своё (rmdir соль-имени), не совпало -- вернуть на место
    и назвать: чужое не удаляется. Файлы этой попытки снимаются по fd --
    имя каталога на пути уже может указывать на чужое.
    """
    gone = f"{dest}.mop-gone-{secrets.token_hex(4)}"
    for name in ("projects", "limits.json", "export.json"):
        try:
            os.unlink(name, dir_fd=dirfd)
        except FileNotFoundError:
            pass  # не писали или уже убрали
        except OSError as e:
            return [f"cleanup failed on {name}: {e}"]
    try:
        os.rename(dest, gone)
    except FileNotFoundError:
        return ["the destination directory disappeared before cleanup"]
    except OSError as e:
        return [f"cleanup failed: cannot claim the directory for removal: {e}"]
    try:
        st = os.stat(gone, follow_symlinks=False)
        ours = os.fstat(dirfd)
        if (st.st_dev, st.st_ino) != (ours.st_dev, ours.st_ino):
            try:
                os.rename(gone, dest)
            except OSError as e:
                return [f"cleanup failed: {dest} was replaced and could not be "
                        f"restored (now at {gone}): {e}"]
            return [f"cannot remove the incomplete export directory: {dest} is "
                    "no longer ours; left in place"]
        os.rmdir(gone)
    except OSError as e:
        # ENOTEMPTY и прочие: в каталоге что-то помимо снятых наших файлов --
        # не снимать вслепую; оставить и назвать причину.
        return [f"cleanup failed: the directory {e}"]
    return []


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
