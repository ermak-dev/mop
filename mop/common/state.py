"""Состояние папета: вердикт по фактам с узла и лечение по вердикту (#145).

Чистые функции, без сети. Раньше вердикт был английской фразой, и её
потребители — doctor, is_free, корзина дашборда, отчёт deploy — решали по
префиксу: переформулировка сообщения молча меняла и лечение, и решение
«свободен», на котором стоит диспатч. Теперь вердикт — State с видом (kind),
решения стоят на виде, а строка (str) — только для показа и не меняется.
"""
import re
import time
from dataclasses import asdict, dataclass

from .domain import CloneFacts, holds_work

# Имена внутри спеки, которые читает и ростер: группа задач папета -- по ней
# JobSummary считает Queued. Одно место на спеку (сервер) и вердикты (все).
GROUP = "puppets"


def queued(job):
    """Сколько аллокаций джоба ждёт места у планировщика."""
    return (job.get("JobSummary", {}).get("Summary", {}).get(GROUP) or {}).get("Queued", 0)



@dataclass(frozen=True)
class State:
    """Вердикт о папете: вид, подробность, ветка.

    Решения стоят на kind: лечение (action_for), свобода (is_free), корзина
    дашборда, отчёт deploy. detail и branch — только для показа, и str()
    складывает из них ровно ту строку, что `mop list` показывал до #145."""
    kind: str
    detail: str = None
    branch: str = None

    def __str__(self):
        return _SHOW[self.kind](self.detail, self.branch)


@dataclass(frozen=True)
class PuppetRow:
    """Строка ростера мастера (#204): то, что читают `mop list`, MCP, gc и
    дашборд. state -- строка вердикта для показа, kind -- его вид, на нём
    решения; owner -- логин с живой арендой или прочерк; disk_kb -- клон
    плюс target, None -- обмер не доехал (прочерк, а не ноль).

    Порядок полей -- порядок ключей прежнего словаря: to_dict уходит в
    снимок дашборда, и страница читает его по имени.

    «Здесь ничего» -- None (#274): узла нет без аллокации, владельца -- без
    живой аренды, origin -- без ключа в мете, состояния -- без ответа.
    Прочерк и вопрос -- только показ (render, SHOWN): раньше их писала
    сборка строки, и решения сравнивали строку `"-"`."""
    name: str
    node: str
    alloc_status: str
    state: str
    kind: str
    owner: str
    llm: str
    origin: str
    disk_kb: int = None

    def render(self):
        """Строка для человека, модели и страницы: None -- прежним прочерком
        или вопросом. Байт в байт то, что раньше лежало в самой строке."""
        out = asdict(self)
        for field, shown in SHOWN.items():
            if out[field] is None:
                out[field] = shown
        return out

    def to_dict(self):
        """Провод дашборда -- показанная строка: страница читает прочерки."""
        return self.render()

    @classmethod
    def from_dict(cls, d):
        """Провод -> строка; показанное «ничего» -- обратно None. Незнакомый
        ключ -- TypeError: молча лишнее поле и было болезнью."""
        return cls(**{k: (None if SHOWN.get(k) is not None and v == SHOWN[k] else v)
                      for k, v in d.items()})


# Как показывается «ничего» в строке ростера (#274): поле -> текст.
SHOWN = {"node": "-", "state": "-", "owner": "-", "origin": "?"}


def _tail(head, sep, tail):
    return f"{head}{sep}{tail}" if tail else head


# Вид -> строка для человека и модели. Формулировки меняются только здесь, и
# ни одно решение от них не зависит.
_SHOW = {
    # Молчит агент узла, а не папет: отдельный вид, не «завис».
    "silent": lambda d, b: f"AGENT SILENT ({d})",
    "hung": lambda d, b: f"HUNG ({d})",
    "dialog": lambda d, b: _tail("needs action", ": ", d),
    "login": lambda d, b: _tail(d, ": ", b),
    "quota": lambda d, b: _tail("no model quota", ": ", d),
    "error": lambda d, b: f"error: {d}",
    "waiting": lambda d, b: "waiting for input",
    "busy": lambda d, b: _tail("busy", ": ", b),
    # Незнакомый статус файла сессии показывается как есть: см. _state_from_session.
    "other": lambda d, b: _tail(d, ": ", b),
    "free": lambda d, b: f"free ({b})" if b else "free",
    "idle": lambda d, b: f"idle: {b} ({d})" if b else f"idle ({d})",
    "unknown": lambda d, b: f"unknown ({d})",
    "failing": lambda d, b: f"FAILED: {d}",
}
KINDS = tuple(_SHOW)


