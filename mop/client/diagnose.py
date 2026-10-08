"""Офлайн-сводка ВЫБРАННОГО клиентского сервера (#536): библиотека.

Ни сети, ни вопросов, ни записей: что уже лежит у человека, то и названо.
Статусы -- классификация creds.snapshot (#533): human -> complete, machine
-> machine, остаток пары -> partial, пусто -> empty. Отказ решателя
(broken/conflict) -- тоже отказ сводки: замалчивать порчу значило бы
«всё в порядке» про каталог, в который потом не пустят. Секреты (пароли,
proxy-ключи) в сводку не попадают: слой -- статус и метаданные, не содержимое.

Выбор адреса повторяет выбор login/ensure: явный (контекст), единственный
joined-каталог, либо отказ про неоднозначность -- ВЫБРАННЫЙ сервер, не
обход чужих профилей. Под клиентской политикой источников (client_sources
накладывает диспетчер, #536): без .env установки, как и у login.
"""
from ..common import context, creds

# Только эти ключи уезжают наружу: новый ключ согласуется, а не приезжает
# с данными (#533). user -- не секрет, но имя человека не нужно для
# «годен ли вход»: адрес и статус объясняют всё.
FIELDS = ("server", "source", "directory", "status", "schema")


def _select():
    """Выбранный сервер: контекст, единственный joined, отказ про двусмысленность."""
    ctx = context.current()
    if ctx.server:
        source = {"cli": "context", "env": "context", "clone": "clone"}.get(
            ctx.sources.get("server"), "context")
        return ctx.server, source
    joined = creds.joined_dirs()
    if len(joined) > 1:
        raise RuntimeError(f"multiple joined servers ({', '.join(joined)}): "
                           f"name one with --server")
    if len(joined) == 1:
        return joined[0], "joined"
    raise RuntimeError("no server selected: name one with --server")


def report():
    """Сводка выбранного сервера. -> {server, source, directory, status, schema}.

    status: complete | partial | empty | machine. broken/conflict/unreadable
    каталог -- исключение (ProfileError от снимка), не строка: отказ
    передаётся вызывающему как отказ, а не печатается как один из вариантов
    «всё в порядке»."""
    host, source = _select()
    directory = creds.server_dir(host)
    try:
        kind, _ = creds.snapshot(directory)
    except creds.ProfileError as e:
        raise RuntimeError(str(e)) from e
    status = {"human": "complete", "machine": "machine",
              "partial": "partial", None: "empty"}[kind]
    return {"server": host, "source": source, "directory": directory,
            "status": status, "schema": creds.CONNECTION_VERSION}
