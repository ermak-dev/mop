"""Локальная IPC-поверхность сессии claude code: файлы сессий и канал сообщений.

Один модуль на два знания, потому что меняются они вместе — это один и тот же
контракт claude, описанный в docs/CHANNEL.md: где лежит файл сессии, что в нём, где
её сокет и как в этот сокет говорить.

Важно: модуль намеренно standalone — только стандартная библиотека и ни одного
импорта из пакета mop. Он работает в двух режимах:

  * импортом на управляющей машине (командлеты bin/, MCP-сервер);
  * исходником, уехавшим внутрь аллокации Nomad: сокет папета host-local, с
    управляющей машины к нему не подключиться, поэтому отправитель и пробник
    едут на узел и исполняются там как `python3 - probe|send …`.

Второй режим и есть причина запрета на импорты: на узел уезжает один файл.
"""
import glob
import json
import os
import socket
import sys
import threading
import time
import uuid

SESSIONS = os.path.expanduser("~/.claude/sessions")
# Записи исхода хода (#222): их пишет хук claude, читает `state`.
TURNS = os.path.expanduser("~/.local/state/mop/turns")
# Метка кредита (#284): имя кредита реестра, которым работает это тело;
# кладёт сервер вместе с кредами (paths.CRED_MARK), хук вписывает в запись
# хода, и провал хода приписывается кредиту, а не только папету.
CRED_MARK = os.path.expanduser("~/.local/state/mop/cred")
CONNECT_TIMEOUT = 5
PRIORITIES = ("now", "next", "later")
MODES = ("bypass", "prompting")


class Ambiguous(LookupError):
    """Под целью подходит несколько сессий. Отдельный тип, потому что «здесь
    такой нет» и «их тут несколько» ведут в разные стороны: первое разрешает
    искать адресата дальше (на шине), второе обязано остановить — иначе
    однажды напишем не тому."""


# ─── файлы сессий ────────────────────────────────────────────────────────
def sessions():
    """Все читаемые файлы сессий, у которых есть адрес инбокса.

    Мёртвые тоже: живость решает сокет, а не файл — файл переживает грязную
    смерть процесса и остаётся лежать с протухшим status."""
    out = []
    for f in sorted(glob.glob(f"{SESSIONS}/*.json")):
        try:
            with open(f) as fh:
                d = json.load(fh)
        except Exception:
            continue
        if d.get("messagingSocketPath"):
            out.append(d)
    return out


def socket_alive(path):
    """Единственная достоверная проба живости: слушает ли кто-то сокет."""
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(1)
    try:
        s.connect(path)
        return True
    except OSError:
        return False
    finally:
        s.close()


def _in_cwd(cwd):
    """Файлы сессий этого каталога, свежие первыми. Сравнение по realpath:
    сессия и спрашивающий могут называть один каталог через разные ссылки."""
    target = os.path.realpath(cwd)
    mine = [d for d in sessions() if os.path.realpath(d.get("cwd") or "") == target]
    return sorted(mine, key=lambda d: d.get("statusUpdatedAt", 0), reverse=True)


def session_for_cwd(cwd):
    """Сессия, работающая в этом каталоге; самая свежая, если их несколько."""
    mine = _in_cwd(cwd)
    return mine[0] if mine else None


def live_session_for_cwd(cwd):
    """Самая свежая живая сессия каталога.

    Файлов на один каталог накапливается много (у одного папета их было
    18): каждая умершая сессия оставляет свой. Брать просто самую свежую
    нельзя — она может быть трупом; спрашиваем сокет и спускаемся по времени,
    пока кто-нибудь не отзовётся."""
    for d in _in_cwd(cwd):
        if socket_alive(d["messagingSocketPath"]):
            return d
    return None


def resolve(target):
    """Куда писать. Каталог -> живая сессия в нём (так адресуют папет пула),
    остальное -> строгий поиск по имени/pid; путь к сокету find принимает как
    есть."""
    if os.path.isdir(target):
        d = live_session_for_cwd(target)
        if d is None:
            raise LookupError(f"no live claude session in {target}")
        return d
    return find(target)


