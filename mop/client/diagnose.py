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

Схема и формат -- метаданные ТОЙ ЖЕ генерации каталога, что и
классификация (#536): файлы пробуются ДО и ПОСЛЕ единственного снимка,
расхождение -- отказ «changed while diagnosing», а не смесь поколений
(status из нового каталога, format из старого). Схема -- версия
ВАЛИДИРОВАННОГО connection.json (у решателя она одна: CONNECTION_
VERSION), у остальных форматов файла с версией нет и схема -- None.
Формат называет ПОЛОВИНЫ по отдельности: машинный operator.json --
"service operator" (машинность называет сам снимок -- второго чтения
содержимого нет), человеческая половина -- "legacy operator"/"legacy
client", целая прежняя пара -- "legacy pair", connection.json --
"connection". Второго парсера нет: presence-проба тех же имён, что знает
решатель. Значения полей -- белый список: новый ключ согласуется, а не
приезжает с данными (#533).
"""
import os

from ..common import context, creds

# Только эти ключи уезжают наружу; user -- не секрет, но имя человека не
# нужно для «годен ли вход»: адрес и статус объясняют всё.
FIELDS = ("server", "source", "directory", "status", "schema", "format")


def _select():
    """Выбранный сервер: контекст, единственный joined, отказ про двусмысленность.

    Источник выбора НЕ сворачивается (#536): cli/env/clone/joined -- как
    их называет resolve, так они и печатаются; цель диагностики -- увидеть,
    откуда именно приехал адрес. Неизвестный слой -- «unknown», без
    угадывания: свёртка трёх случаев в одно слово прятала бы разницу."""
    ctx = context.current()
    if ctx.server:
        source = ctx.sources.get("server") or "unknown"
        return ctx.server, source
    joined = creds.joined_dirs()
    if len(joined) > 1:
        raise RuntimeError(f"multiple joined servers ({', '.join(joined)}): "
                           f"name one with --server")
    if len(joined) == 1:
        return joined[0], "joined"
    raise RuntimeError("no server selected: name one with --server")


def _present(directory):
    """{имя файла: есть ли} одним наблюдением каталога."""
    return {name: os.path.isfile(os.path.join(directory, name))
            for name in (creds.CONNECTION_FILE, creds.OPERATOR_FILE,
                         creds.CLIENT_FILE)}


def _format(kind, files):
    """Формат ЛЕЖАЩЕГО профиля по пробам и классификации СНИМКА.

    Машинность называет сам снимок (kind == machine) -- второго чтения
    содержимого нет; человеческие половины различаются presence-пробой:
    целая прежняя пара -- "legacy pair", operator.json без client.json --
    "legacy operator", client.json без operator.json -- "legacy client",
    connection.json -- "connection"."""
    conn, op, client = (files[creds.CONNECTION_FILE], files[creds.OPERATOR_FILE],
                        files[creds.CLIENT_FILE])
    if conn:
        return "connection"
    if op and client:
        return "legacy pair" if kind != "machine" else "service operator + client"
    if op:
        return "service operator" if kind == "machine" else "legacy operator"
    if client:
        return "legacy client"
    return None


def report():
    """Сводка выбранного сервера. -> {server, source, directory, status,
    schema, format}.

    status: complete | partial | empty | machine. broken/conflict/unreadable
    каталог -- исключение (ProfileError от снимка), не строка: отказ
    передаётся вызывающему как отказ, а не печатается как один из вариантов
    «всё в порядке». schema -- только у валидированного connection.json
    (версия решателя), у остальных -- None: легаси-пара не выдаёт себя за
    схему v1. Формат и статус -- ОДНА генерация каталога: пробы до/после
    снимка, расхождение -- отказ, не смесь поколений."""
    host, source = _select()
    directory = creds.server_dir(host)
    before = _present(directory)
    try:
        kind, _ = creds.snapshot(directory)
    except creds.ProfileError as e:
        raise RuntimeError(str(e)) from e
    after = _present(directory)
    if before != after:
        raise RuntimeError(f"the server directory {directory} changed while "
                           f"diagnosing: run mop diagnose again")
    fmt = _format(kind, after)
    status = {"human": "complete", "machine": "machine",
              "partial": "partial", None: "empty"}[kind]
    schema = creds.CONNECTION_VERSION if fmt == "connection" else None
    return {"server": host, "source": source, "directory": directory,
            "status": status, "schema": schema, "format": fmt}
