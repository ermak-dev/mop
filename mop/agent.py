"""Агент узла: отвечает на запросы шины про папетов, которые живут ЗДЕСЬ.

Заменил собой `alloc exec`. Раньше мастер гонял шелл внутрь аллокации и платил
рукопожатием за каждую пробу; теперь на узле сидит подписчик, а мастер шлёт
ему глагол.

ОДИН НА УЗЕЛ, НЕ НА ПАПЕТА. Переживает рестарт папета — а спрашивают о папете
чаще всего именно тогда, когда он перезапускается. И живёт ВНЕ спеки джоба:
правка спеки не доезжает до работающего папета рестартом аллокации, ей нужна
перерегистрация, а правка агента доезжает одним прогоном плейбука.

НАБОР ГЛАГОЛОВ ЗАКРЫТ. Через шину нельзя попросить «выполни шелл»: это был бы
тот же management-токен Nomad, только по другой трубе, — а ради того, чтобы его
с узлов убрать, всё и затевалось. Отсюда же белые списки на `type` и `write`:
произвольная команда в чужой TUI и произвольная запись в $HOME — это два разных
способа получить исполнение кода на узле.

ШАРД АГЕНТ ПЕРЕСЕКАЕТ СОЗНАТЕЛЬНО: он и есть то, что шарды разделяет, и
обслуживает всех жильцов узла. Прав NATS для этого мало — мастер шарда A
законно пишет в свой субъект, но может подставить в поле `name` папета из B.
Поэтому на каждый глагол, называющий папет, сверяем шард из СУБЪЕКТА с
настоящим origin его клона.

Права проверяются ДВАЖДЫ: на сервере NATS (кто в какой субъект пишет) и здесь
(какой глагол, каким субъектом и про чьего папета). Право, проверенное в одном
месте, однажды окажется проверенным ни в одном.
"""
import asyncio
import base64
import json
import os
import socket
import sys

try:
    import nats
except ImportError:
    sys.exit("нужна библиотека шины: pip install --user --break-system-packages nats-py")

from . import bus, config, session

HOME = os.path.expanduser("~")
CLONES = f"{HOME}/puppets"

# Глаголы, доступные не-мастеру. Папет имеет право написать соседу и посмотреть,
# кто чем занят; печатать в чужой TUI и писать файлы — не имеет.
PUBLIC_VERBS = ("ping", "local", "state", "states", "send", "tail")

# Глаголы УЗЛА, а не проекта. Раздача кредов пишет файлы, а место на диске —
# факт про хост со всеми его жильцами, поэтому мастеру проекта их отдавать
# нельзя: файлами он перезаписал бы креды соседа, а местом видел бы соседнюю
# нагрузку. Всё это — оператору из admin.
ADMIN_VERBS = ("write", "disk")

# Что разрешено отправлять в пейн. Тот же список, что у фронтенда, — но
# проверка здесь настоящая, а там подсказка пользователю.
#
# Escape в списке не ради симметрии: папет, залипший на диалоге, невидим для
# ростера (он показывается занятым или свободным, а сообщения копятся в очереди
# непрочитанными), и единственное лечение — СНЯТЬ диалог, а не ответить на него.
# Ответить значит выбрать из списка, которого не видишь целиком.
SLASH_ALLOWED = ("/model", "/clear", "/compact", "/rc", "/status")
KEYS_ALLOWED = ("Escape",)

# Куда `write` имеет право писать. Токена Nomad в списке нет и не будет: узлы
# лишились его вместе с переездом на шину.
WRITABLE = (
    f"{HOME}/.claude/.credentials.json",
    f"{HOME}/.config/mop/secrets.env",
)

# Префикс имён джобов-папетов; он же префикс tmux-серверов и каталогов клонов.
# Дубль puppets.JOB_PREFIX намеренный: тянуть сюда puppets значит тянуть на узел
# python-nomad, а агенту Nomad не нужен вовсе — в этом половина смысла переезда.
PREFIX = "pu-"
TMUX_DIR = os.environ.get("TMUX_TMPDIR") or f"/tmp/tmux-{os.getuid()}"

