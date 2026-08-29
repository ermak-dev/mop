"""Локальная IPC-поверхность сессии claude code: файлы сессий и канал сообщений.

Один модуль на два знания, потому что меняются они вместе — это один и тот же
контракт claude, описанный в docs/CHANNEL.md: где лежит файл сессии, что в нём, где
её сокет и как в этот сокет говорить.

ВАЖНО: модуль намеренно standalone — только стандартная библиотека и ни одного
импорта из пакета mop. Он работает в двух режимах:

  * импортом на управляющей машине (командлеты bin/, MCP-сервер);
  * исходником, уехавшим внутрь аллокации Nomad: сокет слейва host-local, с
    управляющей машины к нему не подключиться, поэтому отправитель и пробник
    едут на узел и исполняются там как `python3 - probe|send …`.

Второй режим и есть причина запрета на импорты: на узел уезжает ОДИН файл.
"""
import base64
import glob
import json
import os
import socket
import sys
import threading
import time
import uuid

SESSIONS = os.path.expanduser("~/.claude/sessions")
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


def session_for_cwd(cwd):
    """Сессия, работающая в этом каталоге; самая свежая, если их несколько."""
    target = os.path.realpath(cwd)
    best = None
    for d in sessions():
        if os.path.realpath(d.get("cwd") or "") != target:
            continue
        if best is None or d.get("statusUpdatedAt", 0) > best.get("statusUpdatedAt", 0):
            best = d
    return best


def live_session_for_cwd(cwd):
    """Самая свежая ЖИВАЯ сессия каталога.

    Файлов на один каталог накапливается много (у слейва rugent-1 их было
    18): каждая умершая сессия оставляет свой. Брать просто самую свежую
    нельзя — она может быть трупом; спрашиваем сокет и спускаемся по времени,
    пока кто-нибудь не отзовётся."""
    target = os.path.realpath(cwd)
    mine = [d for d in sessions() if os.path.realpath(d.get("cwd") or "") == target]
    for d in sorted(mine, key=lambda d: d.get("statusUpdatedAt", 0), reverse=True):
        if socket_alive(d["messagingSocketPath"]):
            return d
    return None


def resolve(target):
    """Куда писать. Каталог -> живая сессия в нём (так адресуют слейв пула),
    остальное -> строгий поиск по имени/pid."""
    if target.endswith(".sock"):
        return {"messagingSocketPath": target, "pid": None, "name": target}
    if os.path.isdir(target):
        d = live_session_for_cwd(target)
        if d is None:
            raise LookupError(f"в {target} нет живой сессии claude")
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
        hits = [d for d in sessions()
                if os.path.realpath(d.get("cwd") or "") == os.path.realpath(target)]
    if not hits:
        raise LookupError(f"нет сессии: {target}")
    if len(hits) > 1:
        # Имена выводятся из каталога и не уникальны; молча взять первую —
        # значит однажды написать не тому.
        names = ", ".join(f"{d.get('name')}[{d.get('pid')}]" for d in hits)
        raise Ambiguous(f"под '{target}' подходит несколько сессий: {names} — уточни pid")
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


# ─── протокол канала ─────────────────────────────────────────────────────
# Подсказка едет в теле КАЖДОГО сообщения, а не в системном промпте, потому
# что системную инструкцию съедает компактация, а тело — нет. Чинит ровно тот
# случай, на котором мы споткнулись: glm-слейв получило сообщение от
# "mop", попыталось ответить встроенным SendMessage, получило "No agent
# named 'mop' is reachable" и отдало ответ случайному соседу по хосту.
#
# ОБРАТНЫЙ АДРЕС НАЗЫВАЕМ ПРЯМО. Раньше здесь стояло «отправитель обратного
# адреса не даёт», и это была не осторожность, а дыра: слейв, которого просили
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
    how = (f"Ответить отправителю: mcp__mop__send(to=\"{from_name}\", …) — это "
           f"его адрес на шине, а не имя сессии."
           if from_name and from_name != "mop" else
           "Обратного адреса отправитель не назвал.")
    return ("[канал mop] Доставлено сокет-каналом пула. " + how +
            " Кому ещё можно писать — mcp__mop__agents: слейва пула и мастера "
            "шарда. Встроенный SendMessage для этого не годится: он "
            "дотягивается только до сессий этого же хоста и про остальной пул "
            "не знает.")


def envelope(body, from_addr=None, from_name=None, mode="bypass"):
    """Конверт заявки о режиме прав.

    Слейв с --dangerously-skip-permissions НЕ отдаёт модели сообщение от
    отправителя, который не заявил свой режим: оно кладёт его в held-очередь и
    ждёт живого человека. Для слейва это неотличимо от потери сообщения.

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
        raise ConnectionError(f"инбокс не найден: {sock_path} — сессия умерла или переехала")
    except ConnectionRefusedError:
        raise ConnectionError(f"инбокс мёртв: {sock_path} — процесс не слушает")
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
        # агент узла обслуживает всех слейвов хоста, и без метки второй инбокс
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
        # orig_msg_id именно ПОДПИСКИ, а не сообщения.
        sub = control_frame("notify_when_idle", **{"from": from_addr, "from_mode": mode})
        write_frames(sock_path, [sub], token)
        return {"msg_id": frame["msg_id"],
                "idle": inbox.wait_for("peer_idle_notice", sub["msg_id"], wait_idle)}
    finally:
        if inbox:
            inbox.close()


def main(argv):
    if len(argv) >= 2 and argv[0] == "probe":
        print(probe(argv[1]))
        return 0
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
    print("usage: session.py probe <cwd> | send <цель> <текст> [--priority P] "
          "[--mode M] [--from-name N] [--wait СЕК]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