def silent(answer):
    """Агент узла не ответил: про самого папета не известно ничего."""
    return State("silent", str(answer))


# ─── состояние папета ────────────────────────────────────────────────────
# как узнать реальное состояние папета
#
# Узел присылает факты, вердикт собираем здесь. Папет сам ведёт две вещи, и
# они и есть источник правды (обе появляются независимо от моста claude.ai):
#
# 1) файл активности ~/.claude/sessions/<pid>.json: cwd (по нему матчим — он
#    стабилен, в отличие от name), status (idle|busy|shell|requires_action|
#    waiting|offline; список открытый), pid, messagingSocketPath. Слабость: после грязной смерти
#    процесса файл остаётся с протухшим status.
# 2) сокет живости: успешный connect -> процесс жив и слушает. Строже файла:
#    ловит смерть мгновенно, без всяких таймстампов.
#
# Итог: сокет = живость, файл = активность.
#   нет коннекта + pid мёртв ....... offline (файл протух, не верим)
#   нет коннекта + pid жив ......... hung    (сессия рушится/подвисла)
#   коннект есть + idle ............ free
#   коннект есть + busy ............ busy
#   коннект есть + requires_action . needs action
#   коннект есть + waiting ......... waiting for input
#
# Сами строки состояния — по-английски: их читает не только человек, но и
# модель (mop list, инструменты MCP), а промпты и скиллы пула англоязычны.
# Нет файла (сессия ещё не поднялась) -> None, и последнее слово за клоном.
# Исход хода (протухший логин, отказ провайдера) пишут хуки в запись хода,
# агент везёт её фактом state (#224). Экран пейна вердикт не читает (#235):
# разбор был слеп — жалоба посреди хода рисуется не там, где её искали, а
# футер у каждого диалога свой. Экран остался для глаз: tail, slash, attach.
# Папет без хуков — случай перерегистрации, его показывает doctor.
#
# Пробник живёт в mop/session.py и исполняется агентом на узле: сокет папета
# host-local, снаружи к нему не подключиться.
#
# Всё, что ниже, — чистые функции над этими фактами. Так вышло не случайно:
# пока состояние собиралось поверх exec, проверить его без живого пула было
# нельзя, и регрессия однажды спряталась именно здесь.
# Известные статусы файла сессии. Список справочный: незнакомый статус
# больше не отбрасывается (см. _session_state), иначе новое слово claude
# молча уводил вердикт в скоринг по буферу (скоринга больше нет, #235).
SESSION_STATES = ("idle", "busy", "shell", "requires_action", "waiting", "offline")


def _session_state(line):
    """Ответ пробника "<status> <alive> <listen>" -> состояние сессии, либо
    None, если файла сессии нет.

    Незнакомый статус возвращается как есть, и это важно: раньше он приравнивался
    к отсутствию файла, а значит уводил в древний скоринг по словам. Так и
    случилось, когда claude завёл статус `shell` (сессия выполняет команду):
    занятые папета показывались просто «busy», без ветки, — но это было не
    состояние сессии, а угадайка по буферу. Она же могла насчитать в буфере
    больше «idle», чем «working», и объявить занятого папета свободным, а на
    этом ответе стоит решение мастера о диспатче."""
    if not line or line == "none":
        return None
    parts = line.split()
    if len(parts) < 3:
        return None
    status, alive, listen = parts[0], parts[1] == "1", parts[2] == "1"
    if not listen:
        return "hung" if alive else "offline"
    return status


def _clone(f):
    """Факты клона из фактов узла -> domain.CloneFacts или None (#266)."""
    return CloneFacts.from_dict((f or {}).get("clone"))


def _branch(clone):
    """Ветка клона, любая, в том числе дефолтная, — или None."""
    return clone.branch if clone else None


def _work_branch(clone):
    """Ветка папета, если она не дефолтная, — иначе None.

    Занятость места — это не имя ветки, и здесь оно нужно только чтобы
    показать человеку, где папет сидит."""
    if not clone:
        return None
    cur = clone.branch
    return cur if cur and cur != clone.default_branch else None


