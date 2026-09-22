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

Шард агент пересекает сознательно: он и есть то, что шарды разделяет, и
обслуживает всех жильцов узла. Прав NATS для этого мало — мастер шарда A
законно пишет в свой субъект, но может подставить в поле `name` папета из B.
Поэтому на каждый глагол, называющий папет, сверяем шард из субъекта с
настоящим origin его клона.

Права проверяются дважды: на сервере NATS (кто в какой субъект пишет) и здесь
(какой глагол, каким субъектом и про чьего папета). Право, проверенное в одном
месте, однажды окажется проверенным ни в одном.
"""
import asyncio
import base64
import json
import os
import shlex
import socket
import sys

try:
    import nats
except ImportError:
    sys.exit("bus library needed: pip install --user --break-system-packages nats-py")

from . import bus, driver, usage
from .driver import clone_dir, target_dir, why

HOME = os.path.expanduser("~")

# Драйвер узла, не папета, и берётся он из окружения (юнит агента), а не из
# запроса: иначе мастер шарда A прислал бы своё значение и заставил агента
# исполнить команду не там. Дефолт host — узел, ничего про драйверы не
# знающий, обязан вести себя ровно как раньше.
DRIVER = driver.current()

# Глаголы, доступные не-мастеру. Папет имеет право написать соседу и посмотреть,
# кто чем занят; печатать в чужой TUI и писать файлы — не имеет.
PUBLIC_VERBS = ("ping", "local", "state", "states", "send", "tail")

# Глаголы узла, а не проекта: место на диске — факт про хост со всеми его
# жильцами, и мастеру проекта соседняя нагрузка не показывается.
#
# `write` отсюда убран. Он лежал здесь из-за «мастер проекта A перезапишет
# креды проекта B», но перезаписывать в этой установке нечего: оба файла из
# WRITABLE собираются не из проекта мастера, а из машины — .credentials.json
# из логина claude.ai управляющей машины, secrets.env из .env самого mop
# (`puppets.LOCAL_KEYS_FILE` — это PROJECT репозитория mop, а не шарда).
# Мастер любого шарда везёт байт в байт то же, что вёз бы оператор. Ценой
# запрета было `mop login` из мастер-шелла: он отбивался по каждому узлу, и
# doctor оставался без единственного лечения протухшего логина.
#
# Папета это не касается: `write` не в PUBLIC_VERBS, а креды puppet-<шард>
# в субъект .rpc не пишут вовсе.
ADMIN_VERBS = ("disk", "junk")

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


async def puppet_shard(name):
    """Чей это папет. Origin клона — авторитет: врапер сносит клон, если origin
    разошёлся с PU_ORIGIN, так что клон и спека не расходятся никогда.

    Пока клона нет (папет грузится) — откат на имя, которое по построению
    согласовано с origin: next_name строит его из того же basename."""
    out, _ = await bsh(name, f"git -C {clone_dir(name)} remote get-url origin 2>/dev/null")
    origin = out.strip().splitlines()[-1] if out.strip() else ""
    if origin:
        return os.path.basename(origin).removesuffix(".git")
    return driver.shard_of_name(name)


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


async def tmux_alive(name):
    _, code = await bsh(name, f"tmux -L {name} has-session -t {name} 2>/dev/null")
    return code == 0


async def screen(name, lines=SCREEN_LINES):
    out, _ = await bsh(name, f"tmux -L {name} capture-pane -t {name} -p -S - "
                             f"| grep -v '^$' | tail -{lines}")
    return out


async def pane_lines(name):
    """Весь буфер пейна без хвостовых пустых строк, которыми tmux добивает
    видимую часть."""
    out, code = await bsh(name, f"tmux -L {name} capture-pane -p -t {name} -S -")
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
        f'echo "ahead=$(git rev-list --count HEAD --not --remotes 2>/dev/null)"')
    kv = dict(l.split("=", 1) for l in out.splitlines() if "=" in l)
    if "dirty" not in kv:
        return None
    try:
        dirty, ahead = int(kv.get("dirty") or 0), int(kv.get("ahead") or 0)
    except ValueError:
        return None
    return {"cur": kv.get("cur") or "(detached)",
            "def": (kv.get("def") or "").rsplit("/", 1)[-1] or None,
            "origin": kv.get("origin") or None,
            "dirty": dirty, "ahead": ahead}


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
async def v_ping(_req):
    return {"node": node_name()}


async def v_state(req):
    return await facts(req["name"])


async def v_states(req):
    """Пачкой: у узла обычно несколько папетов, и спрашивают о них всегда
    вместе. Одна поездка вместо N.

    Чужих молча выбрасываем, а не отвечаем отказом: мастер спрашивает по своему
    ростеру, и если в списке оказался чужой — это ошибка спрашивающего, из-за
    которой не должна пропасть картина по своим."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    got = await asyncio.gather(*(facts(n) for n in names))
    return {"puppets": dict(zip(names, got))}


