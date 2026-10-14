"""Душа роли: разбор soul.yaml, бандл с digest, принципалы и записи привязок.

Эпик #578 по контракту docs/SOUL.md (#579): все сессии пула -- папеты с
декларативными ролями. Этот модуль -- общее владение #580: одни кодировки
для сервера (#581/#582), узла (#584) и ростера (#587). Реестр привязок и
хранилище бандлов -- серверная работа, здесь только чистые функции над
данными; библиотека молчит (CLAUDE.md).

Словари глаголов -- точные имена таблиц своих сервисов (агент узла, сервис
кластера), но общему слою нельзя импортировать их модули (слои только
вниз, common ← node/server): списки продублированы данными, а tests/soul.py
сверяет их с живыми таблицами по scope -- расхождение ловится проверкой, а
не молчанием. Админские глаголы (NODE/SECRET/ADMIN) в грамматику роли не
входят вовсе: soul: master -- строка, а не привилегия (#579).
"""
import hashlib
import json
import os
import re
import uuid

from . import busnames

# Версия схемы, которую знает этот пакет (#579): сервер молча новые не
# принимает, и незнакомая версия -- громкий отказ, а не «прочиталось как
# получилось».
VERSION = 1

MANIFEST = "soul.yaml"

# Грамматика имени роли: soul и spawn ходят одним правилом.
NAME = re.compile(r"^[a-z][a-z0-9-]*$")

HEX32 = re.compile(r"^[0-9a-f]{32}$")

# Digest бандла -- SHA256 (#579): 64 знака hex. Отдельно от HEX32 id
# принципала: попытка сверить один с другим -- ошибка кодировок, а не
# «короткий digest».
HEX64 = re.compile(r"^[0-9a-f]{64}$")

# Глаголы, доступные роли, по таблицам своих сервисов. Узел: публичные и
# мастерские глаголы агента; кластер: глаголы проекта. Админские (узловые
# disk/junk/sweep, секретные и админские кластера) роли не достаются:
# allowlist роли не может выдать больше, чем есть у человека-мастера
# проекта. Сверка с живыми таблицами -- tests/soul.py.
NODE_VERBS = ("clone", "local", "ping", "send", "sizes", "state", "states",
              "tail", "type", "usage", "wipe", "write")
CLUSTER_VERBS = ("add", "alloc", "bootstrap_result", "delete", "landing",
                 "ping", "pool", "restart", "roster", "spec", "stderr",
                 "stop", "update")

DRIVERS = ("any", "isolating")


# ─── разбор манифеста ─────────────────────────────────────────────────────
def _refuse(field, rule, value, where="soul.yaml"):
    raise ValueError(f"{where} {field}={value!r}: {rule}")


def _name(value, field, where="soul.yaml"):
    if not (isinstance(value, str) and NAME.match(value)):
        _refuse(field, "a role name, [a-z][a-z0-9-]*", value, where)
    return value


def _bool(value, field):
    # bool -- подкласс int, но YAML даёт именно bool: строка 'true' -- не
    # согласие, а опечатка, и чтение её молча меняет роль.
    if not isinstance(value, bool):
        _refuse(field, "true or false", value)
    return value


def _str_list(value, field, vocab=None):
    """Список точных имён: строки, без повторов, из словаря -- если дан."""
    if not isinstance(value, list):
        _refuse(field, "a list", value)
    for item in value:
        if not isinstance(item, str):
            _refuse(field, "names must be strings", item)
        if vocab is not None and item not in vocab:
            _refuse(field, f"not a known verb, known: {', '.join(vocab)}",
                    item)
    if len(set(value)) != len(value):
        _refuse(field, "names must not repeat", value)
    return tuple(value)


# Имена секретов (#592): файл -- точное имя из хранилища проекта (путь от
# корня files/, как его показывает project_secrets.list_files), переменная
# -- KEY строки vars.env. Правило хранилища (valid_name) молча
# нормализует написанное -- для allowlist это не годится: digest закрепляет
# написанное, и «как-нибудь написанное» имя -- не точное имя (#579).
# Поэтому здесь каноническая ФОРМА написанного, без нормализации на лету.
def _secret_file(value, field):
    import posixpath
    ok = isinstance(value, str) and value and value not in (".", "..") \
        and not value.startswith("/") and "\\" not in value \
        and not value.startswith("../") and not any(
            part == ".." for part in value.split("/")) \
        and not any(wild in value for wild in "*?[]") \
        and posixpath.normpath(value) == value
    if not ok:
        _refuse(field, "a canonical file path inside the project secrets, "
                "no escape, no wildcards", value)
    return value


