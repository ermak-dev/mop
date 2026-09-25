"""Доменные значения пула: проект, мета джоба (#265), владелец задания, глагол
(#204). Данные, без печати и без ввода-вывода.

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
    None; умолчания показа (ростер: "?") и профиля (llm.of_meta) -- у
    читателя, одно на каждое. Имя проекта не хранится -- выводится из origin
    одним правилом driver.project_of."""
    origin: str
    llm: str
    branch: str = None
    spec_version: str = None

    @property
    def project(self):
        return driver.project_of(self.origin or "")

    @classmethod
    def from_meta(cls, meta):
        """Словарь Meta (из джоба или из ответа глагола spec) -> JobMeta."""
        m = meta or {}
        return cls(m.get("origin"), m.get("llm"), m.get("branch") or None, m.get(SPEC_META))

    @classmethod
    def from_job(cls, job):
        return cls.from_meta(raw(job))

    def to_meta(self):
        """Словарь для Nomad -- в порядке ключей, каким его писал job_spec:
        origin, llm, ветка (только если есть), версия шаблона."""
        out = {"origin": self.origin, "llm": self.llm}
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
