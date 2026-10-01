"""Доменные значения пула: проект, мета джоба (#265), владелец задания, факты
клона, аллокация Nomad (#377), глагол (#204). Данные, без печати и без ввода-вывода.

Раньше это были словари и кортежи: строковые ключи расходились молча --
тот же класс дефектов, что закрыли State (#145) и разбор origin (#154).
Значение здесь заморожено и полями названо; JSON наружу (шина, файл) --
через to_dict/from_dict, и только у того типа, который из процесса уходит.
Сервисы остаются функциями над значениями, а не методами иерархий.

Модуль узловой: его читают и агент узла, и мастер, поэтому из пакета --
только driver, где живёт единственное определение проекта. Строка ростера
(PuppetRow) -- рядом со своим вердиктом, в mop/common/state.py.
"""
from dataclasses import dataclass, field

from .. import driver


# ─── инварианты (#273) ───────────────────────────────────────────────────
def _refuse(value, field, rule):
    """Отказ значения одним местом: ValueError с именем поля. Замороженное
    значение без инварианта строилось молча, и каждый читатель проверял сам."""
    raise ValueError(f"{type(value).__name__}.{field}={getattr(value, field)!r}: {rule}")


def _count(v):
    """Счётчик клона: неотрицательное целое (bool -- не число)."""
    return isinstance(v, int) and not isinstance(v, bool) and v >= 0


def _optional_str(v):
    return v is None or isinstance(v, str)


# ─── проект ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Project:
    """Проект пула: origin из реестра, потолок папетов (#107) и просьбы его
    `.mop` (#197). Имя не хранится -- выводится из origin: хранимое однажды
    разошлось бы с правилом driver.project_of.

    Лимиты и просьбы на диске -- таблицы по имени проекта (limits.json,
    project-asks.json); из таблицы в проект их переводит одно место, of."""
    origin: str
    limit: int = None
    asks: dict = field(default_factory=dict)

    @property
    def name(self):
        return driver.project_of(self.origin)

    @classmethod
    def of(cls, origin, limits=None, asks=None):
        """Проект по origin и таблицам {проект: ...}. Просьбы копируются:
        правка значения не должна доехать до чужой таблицы. Пусто только
        там, где таблицу не дали: битый файл (JSON null) падает, как падал."""
        name = driver.project_of(origin)
        limits = {} if limits is None else limits
        asks = {} if asks is None else asks
        return cls(origin, limits.get(name), dict(asks.get(name) or {}))


# ─── мета джоба ──────────────────────────────────────────────────────────
# Ключи Meta джоба папета (#265). Пишет их spec.job_spec (через to_meta),
# читают все, кто по джобу решает, чей это папет, на каком он профиле и
# ветке и свежа ли его спека.
SPEC_META = "mop_spec"


@dataclass(frozen=True)
class JobMeta:
    """Meta джоба папета: origin, профиль LLM, ветка мастера (#256), версия
    шаблона спеки (#174).

    Раньше её читали сырым .get в семи модулях с разными умолчаниями ("",
    None, "?"), а перерегистрацию по ней писали четыре места -- и они
    разошлись: сборка образа теряла ветку (#265). Отсутствующий ключ здесь --
    читателя, одно на каждое. Имя проекта не хранится -- выводится из origin
    одним правилом driver.project_of."""
    origin: str
    branch: str = None
    spec_version: str = None

    def __post_init__(self):
        # origin -- None (джоб без Meta: законно, это «ничей») либо
        # непустая строка; пустая -- ни то ни другое (#273).
        if not (self.origin is None or (isinstance(self.origin, str) and self.origin)):
            _refuse(self, "origin", "None or a non-empty string")

    @property
    def project(self):
        return driver.project_of(self.origin or "")

    @classmethod
    def from_meta(cls, meta):
        """Словарь Meta (из джоба или из ответа глагола spec) -> JobMeta."""
        m = meta or {}
        # cred старых мет игнорируется молча: аренды больше нет (#384), и
        # прочитанная -- не отказ, а прошлое, которое перезапишет respec.
        # llm старых мет игнорируется молча: профилей больше нет (#390).
        return cls(m.get("origin"), m.get("branch") or None, m.get(SPEC_META))

    @classmethod
    def from_job(cls, job):
        return cls.from_meta(raw(job))

    def to_meta(self):
        """Словарь для Nomad -- в порядке ключей, каким его писал job_spec:
        origin и ветка (если есть), версия шаблона."""
        out = {"origin": self.origin}
        if self.branch:
            out["branch"] = self.branch
        if self.spec_version is not None:
            out[SPEC_META] = self.spec_version
        return out