def find(target):
    """Цель -> файл сессии. Путь к сокету принимаем как есть: он может
    указывать на сессию, чей файл нам не читается."""
    if target.endswith(".sock"):
        return {"messagingSocketPath": target, "pid": None, "name": target}
    hits = [d for d in sessions()
            if str(d.get("pid")) == target
            or d.get("name") == target
            or d.get("cwd") == target]
    if not hits:
        hits = _in_cwd(target)
    if not hits:
        raise LookupError(f"session not found: {target}")
    if len(hits) > 1:
        # Имена выводятся из каталога и не уникальны; молча взять первую —
        # значит однажды написать не тому.
        names = ", ".join(f"{d.get('name')}[{d.get('pid')}]" for d in hits)
        raise Ambiguous(f"'{target}' matches several sessions: {names} — specify pid")
    return hits[0]


def peer_token(sock_path):
    """Ключ инбокса: ~/.claude/sessions/<pid>.<sha256(канонический путь)>.key

    Без него сообщение примут, но подписку notify_when_idle молча уронят."""
    import hashlib
    h = hashlib.sha256(os.path.realpath(sock_path).encode()).hexdigest()
    for f in glob.glob(f"{SESSIONS}/*.{h}.key"):
        try:
            with open(f) as fh:
                return json.load(fh)["peerToken"]
        except Exception:
            continue
    return None


def probe(cwd):
    """Достоверное состояние сессии, работающей в cwd: "<status> <alive> <listen>"
    либо "none". Формат строковый и плоский, потому что эта функция чаще всего
    исполняется на другом хосте, а вызывающий читает stdout."""
    d = session_for_cwd(cwd)
    if d is None:
        return "none"
    alive = False
    try:
        os.kill(int(d.get("pid")), 0)
        alive = True
    except Exception:
        alive = False
    listen = socket_alive(d["messagingSocketPath"]) if d.get("messagingSocketPath") else False
    return f"{d.get('status', '?')} {int(alive)} {int(listen)}"


# ─── исход хода: сток хуков claude (#222) ────────────────────────────────
# Состояние папета угадывалось по экрану tmux, и 24.09 два папета, умершие
# посреди хода на «Login expired», полтора часа читались как idle. Claude
# Code сообщает исход хода сам: вместо Stop приходит StopFailure с кодом
# ошибки (authentication_failed, rate_limit, billing_error, ...). Хук кладёт
# запись по session_id, `state` отдаёт её вместе с файлом сессии.
#
# Какие события: начало сессии, запрос, нормальный конец хода и смена модели
# снимают ошибку; StopFailure её ставит. PreToolUse/PostToolUse идут на
# каждый вызов инструмента, а busy и так есть в файле сессии; Notification
# не говорит, когда диалог закрылся, -- живой диалог в waitingFor файла.
CLEARING = ("SessionStart", "UserPromptSubmit", "Stop", "PostModelSwitch")
FAILURE = "StopFailure"
DETAIL_MAX = 300
TURN_TTL = 7 * 86400


def _plain_id(sid):
    """session_id -- имя файла: только буквы, цифры, дефис и подчёркивание,
    иначе вход -- не наш (путь в имени писал бы мимо каталога)."""
    return isinstance(sid, str) and bool(sid) and \
        all(c.isalnum() or c in "-_" for c in sid) and sid.isascii()


def turn_record(payload, now, cred=None):
    """Вход хука -> запись исхода хода или None (событие не наше, нет
    session_id). Форма ровно {event, at, error, detail, cred}; cred -- имя
    кредита из метки тела (#284) либо None."""
    if not isinstance(payload, dict) or not _plain_id(payload.get("session_id")):
        return None
    event = payload.get("hook_event_name")
    if event in CLEARING:
        return {"event": event, "at": int(now), "error": None, "detail": None, "cred": cred}
    if event == FAILURE:
        # Кода нет -- всё равно провал: «unknown» из словаря claude, а не
        # None, который читался бы снятой ошибкой.
        detail = payload.get("last_assistant_message")
        return {"event": event, "at": int(now), "error": payload.get("error") or "unknown",
                "detail": detail[:DETAIL_MAX] if isinstance(detail, str) and detail else None,
                "cred": cred}
    return None