def _secret_var(value, field):
    if not (isinstance(value, str) and re.match(r"^[A-Za-z_][A-Za-z0-9_]*$",
                                                value)):
        _refuse(field, "a variable name, [A-Za-z_][A-Za-z0-9_]*", value)
    return value


def _keys(doc, what, fields, optional=()):
    """Ровно названные ключи: лишний -- неизвестное поле, недостающий --
    отсутствие договора, а не умолчание «как-нибудь»."""
    if not isinstance(doc, dict):
        _refuse(what, "must be a mapping", doc)
    got = set(doc)
    for key in got - set(fields) - set(optional):
        _refuse(key, "unknown field", doc[key])
    for key in got & set(optional):
        if doc[key] is None:
            _refuse(key, "must not be null", doc[key])
    for key in set(fields) - got:
        _refuse(key, "is required", what)
    return doc


def _yaml(text):
    """soul.yaml -> словарь. Безопасная загрузка без повторов ключей:
    yaml.safe_load молча берёт последний дубль, и опечатка в редакции
    роли тихо выдавала бы одну из половин (#579: дубликаты -- отказ)."""
    import yaml

    class Unique(yaml.SafeLoader):
        pass

    def no_dupes(loader, node, deep=False):
        out = {}
        for k, v in node.value:
            key = loader.construct_object(k, deep=deep)
            if key in out:
                raise ValueError(f"soul.yaml: duplicate key {key!r}")
            out[key] = loader.construct_object(v, deep=deep)
        return out

    Unique.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, no_dupes)
    try:
        doc = yaml.load(text, Loader=Unique)
    except yaml.YAMLError as e:
        raise ValueError(
            f"soul.yaml is not YAML: {str(e).splitlines()[0]}") from None
    if not isinstance(doc, dict):
        _refuse("soul.yaml", "must be a mapping", doc)
    return doc


def parse(text):
    """Текст soul.yaml -> Soul (замороженное значение, схема v1).

    Форма жёсткая (#579): soul/version/runtime/capabilities/prompt и
    необязательный secrets; отход -- ValueError с именем поля, а не
    «прочиталось как получилось»: молча потерянные capabilities -- роль без
    прав, молча потерянный runtime -- роль без изоляции."""
    import dataclasses

    doc = _yaml(text)
    _keys(doc, "soul.yaml", ("soul", "version", "runtime", "capabilities",
                             "prompt"), optional=("secrets",))
    version = doc["version"]
    if not isinstance(version, int) or isinstance(version, bool):
        _refuse("version", "an integer", version)
    if version != VERSION:
        _refuse("version", f"unknown schema, this package knows {VERSION}",
                version)
    runtime = _keys(doc["runtime"], "runtime",
                    ("driver", "receives_dispatch"))
    if runtime["driver"] not in DRIVERS:
        _refuse("runtime.driver", f"one of: {', '.join(DRIVERS)}",
                runtime["driver"])
    caps = _keys(doc["capabilities"], "capabilities",
                 ("node_verbs", "cluster_verbs", "dispatch", "spawn"))
    # Секреты -- явный allowlist точных имён (#592), пусто по умолчанию
    # (#579): блока нет -- ничего, и половина блока -- названное, остальное
    # пусто. Ложный ТИП блока (false/[]/0/'') -- мусор, а не «пусто»: `or {}`
    # читал его умолчанием и молча выдавал пустой allowlist (review #580).
    block = doc.get("secrets", {})
    if not isinstance(block, dict):
        _refuse("secrets", "a mapping", block)
    secrets = _keys({"files": [], "vars": [], **block}, "secrets",
                    ("files", "vars"))
    return Soul(
        soul=_name(doc["soul"], "soul"),
        version=version,
        runtime=Runtime(driver=runtime["driver"],
                        receives_dispatch=_bool(
                            runtime["receives_dispatch"],
                            "runtime.receives_dispatch")),
        capabilities=Capabilities(
            node_verbs=_str_list(caps["node_verbs"], "node_verbs",
                                 vocab=NODE_VERBS),
            cluster_verbs=_str_list(caps["cluster_verbs"], "cluster_verbs",
                                    vocab=CLUSTER_VERBS),
            dispatch=_bool(caps["dispatch"], "capabilities.dispatch"),
            spawn=tuple(_name(s, "spawn") for s in _str_list(
                caps["spawn"], "spawn"))),
        secrets=Secrets(
            files=tuple(_secret_file(f, "secrets.files") for f in _str_list(
                secrets["files"], "secrets.files")),
            vars=tuple(_secret_var(v, "secrets.vars") for v in _str_list(
                secrets["vars"], "secrets.vars"))),
        prompt=_filename(doc["prompt"]))