def raw(job):
    """Meta джоба как есть, словарём: для ответа глагола spec по шине, где
    форма -- то, что отдал Nomad."""
    return (job or {}).get("Meta") or {}


# ─── владелец задания ────────────────────────────────────────────────────
@dataclass(frozen=True)
class Owner:
    """Кто ведёт задание папета (#161) и когда последний раз слал.

    Два вида одного значения: строка `.git/mop-owner` (render/parse) и
    словарь в фактах клона, которые агент отдаёт по шине (to_dict/from_dict).
    Живость аренды -- lease.live: это политика, а не значение."""
    user: str
    at: int

    def render(self):
        """Запись для файла."""
        return f"{self.user}\t{int(self.at)}\n"

    @classmethod
    def parse(cls, text):
        """Строка файла -> Owner или None. Мусор -- не владелец."""
        user, sep, at = (text or "").strip().partition("\t")
        if not sep or not user.strip():
            return None
        try:
            return cls(user.strip(), int(at))
        except ValueError:
            return None

    def to_dict(self):
        return {"user": self.user, "at": self.at}

    @classmethod
    def from_dict(cls, d):
        """Словарь с шины -> Owner; нет записи -- None."""
        return cls(d["user"], d["at"]) if d else None


# ─── факты клона ─────────────────────────────────────────────────────────
@dataclass(frozen=True)
class CloneFacts:
    """Что держит клон папета (#266): ветка, несохранённое, неотправленное,
    владелец задания. Собирает агент узла (agent.clone_facts), решают мастер,
    сервис кластера и сам агент.

    По шине -- словарём с прежними ключами cur/def/...: во время раската
    агенты обеих версий говорят друг с другом, и провод не двигается.
    dirty/ahead -- None, если агент их не прислал: «не знаю», а не ноль.

    home -- дом клона (#272), `git config mop.home`: ветка мастера из меты
    джоба, иначе ветка по умолчанию; его пишет стадия клона `mop driver run`
    и wipe. Ключа нет (старый агент) или записи нет -- дом это ветка по
    умолчанию (home_branch), и версии во время раската решают одинаково."""
    branch: str = None
    default_branch: str = None
    home: str = None
    origin: str = None
    dirty: int = None
    ahead: int = None
    owner: Owner = None

    def __post_init__(self):
        # Оба числа или ни одного (#273): полусобранный объект не «известен»
        # наполовину, а сломан.
        if (self.dirty is None) != (self.ahead is None):
            missing = "ahead" if self.ahead is None else "dirty"
            _refuse(self, missing, "dirty and ahead come together, or neither")
        for f in ("dirty", "ahead"):
            v = getattr(self, f)
            if v is not None and not _count(v):
                _refuse(self, f, "a non-negative integer")

    @property
    def known(self):
        """Есть ли оба числа: без них про работу в клоне сказать нечего."""
        return self.dirty is not None and self.ahead is not None

    @property
    def home_branch(self):
        """Дом клона: записанный, иначе ветка по умолчанию."""
        return self.home or self.default_branch

    def work(self):
        """Что в клоне держит работу, для показа: пусто -- ничего (#272).
        Одно перечисление на правило (holds_work), строку ростера и отказ
        аренды: иначе причина в отказе разошлась бы с самим решением."""
        out = []
        if self.dirty:
            out.append(f"uncommitted: {self.dirty}")
        if self.ahead:
            out.append(f"unpushed: {self.ahead}")
        home = self.home_branch
        if self.branch and home and self.branch != home:
            out.append(f"off home {home}")
        return out

    def to_dict(self):
        """Ключ home -- только когда дом записан: без записи клон шлёт те
        же байты, что старый агент, и отсутствие ключа значит одно и то же
        в обе стороны (#272)."""
        d = {"cur": self.branch, "def": self.default_branch}
        if self.home:
            d["home"] = self.home
        d.update(origin=self.origin, dirty=self.dirty, ahead=self.ahead,
                 owner=self.owner and self.owner.to_dict())
        return d

    @classmethod
    def from_dict(cls, d):
        """Словарь с шины -> CloneFacts; нет данных -- None. Неполный словарь
        (строка work у du) -- то, что в нём есть."""
        if not d:
            return None
        return cls(d.get("cur"), d.get("def"), d.get("home"), d.get("origin"),
                   d.get("dirty"), d.get("ahead"), Owner.from_dict(d.get("owner")))