SCREEN_LINES = 10        # столько непустых строк пейна едет в состоянии
IDLE_WAIT = 600          # потолок ожидания простоя для notify


def node_name():
    """Имя узла в Nomad. Оно же в субъекте, поэтому берётся из окружения, а не
    угадывается: у gamer имя узла и hostname расходятся."""
    return os.environ.get("MOP_NODE") or socket.gethostname()


def clone_dir(name):
    return f"{CLONES}/{name}"


async def puppet_shard(name):
    """Чей это папет. Origin клона — авторитет: врапер сносит клон, если origin
    разошёлся с PU_ORIGIN, так что клон и спека не расходятся никогда.

    Пока клона нет (папет грузится) — откат на имя, которое по построению
    согласовано с origin: next_name строит его из того же basename."""
    out, _ = await sh(f"git -C {clone_dir(name)} remote get-url origin 2>/dev/null")
    origin = out.strip().splitlines()[-1] if out.strip() else ""
    if origin:
        return os.path.basename(origin).removesuffix(".git")
    return name[len(PREFIX):].rsplit("-", 1)[0] if name.startswith(PREFIX) else ""


# ─── локальные пробы ─────────────────────────────────────────────────────
async def sh(script, timeout=20):
    """Шелл на своём же узле. -> (вывод, код). Единственное место, где агент
    вообще запускает шелл, и скрипт всегда наш, никогда не из запроса."""
    proc = await asyncio.create_subprocess_shell(
        script, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT)
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        return "", None
    return out.decode(errors="replace"), proc.returncode


async def tmux_alive(name):
    _, code = await sh(f"tmux -L {name} has-session -t {name} 2>/dev/null")
    return code == 0


async def screen(name, lines=SCREEN_LINES):
    out, _ = await sh(f"tmux -L {name} capture-pane -t {name} -p -S - "
                      f"| grep -v '^$' | tail -{lines}")
    return out


async def pane_lines(name):
    """Весь буфер пейна без хвостовых пустых строк, которыми tmux добивает
    видимую часть."""
    out, code = await sh(f"tmux -L {name} capture-pane -p -t {name} -S -")
    if code not in (0, None):
        raise RuntimeError(f"tmux в {name}: {out.strip() or f'exit {code}'}")
    lines = out.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


