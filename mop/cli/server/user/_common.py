"""Общее у глаголов `mop server user`: где файл, можно ли здесь, пароль."""
import os
import sys

from mop.common import config
from mop.server import identity
from mop.cli import lib


def settings():
    """Настройки провайдера плюс прежний MOP_OPERATORS (#219). Его больше нет
    в identity.SETTINGS и config.SETTINGS, но config.get читает его из .env:
    по нему import знает, кого переносить, а add, passwd и delete -- кто ещё
    не перенесён."""
    out = {n: config.get(n) for n in identity.SETTINGS}
    out["MOP_OPERATORS"] = config.get("MOP_OPERATORS")
    return out


def refusal(got):
    """Отказ по месту и провайдеру, либо None.

    Сервер -- машина с инвентарём: так узнаётся контроллер, у которого и
    живёт файл операторов. Машину оператора после mop join инвентарь и
    отличает: ~/.config/mop у неё есть, инвентаря нет. secrets/ признаком не
    годится (#238): его заводит mop server deploy, а тот без людей отказывает, и на
    свежем сервере mop server user и deploy отказывали друг из-за друга; каталог
    mop server user заводит сам, когда пишет файл (identity._save).

    Файл правится, когда file -- звено цепочки (#232): при file,ldap в нём
    локальные люди поверх каталога."""
    try:
        kinds = identity.links(got.get("MOP_AUTH_PROVIDER"))
    except ValueError as e:
        return str(e)
    if "file" not in kinds:
        return (f"people live in LDAP (MOP_AUTH_PROVIDER={got.get('MOP_AUTH_PROVIDER')}): "
                "add, change and remove them there")
    inventory = os.environ.get("INVENTORY", "")
    if not os.path.isfile(inventory):
        return (f"mop server user works on the server, where the operators file lives "
                f"(no {inventory or 'inventory.yaml'} here)")
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
    вышло; строка -- только когда копию обновит лишь mop server deploy."""
    try:
        change()
    except ValueError as e:
        lib.fail(str(e))
        return 1
    why = identity.refresh_copy(path)
    if why:
        print(why)
    return 0
