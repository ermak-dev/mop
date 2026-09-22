"""Креды сервера на машине оператора: ~/.config/mop/servers/<адрес>/.

Раньше их рендерил плейбук: bus.json и bus-master-<шард>.json с запечённым
адресом шины лежали на одной управляющей машине, и мастер вне неё не
поднимался, а второй сервер был невозможен в принципе — файлы одни. Мастер и
контроллер ansible были одной машиной, отсюда и схема.

Теперь мастер — любая машина с mop и адресом сервера: `MOP_SERVER_LAN` из
окружения (оно старше .env) переключает NOMAD_ADDR, адрес шины и каталог
кредов разом. В каталоге лежит ровно то, что мастеру положено, под теми же
именами, что lookup('password') заводит в secrets/ сервера — копия без
переименования, её и делает `mop join`:

    nats-admin.pass             оператор: все шарды плюс узловой disk
    nats-master-<шард>.pass     мастер шарда
    bootstrap.json              management-токен Nomad

Паролей узлов и папетов здесь нет намеренно: с ними мастер мог бы
представиться узлом, а границу шардов держат ровно креды.

Конфиг шины собирается из адреса, порта, шарда и пароля — чистая функция,
проверяется в tests/creds.py.
"""
import json
import os

from . import config

ROOT = os.path.expanduser("~/.config/mop/servers")
ADMIN = "admin"          # псевдошард оператора: шарда нет
TOKEN_FILE = "bootstrap.json"


def server_dir(host=None):
    """Каталог кредов сервера. Ключ — адрес, и только он: у двух серверов два
    каталога, и переменная окружения переключает их вместе с NOMAD_ADDR."""
    return os.path.join(ROOT, host or config.get("MOP_SERVER_LAN"))


def user_of(shard):
    """Пользователь NATS по шарду: нет шарда — оператор."""
    return ADMIN if not shard or shard == ADMIN else f"master-{shard}"


def pass_file(shard):
    """Имя файла пароля — такое же, как в secrets/ сервера."""
    return f"nats-{user_of(shard)}.pass"


def bus_config(host, port, shard, password):
    """{url, user, password} — то, что раньше рендерил bus.json.j2."""
    return {"url": f"nats://{host}:{port}", "user": user_of(shard),
            "password": password}


def password(directory, shard):
    """Пароль из каталога сервера, либо None: у вызывающего есть запасной
    путь (старые файлы плейбука), и пустой каталог — не отказ."""
    try:
        with open(os.path.join(directory, pass_file(shard))) as f:
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


def collect(secrets_dir, token_file, dest):
    """Собрать каталог сервера на самом контроллере: он тоже машина
    оператора, и после `mop deploy` на нём всё должно работать без join.
    -> имена положенных файлов."""
    import shutil
    make_dir(dest)
    names = pick(os.listdir(secrets_dir))
    for n in names:
        shutil.copyfile(os.path.join(secrets_dir, n), os.path.join(dest, n))
    if os.path.exists(token_file):
        shutil.copyfile(token_file, os.path.join(dest, TOKEN_FILE))
        names.append(TOKEN_FILE)
    for n in names:
        os.chmod(os.path.join(dest, n), 0o600)
    return names