def _clone_veto(clone):
    """Что в клоне мешает назвать место свободным. -> состояние или None.

    Одна проверка на все пути, ведущие к «free». Путей четыре — файл сессии,
    пустой пейн, скоринг по буферу, откат по ветке, — а в клон смотрел только
    первый. Поймано 01.09 на pu-rugent-3: файла сессии не было, папет стоял на
    дефолтной ветке (её имя откат глушит намеренно, поэтому в строке не было
    даже ветки), в клоне лежал незапушенный мерж — ростер сказал «free».
    Мастер, доверяющий строке, задиспатчил бы поверх. Пустой пейн — тот же
    провал и ровно то окно после рестарта, где неспасённая работа и лежит.

    Отсутствие данных — не свобода. Клон заводит врапер до tmux-сессии, так
    что у живого папета он есть всегда; пустой ответ означает отвалившуюся
    пробу (у неё 20 секунд таймаута, и git на узле под сборкой в них
    упирается), а не пустое место. Мастер должен увидеть «не знаю» и пойти
    смотреть сам, а не гадать.

    Пропадает только то, чего нет ни на одной удалённой ветке. Числа считает
    агент через `--not --remotes`, а не через `@{u}..`: upstream рабочей ветки
    бывает прибит к origin/master, и тогда всё невлитое врёт как «не
    отправлено» (поймано на живом папете).

    «uncommitted» и «unpushed» показываем раздельно: это разные состояния и
    разный разговор с агентом. Сложив их в одно «только локально: 3», мастер
    однажды сказал переродившемуся месту «у тебя было 3 локальных коммита»,
    тогда как там лежали 3 несохранённых файла и ноль коммитов."""
    # Неполные данные — те же «нет данных»: агент отдаёт оба числа всегда или
    # не отдаёт клон вовсе, так что дырой это не станет, а `or 0` на пропуске
    # тихо превращал незнание в ноль, то есть в свободу.
    if not clone or not clone.known:
        return State("unknown", "no clone data")
    # Работа ли это -- решает одно правило на всех (#266): аренда и уборка
    # сирот спрашивают его же. Ветка работа только вне дома клона (#272).
    if not holds_work(clone):
        return None
    # idle, а не busy: сессия здесь стоит, занят только клон. Одним словом
    # на оба случая мастер читал «работает» там, где на деле лежит брошенная
    # посреди тикета работа, — а это разные разговоры: первого ждут, второго
    # спасают. Диспатчу оба одинаково запрещены, и это решает не слово, а
    # вид: свободен только вид free.
    return State("idle", ", ".join(clone.work()), clone.branch)


def _state_from_session(st, clone):
    """Состояние места по достоверному статусу сессии.

    Файл сессии авторитетен про активность, но не про занятость места: клон
    переживает смерть сессии. Поэтому «free» отсюда — ответ предварительный:
    в клон за него смотрит _clone_veto, один на все пути."""
    if st == "requires_action":
        return State("dialog")
    if st == "waiting":
        return State("waiting")
    if st in ("offline", "hung"):
        return State("hung", "not responding")
    # `shell` — claude выполняет команду; для нас это та же работа, что busy.
    #
    # Ветку показываем любую, в том числе дефолтную: свободное место её и так
    # называет («free (master)»), и молчание у занятого читалось как «ветки
    # нет вообще», хотя папет просто работал на дефолтной.
    if st in ("busy", "shell"):
        return State("busy", branch=_branch(clone))
    # Незнакомый статус — не повод считать место свободным. Показываем как есть:
    # так новое слово claude видно сразу, а не прячется за угадыванием.
    if st != "idle":
        return State("other", st, _work_branch(clone))

    return State("free", branch=_branch(clone))


def _state_from_turn(f):
    """Вердикт по записи хода, которую пишут хуки (#224), -> State или None.

    Исход хода claude сообщает хуком StopFailure с кодом ошибки, а экран его
    только рисует — и рисует репликой над рамкой ввода, где разбор экрана
    логин не искал (там жалобы из истории). 24.09 pu-mop-2 и pu-mop-3 умерли
    посреди хода на «Login expired» и полтора часа читались «idle» с
    несохранённой работой: отчёта провал хода не шлёт, мастер ждал.

    None — записи хода нет (папет не перерегистрирован с хуками, #223, или
    ещё не начал сессию) или нет сессии: тогда решает один статус сессии.

    Занятая сессия бьёт запись: новый ход уже идёт, и старый провал к нему не
    относится. Незнакомый код — ошибка, а не свобода: список кодов растёт с
    версиями claude."""
    st = f.get("state") or {}
    rec = st.get("turn")
    status = _session_state(f.get("session"))
    if not rec or status is None:
        return None
    if status == "waiting" and st.get("waitingFor"):
        return State("dialog", st["waitingFor"])
    code = rec.get("error")
    if status != "idle" or rec.get("event") != "StopFailure" or not code:
        return _state_from_session(status, _clone(f))
    if code == "authentication_failed":
        return State("login", "login expired", _work_branch(_clone(f)))
    if code == "billing_error":
        return State("quota", rec.get("detail") or None)
    return State("error", rec.get("detail") or code)