def holds_work(clone):
    """Есть ли в клоне работа: несохранённое, неотправленное или клон не на
    своём доме (#266, #272).

    Одно правило на всех: вердикт ростера, аренду, уборку сирот и ворота
    кластера. Ветка сама по себе -- не работа: с #256 папет стоит на ветке
    своего мастера намеренно, и она его дом. Чистый клон не на доме держит
    тикет: папет запушил и ждёт приёма отчёта, и через окно его брал бы
    другой мастер. Свежий диспатч без коммитов бережёт окно lease.WINDOW.
    Клон неизвестен или без чисел -- держит: «не знаю» не значит «пусто»."""
    if clone is None or not clone.known:
        return True
    return bool(clone.work())


# ─── узел ────────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class PoolNode:
    """Узел в ответе глагола pool (cluster.nomad_pool), #267, #277.

    Три формы, и форма названа (form), а не угадана по заполненным полям:
      down   -- не готов: {name, status};
      broken -- готов, но ёмкость не прочитать: {name, status, error};
      ready  -- {name, status, free_mb, total_mb, slots, slots_total, eligible}.
    Ёмкость у готового обязательна, у остальных её нет: одиннадцать
    необязательных полей на один класс собирали молча и готовый узел без
    памяти, и неготовый с ней (#277). Провод прежний, байт в байт."""
    name: str
    status: str
    free_mb: int = None
    total_mb: int = None
    slots: int = None
    slots_total: int = None
    eligible: bool = True
    error: str = None

    CAPACITY = ("free_mb", "total_mb", "slots", "slots_total")
    # Без slots_total готовый узел читается: сервис старше #243 его не шлёт,
    # и отказ здесь отнял бы пул у мастера на время раската.
    REQUIRED = ("free_mb", "total_mb", "slots")

    def __post_init__(self):
        if self.status != "ready" and self.error is not None:
            raise ValueError(f"node {self.name}: error on a node that is {self.status}")
        ready = self.form == "ready"
        for f in self.CAPACITY:
            value = getattr(self, f)
            if ready and value is None and f in self.REQUIRED:
                raise ValueError(f"node {self.name} (ready): {f} missing")
            if not ready and value is not None:
                raise ValueError(f"node {self.name} ({self.form}): {f} must be absent")

    @property
    def form(self):
        if self.status != "ready":
            return "down"
        return "broken" if self.error else "ready"

    @property
    def placeable(self):
        """Станет ли Nomad что-то сюда ставить: готов и открыт планированию.
        Сломанный (broken) -- готов, и eligible у него по умолчанию: как было."""
        return self.status == "ready" and self.eligible

    def to_pool(self):
        if self.form == "down":
            return {"name": self.name, "status": self.status}
        if self.form == "broken":
            return {"name": self.name, "status": self.status, "error": self.error}
        return {"name": self.name, "status": self.status, "free_mb": self.free_mb,
                "total_mb": self.total_mb, "slots": self.slots,
                "slots_total": self.slots_total, "eligible": self.eligible}

    @classmethod
    def from_pool(cls, d):
        return cls(d["name"], d.get("status"), d.get("free_mb"), d.get("total_mb"),
                   d.get("slots"), d.get("slots_total"), d.get("eligible", True),
                   d.get("error"))


