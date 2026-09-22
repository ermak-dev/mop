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


def files_for(shards):
    """Что `mop join` везёт с сервера: своё и только своё."""
    return ([pass_file(None)] + [pass_file(s) for s in shards] + [TOKEN_FILE])
