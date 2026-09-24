"""Агент узла: отвечает на запросы шины про папетов, которые живут здесь.

Заменил собой `alloc exec`. Раньше мастер гонял шелл внутрь аллокации и платил
рукопожатием за каждую пробу; теперь на узле сидит подписчик, а мастер шлёт
ему глагол.

Один на узел, не на папета. Переживает рестарт папета — а спрашивают о папете
чаще всего именно тогда, когда он перезапускается. И живёт вне спеки джоба:
правка спеки не доезжает до работающего папета рестартом аллокации, ей нужна
перерегистрация, а правка агента доезжает одним прогоном плейбука.

Набор глаголов закрыт. Через шину нельзя попросить «выполни шелл»: это был бы
тот же management-токен Nomad, только по другой трубе, — а ради того, чтобы его
с узлов убрать, всё и затевалось. Отсюда же белые списки на `type` и `write`:
произвольная команда в чужой TUI и произвольная запись в $HOME — это два разных
способа получить исполнение кода на узле.

Проект агент пересекает сознательно: он и есть то, что проекты разделяет, и
обслуживает всех жильцов узла. Прав NATS для этого мало — мастер проекта A
законно пишет в свой субъект, но может подставить в поле `name` папета из B.
Поэтому на каждый глагол, называющий папет, сверяем проект из субъекта с
настоящим origin его клона.

Права проверяются дважды: на сервере NATS (кто в какой субъект пишет) и здесь
(какой глагол, каким субъектом и про чьего папета). Право, проверенное в одном
месте, однажды окажется проверенным ни в одном.

Здесь -- глаголы и решение о праве, данные без печати. Программа (подключение,
петля, --check) -- командлет `mop agent`, mop/cli/service/agent.py (#150).
"""
import sys

if __name__ == "__main__":
    # Переход (#150): юниты узлов зовут `python3 -m mop.agent [--check]`, и
    # звать будут, пока ExecStart не переедет на `mop agent` прогоном deploy.
    # До импортов пакета (#169): без nats-py импорт шины бросает, и отказ
    # обязан быть строкой командлета, а не трассой из импорта.
    from mop.cli.service import agent as program
    sys.exit(program.main(sys.argv[1:]))

import asyncio  # noqa: E402
import base64
import collections
import json
import os
import shlex
import socket
import time

from . import bus, busnames, driver, fsutil, lease, service, usage
from .driver import clone_dir, target_dir, why

HOME = os.path.expanduser("~")

# Драйвер узла, не папета, и берётся он из окружения (юнит агента), а не из
# запроса: иначе мастер проекта A прислал бы своё значение и заставил агента
# исполнить команду не там. Дефолт host — узел, ничего про драйверы не
# знающий, обязан вести себя ровно как раньше.
DRIVER = driver.current()

# Что разрешено отправлять в пейн. Тот же список, что у фронтенда, — но
# проверка здесь настоящая, а там подсказка пользователю.
#
# Escape в списке не ради симметрии: папет, залипший на диалоге, невидим для
# ростера (он показывается занятым или свободным, а сообщения копятся в очереди
# непрочитанными), и единственное лечение — снять диалог, а не ответить на него.
# Ответить значит выбрать из списка, которого не видишь целиком.
SLASH_ALLOWED = ("/model", "/clear", "/compact", "/rc", "/status")
KEYS_ALLOWED = ("Escape",)

# Куда `write` имеет право писать. Токена Nomad в списке нет и не будет: узлы
# лишились его вместе с переездом на шину.
WRITABLE = (
    f"{HOME}/.claude/.credentials.json",
    f"{HOME}/.config/mop/secrets.env",
)

# Ростер тел перечисляет драйвер (у host — по сокетам tmux в /tmp/tmux-<uid>):
# на гипервизоре этот каталог пуст, и знать о нём агенту незачем. Соглашение
# об имени папета живёт там же, в реестре драйверов: это узловой stdlib-модуль,
# а puppets на узел не тянем — он приводит python-nomad, который агенту не
# нужен вовсе.

# Окно должно накрывать не только последний ход, но и жалобу над ним: строка
# «API Error: … Usage limit reached» остаётся стоять, а claude дорисовывает под
# ней сводку задач и рамку ввода. На живом папете (2026-08-31) она оказалась
# 14-й снизу, в окно из 10 строк не попала, и папет с выбранной квотой читался
# в mop list как занятый своей веткой.
#
# Шире делать нельзя бесконечно: окно заодно задаёт свежесть жалобы. Пережитая
# ошибка вытесняется выводом следующего хода, и только поэтому вылеченный папет
# перестаёт читаться больным.
SCREEN_LINES = 20        # столько непустых строк пейна едет в состоянии
IDLE_WAIT = 600          # потолок ожидания простоя для notify