def _canonical(manifest):
    """Манифест -> канонический вход digest: полный набор ключей схемы v1
    (умолчания вставлены), чтобы один смысл не жил двумя бандлами -- yaml без
    блока secrets и с явным пустым одним digest'ом (#579: «по умолчанию --
    пусто»). Один источник канонизации на оба входа бандла: load и
    from_manifest."""
    parsed = parse(json.dumps(dict(manifest), ensure_ascii=False))
    return parsed.to_dict()


def from_manifest(manifest, prompt):
    """Словарь манифеста (как дал yaml или to_dict) + сырой промпт ->
    Bundle. Конструктор in-memory (#581): digest -- то же правило, что у
    load, одним местом, и сервер собирает бандл из словаря, не катая
    каталог по диску. Словарь дубликат ключа унести не может, остальную
    форму проверяет тот же parse. Промпт -- UTF-8, как у load: битые байты
    -- отказ, а не молча в digest (review #580)."""
    doc = _canonical(manifest)
    raw = prompt if isinstance(prompt, bytes) else prompt.encode()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"prompt: not UTF-8: {e}") from None
    parsed = parse(json.dumps(doc, ensure_ascii=False))
    return Bundle(role=parsed.soul, manifest=parsed, prompt=raw,
                  digest=digest(doc, raw))

def _filename(value):
    """Имя файла промпта: одно имя в каталоге роли, не путь. Абсолютный
    путь, вложенность и побег вверх -- не «где лежит промпт», а попытка
    указать за пределы бандла."""
    if not (isinstance(value, str) and value
            and "/" not in value and "\\" not in value
            and value not in (".", "..")):
        _refuse("prompt", "one file name inside the role directory", value)
    return value


# ─── значение ─────────────────────────────────────────────────────────────
import dataclasses  # noqa: E402 -- значения ниже разбора, разбор их зовёт


@dataclasses.dataclass(frozen=True)
class Runtime:
    """Где роль живёт и принимает ли задания: isolating -- только драйверы
    с отдельными телами (решает placement), receives_dispatch -- цель
    dispatch."""
    driver: str
    receives_dispatch: bool


@dataclasses.dataclass(frozen=True)
class Capabilities:
    """Что роль может: глаголы узла и кластера порознь, право диспетчерить
    и право поднимать роли. Секреты -- отдельным allowlist (#592)."""
    node_verbs: tuple
    cluster_verbs: tuple
    dispatch: bool
    spawn: tuple


@dataclasses.dataclass(frozen=True)
class Secrets:
    files: tuple = ()
    vars: tuple = ()


@dataclasses.dataclass(frozen=True)
class Soul:
    """Разобранный soul.yaml: значения, а не словарь, -- читатель не сможет
    сослаться на ключ, которого нет, и получить молча None."""
    soul: str
    version: int
    runtime: Runtime
    capabilities: Capabilities
    secrets: Secrets
    prompt: str

    def to_dict(self):
        """Soul -> словарь манифеста (шина, реестр, manifest.json #581).
        Обратная -- from_manifest; значения примитивны, форма -- та же
        схема v1."""
        return {"soul": self.soul, "version": self.version,
                "runtime": {"driver": self.runtime.driver,
                            "receives_dispatch":
                                self.runtime.receives_dispatch},
                "capabilities": {
                    "node_verbs": list(self.capabilities.node_verbs),
                    "cluster_verbs": list(self.capabilities.cluster_verbs),
                    "dispatch": self.capabilities.dispatch,
                    "spawn": list(self.capabilities.spawn)},
                "secrets": {"files": list(self.secrets.files),
                            "vars": list(self.secrets.vars)},
                "prompt": self.prompt}


