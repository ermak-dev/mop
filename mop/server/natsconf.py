"""Пользователи шины: файл `users.conf`, который NATS включает в authorization.

Список пользователей собирает один код (#116). Раньше его рендерил шаблон
ansible на контроллере, и завести проект мог только прогон под root. Теперь
файл пишет сервер: люди, сервисы и узлы приходят базовым JSON от `mop server deploy`
(`base-users.json` -- их пароли знает только контроллер), папеты -- из
реестра сервера, с паролями, которые сервер заводит сам.

Один файл, а не два: включить второй файл внутрь массива `users` NATS не
умеет (`nats-server -t`: ошибка разбора на include), а второй блок
authorization не сливается с первым.

Данные, без печати: печатает `mop server cluster users`.
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

from ..common import busnames, config, fsutil

DIR = "/etc/nats"
# Копия файлов провайдера личностей (#206, #167): файл операторов и
# переходные пароли. Их читают сервисы сервера под пользователем пула, а
# secrets/ контроллера ему закрыт; кладёт копию deploy (роль bus).
IDENTITY_DIR = os.path.join(DIR, "identity")
BASE = os.path.join(DIR, "base-users.json")
USERS = os.path.join(DIR, "users.conf")
# Блок auth callout (#206) -- своим файлом: его смену nats перечитать не
# умеет («config reload not supported for AuthCallout»), и deploy по его
# контрольной сумме решает рестарт, а по users.conf -- reload.
CALLOUT = os.path.join(DIR, "callout.conf")
# Сколько сервер ждёт callout: у nats-py 2.16 рукопожатие короче, и при 5 с
# connect() возвращался до вердикта (стенд #203) -- выше 2 не поднимать.
CALLOUT_TIMEOUT = 2
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


def _perms(allow, deny=None, sub_allow=None, sub_deny=None):
    """Блок прав: allow/deny -- публикация. Подписка не задана -- те же
    права, что на публикацию."""
    def side(a, d):
        return "{ allow: " + _list(a) + (f", deny: {_list(d)}" if d else "") + " }"
    sub = side(allow, deny) if sub_allow is None else side(sub_allow, sub_deny)
    return (f"    permissions: {{\n      publish:   {side(allow, deny)}\n"
            f"      subscribe: {sub}\n    }}")


def _user(name, password, perms, extra=""):
    return (f"  {{\n    user: {_q(name)}, password: {_q(password)}\n"
            f"{extra}{perms}\n  }}")


# Сервисы сервера (#104): машина, а не человек. Права пока как у admin.
SERVICE_PERMISSIONS = [busnames.everything(), busnames.INBOX]
# ...кроме субъектов с логином человека (#207): машина его не называет, а
# сервис с mop.> иначе опубликовал бы запрос от чьего угодно имени.
SERVICE_PUBLISH_DENY = [busnames.node(busnames.ANY, "*", "rpc", login=busnames.ANY),
                        busnames.cluster(busnames.ANY, login=busnames.ANY)]


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


# Пользователь callout (#206): отвечает на запросы авторизации и больше ничего
# -- ответ сервер ждёт на $SYS._INBOX (стенд #206).
CALLOUT_PERMISSIONS = {"publish": ["$SYS._INBOX.>"], "subscribe": ["$SYS.REQ.USER.AUTH"]}


def _machines(base):
    """Машины шины -> [(имя, запись users.conf)]: ровно те, кого callout не
    спрашивает. Один список на статические записи и на auth_users
    (render_callout): два набранных порознь разошлись бы, и машина вне
    auth_users пошла бы в callout за отказом."""
    pw = base.get(busnames.CALLOUT)
    if not pw:
        # Без callout на шину не войдёт ни человек, ни папет (#219).
        raise ValueError(f"{BASE} has no {busnames.CALLOUT} password -- run mop server deploy")
    return [(busnames.CALLOUT, _user(busnames.CALLOUT, pw, _perms(
                CALLOUT_PERMISSIONS["publish"], sub_allow=CALLOUT_PERMISSIONS["subscribe"]))),
            (busnames.SERVICE, _user(busnames.SERVICE, base[busnames.SERVICE],
                                     _perms(SERVICE_PERMISSIONS, SERVICE_PUBLISH_DENY,
                                            sub_allow=SERVICE_PERMISSIONS)))]


def _nodes(base):
    out = []
    for node, pw in sorted((base.get("nodes") or {}).items()):
        perms = node_permissions(node)
        out.append((busnames.node_user(node), _user(busnames.node_user(node), pw,
                    _perms(perms["publish"], sub_allow=perms["subscribe"]))))
    return out


def render(base):
    """Машины шины -> текст `users.conf`.

    base -- {service: пароль, callout: пароль, nodes: {узел: пароль}}.
    Людей и папетов здесь нет (#206, #219): их пускает auth callout --
    людей по провайдеру личностей, папетов по файлам паролей сервера.
    Порядок -- по именам: одинаковый вход даёт одинаковый файл, и прогон
    без изменений не дёргает reload."""
    out = ["users = ["]
    out += [entry for _, entry in _machines(base) + _nodes(base)]
    out.append("]")
    return "\n".join(out) + "\n"


def render_callout(base, issuer, xkey):
    """Текст callout.conf: блок auth_callout внутри authorization.

    auth_users -- те же машины, что статические записи render, из одной
    функции."""
    names = [n for n, _ in _machines(base) + _nodes(base)]
    return (f"# auth callout (#206): people and puppets are asked of mop-callout;\n"
            f"# the machines below log in from users.conf.\n"
            f"timeout: {CALLOUT_TIMEOUT}\n"
            f"auth_callout {{\n"
            f"  issuer: {issuer}\n"
            f"  auth_users: {_list(names)}\n"
            f"  account: {_q('$G')}\n"
            f"  xkey: {xkey}\n"
            f"}}\n")


def needs_restart(old, new):
    """Смена callout.conf -- рестарт nats, а не reload: блок auth_callout
    reload не перечитывает и валит весь reload (стенд #206). old None --
    файла не было."""
    return old != new


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
    """Пароли папетов по реестру и файл пользователей. -> (изменился ли,
    {проект: пароль}). Reload -- дело вызывающего.

    Пароли папетов заводятся здесь, а проверяет их callout на входе: новый
    проект users.conf не трогает, и reload ему не нужен."""
    pw = passwords(names, creds_dir)
    return write(render(read_base()), path), pw


def apply_callout(path=CALLOUT):
    """callout.conf по ключам сервиса. -> изменился ли. Пишет только `mop
    cluster users` (deploy): смена требует рестарта, а рестарт nats -- дело
    прогона, не сервиса."""
    from . import callout as service   # nkeys: подпись и ящики xkey
    keys = service.keys()
    return write(render_callout(read_base(), keys.issuer, keys.xkey), path)


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
                f"a nats started without monitoring needs a restart")
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
