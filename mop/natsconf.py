"""Пользователи шины: файл `users.conf`, который NATS включает в authorization.

Список пользователей собирает один код (#116). Раньше его рендерил шаблон
ansible на контроллере, и завести проект мог только прогон под root. Теперь
файл пишет сервер: люди, сервисы и узлы приходят базовым JSON от `mop deploy`
(`base-users.json` -- их пароли знает только контроллер), папеты -- из
реестра сервера, с паролями, которые сервер заводит сам.

Один файл, а не два: включить второй файл внутрь массива `users` NATS не
умеет (`nats-server -t`: ошибка разбора на include), а второй блок
authorization не сливается с первым.

Данные, без печати: печатает `mop cluster users`.
"""
import json
import os
import secrets
import signal
import string
import subprocess

DIR = "/etc/nats"
BASE = os.path.join(DIR, "base-users.json")
USERS = os.path.join(DIR, "users.conf")
UNIT = "nats"


# ─── чистое: текст файла ─────────────────────────────────────────────────
def _q(text):
    """Строка конфига NATS. JSON-кавычки NATS понимает как свои."""
    return json.dumps(str(text))


def _list(items):
    return "[" + ", ".join(_q(i) for i in items) + "]"


def _perms(allow, deny=None, sub_allow=None):
    """Блок прав. Подписка не задана -- те же права, что на публикацию."""
    def side(a, d):
        return "{ allow: " + _list(a) + (f", deny: {_list(d)}" if d else "") + " }"
    sub = side(allow, deny) if sub_allow is None else side(sub_allow, None)
    return (f"    permissions: {{\n      publish:   {side(allow, deny)}\n"
            f"      subscribe: {sub}\n    }}")


def _user(name, password, perms, extra=""):
    return (f"  {{\n    user: {_q(name)}, password: {_q(password)}\n"
            f"{extra}{perms}\n  }}")


def render(base, projects):
    """Базовые пользователи и папеты проектов -> текст `users.conf`.

    base -- {service: пароль, operators: {имя: {password, allow, deny}},
    nodes: {узел: пароль}}; projects -- {проект: пароль}. Порядок -- по
    именам: одинаковый вход даёт одинаковый файл, и прогон без изменений не
    дёргает reload. Проект без пароля -- отказ: пользователь без пароля на
    шине хуже отсутствующего."""
    for p, pw in projects.items():
        if not pw:
            raise ValueError(f"project {p} has no bus password")
    out = ["users = ["]
    # Сервисы сервера (#104): машина, а не человек. Права пока как у admin.
    out.append(_user("service", base["service"], _perms(["mop.>", "_INBOX.>"])))
    # Люди (#106): вход только через WebSocket (#105), права по роли.
    for name, op in sorted((base.get("operators") or {}).items()):
        out.append(_user(name, op["password"], _perms(op["allow"], op.get("deny")),
                         '    allowed_connection_types: ["WEBSOCKET"]\n'))
    # Папеты проекта: пишут соседям и своему мастеру; управляющий .rpc сюда
    # не входит -- печать в чужой TUI и запись файлов остаются за мастером.
    for p in sorted(projects):
        pub = [f"mop.{p}.node.*.msg", f"mop.{p}.all.msg", f"mop.{p}.master.>",
               f"mop.{p}.events", "_INBOX.>"]
        out.append(_user(f"puppet-{p}", projects[p], _perms(pub, sub_allow=["_INBOX.>"])))
    # Агент узла: пересекает проекты сознательно (он их и разделяет), но
    # слушает только свой узел -- подписка в NATS не эксклюзивна.
    for node, pw in sorted((base.get("nodes") or {}).items()):
        pub = ["mop.*.master.>", "mop.*.events", f"mop.*.node.{node}.msg",
               "mop.*.server.rpc", "_INBOX.>"]
        sub = [f"mop.*.node.{node}.>", "mop.*.all.msg", "_INBOX.>"]
        out.append(_user(f"node-{node}", pw, _perms(pub, sub_allow=sub)))
    out.append("]")
    return "\n".join(out) + "\n"


# ─── сервер: пароли, файл, reload ────────────────────────────────────────
def _pass_file(root, project):
    return os.path.join(root, f"nats-puppet-{project}.pass")


def _write_private(path, text):
    """Атомарно и 0600: nats-server может перечитать файл в любой момент."""
    tmp = f"{path}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        f.write(text)
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def passwords(names, root):
    """{проект: пароль}; недостающий пароль заводится, имеющийся не меняется.

    Пароли папетов рождаются здесь, на сервере (#116): смени имеющийся -- и
    живые папеты отвалятся от шины при первом переподключении."""
    os.makedirs(root, mode=0o700, exist_ok=True)
    alphabet = string.ascii_letters + string.digits
    out = {}
    for p in names:
        path = _pass_file(root, p)
        try:
            with open(path) as f:
                out[p] = f.read().strip()
        except FileNotFoundError:
            out[p] = ""
        if not out[p]:
            out[p] = "".join(secrets.choice(alphabet) for _ in range(32))
            _write_private(path, out[p] + "\n")
    return out


def read_base(path=BASE):
    with open(path) as f:
        return json.load(f)


def write(text, path=USERS):
    """-> изменился ли файл."""
    try:
        with open(path) as f:
            if f.read() == text:
                return False
    except FileNotFoundError:
        pass
    _write_private(path, text)
    return True


def reload():
    """SIGHUP nats-server: он работает под пользователем пула (#115), и сигнал
    свой uid шлёт без посредников. О битом конфиге SIGHUP не сообщает --
    проверяет вызывающий, подключением."""
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", UNIT],
                         capture_output=True, text=True).stdout.strip()
    if not pid.isdigit() or pid == "0":
        raise RuntimeError(f"{UNIT} is not running")
    os.kill(int(pid), signal.SIGHUP)