@dataclasses.dataclass(frozen=True)
class Bundle:
    """Бандл роли (#579): что закрепляет digest при регистрации. Промпт --
    сырые байты: они входят в digest и едут в тело как есть."""
    role: str
    manifest: Soul
    prompt: bytes
    digest: str


# ─── бандл: каталог роли ──────────────────────────────────────────────────
def digest(manifest, prompt):
    """Digest бандла -- формула контракта #579 одним общим местом:
    sha256(канонический JSON разобранного манифеста + NUL + сырой промпт).
    manifest -- словарь в ЛЮБОМ допустимом виде схемы (yaml как есть или
    to_dict): канонизацию до полного набора ключей делает _canonical, и
    оба входа бандла сходятся в один digest. Digest -- 64 знака hex
    (SHA256), НЕ HEX32 id принципала."""
    canonical = json.dumps(_canonical(manifest), sort_keys=True,
                           separators=(",", ":"), ensure_ascii=False).encode()
    raw = prompt if isinstance(prompt, bytes) else prompt.encode()
    return hashlib.sha256(canonical + b"\x00" + raw).hexdigest()


def load(path):
    """Каталог роли -> Bundle. Ровно два файла: soul.yaml и промпт, --
    лишний файл -- не бандл (digest его не считает, и «ровно два» --
    граница, а не привычка). Промпт -- из своего каталога, не симлинк
    наружу: бандл закрепляет содержимое, а не указатель на чужое."""
    if not os.path.isdir(path):
        raise ValueError(f"{path}: no such role directory")
    with open(os.path.join(path, MANIFEST), encoding="utf-8") as f:
        text = f.read()
    doc = _yaml(text)
    manifest = parse(text)
    prompt_path = os.path.join(path, manifest.prompt)
    if os.path.islink(prompt_path) or os.path.realpath(prompt_path) != \
            os.path.abspath(prompt_path):
        raise ValueError(
            f"{path}: the prompt points outside the role directory")
    found = sorted(os.listdir(path))
    if found != sorted([MANIFEST, manifest.prompt]):
        raise ValueError(
            f"{path}: a role directory holds exactly {MANIFEST} and the "
            f"prompt, found: {', '.join(found)}")
    with open(prompt_path, "rb") as f:
        raw = f.read()
    try:
        raw.decode("utf-8")
    except UnicodeDecodeError as e:
        raise ValueError(f"{path}/{manifest.prompt}: not UTF-8: {e}") from None
    return Bundle(role=manifest.soul, manifest=manifest, prompt=raw,
                  digest=digest(doc, raw))


def catalogue(dirs):
    """Каталоги ролей -> {роль: каталог}: пакет и внешние каталоги
    установки. Имя роли -- из soul.yaml, не из имени каталога. Кто раньше
    в dirs -- тот и хозяин имени: приоритет каталогов называет вызывающий,
    а не порядок на диске; повтор позже молча пропущен. Подкаталог без
    soul.yaml -- не роль; сломанный soul.yaml -- громкий отказ: роль,
    молча пропавшая из каталога, хуже ошибки при подъёме."""
    out = {}
    for d in dirs:
        if not os.path.isdir(d):
            raise ValueError(f"{d}: no such role directory")
        for entry in sorted(os.listdir(d)):
            role_dir = os.path.join(d, entry)
            manifest = os.path.join(role_dir, MANIFEST)
            if not os.path.isfile(manifest):
                continue
            with open(manifest, encoding="utf-8") as f:
                soul = parse(f.read()).soul
            if soul not in out:
                out[soul] = role_dir
    return out


# ─── принципалы и записи ──────────────────────────────────────────────────
def _hex32(value, field, where="soul.yaml"):
    if not (isinstance(value, str) and HEX32.match(value)):
        _refuse(field, "32 lower-case hex characters", value, where)
    return value


def _hex64(value, field, where="soul.yaml"):
    if not (isinstance(value, str) and HEX64.match(value)):
        _refuse(field, "64 lower-case hex characters (SHA256)", value, where)
    return value