async def v_sizes(req):
    """Место папетов пачкой: клон + target, du по спросу.

    Отдельный глагол, а не поле в states: du небыстрый, и воткнуть его в
    быстрый ответ о состояниях — значит читать медленный обмер как «агент
    молчит 20с». Не доехал за таймаут — у мастера прочерк, а не ложный
    диагноз."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    kbs = await asyncio.gather(*(du_kb(n) for n in names))
    return {"sizes": dict(zip(names, kbs))}


async def v_local(req):
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


async def v_send(req):
    """Сообщение в сессию папета. -> {msg_id} либо {error}.

    `notify=true` не блокирует ответ: подписку на простой держит фоновая
    задача здесь, на узле, рядом с сокетом, и, дождавшись, публикует в инбокс
    мастера. Отсюда push без опроса — и без потока внутри MCP-сервера, который
    раньше ждал простоя, сидя в аллокации."""
    name = req["name"]
    wait = min(max(int(req.get("wait") or 0), 0), IDLE_WAIT)
    # Цель — клон, а не сокет: session.py резолвит сессию сам, внутри тела, где
    # только и лежат её файлы. Снаружи сокет контейнерного папета не виден.
    out = await session_json(name, _session_cmd(
        "send", clone_dir(name), req["message"],
        "--priority", req.get("priority", "next"),
        "--from-name", req.get("from_name", "mop"),
        "--wait", wait), timeout=wait + 20)
    if out.get("error"):
        return out
    if req.get("notify") and not wait:
        # Куда отвечать, говорит сам мастер: инбокс адресуется мастером, а не
        # шардом, иначе два терминала в одном проекте получали бы вести друг
        # друга. Без reply_to ждать бессмысленно — некому сказать.
        if req.get("reply_to"):
            asyncio.create_task(_watch_idle(name, req["reply_to"]))
        else:
            out["notify"] = "no reply_to given — nothing to notify"
    return out


async def _watch_idle(name, reply_to):
    """Дождаться простоя и сказать мастеру, который об этом попросил.

    Ожидание держит session.py в теле (глагол wait-idle): подписка идёт к
    сокету сессии, а он host-local внутри тела. Метка нужна, когда узел ждёт
    сразу нескольких папетов — иначе два инбокса отберут друг у друга путь
    <pid>.sock."""
    r = await session_json(name, _session_cmd(
        "wait-idle", clone_dir(name), IDLE_WAIT, name[-8:]), timeout=IDLE_WAIT + 20)
    if r.get("error"):
        return await _tell_master(
            reply_to, f"mop: gave up waiting for {name} to idle: {r['error']}")
    state = r.get("state")
    await _tell_master(reply_to, f"mop: puppet {name} — {state}" if state
                       else f"mop: {name} did not report idle within {IDLE_WAIT}s")


async def _tell_master(reply_to, text):
    if _conn is None:
        return
    try:
        await _conn.publish(reply_to, json.dumps(
            {"node": node_name(), "text": text}, ensure_ascii=False).encode())
    except Exception:
        pass


async def v_tail(req):
    return {"lines": await pane_lines(req["name"])}


async def v_disk(_req):
    """Место в хранилище тел. Узловой факт: давление оценивает мастер
    (mop gc), здесь только цифра.

    Меряет драйвер: у host хранилище тел — это $HOME со всеми клонами и
    target-каталогами, а на гипервизоре в $HOME не лежит ни одного папета, и
    `df $HOME` там отвечал бы про совершенно постороннюю файловую систему.
    Молча: число выглядит правдоподобно, а `mop gc` принимает по нему решение
    о сносе."""
    return await DRIVER.capacity()


async def v_wipe(req):
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
    return await DRIVER.destroy(name)


async def v_type(req):
    """Напечатать слэш-команду в пейн и вернуть экран после неё.

    Печатью, а не сообщением по каналу: слэш-команды через канал не проходят
    (сообщение кладётся в очередь с skipSlashCommands), а у папета с
    исчерпанной квотой любой ход падает, не начавшись, — слэш-команду же
    исполняет сам TUI, ход на неё не тратится.

    Перед вводом чистим строку (C-u): в пейне мог остаться недобитый текст,
    и тогда команда склеилась бы с ним в мусор."""
    name, command = req["name"], (req.get("command") or "").strip()
    if command in KEYS_ALLOWED:
        # Голая клавиша: ни очистки строки, ни Enter следом — Escape снимает
        # диалог, а Enter после него отправил бы пустой ход.
        out, code = await bsh(name,
                              f"tmux -L {name} send-keys -t {name} {command}; "
                              f"sleep 1; tmux -L {name} capture-pane -p -t {name}")
        return {"screen": out} if code in (0, None) else {"error": out.strip()}
    if command.split()[0:1] and command.split()[0] not in SLASH_ALLOWED:
        return {"error": f"only allowed: {', '.join(SLASH_ALLOWED + KEYS_ALLOWED)}"}
    if "'" in command:
        return {"error": "quote in command: command goes to the shell as one line"}
    keys = ""
    if command:
        keys = (f"tmux -L {name} send-keys -t {name} C-u; sleep 0.3; "
                f"tmux -L {name} send-keys -t {name} '{command}'; sleep 0.3; ")
    out, code = await bsh(name, keys + f"tmux -L {name} send-keys -t {name} Enter; "
                                      f"sleep 2; tmux -L {name} capture-pane -p -t {name}")
    if code not in (0, None):
        return {"error": out.strip() or f"tmux exit {code}"}
    return {"screen": out}


async def v_write(req):
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
    bodies = [] if DRIVER.BODY_IS_NODE else await DRIVER.bodies()
    for name in bodies:
        for path, data in files:
            r = await DRIVER.push(name, path, data)
            if r.get("error"):
                # Отказ по одному телу не отменяет остальных: узел уже получил
                # свежую копию, и молчащее тело -- отдельная беда.
                written.append(f"{name}:{path} FAILED — {r['error']}")
            else:
                written.append(f"{name}:{path}")
    return {"written": written}


async def v_junk(req):
    """Что стоит на этом узле, БЕЗ фильтров: {node, driver, bodies,
    templates}.

    От `local` отличается тем, ради чего и заведён: тот показывает папетов
    (живая сессия, свой шард), а этот — объекты. Мусор по определению не
    имеет живой сессии и не принадлежит никому, так что фильтры `local`
    отсеяли бы ровно то, что ищут. Поэтому глагол админский: он рассказывает
    про чужие шарды тоже, а сопоставлять с Nomad всё равно некому, кроме
    управляющей машины.

    `templates` есть не у всякого драйвера — у host сборочных тел не бывает
    вовсе, и пустой список там честнее выдуманного."""
    tmpl = getattr(DRIVER, "templates", None)
    names = await DRIVER.bodies()
    # Работу в клоне спрашиваем ЗДЕСЬ, а не оставляем решать по имени. Тело
    # без tmux-сессии `facts` описывает как {present: False} и про клон молчит
    # — верно для узла, до которого не достучаться, но сирота на гипервизоре
    # жива и отвечает по ssh. Без этого уборка сносила бы тела, не спросив,
    # есть ли в них несохранённое: 22.09 она так снесла два контейнера чужих
    # шардов, и повезло, что пустых.
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
            "templates": (await tmpl()) if tmpl else []}


async def v_usage(req):
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
    дефисами), и обратно в имя папета, которое сверяется с шардом, его не
    собрать, — поэтому идём от имени к каталогу, а не наоборот.

    Чужих папетов выбрасываем молча, как states: мастер шарда видит расход
    своего шарда, оператор — всего узла."""
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