def cred_mark(path=None):
    """Имя кредита из метки тела либо None: нет файла -- нет аренды."""
    try:
        with open(path or CRED_MARK) as f:
            name = f.read().strip()
    except OSError:
        return None
    return name or None


def _write_turn(root, sid, record, now):
    """Запись атомарно: временный файл рядом + os.replace. Попутно снимаются
    записи старше недели -- мёртвые сессии копятся."""
    os.makedirs(root, exist_ok=True)
    tmp = os.path.join(root, f".{sid}.{uuid.uuid4().hex}.tmp")
    try:
        with open(tmp, "w") as f:
            json.dump(record, f)
        os.replace(tmp, os.path.join(root, f"{sid}.json"))
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)
    for f in glob.glob(os.path.join(root, "*.json")):
        try:
            if os.path.getmtime(f) < now - TURN_TTL:
                os.remove(f)
        except OSError:
            pass


def hook(text, now, root=None):
    """Вход хука (stdin) -> запись на диск, если событие наше."""
    payload = json.loads(text)
    record = turn_record(payload, now, cred_mark())
    if record is not None:
        _write_turn(root or TURNS, payload["session_id"], record, now)


def read_turn(sid, root=None):
    """Запись исхода хода сессии или None."""
    if not _plain_id(sid):
        return None
    try:
        with open(os.path.join(root or TURNS, f"{sid}.json")) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    return got if isinstance(got, dict) else None


def state(cwd):
    """Состояние свежейшей сессии каталога одной записью: status, alive и
    listen -- как у probe, waitingFor -- из файла сессии (открытый диалог),
    turn -- запись исхода хода этой сессии. Сессии нет -- те же ключи,
    пустые: status и waitingFor null, alive и listen false, turn null."""
    d = session_for_cwd(cwd)
    if d is None:
        return {"status": None, "waitingFor": None, "alive": False, "listen": False,
                "turn": None}
    try:
        os.kill(int(d.get("pid")), 0)
        alive = True
    except Exception:
        alive = False
    listen = socket_alive(d["messagingSocketPath"]) if d.get("messagingSocketPath") else False
    return {"status": d.get("status", "?"), "waitingFor": d.get("waitingFor"),
            "alive": alive, "listen": listen, "turn": read_turn(d.get("sessionId"))}


# ─── протокол канала ─────────────────────────────────────────────────────
# Подсказка едет в теле каждого сообщения, а не в системном промпте, потому
# что системную инструкцию съедает компактация, а тело — нет. Чинит ровно тот
# случай, на котором мы споткнулись: glm-папет получило сообщение от
# "mop", попыталось ответить встроенным SendMessage, получило "No agent
# named 'mop' is reachable" и отдало ответ случайному соседу по хосту.
#
# Обратный адрес называем прямо. Раньше здесь стояло «отправитель обратного
# адреса не даёт», и это была не осторожность, а дыра: папет, которого просили
# отчитаться, шёл в mcp__mop__send с любым именем, какое смог придумать, и
# получал глухое "Error executing tool send" — маршрута до мастера в send
# просто не было. Теперь у мастера есть адрес (его инбокс на шине), он едет в
# конверте полем from-name, и подсказка называет его тем же словом, каким его
# примет send.
#
# uds-адрес отправителя по-прежнему не обещаем: доставляет агент узла, а сокет
# его собственного инбокса живёт ровно столько, сколько ждёт доставка.
def reply_hint(from_name=None):
    """Чем закончить тело сообщения: куда адресату отвечать."""
    how = (f"Reply to the sender: mcp__mop__send(to=\"{from_name}\", …) — that "
           f"is its bus address, not a session name."
           if from_name and from_name != "mop" else
           "The sender did not give a reply address.")
    return ("[mop channel] Delivered over the pool's socket channel. " + how +
            " Who else you can write to — mcp__mop__agents: the pool's puppets and "
            "the project's masters. The built-in SendMessage won't work for this: it "
            "only reaches sessions on this same host and knows nothing about the "
            "rest of the pool.")


