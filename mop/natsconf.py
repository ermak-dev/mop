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
import re
import secrets
import signal
import string
import subprocess
import time
import urllib.request

from . import busnames, config, fsutil

DIR = "/etc/nats"
BASE = os.path.join(DIR, "base-users.json")
USERS = os.path.join(DIR, "users.conf")
CONF = os.path.join(DIR, "nats-server.conf")
UNIT = "nats"
# Туда его ставит роль bus, и оттуда же его запускает юнит.
NATS_BIN = "/usr/local/bin/nats-server"
# Сколько ждать, пока nats перечитает конфиг: на стенде -- доли секунды.
RELOAD_WAIT = 5


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


# Сервисы сервера (#104): машина, а не человек. Права пока как у admin.
SERVICE_PERMISSIONS = [busnames.everything(), busnames.INBOX]


def puppet_permissions(project):
    """Папет проекта: пишет соседям и своему мастеру; управляющий .rpc сюда
    не входит -- печать в чужой TUI и запись файлов остаются за мастером.
    Слушает только ответы на свои запросы."""
    return {"publish": [busnames.node(project, "*", "msg"), busnames.broadcast(project),
                        busnames.masters(project), busnames.events(project), busnames.INBOX],
            "subscribe": [busnames.INBOX]}


def node_permissions(node):
    """Агент узла: пересекает проекты сознательно (он их и разделяет), но
    слушает только свой узел -- подписка в NATS не эксклюзивна."""
    any_ = busnames.ANY
    return {"publish": [busnames.masters(any_), busnames.events(any_),
                        busnames.node(any_, node, "msg"), busnames.server(any_),
                        busnames.INBOX],
            "subscribe": [busnames.node(any_, node, ">"), busnames.broadcast(any_),
                          busnames.INBOX]}


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
    out.append(_user(busnames.SERVICE, base[busnames.SERVICE], _perms(SERVICE_PERMISSIONS)))
    # Люди (#106): вход только через WebSocket (#105), права по роли.
    for name, op in sorted((base.get("operators") or {}).items()):
        out.append(_user(name, op["password"], _perms(op["allow"], op.get("deny")),
                         '    allowed_connection_types: ["WEBSOCKET"]\n'))
    for p in sorted(projects):
        perms = puppet_permissions(p)
        out.append(_user(busnames.puppet_user(p), projects[p],
                         _perms(perms["publish"], sub_allow=perms["subscribe"])))
    for node, pw in sorted((base.get("nodes") or {}).items()):
        perms = node_permissions(node)
        out.append(_user(busnames.node_user(node), pw,
                         _perms(perms["publish"], sub_allow=perms["subscribe"])))
    out.append("]")
    return "\n".join(out) + "\n"


# ─── сервер: пароли, файл, reload ────────────────────────────────────────
def _pass_file(root, project):
    return os.path.join(root, busnames.pass_file(busnames.puppet_user(project)))


def passwords(names, root):
    """{проект: пароль}; недостающий пароль заводится, имеющийся не меняется.

    Пароли папетов рождаются здесь, на сервере (#116): смени имеющийся -- и
    живые папеты отвалятся от шины при первом переподключении.

    Каталог -- 0700, в том числе уже лежащий (#170): mode у makedirs
    действует только на новый, и лежащий шире оставлял пароли читаемыми."""
    fsutil.make_private_dir(root)
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
            fsutil.write_private(path, out[p] + "\n")
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
    # Атомарно: nats-server может перечитать файл в любой момент.
    fsutil.write_private(path, text)
    return True


def apply(names, creds_dir, path=USERS):
    """Файл пользователей по реестру: пароли, текст, запись.
    -> (изменился ли, {проект: пароль}). Reload -- дело вызывающего."""
    pw = passwords(names, creds_dir)
    return write(render(read_base(), pw), path), pw


def read_users(path=USERS):
    """Текст файла, либо None -- его ещё нет. Нужен для отката."""
    try:
        with open(path) as f:
            return f.read()
    except FileNotFoundError:
        return None


def digest_of(check):
    """Дайджест конфига из вывода `nats-server -t`, либо None -- файл битый.
    Чистая функция."""
    m = re.search(r"is valid \((sha256:[0-9a-f]{64})\)", check or "")
    return m.group(1) if m else None


def reload_verdict(check, running):
    """Принял ли nats файл. -> None, либо причина отказа. Чистая функция.

    check -- вывод `nats-server -t` по файлу на диске, running -- дайджест
    работающего конфига из /varz (None -- мониторинг не ответил).

    Сравнение дайджестов, а не время загрузки и не журнал: config_digest в
    /varz -- тот же sha256, что печатает -t (стенд #211, 2.14.6), поэтому
    равенство значит «работает ровно этот файл», а не «что-то перечитано».
    Отвергнутый reload -- и синтаксис, и правка, которую nats перечитывать не
    умеет (listen, auth_callout), -- оставляет старый дайджест."""
    want = digest_of(check)
    if want is None:
        lines = (check or "").strip().splitlines()
        return f"{CONF} is invalid: {lines[-1] if lines else 'nats-server -t said nothing'}"
    if running is None:
        return (f"nats monitoring (/varz on 127.0.0.1:{config.get('MOP_NATS_MONITOR_PORT')}) "
                f"did not answer: cannot tell whether nats took {want} -- "
                f"a nats started before #211 has no monitoring: restart nats")
    if running == want:
        return None
    return (f"nats kept its old config {running}, the file is {want}: the change "
            f"cannot be reloaded (listen, auth_callout...) -- restart nats; "
            f"journalctl -u {UNIT} names the reason")


def check_file(path=None):
    """Вывод `nats-server -t` по файлу: он же говорит, битый ли файл."""
    r = subprocess.run([NATS_BIN, "-t", "-c", path or CONF], capture_output=True, text=True)
    return (r.stdout + r.stderr).strip()


def running_digest():
    """config_digest работающего nats из /varz, либо None."""
    url = f"http://127.0.0.1:{config.get('MOP_NATS_MONITOR_PORT')}/varz"
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            return json.load(r).get("config_digest")
    except (OSError, ValueError):
        return None


def _main_pid():
    pid = subprocess.run(["systemctl", "show", "-p", "MainPID", "--value", UNIT],
                         capture_output=True, text=True).stdout.strip()
    if not pid.isdigit() or pid == "0":
        raise RuntimeError(f"{UNIT} is not running")
    return int(pid)


def reload():
    """SIGHUP nats-server и проверка, что он принял файл (#211). Отказ --
    RuntimeError с причиной.

    Битый файл -- отказ до сигнала: nats его всё равно отверг бы, молча.
    Сигнал свой uid шлёт без посредников: nats под пользователем пула (#115)."""
    check = check_file()
    if digest_of(check) is None:
        raise RuntimeError(reload_verdict(check, None))
    os.kill(_main_pid(), signal.SIGHUP)
    want, running = digest_of(check), None
    deadline = time.monotonic() + RELOAD_WAIT
    while time.monotonic() < deadline:
        running = running_digest()
        if running == want:
            return
        time.sleep(0.1)
    raise RuntimeError(reload_verdict(check, running))