VERBS = {"ping": v_ping, "local": v_local, "state": v_state,
         "states": v_states, "sizes": v_sizes, "send": v_send,
         "tail": v_tail, "type": v_type, "write": v_write,
         "disk": v_disk, "wipe": v_wipe, "usage": v_usage, "junk": v_junk}

# Глаголы, которые называют конкретного папета: у них шард запроса обязан
# сойтись с настоящим шардом папета.
NAMED_VERBS = ("state", "send", "tail", "type", "wipe")


async def _mine(req, name):
    """Принадлежит ли папет шарду, из чьего субъекта пришёл запрос."""
    return req.get("_shard") == bus.ADMIN or await puppet_shard(name) == req.get("_shard")


# ─── петля ───────────────────────────────────────────────────────────────
_conn = None


async def handle(msg, public):
    """Разбор и три проверки: глагол существует, субъект его допускает, папет
    принадлежит спрашивающему шарду.

    Шард берём из субъекта (`mop.<шард>.node.<узел>.<канал>`), а не из тела
    запроса: тело пишет отправитель, субъект — права NATS."""
    try:
        req = json.loads(msg.data.decode())
    except ValueError:
        return await msg.respond(
            json.dumps({"error": "request is not JSON"}, ensure_ascii=False).encode())
    parts = msg.subject.split(".")
    req["_shard"] = parts[1] if len(parts) > 1 else ""

    verb = req.get("verb")
    fn = VERBS.get(verb)
    if fn is None:
        out = {"error": f"no such verb {verb}; available: {', '.join(sorted(VERBS))}"}
    elif public and verb not in PUBLIC_VERBS:
        # Не «нет прав», а прямо: глагол существует, но не в этом субъекте.
        out = {"error": f"verb {verb} is available to the master only"}
    elif verb in ADMIN_VERBS and req["_shard"] != bus.ADMIN:
        out = {"error": f"verb {verb} is node-level, not given to shard {req['_shard']}"}
    elif verb in NAMED_VERBS and not await _mine(req, req.get("name") or ""):
        # Главная проверка шардирования. Прав NATS тут мало: мастер шарда A
        # законно пишет в свой субъект, но может назвать папета из B.
        out = {"error": f"puppet {req.get('name')} is not in shard {req['_shard']}"}
    else:
        try:
            out = await fn(req)
        except Exception as e:
            out = {"error": f"{verb}: {e}"}
    try:
        await msg.respond(json.dumps(out, ensure_ascii=False).encode())
    except Exception:
        pass