def _uuid(value, field, where="soul.yaml"):
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError, TypeError):
        _refuse(field, "a UUID", value, where)
    if str(parsed) != value:
        _refuse(field, "a canonical UUID string", value, where)
    return value


def is_incarnation(value):
    """Каноническая строка UUID инкарнации (str(uuid4), #579)? Грамматика
    одной функцией: запись привязки (#581), лаунчер (#582) и клиент тела
    (#585: корень инкарнации в раскладке) читают её одинаково, и копия в
    клиенте разъехалась бы с первой же правкой формы."""
    try:
        return str(uuid.UUID(value)) == value
    except (ValueError, AttributeError, TypeError):
        return False


def grant_user(grant_id):
    """id принципала (32 знака нижнего hex) -> имя на шине grant-<id>.
    Публичный идентификатор, не пароль (#579)."""
    return busnames.GRANT_PREFIX + _hex32(grant_id, "grant id")


def grant_id(user):
    """Имя шины -> id принципала или None: не принципал -- None, отказ
    здесь был бы ошибкой типа, а не договора."""
    if is_grant(user):
        return user[len(busnames.GRANT_PREFIX):]
    return None


def is_grant(user):
    """Это имя -- делегированный принципал роли? Легаси-пользователь
    проекта (puppet-<проект>) -- нет: префикс новый и зарезервирован."""
    return isinstance(user, str) \
        and user.startswith(busnames.GRANT_PREFIX) \
        and bool(HEX32.match(user[len(busnames.GRANT_PREFIX):]))


def incarnation():
    """Новая инкарнация: UUID v4 строкой. Стабильна, пока жива регистрация
    (#579): переживает рестарты и respec, сменяется лишь новой регистрацией
    -- её выдаёт #582, здесь только генерация."""
    return str(uuid.uuid4())


# Запись реестра привязок (#581): точные ключи, иное -- не запись. Хранит
# файл сервера, здесь форма и проверки -- чтобы узел (#584) и сервис
# кластера читали одну и ту же.
BINDING_KEYS = ("principal", "owner", "project", "job", "soul_inc",
                "soul_digest", "role", "active")


def binding(principal, owner, project, job, soul_inc, soul_digest, role,
            active=True):
    """Собрать запись привязки. -> словарь с точными ключами; мусор --
    ValueError с именем поля. active -- точный bool: строка 'false' --
    опечатка, а не согласие, и отказ, а не молчаливый True (review #580)."""
    record = {"principal": principal, "owner": owner, "project": project,
              "job": job, "soul_inc": soul_inc, "soul_digest": soul_digest,
              "role": role, "active": active}
    _check_binding(record)
    return record


def binding_from(record):
    """Словарь (файл реестра, шина) -> проекция записи: ТОЛЬКО ключи
    привязки, без копии. Сервер (#581) хранит запись с полями повыше
    (request_id, intent, revoked_at) -- они не его собственность common,
    и проекция читает ядро, не заставляя сервер держать вторую форму.
    Недостающий ключ ядра -- ValueError: реестр -- право, и «примерно
    такая» запись прав не даёт; лишний сверху -- жизнь сервера, не мусор."""
    if not isinstance(record, dict):
        _refuse("binding", "must be a mapping", record)
    for key in BINDING_KEYS:
        if key not in record:
            _refuse(key, "is required", "binding")
    _check_binding(record)
    return {key: record[key] for key in BINDING_KEYS}


def _check_binding(record):
    """Одни проверки на оба пути записи: собрать и прочесть."""
    W = "binding"
    if not is_grant(record["principal"]):
        _refuse("principal", "a delegated principal grant-<32 hex>",
                record["principal"], W)
    for key in ("owner", "project", "job"):
        if not (isinstance(record[key], str) and record[key]):
            _refuse(key, "a non-empty string", record[key], W)
    _uuid(record["soul_inc"], "soul_inc", W)
    # Digest бандла -- SHA256 (64 hex), не HEX32 id принципала: живой бандл
    # обязан проходить собственную привязку (review #580).
    _hex64(record["soul_digest"], "soul_digest", W)
    _name(record["role"], "role", W)
    if not isinstance(record["active"], bool):
        _refuse("active", "true or false", record["active"])
