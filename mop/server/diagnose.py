"""Офлайн-сводка ЛОКАЛЬНОЙ установки сервера (#536): библиотека.

Только эта машина: откуда взято каждое действующее значение настроек
(config.effective, тот же решатель, что у `mop server config`) и чем жив
местный реестр проектов -- снимок v1, легаси-пара или пусто.
Это НЕ реестр удалённого выбранного сервера: клиент такой вопрос решает
на своей стороне, а здесь -- только то, что стоит на этом хосте.

Ни сети, ни миграции, ни записей: view_of читает, диагностика не чинит.
Отказ реестра (битый снимок) -- громкий отказ с именем файла, секреты
(значения version, чужие поля) в текст не цитируются -- уже так у
_checked (#534), второй парсер здесь не нужен. Значения настроек в сводке
НЕ печатаются: источник каждого -- да (значения умеет печатать
`mop server config` под своим замком redacted).
"""
import os

from ..common import config, projects


def registry_source(directory=None):
    """Источник ЛОКАЛЬНОГО реестра: "snapshot" | "legacy pair" | "empty".

    Отказ ставится рядом с фактом: view_of не различает «файла нет» и
    «читать не могу» по возвращаемому значению, поэтому источник
    определяется по наличию файлов -- тем же _present, что решает
    это для читателя (#534), -- а сам снимок остаётся за view_of: он же
    проверяет схему, и его отказ -- единственная правда о содержимом."""
    directory = directory if directory is not None else projects.SNAPSHOT_DIR
    if projects._present(os.path.join(directory, "projects.json")):
        return "snapshot"
    old = os.path.join(directory, "projects")
    if os.path.exists(old) or os.path.exists(os.path.join(directory, "limits.json")):
        return "legacy pair"
    return "empty"


def report(settings=None, registry_dir=None):
    """Сводка локальной установки. -> {settings, registry}.

    settings: {имя: источник} по config.effective (значения -- НЕ здесь:
    их печатает `mop server config`, и его redacted -- его забота).
    registry: {source, file, projects} -- источник, путь и число проектов
    снимка view_of. Битый реестр -- исключение, не «пусто»: молчание
    выдало бы поломку за чистую установку."""
    eff = settings if settings is not None else config.effective()
    directory = registry_dir if registry_dir is not None else projects.SNAPSHOT_DIR
    view = projects.view_of(directory)
    return {"settings": {name: src for name, (_, src) in eff.items()},
            "registry": {"source": registry_source(directory),
                         "file": projects.SNAPSHOT,
                         "projects": len(view.get("projects") or [])}}