async def serve():
    global _conn
    c = bus.config()
    node = node_name()
    _conn = await nats.connect(
        **bus.auth(c), name=f"mop-agent/{node}",
        allow_reconnect=True, max_reconnect_attempts=-1, reconnect_time_wait=2)
    # cb обязан быть корутиной — nats-py отвергает обычную функцию. И каждый
    # запрос уходит в свою задачу: последовательная обработка означала бы, что
    # одно долгое ожидание простоя запирает весь узел.
    async def on_rpc(msg):
        asyncio.create_task(handle(msg, public=False))

    async def on_msg(msg):
        asyncio.create_task(handle(msg, public=True))

    # Маска по шарду: агент обслуживает всех жильцов узла, а кто из какого
    # шарда — решает уже проверка в handle.
    await _conn.subscribe(f"mop.*.node.{node}.rpc", cb=on_rpc)
    await _conn.subscribe(f"mop.*.node.{node}.msg", cb=on_msg)
    # Общий субъект: сюда спрашивают те, кто не знает состава пула.
    await _conn.subscribe("mop.*.all.msg", cb=on_msg)
    print(f"mop-agent: node {node}, subscribed to mop.*.node.{node}.rpc|msg "
          f"and mop.*.all.msg", flush=True)
    await asyncio.Event().wait()


async def check():
    """Проверка прогоном, а не чтением конфига: юнит, упавший в бесконечный
    реконнект, systemd вполне устраивает, и «запущен» не значит «подписан».
    Спрашиваем через публичный субъект узла, а не через .rpc: туда узлу писать
    и не положено — это и есть та граница прав, ради которой шину заводили.
    Первый прогон проверки уткнулся ровно в неё, и был неправ он, а не права."""
    c = bus.config()
    print(f"mop-agent: node {node_name()}, bus {c['url']}, "
          f"{len(VERBS)} verbs ({len(PUBLIC_VERBS)} public)")
    nc = await nats.connect(**bus.auth(c), name="mop-agent/check",
                            allow_reconnect=False, connect_timeout=5)
    try:
        msg = await nc.request(bus.subject(node_name(), "msg", shard=bus.ADMIN),
                               json.dumps({"verb": "ping"}).encode(), timeout=5)
        print(f"subscribed: {msg.data.decode()}")
    finally:
        await nc.close()


def main(argv):
    if "--check" in argv:
        try:
            asyncio.run(check())
        except Exception as e:
            print(f"agent is NOT answering on its subject: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
