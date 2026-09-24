"""Общее у глаголов `mop user` (#218): где файл, можно ли здесь, пароль."""
import os
import sys

from mop import config, identity
from mop.cli import lib


def settings():
    return {n: config.get(n) for n in identity.SETTINGS}


def refusal(got):
    """Отказ по месту и провайдеру, либо None.

    Сервер -- машина, где лежат secrets/ установки (их заводит deploy) и
    инвентарь: так узнаётся контроллер, у которого и живёт файл операторов.
    Проверка по одному каталогу пропустила бы машину оператора после mop
    join -- у неё ~/.config/mop есть, но secrets/ и инвентаря нет."""
    if (got.get("MOP_AUTH_PROVIDER") or "file") == "ldap":
        return ("people live in LDAP (MOP_AUTH_PROVIDER=ldap): "
                "add, change and remove them there")
    inventory = os.environ.get("INVENTORY", "")
    if not os.path.isdir(identity.SECRETS) or not os.path.isfile(inventory):
        return (f"mop user works on the server, where the operators file lives "
                f"(no {identity.SECRETS} or {inventory or 'inventory.yaml'} here)")
    return None


def parse(argv, doc, values=(), flags=()):
    """argv -> (позиционные, {опция: значение}). Незнакомое -- usage."""
    args, opts = [], {}
    it = iter(argv)
    for a in it:
        if a in values:
            opts[a] = next(it, None)
            if opts[a] is None:
                lib.usage(doc)
        elif a in flags:
            opts[a] = True
        elif a.startswith("-"):
            lib.usage(doc)
        else:
            args.append(a)
    return args, opts


def password(from_stdin, ask):
    """Пароль: строка stdin, либо дважды без эха. -> пароль | ValueError."""
    if from_stdin:
        return sys.stdin.readline().rstrip("\r\n")
    first = ask("new password: ")
    if first != ask("again: "):
        raise ValueError("the passwords differ")
    return first


def run(change, path):
    """Правка файла и копия для сервисов. -> код выхода. Молча, если всё
    вышло; строка -- только когда копию обновит лишь mop deploy."""
    try:
        change()
    except ValueError as e:
        lib.fail(str(e))
        return 1
    if not identity.refresh_copy(path):
        print("the server's services read a copy of the operators file: run mop deploy")
    return 0