@dataclass(frozen=True)
class NodeRow:
    """Строка узла в ответе глагола nodes (nodes.row), #267, #277:
    {name, driver, [error], serves, state, free_mb, total_mb, slots,
    slots_total} -- всегда все, ёмкость None у узла вне пула, error -- только
    у узла с отказом (#175). Статуса в ней нет: состояние планирования --
    строка state, и в форму pool строку не превратить."""
    name: str
    driver: str
    serves: str
    state: str
    free_mb: int = None
    total_mb: int = None
    slots: int = None
    slots_total: int = None
    error: str = None

    def to_row(self):
        # Поле error -- только у узла с отказом (#175): строка исправного прежняя.
        return {"name": self.name, "driver": self.driver,
                **({"error": self.error} if self.error else {}),
                "serves": self.serves, "state": self.state,
                "free_mb": self.free_mb, "total_mb": self.total_mb,
                "slots": self.slots, "slots_total": self.slots_total}

    @classmethod
    def from_row(cls, d):
        return cls(d["name"], d.get("driver"), d.get("serves"), d.get("state"),
                   d.get("free_mb"), d.get("total_mb"), d.get("slots"),
                   d.get("slots_total"), d.get("error"))


# ─── аллокация Nomad (#377) ──────────────────────────────────────────────
@dataclass(frozen=True)
class Alloc:
    """Аллокация папета -- то, что о ней возит сервис кластера (#377).

    Ходила по пакету сырым словарём: "NodeName" пятью идиомами в одиннадцати
    файлах, `== "running"` в нескольких местах -- тот же класс дефектов, что
    закрыли JobMeta (#265) и PoolNode (#277). Провод прежний, ключи Nomad байт
    в байт: в элементе ростера task/reason лежат рядом с аллокацией
    (to_dict()), в ответе глагола alloc -- внутри неё (to_dict(task=True)).

    Все поля необязательны: у старого ответа и у фикстуры часть ключей
    бывает не заполнена, и отсутствие -- None, а не отказ. Мусор (не тот тип)
    -- отказ с именем поля."""
    id: str = None
    job: str = None
    node: str = None
    client_status: str = None
    desired_status: str = None
    # Сводка задачи (state.task_summary) и причина падения (#126).
    task: dict = None
    reason: str = None

    KEYS = (("id", "ID"), ("job", "JobID"), ("node", "NodeName"),
            ("client_status", "ClientStatus"), ("desired_status", "DesiredStatus"))

    def __post_init__(self):
        for f, _ in self.KEYS + (("reason", "reason"),):
            if not _optional_str(getattr(self, f)):
                _refuse(self, f, "a string or absent")
        if not (self.task is None or isinstance(self.task, dict)):
            _refuse(self, "task", "a task summary (dict) or absent")

    @property
    def running(self):
        return self.client_status == "running"

    def to_dict(self, task=False):
        d = {key: getattr(self, f) for f, key in self.KEYS}
        if task:
            d.update(task=self.task, reason=self.reason)
        return d

    @classmethod
    def from_dict(cls, d):
        """Словарь аллокации (ответ API Nomad, элемент ростера, ответ глагола
        alloc) -> Alloc | None. Лишние ключи Nomad не читаются."""
        if d is None:
            return None
        return cls(**{f: d.get(key) for f, key in cls.KEYS},
                   task=d.get("task"), reason=d.get("reason"))