def envelope(body, from_addr=None, from_name=None, mode="bypass"):
    """Конверт заявки о режиме прав.

    Папет с --dangerously-skip-permissions не отдаёт модели сообщение от
    отправителя, который не заявил свой режим: оно кладёт его в held-очередь и
    ждёт живого человека. Для папета это неотличимо от потери сообщения.

    Порядок атрибутов фиксирован: приёмник разбирает конверт регуляркой и
    проверяет, что обратная сборка даёт исходную строку байт в байт —
    переставленные атрибуты просто не распознаются."""
    attrs = ""
    if from_addr:
        attrs += f' from="{from_addr}"'
    if from_name:
        attrs += f' from-name="{from_name}"'
    if mode:
        attrs += f' from-mode="{mode}"'
    return f"<cross-session-message{attrs}>\n{body}\n</cross-session-message>"


def user_frame(content, priority="next", from_addr=None):
    f = {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "user",
         "message": {"role": "user", "content": content}, "priority": priority}
    if from_addr:
        f["from"] = from_addr
    return f


def control_frame(action, **fields):
    return {"msgV": 1, "msg_id": str(uuid.uuid4()), "type": "control",
            "action": action, **fields}


def write_frames(sock_path, frames, token=None):
    """Одно соединение, NDJSON, закрыть.

    Ответа в этом же соединении не бывает никогда: квитанции сессия шлёт
    отдельным коннектом на адрес из поля from."""
    line = ""
    if token:
        line += json.dumps({"type": "auth", "token": token}) + "\n"
    for f in frames:
        line += json.dumps(f, ensure_ascii=False) + "\n"
    s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    s.settimeout(CONNECT_TIMEOUT)
    try:
        s.connect(sock_path)
    except FileNotFoundError:
        raise ConnectionError(f"inbox not found: {sock_path} — session died or moved")
    except ConnectionRefusedError:
        raise ConnectionError(f"inbox is dead: {sock_path} — process is not listening")
    try:
        s.sendall(line.encode())
    finally:
        s.close()


class Inbox:
    """Свой инбокс для квитанций и peer_idle_notice.

    Имя и каталог не произвольные: приёмник отвечает, только если адрес лежит в
    том же каталоге сокетов и назван как <pid>.sock — иначе он отказывается
    писать в чужое пространство имён и молча роняет ответ."""

    def __init__(self, sockets_dir, tag=None):
        # tag нужен, когда один процесс ждёт простоя сразу нескольких сессий:
        # агент узла обслуживает всех папетов хоста, и без метки второй инбокс
        # отобрал бы путь <pid>.sock у первого. Форма <pid>-<hex>.sock приёмнику
        # тоже годится — других он молча не отвечает.
        leaf = f"{os.getpid()}-{tag}.sock" if tag else f"{os.getpid()}.sock"
        self.path = os.path.join(sockets_dir, leaf)
        self.frames = []
        self._event = threading.Event()
        try:
            os.unlink(self.path)
        except FileNotFoundError:
            pass
        self.srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.srv.bind(self.path)
        os.chmod(self.path, 0o600)
        self.srv.listen(8)
        threading.Thread(target=self._serve, daemon=True).start()

    @property
    def address(self):
        return f"uds:{self.path}"

    def _serve(self):
        while True:
            try:
                conn, _ = self.srv.accept()
            except OSError:
                return
            buf = b""
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                buf += chunk
            conn.close()
            for ln in buf.decode(errors="replace").splitlines():
                if not ln.strip():
                    continue
                try:
                    self.frames.append(json.loads(ln))
                except ValueError:
                    continue
                self._event.set()

    def wait_for(self, action, orig_msg_id, timeout):
        deadline = time.time() + timeout
        while time.time() < deadline:
            for f in self.frames:
                if f.get("action") == action and f.get("orig_msg_id") == orig_msg_id:
                    return f
            self._event.wait(1)
            self._event.clear()
        return None

    def close(self):
        try:
            self.srv.close()
        finally:
            try:
                os.unlink(self.path)
            except FileNotFoundError:
                pass