def node_name():
    """Имя узла в Nomad. Оно же в субъекте, поэтому берётся из окружения, а не
    угадывается: имя узла и hostname могут расходиться."""
    return os.environ.get("MOP_NODE") or socket.gethostname()


async def puppet_project(name):
    """Чей это папет. Origin клона — авторитет: врапер сносит клон, если origin
    разошёлся с PU_ORIGIN, так что клон и спека не расходятся никогда.

    Пока клона нет (папет грузится) — откат на имя, которое по построению
    согласовано с origin: next_name строит его из того же basename."""
    out, _ = await bsh(name, f"git -C {clone_dir(name)} remote get-url origin 2>/dev/null")
    origin = out.strip().splitlines()[-1] if out.strip() else ""
    if origin:
        return driver.project_of(origin)
    return driver.project_of_name(name)


# ─── локальные пробы ─────────────────────────────────────────────────────
async def bsh(name, script, timeout=20):
    """Шелл внутри тела папета. -> (вывод, код).

    У драйвера host префикс пустой и это тот же шелл на узле; у контейнерного
    драйвера — ssh внутрь. Соблазн оставить пробу снаружи (смонтировать наружу
    tmux-сокет и каталог сессий) ложный: `session.probe` проверяет живость
    через `os.kill(pid, 0)`, а pid тела в namespace хоста означает другой
    процесс или никакой — ломается различение `hung` и `offline`. Плюс tmux
    отказывается соединять клиента с сервером другой версии.

    Скрипт всегда наш, никогда не из запроса, а имя папета попадает в него
    только после driver.valid_name."""
    if not driver.valid_name(name):
        return "", None
    return await driver.sh(script, timeout, prefix=DRIVER.argv(name))


class Tmux:
    """Строки скрипта для tmux папета: и сервер (-L), и сессия (-t) зовутся
    его именем. Только строки -- исполняет bsh, и имя до шелла доходит лишь
    после driver.valid_name."""

    def __init__(self, name):
        self.name = name
        self.base = f"tmux -L {name}"

    def alive(self):
        return f"{self.base} has-session -t {self.name} 2>/dev/null"

    def screen(self, lines):
        """Последние непустые строки буфера."""
        return (f"{self.base} capture-pane -t {self.name} -p -S - "
                f"| grep -v '^$' | tail -{lines}")

    def buffer(self):
        """Весь буфер, с историей."""
        return f"{self.base} capture-pane -p -t {self.name} -S -"

    def visible(self):
        return f"{self.base} capture-pane -p -t {self.name}"

    def keys(self, keys):
        return f"{self.base} send-keys -t {self.name} {keys}"

    def press(self, key):
        """Голая клавиша и экран после неё."""
        return f"{self.keys(key)}; sleep 1; {self.visible()}"

    def type(self, command):
        """Очистить строку, напечатать команду, Enter, экран. Кавычку в
        команде отбивает вызывающий: команда идёт в шелл одной строкой."""
        quoted = f"'{command}'"
        keys = (f"{self.keys('C-u')}; sleep 0.3; "
                f"{self.keys(quoted)}; sleep 0.3; ") if command else ""
        return keys + f"{self.keys('Enter')}; sleep 2; {self.visible()}"


async def tmux_alive(name):
    _, code = await bsh(name, Tmux(name).alive())
    return code == 0


async def screen(name, lines=SCREEN_LINES):
    out, _ = await bsh(name, Tmux(name).screen(lines))
    return out


async def pane_lines(name):
    """Весь буфер пейна без хвостовых пустых строк, которыми tmux добивает
    видимую часть."""
    out, code = await bsh(name, Tmux(name).buffer())
    if code not in (0, None):
        raise RuntimeError(f"tmux in {name}: {why(out, code)}")
    lines = out.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


