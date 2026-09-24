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
    снимок дашборда, и страница читает его по имени."""
    name: str
    node: str
    alloc_status: str
    state: str
    kind: str
    owner: str
    llm: str
    origin: str
    disk_kb: int = None

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d):
        """Незнакомый ключ -- TypeError: молча лишнее поле и было болезнью."""
        return cls(**d)


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
# Нет файла (старый claude / нет python3) -> None, и мы откатываемся на
# прежнюю tmux-эвристику. Детект протухшего логина остаётся на tmux — в файле
# он не виден.
#
# Пробник живёт в mop/session.py и исполняется агентом на узле: сокет папета
# host-local, снаружи к нему не подключиться.
#
# Всё, что ниже, — чистые функции над этими фактами. Так вышло не случайно:
# пока состояние собиралось поверх exec, проверить его без живого пула было
# нельзя, и регрессия однажды спряталась именно здесь.
# Известные статусы файла сессии. Список справочный: незнакомый статус
# больше не отбрасывается (см. _session_state), иначе новое слово claude
# молча уводит вердикт в скоринг по буферу.
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


# Следы хода: результат инструмента, запуск команд, реплика модели. Индикатор
# «✻ Baked for 49m · done» в этот список не входит намеренно — им подписан и
# тот самый ход, который отказом и кончился.
_TURN_AFTER = re.compile(r"^\s*(⎿|Ran \d|● (?!API Error))", re.M | re.I)


def _outlived(activity, low, mark):
    """Пережита ли жалоба, стоящая в буфере на позиции mark.

    Восстановленная сессия приносит с собой весь скроллбэк, включая отказ, на
    котором её когда-то оборвало: pu-cloudpub-1 поднялся с 251.5k токенов
    истории, прекрасно работал под другой моделью — и читался в ростере
    больным, потому что жалоба в буфере была.

    Отличает живого от больного не сама жалоба, а то, что под ней. У живого
    ниже лежат следы ходов; у больного — только подпись оборванного хода,
    сводка задач и пустая рамка ввода. Переключение модели считается тем же
    доказательством: жалоба на прежнюю модель к новой не относится."""
    if low.rfind("set model to") > mark:
        return True
    return bool(_TURN_AFTER.search(activity, mark))


def _screen_complaint(activity):
    """Жалоба, видимая только на экране. -> State или None.

    Оба случая — один класс: сессия жива, отвечает на ping'и, но ни одного хода
    выдать не может. Для мастер это неотличимо от молчания.
    """
    low = activity.lower()
    # Модальный диалог claude. Ловим его по футеру, а не по тексту конкретного
    # вопроса: «Enter to confirm · Esc to cancel» стоит под любым выбором, и
    # список вопросов, которые claude умеет задать, нам не принадлежит — он
    # растёт с каждой версией, а список причин залипания расти не должен.
    #
    # Только в хвосте экрана: диалог рисуется внизу, а уехавший вверх футер
    # означает уже отвеченный вопрос. Поймано на живом папете 2026-08-31 —
    # выбор, чем поднимать историю (251.5k токенов), и файл сессии при этом
    # показывал живую сессию, так что ростер читал папета здоровым.
    lines = [l for l in activity.splitlines() if l.strip()]
    tail = lines[-2:]
    if any("enter to confirm" in l.lower() for l in tail):
        what = "resume prompt" if "resume full session as-is" in low else "dialog"
        return State("dialog", what)
    # Логин. Варианты экрана: "Not logged in · Run /login", "Login expired ·
    # Please run /login". Проверять до скоринга: у залипшего мид-таск в буфере
    # полно рабочих слов.
    #
    # Строки про Remote Control отсюда убраны вместе с самим --remote-control:
    # папета больше не ходят на мост claude.ai, и "/rc failed" на их экране
    # означало бы что угодно, только не болезнь. Для профилей с ключом
    # провайдера (glm) логин claude.ai вообще не при делах.
    # Логин ищем в хвосте экрана, и это не мелочь. Живая жалоба стоит в
    # статус-баре, который claude дорисовывает под рамкой ввода; жалоба из
    # прошлого приезжает вместе с историей (`--continue`) обычной репликой с
    # маркером «●» посреди буфера. Поймано 2026-08-31 сразу после раздачи
    # свежих кредов: папет поднялся залогиненным, а ростер держал его больным
    # по строке, которой был час от роду.
    #
    # _outlived здесь не годится: у только что поднявшегося папета ходов ещё
    # нет, и любая жалоба из истории выглядела бы свежей.
    foot = " ".join(l.lower() for l in lines[-3:])
    if "not logged in" in foot or "login expired" in foot:
        return State("login", "not logged in" if "not logged in" in foot else "login expired")
    # Квота модели и прочие отказы провайдера. Жалоба остаётся в скроллбэке и
    # после лечения — актуальна она только пока её не пережили.
    mark = low.rfind("out of usage credits")
    if mark >= 0 and not _outlived(activity, low, mark):
        m = re.search(r"keep using ([^\s]+(?: [0-9.]+)?)", activity, re.I)
        return State("quota", m.group(1) if m else None)
    mark = low.rfind("api error")
    if mark >= 0 and not _outlived(activity, low, mark):
        text = _api_error_text(activity)
        if text:
            return State("error", text)
    return None


def _api_error_text(activity):
    """Человекочитаемая часть отказа провайдера, либо None.

    Экран: «● API Error: Request rejected (429) · [1308][Usage limit reached
    for 5 hour. Your limit will reset at …][<request id>]». Мастеру нужен
    только средний блок: код и request id ему ничего не говорят, а «Request
    rejected (429)» умалчивает главное — когда квота вернётся.

    Сообщение длинное, и рендер claude переносит его на следующую строку. Где
    оно кончилось, видно по балансу скобок, а не по концу строки: пустые строки
    агент из пейна уже вырезал, поэтому следующий блок экрана начинается сразу
    за жалобой. Склейка нормализует отступ переноса — она врёт, если рендер
    разорвал слово посередине, и тогда в таблицу приедет лишний пробел.
    """
    m = re.search(r"API Error:", activity, re.I)
    if not m:
        return None
    buf, depth, opened = [], 0, False
    for line in activity[m.end():].splitlines()[:6]:
        buf.append(line.strip())
        depth += line.count("[") - line.count("]")
        opened = opened or "[" in line
        if opened and depth <= 0:
            break
    text = " ".join(b for b in buf if b).strip()
    # Блок со словами и есть сообщение: код и request id пробелов не содержат.
    for group in re.findall(r"\[([^\[\]]*)\]", text):
        if " " in group.strip():
            return group.strip()
    return text or None


def _tmux_guess(activity):
    """Древний скоринг по словам в буфере. Работает только там, где файла
    сессии нет (старый claude / нет python3 на узле). -> State или None."""
    low = activity.lower()
    work = sum(1 for x in ("working", "herding", "garnishing", "finding",
                           "checking", "running") if x in low)
    idle = sum(1 for x in ("резерв", "reserve", "waiting", "idle",
                           "свободен", "await") if x in low)
    if work > idle:
        return State("busy")
    if idle > work:
        return State("free")
    return None


def _work_branch(clone):
    """Ветка папета, если она не дефолтная, — иначе None.

    Занятость места — это не имя ветки, и здесь оно нужно только чтобы
    показать человеку, где папет сидит."""
    if not clone:
        return None
    cur, default = clone.get("cur"), clone.get("def")
    return cur if cur and cur != default else None


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
    if not clone or clone.get("dirty") is None or clone.get("ahead") is None:
        return State("unknown", "no clone data")
    dirty, ahead = clone["dirty"], clone["ahead"]
    if not (dirty or ahead):
        return None
    # idle, а не busy: сессия здесь стоит, занят только клон. Одним словом
    # на оба случая мастер читал «работает» там, где на деле лежит брошенная
    # посреди тикета работа, — а это разные разговоры: первого ждут, второго
    # спасают. Диспатчу оба одинаково запрещены, и это решает не слово, а
    # вид: свободен только вид free.
    what = ", ".join(p for p in (f"uncommitted: {dirty}" if dirty else "",
                                 f"unpushed: {ahead}" if ahead else "") if p)
    return State("idle", what, clone.get("cur"))


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
        return State("busy", branch=(clone or {}).get("cur"))
    # Незнакомый статус — не повод считать место свободным. Показываем как есть:
    # так новое слово claude видно сразу, а не прячется за угадыванием.
    if st != "idle":
        return State("other", st, _work_branch(clone))

    return State("free", branch=(clone or {}).get("cur"))


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
    return _clone_veto(f.get("clone")) or state


def _verdict(f):
    """Активность папета по экрану и файлу сессии. Про клон см. verdict."""
    if not f or f.get("error"):
        return State("hung", (f or {}).get("error", "no answer")[:40])
    if not f.get("present"):
        return State("hung", "no tmux session")

    activity = f.get("screen") or ""
    if not activity.strip():
        return State("free")

    complaint = _screen_complaint(activity)
    if complaint:
        if complaint.kind != "login":
            return complaint
        return State("login", complaint.detail, _work_branch(f.get("clone")))

    st = _session_state(f.get("session"))
    if st is not None:
        return _state_from_session(st, f.get("clone"))

    guess = _tmux_guess(activity)
    if guess:
        return guess

    return _state_from_clone(f.get("clone"))


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
    и есть корень. Незнакомый вывод -- последняя непустая строка."""
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
    "login": "login+restart",
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