def _state_from_clone(clone):
    """Последний откат: одна лишь ветка клона.

    «free» здесь такой же предварительный: и несохранённое, и полное
    отсутствие данных о клоне разбирает _clone_veto."""
    branch = _work_branch(clone)
    return State("busy", branch=branch) if branch else State("free")


def verdict(f):
    """Занятость места по фактам с узла -> State. Чистая функция: см. блок выше.

    «Свободен» означает, что в клоне нет несохранённой работы, а не что сессия
    молчит. На этом стоит решение мастера о диспатче, и ломать условие нельзя.

    Поэтому вердикт собирается в два шага: _verdict отвечает про активность и
    вправе сказать «free», а последнее слово за клоном — _clone_veto. Пока
    вето стояло внутри одной ветки _verdict, три остальные дороги к «free»
    обходили клон стороной."""
    state = _verdict(f)
    if not is_free(state.kind):
        return state
    return _clone_veto(_clone(f)) or state


def _verdict(f):
    """Активность папета по записи хода и файлу сессии. Про клон см. verdict."""
    if not f or f.get("error"):
        return State("hung", (f or {}).get("error", "no answer")[:40])
    if not f.get("present"):
        return State("hung", "no tmux session")

    hooked = _state_from_turn(f)
    if hooked:
        return hooked

    st = _session_state(f.get("session"))
    if st is not None:
        return _state_from_session(st, _clone(f))

    return _state_from_clone(_clone(f))


def is_free(kind):
    """Свободен ли папет по виду вердикта (State.kind; None — не спрашивали).

    По виду, а не по строке: строку разбирали префиксом, потому что у
    свободного места она несёт ещё и ветку — «free (master)», — и любая
    переформулировка молча меняла ответ. На этом ответе стоит и решение о
    диспатче, и выбор жертвы для рецикла."""
    return kind == "free"


# ─── падающий на старте папет (#126) ────────────────────────────────────
# Nomad держит аллокацию, чья задача упала и ждёт перезапуска, в `pending` --
# том же слове, что у аллокации, которой ещё ищут место. Ростер читал только
# ClientStatus и показывал падающего папета ищущим место; причина была видна
# лишь в `nomad alloc logs -stderr`. Сводку задачи и причину собирает сервис
# кластера (у него Nomad), вердикт -- здесь.
_WRAPPER_FAIL = re.compile(r"^(\S+) of pu-\S+ (?:failed|brought no )|did not reach the body")
_ANSIBLE_NOISE = ("[ERROR]: ", "Task failed: ", "Unexpected AnsibleActionFail error: ")
# Строка врапера с упавшей задачей bootstrap'а (#333, run.refusal).
_TASK_FAIL = re.compile(r"^bootstrap of pu-\S+ failed at task «(.+?)»: (.*)$")


def task_name(alloc):
    """Имя задачи папета в аллокации | None. Одна на сводку и на чтение
    stderr (#152): разойдись они, причина падения читалась бы из чужой
    задачи."""
    states = (alloc or {}).get("TaskStates") or {}
    return sorted(states)[0] if states else None


