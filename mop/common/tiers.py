"""Ярусы LLM: сильная модель, потом слабее, потом другой поставщик (#286).

В claude автоматического отката нет ни на модель ниже, ни на другой ключ,
поэтому политика целиком наша. Здесь она чистая: разбор настройки
MOP_LLM_TIERS и выбор хода по таблице кредитов. Кто её применяет -- реестр
кредитов (#283) и аренда (#284); лечение `quota` в doctor пока печатает
одну модель из MOP_FALLBACK_MODEL и на эту функцию не переведено.

Ходы стоят по-разному, и порядок правил -- это порядок цены:

  stay       свой кредит жив -- ничего не делать;
  swap_cred  другой живой кредит того же провайдера -- горячо: claude
             перечитывает .credentials.json на каждом ходу (проверено
             26.09 на pu-mop-6). Ключ GLM сегодня уезжает в окружение tmux
             при старте claude (spec.py), так что для ключевых провайдеров
             подмена пока класса «рестарт» -- до аренды #284;
  model      ярус ниже того же провайдера -- горячо, `/model` в пейн
             (puppets.switch_model). Только когда провалилась МОДЕЛЬ
             (вердикт quota/error на ней), а кредит жив: у claude окно
             квоты общее на все модели, и при исчерпанном кредите /model
             не спасает;
  provider   ярус другого провайдера -- перерегистрация `mop update --llm`
             на том же узле (#289) с продолжением разговора (#291), то
             есть рестарт claude; под занятым папетом -- никогда без
             явного разрешения мастера, вместо этого wait;
  wait       живых кредитов нет либо ход дорог -- ждать: до ближайшего
             сброса окна, ручной авторизации или решения мастера.

Детерминизм: при равных вариантах -- порядок ярусов, потом имя кредита.
"""
from dataclasses import dataclass
import re
import time

from . import config, credreg

SETTING = "MOP_LLM_TIERS"
_ITEM = re.compile(r"^([A-Za-z][A-Za-z0-9_-]*)(?::([A-Za-z][A-Za-z0-9_.\[\]-]*))?$")


@dataclass(frozen=True)
class Tier:
    """Ярус: профиль LLM и модель в нём; model=None -- модель профиля по
    умолчанию (карта моделей профиля решает, что это)."""
    profile: str
    model: object = None


@dataclass(frozen=True)
class Move:
    """Ход политики: вид и цель. reason -- одна строка человеку и в журнал."""
    kind: str
    reason: str
    profile: object = None
    model: object = None
    cred: object = None
    resets_at: object = None

    KINDS = ("stay", "swap_cred", "model", "provider", "wait")

    def __post_init__(self):
        if self.kind not in self.KINDS:
            raise ValueError(f"no such move {self.kind!r}; one of {', '.join(self.KINDS)}")


def parse(text):
    """MOP_LLM_TIERS -> [Tier], от сильного к слабому. Пустые элементы и пробелы
    прощаются, остальное -- отказ ValueError с виновником: молча пропущенный
    ярус означал бы, что папет никогда не попадёт туда, куда его посылали.
    Имена профилей здесь не сверяются с реестром -- это делает default()."""
    out = []
    for raw in (text or "").split(","):
        item = raw.strip()
        if not item:
            continue
        m = _ITEM.match(item)
        if not m:
            raise ValueError(f"{SETTING}: {item!r} is not profile[:model]")
        tier = Tier(m.group(1), m.group(2))
        if tier in out:
            raise ValueError(f"{SETTING}: {item!r} is listed twice")
        out.append(tier)
    if not out:
        raise ValueError(f"{SETTING}: no tiers named")
    return out


def default(profiles):
    """Ярусы установки по настройке, сверенные с реестром профилей
    (llm.profiles()). Незнакомый профиль -- отказ с именем."""
    got = parse(config.get(SETTING))
    unknown = sorted({t.profile for t in got if t.profile not in profiles})
    if unknown:
        raise ValueError(f"{SETTING}: no such profile(s) {', '.join(unknown)}; "
                         f"known: {', '.join(sorted(profiles))}")
    return got


def _index(tiers, profile, model):
    """Позиция текущей пары в ярусах; модель None подходит любому ярусу
    профиля (берётся его первый); нет такого -- None."""
    for i, t in enumerate(tiers):
        if t.profile == profile and (model is None or t.model == model or t.model is None):
            return i
    return None


def _alive(creds, profile):
    """Живые кредиты профиля, по имени: детерминизм при равных."""
    return credreg.usable(profile, ((n, p, st.kind) for n, (p, st) in creds.items()))


def choose(current, creds, tiers, busy, model_failed=False, now=None):
    """Ход для папета. current -- (профиль, модель|None, имя кредита);
    creds -- {имя: (профиль, CredStatus)}; tiers -- [Tier] от сильного к
    слабому; busy -- есть ли у папета работа; model_failed -- провал был
    про модель при живом кредите (вердикт quota/error), а не про кредит.
    -> Move. Правила в порядке цены, см. докстринг модуля."""
    profile, model, cred = current
    mine = creds.get(cred)
    alive_here = mine is not None and mine[1].kind == "active"

    if alive_here and not model_failed:
        return Move("stay", f"{cred} is active")

    if alive_here and model_failed:
        # Модель ниже того же профиля, если она есть; иначе -- как будто
        # кредит кончился: следующий поставщик.
        here = _index(tiers, profile, model)
        lower = [t for t in tiers[(here + 1 if here is not None else 0):]
                 if t.profile == profile and t.model != model]
        if lower:
            return Move("model", f"{model or 'default'} failed on {cred}, "
                        f"weaker tier of {profile}: {lower[0].model}",
                        profile=profile, model=lower[0].model, cred=cred)

    # Другой живой кредит того же провайдера -- горячая подмена.
    others = [n for n in _alive(creds, profile) if n != cred]
    if others and not (alive_here and model_failed):
        return Move("swap_cred", f"{cred} is not usable, {others[0]} of {profile} is active",
                    profile=profile, cred=others[0])

    # Следующий поставщик по ярусам, у которого есть живой кредит.
    here = _index(tiers, profile, model)
    for t in tiers[(here + 1 if here is not None else 0):]:
        if t.profile == profile:
            continue
        names = _alive(creds, t.profile)
        if not names:
            continue
        if busy:
            return Move("wait", f"provider change to {t.profile} needs a restart; "
                        f"puppet is busy -- the master decides",
                        profile=t.profile, model=t.model, cred=names[0])
        return Move("provider", f"no usable credential of {profile}; "
                    f"{t.profile} has {names[0]} active",
                    profile=t.profile, model=t.model, cred=names[0])

    # Живых нет нигде: ждать сброса, если он назван, иначе -- логина.
    resets = sorted(st.resets_at for _, st in creds.values()
                    if st.kind == "quota_wait" and st.resets_at)
    if resets:
        when = time.strftime("%Y-%m-%d %H:%M", time.localtime(resets[0]))
        return Move("wait", f"no active credential in any tier; earliest reset {when}",
                    resets_at=resets[0])
    dead = sorted(n for n, (_, st) in creds.items() if st.kind == "needs_login")
    return Move("wait", "no active credential in any tier; "
                + (f"{', '.join(dead)} need a login" if dead else "nothing to wait for"))
