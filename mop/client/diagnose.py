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

Схема и формат -- ФАКТЫ прочитанных файлов, не константа (#536): schema --
версия ВАЛИДИРОВАННОГО connection.json (у решателя она одна: CONNECTION_
VERSION), у легаси-пары, машинного и пустого каталога файла с версией нет
и схема -- None; format различает, ЧТО лежит: "connection", "legacy pair"
или None. Значения полей -- белый список: новый ключ согласуется, а не
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


def _format(directory):
    """Формат ЛЕЖАЩЕГО профиля: "connection" | "legacy pair" | None.

    По факту файлов, не по классификации: машинный и пустой каталоги --
    None, валидный вход из прежней пары -- "legacy pair". Второго парсера
    нет: presence-проба тех же имён, что знает решатель."""
    if os.path.isfile(os.path.join(directory, creds.CONNECTION_FILE)):
        return "connection"
    if os.path.isfile(os.path.join(directory, creds.OPERATOR_FILE)) or \
            os.path.isfile(os.path.join(directory, creds.CLIENT_FILE)):
        return "legacy pair"
    return None


def report():
    """Сводка выбранного сервера. -> {server, source, directory, status,
    schema, format}.

    status: complete | partial | empty | machine. broken/conflict/unreadable
    каталог -- исключение (ProfileError от снимка), не строка: отказ
    передаётся вызывающему как отказ, а не печатается как один из вариантов
    «всё в порядке». schema -- только у валидированного connection.json
    (версия решателя), у остальных -- None: легаси-пара не выдаёт себя за
    схему v1."""
    host, source = _select()
    directory = creds.server_dir(host)
    fmt = _format(directory)
    try:
        kind, _ = creds.snapshot(directory)
    except creds.ProfileError as e:
        raise RuntimeError(str(e)) from e
    status = {"human": "complete", "machine": "machine",
              "partial": "partial", None: "empty"}[kind]
    schema = creds.CONNECTION_VERSION if fmt == "connection" else None
    return {"server": host, "source": source, "directory": directory,
            "status": status, "schema": schema, "format": fmt}