def task_summary(alloc, now=None):
    """Задача аллокации -> {state, restarts, exit, next_s, failed} | None.

    exit -- код последнего выхода, next_s -- через сколько секунд от now Nomad
    перезапустит (только если задача сейчас этого и ждёт): время события плюс
    задержка, а не задержка сама -- та верна лишь в миг события."""
    name = task_name(alloc)
    if name is None:
        return None
    t = alloc["TaskStates"][name]
    events = t.get("Events") or []
    exits = [e.get("ExitCode") for e in events if e.get("Type") == "Terminated"]
    last = events[-1] if events else {}
    next_s = None
    if last.get("Type") == "Restarting" and last.get("StartDelay"):
        at = (last.get("Time") or 0) + last["StartDelay"]
        now = time.time() if now is None else now
        next_s = max(0, int(at // 1_000_000_000 - now))
    return {"state": t.get("State"), "restarts": t.get("Restarts") or 0,
            "exit": exits[-1] if exits else None,
            "next_s": next_s,
            "failed": bool(t.get("Failed"))}


def failure_reason(stderr):
    """Хвост stderr задачи -> одна строка причины | None.

    В stderr копятся все попытки, поэтому -- последняя: строка врапера
    («bootstrap of pu-x failed: …») и первая ошибка ansible после неё, она
    и есть корень. Врапер назвал упавшую задачу (#333) -- она и её
    сообщение. Незнакомый вывод -- последняя непустая строка."""
    lines = [l.strip() for l in (stderr or "").splitlines() if l.strip()]
    if not lines:
        return None

    def clean(line):
        for noise in _ANSIBLE_NOISE:
            if line.startswith(noise):
                line = line[len(noise):]
        return line[:200]
    marks = [i for i, l in enumerate(lines) if _WRAPPER_FAIL.search(l)]
    if marks:
        i = marks[-1]
        task = _TASK_FAIL.match(lines[i])
        if task:
            return f"bootstrap task «{task.group(1)}» failed: {task.group(2)}"[:200]
        m = _WRAPPER_FAIL.match(lines[i])
        errors = [l for l in lines[i + 1:] if l.startswith("[ERROR]: ")]
        if errors and m:
            return f"{m.group(1)}: {clean(errors[0])}"
        return clean(lines[i])
    errors = [l for l in lines if l.startswith("[ERROR]: ")]
    return clean(errors[-1] if errors else lines[-1])


def _human(seconds):
    if seconds >= 3600:
        return f"{seconds // 3600}h"
    if seconds >= 60:
        return f"{seconds // 60}m"
    return f"{seconds}s"


def failing_row(alloc_status, task, reason):
    """(колонка аллокации, колонка состояния) падающего папета | None.

    Падающий -- задача не работает, уже падала ненулём или сдалась. Работающую
    задачу с прошлыми падениями не трогаем: она поднялась."""
    if not task or task["state"] == "running":
        return None
    gave_up = task["failed"] or task["state"] == "dead"
    if not gave_up and not (task["restarts"] and task["exit"] not in (None, 0)):
        return None
    why = reason or f"exit {task['exit']}"
    when = "gave up" if gave_up else (f"next in {_human(task['next_s'])}"
                                      if task["next_s"] else "restarting")
    return "failing", str(State("failing", f"{why} ({task['restarts']} restarts, {when})"))


# ─── лечение ─────────────────────────────────────────────────────────────
_ACTIONS = {
    # Молчит агент, а не папет. Рестарт папета тут ничего не лечит и вполне
    # может убить живую работу в клоне: про сам папет мы в этот момент не
    # знаем ничего. Показать — да, трогать — нет.
    "silent": None,
    "hung": "restart",
    # Сессия жива, но упёрлась в запрос действия и сама не сдвинется.
    # "waiting for input" не трогаем: это бывает и нормальным межходовым
    # состоянием, автолечить его опасно — только показываем.
    #
    # Рестарт лечит и залипание на диалоге: разрешение на подъём истории
    # разовое, и врапер погасил его маркером ещё до запуска claude — папет
    # поднимется чисто и спрашивать будет не о чем.
    "dialog": "restart",
    # Протухший логин -- не рестарт (#290): claude перечитывает креды на
    # каждом ходу, а ход, оборванный отказом API, сам не продолжается и
    # отчёта не шлёт. Раздать креды и разбудить сообщением -- разговор
    # папета цел; рестарт поднял бы сессию начисто (26.09: три папета
    # rugent потеряли контекст задач).
    "login": "login+nudge",
    "quota": "model",
    "error": "model",
}


def spec_action(kind):
    """Лечение устаревшей спеки по виду вердикта (#174). Перерегистрация
    перезапускает сессию: свободному (и тому, где сессии нет, kind None) это
    ничего не стоит, занятому — убивает ход. У остальных спека показана и
    ждёт, пока папет освободится."""
    return "update" if kind in (None, "free") else None


def action_for(kind):
    """Лечение по виду вердикта. False — вид здоровый, проблемы нет; None —
    проблема есть, но трогать нельзя."""
    return _ACTIONS.get(kind, False)