# ─── тело папета: ответы драйвера ────────────────────────────────────────
@dataclass(frozen=True)
class Body:
    """В чём живёт папет -- ответ ensure драйвера (#267). vmid None -- тело
    это сам узел (host), иначе номер тела гипервизора (pve).

    По шине и из драйвера -- прежний словарь {name, body, created, address}:
    раньше host отдавал body None, pve -- vmid, и каждый читатель разбирал
    ключи сам."""
    name: str
    vmid: object = None
    address: str = None
    created: bool = False

    def __post_init__(self):
        if not driver.valid_name(self.name):
            _refuse(self, "name", "a puppet name, pu-<project>-<n>")
        if not (self.vmid is None or (isinstance(self.vmid, int) and not isinstance(self.vmid, bool))):
            _refuse(self, "vmid", "None (the node itself) or an integer")
        if not isinstance(self.created, bool):
            _refuse(self, "created", "a boolean")

    def to_dict(self):
        return {"name": self.name, "body": self.vmid, "created": self.created,
                "address": self.address}

    @classmethod
    def from_dict(cls, d):
        """Ответ ensure -> Body; отказ ({error}) или пусто -- None. Нет
        created (старый агент) -- False; мусор вместо него -- отказ (#273)."""
        if not d or d.get("error"):
            return None
        return cls(d["name"], d.get("body"), d.get("address"), d.get("created", False))

    def describe(self):
        """Строка `mop driver run`: чем стало тело и где оно."""
        return (f"body {self.vmid or 'the node itself'}"
                + (f" at {self.address}" if self.address else ""))


@dataclass(frozen=True)
class Gone:
    """Что снёс destroy драйвера (#267): target -- что именно, vmid -- тело
    гипервизора целиком (pve) либо None -- сброшен клон на самом узле (host).

    Провод прежний: host -- {reset, target}, pve -- {destroyed, target}."""
    target: str
    vmid: object = None

    def __post_init__(self):
        if not (isinstance(self.target, str) and self.target):
            _refuse(self, "target", "a non-empty string: what was destroyed")

    @property
    def reset(self):
        """Клон сброшен на месте, а тело осталось: узел снести нельзя."""
        return self.vmid is None

    def to_dict(self):
        if self.reset:
            return {"reset": True, "target": self.target}
        return {"destroyed": self.vmid, "target": self.target}

    @classmethod
    def from_dict(cls, d):
        """Ответ destroy -> Gone; отказ ({error}) или пусто -- None."""
        if not d or d.get("error"):
            return None
        return cls(d.get("target"), d.get("destroyed"))


# ─── глагол ──────────────────────────────────────────────────────────────
@dataclass(frozen=True)
class Verb:
    """Строка таблицы прав: обработчик, кому дан, называет ли папета.

    Один на агента узла (#150) и сервис кластера (#173): два namedtuple
    расходились полями. acting -- только у сервиса кластера (глагол ДЕЛАЕТ,
    и отсутствие джоба ему -- отказ); у агента его нет, и там он всегда
    False."""
    fn: object
    scope: str
    named: bool
    acting: bool = False


# ─── ответ мастера на who (#319) ─────────────────────────────────────────
# Сколько ждать ответов who: мастер отвечает из памяти, а gather не знает,
# сколько ответов ждать, и честно досиживает до таймаута. Одно число на
# инструмент agents (mcp) и сборщик дашборда (web).
WHO_WAIT = 2


@dataclass(frozen=True)
class MasterAnswer:
    """Ответ мастера на опрос who: адрес для send, проект, логин, сессия,
    каталог. Пишет его mcp.on_inbox, читают инструмент agents и сборщик
    дашборда -- прежде каждый по своим строковым ключам.

    Поля -- как пришли: None -- поля не было. Умолчание показа («-», проект
    спрошенного инбокса) -- у читателя, у каждого своё."""
    master: object = None
    project: object = None
    user: object = None
    session: object = None
    cwd: object = None

    def to_dict(self):
        return {"master": self.master, "project": self.project, "user": self.user,
                "session": self.session, "cwd": self.cwd}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("master"), d.get("project"), d.get("user"), d.get("session"), d.get("cwd"))