async def clone_facts(name):
    """Что клон держит: ветка, несохранённое, неотправленное.

    Пробы переехали с `alloc exec` слово в слово, и `--not --remotes` здесь не
    случайность: upstream рабочей ветки бывает прибит к origin/master, и тогда
    `@{u}..` считает влитое неотправленным. На этих числах стоит решение
    мастера о диспатче, переписывать их вместе с транспортом нельзя."""
    d = clone_dir(name)
    out, _ = await bsh(
        name,
        f'cd {d} 2>/dev/null || exit 0; '
        f'echo "cur=$(git branch --show-current 2>/dev/null)"; '
        f'echo "def=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null)"; '
        f'echo "origin=$(git remote get-url origin 2>/dev/null)"; '
        f'echo "dirty=$(git status --porcelain 2>/dev/null | wc -l)"; '
        f'echo "ahead=$(git rev-list --count HEAD --not --remotes 2>/dev/null)"; '
        f'echo "owner=$(head -1 {lease.FILE} 2>/dev/null)"')
    kv = fsutil.read_kv(out, raw=True)
    if "dirty" not in kv:
        return None
    try:
        dirty, ahead = int(kv.get("dirty") or 0), int(kv.get("ahead") or 0)
    except ValueError:
        return None
    return {"cur": kv.get("cur") or "(detached)",
            "def": (kv.get("def") or "").rsplit("/", 1)[-1] or None,
            "origin": kv.get("origin") or None,
            "dirty": dirty, "ahead": ahead,
            # Кто ведёт задание (#161): сырая запись, живость считает мастер.
            "owner": lease.parse(kv.get("owner"))}


async def du_kb(name):
    """Сколько места занимает папет: клон плюс его target-каталог, в КБ.

    Меряется по спросу, без кэша (пока): du по большому target — обход сотен
    тысяч inode, и цену платит каждый спрашивающий. nice обязателен — обмер
    конкурирует за IO с живыми сборками. Отказ du — None, а не ноль: ноль
    это измеренное «пусто», отказ — «не знаю».

    Каталоги отбирает сам шелл в теле (`-d`), а не os.path.isdir здесь: у
    контейнерного папета этих путей на узле нет вовсе, и проверка снаружи
    вернула бы «ничего не занимает» для полного клона."""
    paths = f"{clone_dir(name)} {target_dir(name)}"
    out, code = await bsh(
        name,
        f'p=""; for x in {paths}; do [ -d "$x" ] && p="$p $x"; done; '
        f'[ -n "$p" ] && nice -n 19 du -sx $p 2>/dev/null',
        timeout=120)
    if code is None:
        return None
    kbs = [int(l.split()[0]) for l in out.splitlines() if l[:1].isdigit()]
    return sum(kbs) if kbs else None


# ─── сессия папета: всегда внутри тела ───────────────────────────────────
# session.py ходит к сокету сессии и проверяет живость через os.kill(pid, 0) —
# то и другое имеет смысл только там, где сессия и живёт. Поэтому агент зовёт
# его не импортом, а отдельным процессом через тот же префикс, что и tmux:
# у host это тот же узел, у контейнерного драйвера — ssh внутрь. Модуль для
# того и сделан standalone, с CLI, печатающим в stdout.
def _session_cmd(verb, *args):
    return " ".join(["python3", shlex.quote(DRIVER.SESSION_PY), verb]
                    + [shlex.quote(str(a)) for a in args])


async def session_probe(name):
    """Достоверное состояние сессии: "<status> <alive> <listen>" либо "none".

    Отказ пробы отдаём как "none", а не как исключение: у мастера это значит
    «файла сессии нет», и он уходит на откат по буферу пейна — ровно то же,
    что было, когда пробник не находил сессии."""
    out, code = await bsh(name, _session_cmd("probe", clone_dir(name)))
    if code not in (0, None) or not out.strip():
        return "none"
    return out.strip().splitlines()[-1].strip()