async def clone_facts(name):
    """Что клон держит: ветка, несохранённое, неотправленное.

    Пробы переехали с `alloc exec` СЛОВО В СЛОВО, и `--not --remotes` здесь не
    случайность: upstream рабочей ветки бывает прибит к origin/master, и тогда
    `@{u}..` считает влитое неотправленным. На этих числах стоит решение
    мастера о диспатче, переписывать их вместе с транспортом нельзя."""
    d = clone_dir(name)
    out, _ = await sh(
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

    Меряется ПО СПРОСУ, без кэша (пока): du по большому target — обход сотен
    тысяч inode, и цену платит каждый спрашивающий. nice обязателен — обмер
    конкурирует за IO с живыми сборками. Отказ du — None, а не ноль: ноль
    это измеренное «пусто», отказ — «не знаю»."""
    paths = [p for p in (clone_dir(name), f"{HOME}/.cache/target-{name}")
             if os.path.isdir(p)]
    if not paths:
        return None
    out, _ = await sh(f"nice -n 19 du -sx {' '.join(paths)} 2>/dev/null",
                      timeout=120)
    return sum(int(l.split()[0]) for l in out.splitlines() if l[:1].isdigit())


async def facts(name):
    """Всё, что узел знает о папете, одним ответом.

    Агент отдаёт ФАКТЫ, а не вердикт: собирает состояние мастер. Так логика
    «свободен/занят/завис» остаётся в одном месте и, главное, становится
    чистой функцией — её можно проверить без пула, чего про неё не скажешь
    с тех пор, как она жила поверх exec."""
    if not await tmux_alive(name):
        return {"present": False}
    scr, sess, clone = await asyncio.gather(
        screen(name),
        asyncio.to_thread(session.probe, clone_dir(name)),
        clone_facts(name))
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

    ОТДЕЛЬНЫЙ глагол, а не поле в states: du небыстрый, и воткнуть его в
    быстрый ответ о состояниях — значит читать медленный обмер как «агент
    молчит 20с». Не доехал за таймаут — у мастера прочерк, а не ложный
    диагноз."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    kbs = await asyncio.gather(*(du_kb(n) for n in names))
    return {"sizes": dict(zip(names, kbs))}


async def v_local(req):
    """Папета, живущие на ЭТОМ узле, — по сокетам tmux-серверов.

    Ростер без Nomad. Нужен узловому `mop mcp`: токена у него больше нет, и
    список джобов взять неоткуда. Мастер этим глаголом не пользуется — у него
    ростер богаче: аллокации, профиль LLM, репозиторий."""
    try:
        names = sorted(n for n in os.listdir(TMUX_DIR) if n.startswith(PREFIX))
    except OSError:
        names = []
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
    try:
        sock = session.resolve(clone_dir(name))["messagingSocketPath"]
    except Exception as e:
        return {"error": str(e)}
    wait = min(max(int(req.get("wait") or 0), 0), IDLE_WAIT)
    try:
        r = await asyncio.to_thread(
            session.send, sock, req["message"],
            priority=req.get("priority", "next"),
            from_name=req.get("from_name", "mop"),
            wait_idle=wait)
    except Exception as e:
        return {"error": str(e)}
    out = {"msg_id": r["msg_id"], "idle": (r["idle"] or {}).get("state")}
    if req.get("notify") and not wait:
        # Куда отвечать, говорит сам мастер: инбокс адресуется мастером, а не
        # шардом, иначе два терминала в одном проекте получали бы вести друг
        # друга. Без reply_to ждать бессмысленно — некому сказать.
        if req.get("reply_to"):
            asyncio.create_task(_watch_idle(name, sock, req["reply_to"]))
        else:
            out["notify"] = "не указан reply_to — уведомлять некуда"
    return out


def _await_idle(name, sock, timeout):
    """Подписка на простой БЕЗ сообщения папету.

    `session.send` всегда пишет пользовательский кадр первым, и прежний
    watch_idle этим и пользовался: папет получал пустое тело с одной лишь
    подсказкой. Здесь нужен только control-кадр — спрашивать «ты освободился?»,
    занимая ход, значит мешать ровно тому, чего ждёшь."""
    inbox = session.Inbox(os.path.dirname(sock), tag=name[-8:])
    try:
        sub = session.control_frame(
            "notify_when_idle", **{"from": inbox.address, "from_mode": "bypass"})
        session.write_frames(sock, [sub], session.peer_token(sock))
        return (inbox.wait_for("peer_idle_notice", sub["msg_id"], timeout)
                or {}).get("state")
    finally:
        inbox.close()


async def _watch_idle(name, sock, reply_to):
    """Дождаться простоя и сказать мастеру, который об этом попросил."""
    try:
        state = await asyncio.to_thread(_await_idle, name, sock, IDLE_WAIT)
    except Exception as e:
        return await _tell_master(reply_to, f"mop: не дождался простоя {name}: {e}")
    await _tell_master(reply_to, f"mop: папет {name} — {state}" if state
                       else f"mop: {name} не отчитался о простое за {IDLE_WAIT}с")


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
    """df по ФС, где живут клоны и target-каталоги. Узловой ФАКТ: давление
    оценивает мастер (mop gc), здесь только цифра.

    Одна ФС — $HOME: WSL-узла в кластере больше нет, и хитрости с бэкинг-
    стором уехали вместе с ним."""
    out, code = await sh(f"df -BG --output=avail,size {HOME}")
    if code not in (0, None) or not out.strip():
        return {"error": f"df не ответил: {out.strip() or f'exit {code}'}"}
    try:
        avail, size = out.splitlines()[1].split()
        return {"path": HOME, "free_gb": int(avail.rstrip("G")),
                "total_gb": int(size.rstrip("G"))}
    except (IndexError, ValueError):
        return {"error": f"df ответил не тем: {out.strip()!r}"}


async def v_wipe(req):
    """Снести рабочую копию папета и восстановить её из git; target — целиком.

    Это половина рецикла (вторая — перерегистрация джоба у мастера). Клон не
    переклонируется — дорого и незачем: reset откатывает отслеживаемое,
    `clean -xdff` выметает и untracked, и игнорируемое (внутриклоновые
    кэши, node_modules), но `-e` защищает подсеянное врапером — список
    живёт в MOP_PUPPET_SEED и у врапера, и здесь один. target-каталог —
    чисто производные данные, он удаляется rm -rf и тем самым снимается
    почти весь объём.

    Предохранитель: живая tmux-сессия — отказ. Агент не судит, свободен ли
    папет, но «сессия жива» — факт, и снос под живой сессией недопустим
    независимо от того, что решил мастер."""
    name = req["name"]
    if not name.startswith(PREFIX) or "/" in name:
        return {"error": f"имя {name!r} не похоже на {PREFIX}<проект>-<n>"}
    if await tmux_alive(name):
        return {"error": f"{name}: tmux-сессия жива — сначала останови джоб"}
    d = clone_dir(name)
    # Исключения чистки — из НАСТРОЙКИ, той же, что сеет врапер (PU_SEED в
    # спеке). Список в двух местах — здесь и в врапере — расползается ровно
    # к «посеяли одно, снесли другое».
    excl = " ".join(f"-e '{p}'" for p in
                     (s.strip() for s in config.get("MOP_PUPPET_SEED").split(",")) if p)
    out, code = await sh(
        f"git -C {d} reset --hard HEAD && git -C {d} clean -xdff {excl}")
    if code not in (0, None):
        return {"error": f"git в {d}: {out.strip() or f'exit {code}'}"}
    target = f"{HOME}/.cache/target-{name}"
    # Долго: сотни тысяч inode. Таймаут шире офисного — и обычный вызов шела
    # сюда не годится, он бы убил rm на полпути.
    _, code = await sh(f"rm -rf {target}", timeout=600)
    if code not in (0, None):
        return {"error": f"rm {target}: exit {code}"}
    return {"reset": True, "target": target}


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
        out, code = await sh(f"tmux -L {name} send-keys -t {name} {command}; "
                             f"sleep 1; tmux -L {name} capture-pane -p -t {name}")
        return {"screen": out} if code in (0, None) else {"error": out.strip()}
    if command.split()[0:1] and command.split()[0] not in SLASH_ALLOWED:
        return {"error": f"разрешены только: {', '.join(SLASH_ALLOWED + KEYS_ALLOWED)}"}
    if "'" in command:
        return {"error": "кавычка в команде: команда едет в шелл одной строкой"}
    keys = ""
    if command:
        keys = (f"tmux -L {name} send-keys -t {name} C-u; sleep 0.3; "
                f"tmux -L {name} send-keys -t {name} '{command}'; sleep 0.3; ")
    out, code = await sh(keys + f"tmux -L {name} send-keys -t {name} Enter; "
                                f"sleep 2; tmux -L {name} capture-pane -p -t {name}")
    if code not in (0, None):
        return {"error": out.strip() or f"tmux exit {code}"}
    return {"screen": out}


async def v_write(req):
    """Атомарная запись файла из белого списка, 600.

    Заменяет ту ветку раздачи кредов, что ездила шеллом в аллокацию. Список
    закрыт: без него это была бы произвольная запись в $HOME, то есть
    исполнение кода через ~/.bashrc."""
    written = []
    for path, b64 in req.get("files") or []:
        if path not in WRITABLE:
            return {"error": f"писать в {path} агенту не разрешено"}
        try:
            data = base64.b64decode(b64)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            tmp = f"{path}.tmp"
            with open(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600),
                      "wb") as f:
                f.write(data)
            os.replace(tmp, path)
        except Exception as e:
            return {"error": f"{path}: {e}"}
        written.append(path)
    return {"written": written}


VERBS = {"ping": v_ping, "local": v_local, "state": v_state,
         "states": v_states, "sizes": v_sizes, "send": v_send,
         "tail": v_tail, "type": v_type, "write": v_write,
         "disk": v_disk, "wipe": v_wipe}

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

    Шард берём ИЗ СУБЪЕКТА (`mop.<шард>.node.<узел>.<канал>`), а не из тела
    запроса: тело пишет отправитель, субъект — права NATS."""
    try:
        req = json.loads(msg.data.decode())
    except ValueError:
        return await msg.respond(
            json.dumps({"error": "запрос не JSON"}, ensure_ascii=False).encode())
    parts = msg.subject.split(".")
    req["_shard"] = parts[1] if len(parts) > 1 else ""

    verb = req.get("verb")
    fn = VERBS.get(verb)
    if fn is None:
        out = {"error": f"нет глагола {verb}; есть: {', '.join(sorted(VERBS))}"}
    elif public and verb not in PUBLIC_VERBS:
        # Не «нет прав», а прямо: глагол существует, но не в этом субъекте.
        out = {"error": f"глагол {verb} доступен только мастеру"}
    elif verb in ADMIN_VERBS and req["_shard"] != bus.ADMIN:
        out = {"error": f"глагол {verb} — узловой, шарду {req['_shard']} не отдаётся"}
    elif verb in NAMED_VERBS and not await _mine(req, req.get("name") or ""):
        # Главная проверка шардирования. Прав NATS тут мало: мастер шарда A
        # законно пишет в свой субъект, но может назвать папета из B.
        out = {"error": f"папет {req.get('name')} не в шарде {req['_shard']}"}
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
        servers=[c["url"]], user=c.get("user"), password=c.get("password"),
        name=f"mop-agent/{node}",
        allow_reconnect=True, max_reconnect_attempts=-1, reconnect_time_wait=2)
    # cb ОБЯЗАН быть корутиной — nats-py отвергает обычную функцию. И каждый
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
    print(f"mop-agent: узел {node}, подписан на mop.*.node.{node}.rpc|msg "
          f"и mop.*.all.msg", flush=True)
    await asyncio.Event().wait()


