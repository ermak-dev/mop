"""Креды сервера на машине оператора: ~/.config/mop/servers/<адрес>/.

Раньше их рендерил плейбук: bus.json и bus-master-<проект>.json с запечённым
адресом шины лежали на одной управляющей машине, и мастер вне неё не
поднимался, а второй сервер был невозможен в принципе — файлы одни. Мастер и
контроллер ansible были одной машиной, отсюда и схема.

Теперь мастер — любая машина с mop и адресом сервера: `MOP_SERVER_LAN` из
окружения (оно старше .env) переключает NOMAD_ADDR, адрес шины и каталог
кредов разом. В каталоге лежит ровно то, что мастеру положено, под теми же
именами, что lookup('password') заводит в secrets/ сервера — копия без
переименования, её и делает `mop join`:

    nats-admin.pass             оператор: все проекты плюс узловой disk
    nats-master-<проект>.pass     мастер проекта
    bootstrap.json              management-токен Nomad

Паролей узлов и папетов здесь нет намеренно: с ними мастер мог бы
представиться узлом, а границу проектов держат ровно креды.

Конфиг шины собирается из адреса, порта, проекта и пароля — чистая функция,
проверяется в tests/creds.py.
"""
import json
import os

from . import config

ROOT = os.path.expanduser("~/.config/mop/servers")
# Кред оператора-человека (#84): один файл на машину, а не по паролю на
# проект. Пользователь тут один — сам человек, — и от проекта он не зависит:
# проект живёт в СУБЪЕКТЕ, права на субъект проверяет сервер NATS.
OPERATOR_FILE = "operator.json"
ADMIN = "admin"          # псевдопроект оператора: проекта нет
TOKEN_FILE = "bootstrap.json"


def server_dir(host=None):
    """Каталог кредов сервера. Ключ — адрес, и только он: у двух серверов два
    каталога, и переменная окружения переключает их вместе с NOMAD_ADDR."""
    return os.path.join(ROOT, host or config.get("MOP_SERVER_LAN"))


def user_of(project):
    """Пользователь NATS по проекту: нет проекта — оператор."""
    return ADMIN if not project or project == ADMIN else f"master-{project}"


def pass_file(project):
    """Имя файла пароля — такое же, как в secrets/ сервера."""
    return f"nats-{user_of(project)}.pass"


def puppet_user(project):
    """Пользователь NATS папета. Отдельно от user_of: тот отвечает про
    мастера, и молча получить master-<проект> там, где нужен puppet-<проект>,
    значит выдать папету права мастера."""
    return f"puppet-{project}"


def puppet_pass_file(project):
    """Имя файла пароля папета — как в secrets/ сервера."""
    return f"nats-{puppet_user(project)}.pass"


def bus_config(host, port, project, password, user=None):
    """{url, user, password} — то, что раньше рендерил bus.json.j2.

    user называют явно там, где это не мастер: сервер выдаёт папету его кред
    в ответе на bootstrap песочницы (#83, docs/BOOTSTRAP.md)."""
    return {"url": f"nats://{host}:{port}", "user": user or user_of(project),
            "password": password}


def operator(directory):
    """{user, password} оператора этой машины, либо None.

    None — не отказ: пока установка не перевела операторов на собственные
    имена, рядом живёт прежний путь (пароль роли `master-<проект>`), и пустой
    файл означает просто «этот путь не выбран»."""
    try:
        with open(os.path.join(directory, OPERATOR_FILE)) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return None
    if not got.get("user") or not got.get("password"):
        return None
    return {"user": got["user"], "password": got["password"]}


def write_operator(directory, user, password):
    """Положить кред оператора. Каталог и файл закрыты: там пароль."""
    make_dir(directory)
    path = os.path.join(directory, OPERATOR_FILE)
    with open(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), "w") as f:
        json.dump({"user": user, "password": password}, f)
    return path


def password(directory, project):
    """Пароль из каталога сервера, либо None: у вызывающего есть запасной
    путь (старые файлы плейбука), и пустой каталог — не отказ."""
    try:
        with open(os.path.join(directory, pass_file(project))) as f:
            return f.read().strip()
    except FileNotFoundError:
        return None


def token(directory):
    """SecretID из bootstrap.json, либо None: на отсутствии токена держится
    профиль `mop mcp` (узел не управляет джобами), и исключение тут —
    неверный сигнал."""
    try:
        with open(os.path.join(directory, TOKEN_FILE)) as f:
            return json.load(f)["SecretID"]
    except (FileNotFoundError, ValueError, KeyError):
        return None


def make_dir(dest):
    """Каталог сервера и его родитель закрыты от всех: там пароли. mode у
    makedirs действует только на последний уровень, поэтому явно."""
    os.makedirs(dest, mode=0o700, exist_ok=True)
    for d in (os.path.dirname(dest), dest):
        os.chmod(d, 0o700)


def pick(listing):
    """Что из secrets/ сервера едет мастеру: своё и только своё. Один отбор
    для контроллера (collect) и для `mop join` — иначе на одной машине
    оператор видел бы больше, чем на другой."""
    return sorted(n for n in listing
                  if n == pass_file(None)
                  or (n.startswith("nats-master-") and n.endswith(".pass")))


def stale(local, remote):
    """Файлы каталога, которых у сервера больше нет. -> [имена].

    Проект сняли (#79) — его пароль мастера остаётся лежать у оператора, и
    `lib.project_ready` по нему отвечает «проект на шине есть»: мастер
    поднимется, чтобы не подключиться. Отбор строго по паролям мастеров:
    админский пароль и токен к проектам отношения не имеют, а чужое в
    каталоге не наше дело.

    Пустой список сервера ничего не чистит: tar мог прийти пустым, а
    снести по этому поводу все креды — худшее из возможных прочтений."""
    if not remote:
        return []
    mine = {n for n in local
            if n.startswith("nats-master-") and n.endswith(".pass")}
    return sorted(mine - set(remote))


def forget(project, *dirs):
    """Снять пароли проекта в названных каталогах. -> [снятые пути].

    И secrets/ контроллера, и каталог сервера: оба читает один и тот же
    project_ready, и оставленный в одном пароль отвечал бы за оба."""
    gone = []
    for d in dirs:
        for n in (pass_file(project), f"nats-puppet-{project}.pass"):
            path = os.path.join(d, n)
            if os.path.exists(path):
                os.remove(path)
                gone.append(path)
    return gone


def collect(secrets_dir, dest):
    """Собрать каталог сервера на самом контроллере: он тоже машина
    оператора, и после `mop deploy` на нём всё должно работать без join.
    Токен сюда кладёт игра сервера (fetch), а тот пишет с правами по
    umask -- поэтому права закрываются у всего, что лежит в каталоге, а не
    только у скопированного. -> имена положенных паролей."""
    import shutil
    make_dir(dest)
    names = pick(os.listdir(secrets_dir))
    for n in names:
        shutil.copyfile(os.path.join(secrets_dir, n), os.path.join(dest, n))
    for n in os.listdir(dest):
        os.chmod(os.path.join(dest, n), 0o600)
    return names