def _last_json(out):
    """Последняя JSON-строка вывода либо None.

    Последняя, а не первая: ssh в тело волен подмешать сверху свои
    предупреждения (баннер, добавленный ключ хоста), и первая строка тогда
    не JSON. Так читаются и session.py, и usage.py."""
    for line in reversed(out.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


async def session_json(name, script, timeout=20):
    """Ответ session.py, который печатает JSON (send, wait-idle)."""
    out, code = await bsh(name, script, timeout)
    if code is None:
        return {"error": f"{name}: the body did not answer in {timeout}s"}
    got = _last_json(out)
    if got is not None:
        return got
    return {"error": out.strip() or f"session.py exit {code}"}


async def facts(name):
    """Всё, что узел знает о папете, одним ответом.

    Агент отдаёт факты, а не вердикт: собирает состояние мастер. Так логика
    «free/busy/HUNG» остаётся в одном месте и, главное, становится
    чистой функцией — её можно проверить без пула, чего про неё не скажешь
    с тех пор, как она жила поверх exec."""
    if not await tmux_alive(name):
        return {"present": False}
    scr, sess, clone = await asyncio.gather(
        screen(name), session_probe(name), clone_facts(name))
    return {"present": True, "screen": scr, "session": sess, "clone": clone}


# ─── глаголы ─────────────────────────────────────────────────────────────
async def v_ping(_conn, _req):
    return {"node": node_name()}


async def v_state(_conn, req):
    return await facts(req["name"])


async def v_states(_conn, req):
    """Пачкой: у узла обычно несколько папетов, и спрашивают о них всегда
    вместе. Одна поездка вместо N.

    Чужих молча выбрасываем, а не отвечаем отказом: мастер спрашивает по своему
    ростеру, и если в списке оказался чужой — это ошибка спрашивающего, из-за
    которой не должна пропасть картина по своим."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    got = await asyncio.gather(*(facts(n) for n in names))
    return {"puppets": dict(zip(names, got))}


async def v_sizes(_conn, req):
    """Место папетов пачкой: клон + target, du по спросу.

    Отдельный глагол, а не поле в states: du небыстрый, и воткнуть его в
    быстрый ответ о состояниях — значит читать медленный обмер как «агент
    молчит 20с». Не доехал за таймаут — у мастера прочерк, а не ложный
    диагноз."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    kbs = await asyncio.gather(*(du_kb(n) for n in names))
    return {"sizes": dict(zip(names, kbs))}


async def v_local(_conn, req):
    """Папета, живущие на этом узле, — по сокетам tmux-серверов.

    Ростер без Nomad. Нужен узловому `mop mcp`: токена у него больше нет, и
    список джобов взять неоткуда. Мастер этим глаголом не пользуется — у него
    ростер богаче: аллокации, профиль LLM, репозиторий.

    Перечисляет драйвер: у host это сокеты tmux-серверов в /tmp/tmux-<uid>, а
    на гипервизоре этот каталог пуст — тела там отдельные объекты."""
    names = await DRIVER.bodies()
    alive = [n for n in names if await tmux_alive(n) and await _mine(req, n)]
    got = await asyncio.gather(*(facts(n) for n in alive))
    return {"node": node_name(), "puppets": dict(zip(alive, got))}


# Проверка владельца и запись -- под замком на папета: два `send` в одно
# окно иначе оба увидели бы «ничей» (#161). Агент на узле один, поэтому
# замка в процессе достаточно.
_owner_locks = {}


async def _claim(name, req):
    """Аренда задания перед доставкой. -> (отказ|None, откат|None, заметка|None).

    Откат -- прежняя запись: не доехало сообщение -- возвращаем её, иначе
    неудачный `send` держал бы папета окно диспатча впустую."""
    me = req.get("owner")
    if not me:
        return None, None, None
    clone = await clone_facts(name)
    owner = (clone or {}).get("owner")
    act, note = lease.verdict(owner, me, clone, time.time(), bool(req.get("force")))
    if act == "refuse":
        return f"{name}: {note}", None, None
    if act != "take" or clone is None:
        return None, None, note
    path = f"{clone_dir(name)}/{lease.FILE}"
    out, code = await bsh(name, f"printf %s {shlex.quote(lease.render(me, time.time()))} "
                                f"> {shlex.quote(path)}")
    # Таймаут -- не записано (#171): иначе «владелец есть», а файла нет.
    if code != 0:
        return f"{name}: owner not recorded: {why(out, code)}", None, None
    return None, (path, owner), note


async def _unclaim(name, undo):
    """Вернуть прежнюю аренду. -> причина неудачи | None.

    Не вернули (таймаут, #171, или ошибка) -- аренда осталась за мастером,
    чей send не доехал, и следующему откажут, назвав не того владельца (#181):
    звавший обязан это сказать."""
    path, owner = undo
    body = lease.render(owner["user"], owner["at"]) if owner else ""
    out, code = await bsh(name, f"printf %s {shlex.quote(body)} > {shlex.quote(path)}"
                                if body else f"rm -f {shlex.quote(path)}")
    return why(out, code) if code != 0 else None


async def v_send(conn, req):
    """Сообщение в сессию папета. -> {msg_id} либо {error}.

    `notify=true` не блокирует ответ: подписку на простой держит фоновая
    задача здесь, на узле, рядом с сокетом, и, дождавшись, публикует в инбокс
    мастера. Отсюда push без опроса — и без потока внутри MCP-сервера, который
    раньше ждал простоя, сидя в аллокации."""
    name = req["name"]
    wait = min(max(int(req.get("wait") or 0), 0), IDLE_WAIT)
    async with _owner_locks.setdefault(name, asyncio.Lock()):
        refused, undo, note = await _claim(name, req)
    if refused:
        return {"error": refused}
    # Цель — клон, а не сокет: session.py резолвит сессию сам, внутри тела, где
    # только и лежат её файлы. Снаружи сокет контейнерного папета не виден.
    out = await session_json(name, _session_cmd(
        "send", clone_dir(name), req["message"],
        "--priority", req.get("priority", "next"),
        "--from-name", req.get("from_name", "mop"),
        "--wait", wait), timeout=wait + 20)
    if out.get("error"):
        failed = await _unclaim(name, undo) if undo else None
        if failed:
            out["error"] += f"; owner not restored: {failed}"
        return out
    if note:
        out["owner_note"] = note
    await _event(conn, "send", name, text=f"from {req.get('from_name', 'mop')}")
    if req.get("notify") and not wait:
        # Куда отвечать, говорит сам мастер: инбокс адресуется мастером, а не
        # проектом, иначе два терминала в одном проекте получали бы вести друг
        # друга. Без reply_to ждать бессмысленно — некому сказать.
        if req.get("reply_to"):
            asyncio.create_task(_watch_idle(conn, name, req["reply_to"]))
        else:
            out["notify"] = "no reply_to given — nothing to notify"
    return out


async def _watch_idle(conn, name, reply_to):
    """Дождаться простоя и сказать мастеру, который об этом попросил.

    Ожидание держит session.py в теле (глагол wait-idle): подписка идёт к
    сокету сессии, а он host-local внутри тела. Метка нужна, когда узел ждёт
    сразу нескольких папетов — иначе два инбокса отберут друг у друга путь
    <pid>.sock."""
    r = await session_json(name, _session_cmd(
        "wait-idle", clone_dir(name), IDLE_WAIT, name[-8:]), timeout=IDLE_WAIT + 20)
    if r.get("error"):
        return await _tell_master(
            conn, reply_to, f"mop: gave up waiting for {name} to idle: {r['error']}")
    state = r.get("state")
    await _tell_master(conn, reply_to, f"mop: puppet {name} — {state}" if state
                       else f"mop: {name} did not report idle within {IDLE_WAIT}s")
    await _event(conn, "idle", name, text=state or f"no idle within {IDLE_WAIT}s")


async def _tell_master(conn, reply_to, text):
    try:
        await conn.publish(reply_to, json.dumps(
            {"node": node_name(), "text": text}, ensure_ascii=False).encode())
    except Exception:
        pass


async def _event(conn, kind, name=None, **fields):
    """Запись в журнал проекта (mop.<проект>.events, #66).

    Агент публикует то, что делает сам: доставил сообщение, дождался
    простоя, напечатал команду, снёс тело, поднялся. Смену состояния внутри
    сессии он не видит — её по-прежнему спрашивает мастер, — но по этим
    событиям дашборд сдвигает опрос вперёд и держит журнал. Проект берётся у
    самого папета, а не из субъекта запроса: оператор пишет из admin, а
    событие принадлежит проекту. Отказ — тишина: журнал вторичен, и ронять
    глагол из-за него нельзя."""
    project = await puppet_project(name) if name else bus.ADMIN
    try:
        await conn.publish(bus.events(project), json.dumps(
            {"event": kind, "node": node_name(), "name": name, "project": project,
             "at": time.time(), **fields}, ensure_ascii=False).encode())
    except Exception:
        pass


async def v_tail(_conn, req):
    return {"lines": await pane_lines(req["name"])}


async def v_disk(_conn, _req):
    """Место в хранилище тел. Узловой факт: давление оценивает мастер
    (mop gc), здесь только цифра.

    Меряет драйвер: у host хранилище тел — это $HOME со всеми клонами и
    target-каталогами, а на гипервизоре в $HOME не лежит ни одного папета, и
    `df $HOME` там отвечал бы про совершенно постороннюю файловую систему.
    Молча: число выглядит правдоподобно, а `mop gc` принимает по нему решение
    о сносе."""
    return await DRIVER.capacity()


async def v_wipe(conn, req):
    """Снести рабочую копию папета и восстановить её из git; target — целиком.

    Это половина рецикла (вторая — перерегистрация джоба у мастера). Клон не
    переклонируется — дорого и незачем: reset откатывает отслеживаемое,
    `clean -xdff` выметает и untracked, и игнорируемое (внутриклоновые
    кэши, node_modules), но `-e` защищает подсеянное врапером — список
    живёт в MOP_PUPPET_SEED и у врапера, и здесь один. target-каталог —
    чисто производные данные, он удаляется rm -rf и тем самым снимается
    почти весь объём.

    Что именно сносится, решает драйвер: у host тело — сам узел, снести его
    нельзя, и сносится всё, что папет в нём нажил; у контейнерного драйвера
    уходит целиком контейнер, а следующий подъём делает новый.

    Предохранитель: живая tmux-сессия — отказ. Агент не судит, свободен ли
    папет, но «сессия жива» — факт, и снос под живой сессией недопустим
    независимо от того, что решил мастер."""
    name = req["name"]
    if not driver.valid_name(name):
        return {"error": driver.bad_name(name)}
    if await tmux_alive(name):
        return {"error": f"{name}: tmux session is alive — stop the job first"}
    # Событие до сноса: после него origin клона спрашивать уже не у кого.
    await _event(conn, "wipe", name)
    return await DRIVER.destroy(name)


async def v_type(conn, req):
    """Напечатать слэш-команду в пейн и вернуть экран после неё.

    Печатью, а не сообщением по каналу: слэш-команды через канал не проходят
    (сообщение кладётся в очередь с skipSlashCommands), а у папета с
    исчерпанной квотой любой ход падает, не начавшись, — слэш-команду же
    исполняет сам TUI, ход на неё не тратится.

    Перед вводом чистим строку (C-u): в пейне мог остаться недобитый текст,
    и тогда команда склеилась бы с ним в мусор."""
    name, command = req["name"], (req.get("command") or "").strip()
    # Имя -- до шелла: bsh на кривом имени отдаёт тот же None, что таймаут,
    # и отказ назвал бы не ту причину (#171).
    if not driver.valid_name(name):
        return {"error": driver.bad_name(name)}
    if command in KEYS_ALLOWED:
        # Голая клавиша: ни очистки строки, ни Enter следом — Escape снимает
        # диалог, а Enter после него отправил бы пустой ход.
        out, code = await bsh(name, Tmux(name).press(command))
        if code is None:
            # Таймаут -- не нажато (#171); ненулевой код отвечает как раньше.
            return {"error": why(out, code)}
        return {"screen": out} if code == 0 else {"error": out.strip()}
    if command.split()[0:1] and command.split()[0] not in SLASH_ALLOWED:
        return {"error": f"only allowed: {', '.join(SLASH_ALLOWED + KEYS_ALLOWED)}"}
    if "'" in command:
        return {"error": "quote in command: command goes to the shell as one line"}
    out, code = await bsh(name, Tmux(name).type(command))
    if code is None:
        return {"error": why(out, code)}
    if code != 0:
        return {"error": out.strip() or f"tmux exit {code}"}
    await _event(conn, "type", name, text=command)
    return {"screen": out}


async def v_write(_conn, req):
    """Атомарная запись файла из белого списка, 600.

    Заменяет ту ветку раздачи кредов, что ездила шеллом в аллокацию. Список
    закрыт: без него это была бы произвольная запись в $HOME, то есть
    исполнение кода через ~/.bashrc."""
    files = []
    for path, b64 in req.get("files") or []:
        if path not in WRITABLE:
            return {"error": f"agent is not allowed to write to {path}"}
        files.append((path, base64.b64decode(b64)))

    # На узел — всегда: отсюда драйвер сеет файл в каждое новое тело при
    # подъёме, и узел обязан держать свежую копию, даже когда тел сейчас нет.
    written = []
    for path, data in files:
        try:
            driver.write_private(path, data)
        except Exception as e:
            return {"error": f"{path}: {e}"}
        written.append(path)

    # ...И в каждое живое тело, иначе протухший логин лечился бы только
    # рестартом папета — то есть ценой его работы.
    #
    # У драйвера, где тело и есть узел, второй записи не бывает: это тот же
    # файл, а список тел там просто перечисляет папетов.
    #
    # Все тела разом и все файлы тела одним вызовом (#137): по очереди и по
    # файлу это стоило ~4 с на файл, и `mop login` на узле с двумя телами
    # читался молчащим агентом.
    bodies = await driver.bodies_apart(DRIVER)
    answers = await asyncio.gather(*(DRIVER.push_many(name, files) for name in bodies))
    for name, r in zip(bodies, answers):
        if r.get("error"):
            # Отказ по одному телу не отменяет остальных: узел уже получил
            # свежую копию, и молчащее тело -- отдельная беда.
            written.append(f"{name} FAILED — {r['error']}")
        else:
            written += [f"{name}:{p}" for p in r.get("written") or []]
    return {"written": written}


async def v_junk(_conn, req):
    """Что стоит на этом узле, БЕЗ фильтров: {node, driver, bodies,
    templates}.

    От `local` отличается тем, ради чего и заведён: тот показывает папетов
    (живая сессия, свой проект), а этот — объекты. Мусор по определению не
    имеет живой сессии и не принадлежит никому, так что фильтры `local`
    отсеяли бы ровно то, что ищут. Поэтому глагол админский: он рассказывает
    про чужие проекты тоже, а сопоставлять с Nomad всё равно некому, кроме
    управляющей машины.

    `templates` у host пуст: сборочных тел там не бывает вовсе, и пустой
    список честнее выдуманного."""
    names = await DRIVER.bodies()
    # Работу в клоне спрашиваем ЗДЕСЬ, а не оставляем решать по имени. Тело
    # без tmux-сессии `facts` описывает как {present: False} и про клон молчит
    # — верно для узла, до которого не достучаться, но сирота на гипервизоре
    # жива и отвечает по ssh. Без этого уборка сносила бы тела, не спросив,
    # есть ли в них несохранённое: 22.09 она так снесла два контейнера чужих
    # проектов, и повезло, что пустых.
    work = {}
    for n in names:
        c = await clone_facts(n)
        if c:
            work[n] = {"dirty": c.get("dirty"), "ahead": c.get("ahead"),
                       "cur": c.get("cur")}
    return {"node": node_name(),
            "driver": driver.current_name(),
            "bodies": names,
            "work": work,
            "templates": await DRIVER.templates()}


async def v_usage(_conn, req):
    """Расход токенов папетов этого узла по дням: {usage: {папет: {дата:
    {input, output, cache_write, cache_read}}}}, окно — `days` суток включая
    сегодня, по местному времени узла.

    Считается по транскриптам <projects_dir>/<slug клона>/ (подробности и
    ловушка с дублями — в usage.py), и считается внутри тела: у контейнерного
    папета этих файлов на узле нет вовсе, а отсутствие файлов неотличимо от
    нулевого расхода — счёт снаружи показал бы ноль там, где папет сжёг
    миллионы.

    Папета перечисляет драйвер (bodies), а не listdir клонов: на гипервизоре
    каталога клонов нет. Имя в slug'е искалечено (точки и подчёркивания стали
    дефисами), и обратно в имя папета, которое сверяется с проектом, его не
    собрать, — поэтому идём от имени к каталогу, а не наоборот.

    Чужих папетов выбрасываем молча, как states: мастер проекта видит расход
    своего проекта, оператор — всего узла."""
    days = min(max(int(req.get("days") or 7), 1), 366)
    names = [n for n in await DRIVER.bodies() if await _mine(req, n)]
    # usage.py лежит рядом с session.py: SESSION_PY и называет то место, куда
    # пакет mop приехал внутри тела.
    usage_py = os.path.join(os.path.dirname(DRIVER.SESSION_PY), "usage.py")

    async def one(name):
        d = f"{DRIVER.projects_dir(name)}/{usage.slug(clone_dir(name))}"
        out, code = await bsh(name, " ".join(
            ["python3", shlex.quote(usage_py), shlex.quote(d), str(days)]), 120)
        if code not in (0, None):
            return {}
        return _last_json(out) or {}

    got = await asyncio.gather(*(one(n) for n in names))
    return {"node": node_name(), "usage": dict(zip(names, got))}


# ─── права: одна таблица ─────────────────────────────────────────────────
# Кому глагол дан (#150). Раньше это были четыре параллельных набора, и новый
# глагол правился в четырёх местах.
#
# PUBLIC -- и не-мастеру, субъектом .msg. Папет имеет право написать соседу и
# посмотреть, кто чем занят; печатать в чужой TUI и писать файлы -- не имеет.
#
# MASTER -- только субъектом .rpc: мастеру проекта и оператору.
#
# NODE -- глагол узла, а не проекта: только оператору (`mop.admin.*`). Место на
# диске -- факт про хост со всеми его жильцами, и мастеру проекта соседняя
# нагрузка не показывается.
#
# `write` из NODE убран. Он лежал там из-за «мастер проекта A перезапишет
# креды проекта B», но перезаписывать в этой установке нечего: оба файла из
# WRITABLE собираются не из проекта мастера, а из машины — .credentials.json
# из логина claude.ai управляющей машины, secrets.env из .env самого mop
# (`puppets.LOCAL_KEYS_FILE` — это PROJECT репозитория mop, а не проекта).
# Мастер любого проекта везёт байт в байт то же, что вёз бы оператор. Ценой
# запрета было `mop login` из мастер-шелла: он отбивался по каждому узлу, и
# doctor оставался без единственного лечения протухшего логина. Папета это не
# касается: `write` не PUBLIC, а креды puppet-<проект> в субъект .rpc не пишут
# вовсе.
#
# named -- глагол называет конкретного папета, и проект запроса обязан сойтись
# с настоящим проектом папета.
PUBLIC, MASTER, NODE = "public", "master", "node"
Verb = collections.namedtuple("Verb", "fn scope named")
VERBS = {
    "ping":   Verb(v_ping,   PUBLIC, False),
    "local":  Verb(v_local,  PUBLIC, False),
    "state":  Verb(v_state,  PUBLIC, True),
    "states": Verb(v_states, PUBLIC, False),
    "sizes":  Verb(v_sizes,  MASTER, False),
    "send":   Verb(v_send,   PUBLIC, True),
    "tail":   Verb(v_tail,   PUBLIC, True),
    "type":   Verb(v_type,   MASTER, True),
    "write":  Verb(v_write,  MASTER, False),
    "disk":   Verb(v_disk,   NODE,   False),
    "wipe":   Verb(v_wipe,   MASTER, True),
    "usage":  Verb(v_usage,  MASTER, False),
    "junk":   Verb(v_junk,   NODE,   False),
}
# Прежние наборы -- выводом из таблицы, для тех, кто их читает.
PUBLIC_VERBS = tuple(v for v, d in VERBS.items() if d.scope == PUBLIC)
ADMIN_VERBS = tuple(v for v, d in VERBS.items() if d.scope == NODE)
NAMED_VERBS = tuple(v for v, d in VERBS.items() if d.named)


def refusal(verb, public, project):
    """Отказ по глаголу, субъекту и проекту, либо None. Проверку владельца
    папета (named) делает handle: она спрашивает клон.

    Нестроковый глагол -- неизвестный (#168): `[1]` как ключ таблицы бросал
    TypeError, и запрос оставался без ответа."""
    spec = VERBS.get(verb) if isinstance(verb, str) else None
    if spec is None:
        return f"no such verb {verb}; available: {', '.join(sorted(VERBS))}"
    if public and spec.scope != PUBLIC:
        # Не «нет прав», а прямо: глагол существует, но не в этом субъекте.
        return f"verb {verb} is available to the master only"
    if spec.scope == NODE and project != busnames.ADMIN:
        return f"verb {verb} is node-level, not given to project {project}"
    return None


def foreign(name, project):
    return f"puppet {name} is not in project {project}"


async def _mine(req, name):
    """Принадлежит ли папет проекту, из чьего субъекта пришёл запрос."""
    return req.get("_project") == bus.ADMIN or await puppet_project(name) == req.get("_project")


# ─── петля ───────────────────────────────────────────────────────────────
async def handle(conn, msg, public):
    """Разбор и три проверки: глагол существует, субъект его допускает, папет
    принадлежит спрашивающему проекту.

    Проект берём из субъекта (`mop.<проект>.node.<узел>.<канал>`), а не из тела
    запроса: тело пишет отправитель, субъект — права NATS."""
    try:
        req = json.loads(msg.data.decode())
    except ValueError:
        return await msg.respond(
            json.dumps({"error": "request is not JSON"}, ensure_ascii=False).encode())
    if not isinstance(req, dict):
        # До таблицы (#168): `[1]` падал на req["_project"], и задача умирала
        # без ответа -- проситель ждал таймаут.
        return await msg.respond(
            json.dumps({"error": "request is not a JSON object"}).encode())
    # Одно правило на всех (#173): mop.<проект>.node.<узел>.<канал>.
    req["_project"] = service.project_from_subject(msg.subject)

    verb = req.get("verb")
    why = refusal(verb, public, req["_project"])
    if why is None and VERBS[verb].named and not await _mine(req, req.get("name") or ""):
        # Главная проверка проектирования. Прав NATS тут мало: мастер проекта A
        # законно пишет в свой субъект, но может назвать папета из B.
        why = foreign(req.get("name"), req["_project"])
    if why:
        out = {"error": why}
    else:
        try:
            out = await VERBS[verb].fn(conn, req)
        except Exception as e:
            out = {"error": f"{verb}: {e}"}
    try:
        await msg.respond(json.dumps(out, ensure_ascii=False).encode())
    except Exception:
        pass


async def attach(conn, node):
    """Подписать агента узла на его субъекты и объявить подъём. -> [субъекты].

    cb обязан быть корутиной — nats-py отвергает обычную функцию. И каждый
    запрос уходит в свою задачу: последовательная обработка означала бы, что
    одно долгое ожидание простоя запирает весь узел.

    Маска по проекту: агент обслуживает всех жильцов узла, а кто из какого
    проекта — решает уже проверка в handle. Общий all.msg — для тех, кто не
    знает состава пула. Те же субъекты, что в правах узла (natsconf, #144)."""
    async def on_rpc(msg):
        asyncio.create_task(handle(conn, msg, public=False))

    async def on_msg(msg):
        asyncio.create_task(handle(conn, msg, public=True))

    subs = busnames.agent_subscriptions(node)
    for subj in subs["rpc"]:
        await conn.subscribe(subj, cb=on_rpc)
    for subj in subs["msg"]:
        await conn.subscribe(subj, cb=on_msg)
    await _event(conn, "up", text=f"agent on {node}")
    return subs["rpc"] + subs["msg"]