async def check():
    """Проверка прогоном, а не чтением конфига: юнит, упавший в бесконечный
    реконнект, systemd вполне устраивает, и «запущен» не значит «подписан».
    Спрашиваем через ПУБЛИЧНЫЙ субъект узла, а не через .rpc: туда узлу писать
    и не положено — это и есть та граница прав, ради которой шину заводили.
    Первый прогон проверки уткнулся ровно в неё, и был неправ он, а не права."""
    c = bus.config()
    print(f"mop-agent: узел {node_name()}, шина {c['url']}, "
          f"глаголов {len(VERBS)} (публичных {len(PUBLIC_VERBS)})")
    nc = await nats.connect(servers=[c["url"]], user=c.get("user"),
                            password=c.get("password"),
                            name="mop-agent/check",
                            allow_reconnect=False, connect_timeout=5)
    try:
        msg = await nc.request(bus.subject(node_name(), "msg", shard=bus.ADMIN),
                               json.dumps({"verb": "ping"}).encode(), timeout=5)
        print(f"подписан: {msg.data.decode()}")
    finally:
        await nc.close()


def main(argv):
    if "--check" in argv:
        try:
            asyncio.run(check())
        except Exception as e:
            print(f"агент НЕ отвечает на своём субъекте: {e}", file=sys.stderr)
            return 1
        return 0
    try:
        asyncio.run(serve())
    except KeyboardInterrupt:
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