def send(sock_path, body, priority="next", mode="bypass", from_name="mop",
         wait_idle=0, hint=True):
    """Отправить сообщение сессии. -> {"msg_id":…, "idle":<кадр|None>}

    wait_idle > 0 поднимает свой инбокс, подписывается на notify_when_idle и
    ждёт до wait_idle секунд, пока сессия отчитается о простое."""
    token = peer_token(sock_path)
    inbox = Inbox(os.path.dirname(sock_path)) if wait_idle else None
    try:
        from_addr = inbox.address if inbox else None
        text = f"{body}\n\n{reply_hint(from_name)}" if hint else body
        frame = user_frame(envelope(text, from_addr, from_name, mode), priority, from_addr)
        write_frames(sock_path, [frame], token)
        if not inbox:
            return {"msg_id": frame["msg_id"], "idle": None}
        # Подписка отдельным кадром и со своим msg_id: notice приходит с
        # orig_msg_id именно подписки, а не сообщения.
        sub = control_frame("notify_when_idle", **{"from": from_addr, "from_mode": mode})
        write_frames(sock_path, [sub], token)
        return {"msg_id": frame["msg_id"],
                "idle": inbox.wait_for("peer_idle_notice", sub["msg_id"], wait_idle)}
    finally:
        if inbox:
            inbox.close()


def wait_idle(cwd, timeout, tag=None):
    """Дождаться простоя сессии в cwd, не занимая её ход. -> состояние | None.

    `send` всегда пишет пользовательский кадр первым, и прежний ожидатель этим
    и пользовался: сессия получала пустое тело с одной лишь подсказкой. Здесь
    нужен только control-кадр — спрашивать «ты освободился?», занимая ход,
    значит мешать ровно тому, чего ждёшь.

    Форма CLI обязательна, а не удобна: у контейнерного папета сокет сессии
    живёт внутри тела, и ждать его импортом с узла некому. Без этого глагола
    push-уведомления мастеру у таких папетов молча не приходят."""
    sock = resolve(cwd)["messagingSocketPath"]
    inbox = Inbox(os.path.dirname(sock), tag=tag)
    try:
        sub = control_frame("notify_when_idle",
                            **{"from": inbox.address, "from_mode": "bypass"})
        write_frames(sock, [sub], peer_token(sock))
        return (inbox.wait_for("peer_idle_notice", sub["msg_id"], timeout)
                or {}).get("state")
    finally:
        inbox.close()


def main(argv):
    if argv[:1] == ["hook"]:
        # Молча и всегда 0: stdout хуков SessionStart и UserPromptSubmit
        # уходит в контекст модели, а код 2 на Stop заставил бы claude
        # продолжить ход. Любой сбой стока -- не дело сессии.
        try:
            hook(sys.stdin.read(), time.time())
        except BaseException:
            pass
        return 0
    if len(argv) >= 2 and argv[0] == "state":
        try:
            out = state(argv[1])
        except Exception as e:
            out = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if "error" not in out else 1
    if len(argv) >= 2 and argv[0] == "probe":
        print(probe(argv[1]))
        return 0
    if len(argv) >= 3 and argv[0] == "wait-idle":
        # JSON и здесь, по той же причине, что у send: чаще всего эта ветка
        # исполняется в другой машине, и вызывающий читает один лишь stdout.
        try:
            out = {"state": wait_idle(argv[1], int(argv[2]),
                                      tag=(argv[3] if len(argv) > 3 else None))}
        except Exception as e:
            out = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if "error" not in out else 1
    if len(argv) >= 3 and argv[0] == "send":
        # Ответ всегда JSON, в том числе на ошибку: чаще всего эта ветка
        # исполняется на другом хосте, и вызывающий читает один лишь stdout.
        target, body = argv[1], argv[2]
        rest = argv[3:]
        opts = dict(zip(rest[::2], rest[1::2]))
        try:
            sock = resolve(target)["messagingSocketPath"]
            r = send(sock, body,
                     priority=opts.get("--priority", "next"),
                     mode=opts.get("--mode", "bypass"),
                     from_name=opts.get("--from-name", "mop"),
                     wait_idle=int(opts.get("--wait", 0)))
            out = {"msg_id": r["msg_id"], "idle": (r["idle"] or {}).get("state")}
        except Exception as e:
            out = {"error": str(e)}
        print(json.dumps(out, ensure_ascii=False))
        return 0 if "error" not in out else 1
    print("usage: session.py probe <cwd> | state <cwd> | hook (stdin) | "
          "wait-idle <cwd> <timeout> [tag] | "
          "send <target> <text> [--priority P] [--mode M] [--from-name N] "
          "[--wait SEC]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
