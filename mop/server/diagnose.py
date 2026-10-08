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

Пути и числа -- от ИМЕННО выбранного каталога реестра (#536): глобальный
SNAPSHOT не просачивается, когда каталог подменён проверкой или званием
установки. Число проектов -- ВИДИМЫЕ имена (snapshot_names), не счёт
origin-записей: голые легаси-имена в счёте терять нельзя.
"""
import os

from ..common import config, projects

SNAPSHOT_NAME = "projects.json"
LEGACY_NAMES = ("projects", "limits.json")


def registry_source(directory=None):
    """Источник ЛОКАЛЬНОГО реестра: "snapshot" | "legacy pair" | "empty".

    Отказ ставится рядом с фактом: view_of не различает «файла нет» и
    «читать не могу» по возвращаемому значению, поэтому источник
    определяется по наличию файлов -- тем же _present, что решает
    это для читателя (#534), -- а сам снимок остаётся за view_of: он же
    проверяет схему, и его отказ -- единственная правда о содержимом."""
    directory = directory if directory is not None else projects.SNAPSHOT_DIR
    if projects._present(os.path.join(directory, SNAPSHOT_NAME)):
        return "snapshot"
    if any(os.path.exists(os.path.join(directory, n)) for n in LEGACY_NAMES):
        return "legacy pair"
    return "empty"


def report(settings=None, registry_dir=None):
    """Сводка локальной установки. -> {settings, registry}.

    settings: {имя: источник} по config.effective (значения -- НЕ здесь:
    их печатает `mop server config`, и его redacted -- его забота).
    registry: {source, file, files, projects} -- источник, путь v1-файла
    (ожидаемый для empty), фактические файлы формата и число ВИДИМЫХ
    проектов. Битый реестр -- исключение, не «пусто»: молчание выдало бы
    поломку за чистую установку."""
    eff = settings if settings is not None else config.effective()
    directory = registry_dir if registry_dir is not None else projects.SNAPSHOT_DIR

    # Источник и снимок -- ОДНА генерация каталога (#536): файлы пробуются
    # ДО и ПОСЛЕ единственного view_of, расхождение -- отказ, а не источник
    # из старого каталога со числом из нового. Источник -- наблюдение НАД
    # теми же файлами, которые решает сам view_of (presence-проба его
    # имён), не второй разбор содержимого.
    def present():
        return tuple(projects._present(os.path.join(directory, n))
                     for n in (SNAPSHOT_NAME,) + LEGACY_NAMES)

    before = present()
    view = projects.view_of(directory)
    after = present()
    if before != after:
        raise RuntimeError(f"the project registry in {directory} changed while "
                           f"diagnosing: run mop server diagnose again")
    if after[0]:
        source = "snapshot"
    elif after[1] or after[2]:
        source = "legacy pair"
    else:
        source = "empty"
    files = {"snapshot": [os.path.join(directory, SNAPSHOT_NAME)],
             "legacy pair": [os.path.join(directory, n) for n in LEGACY_NAMES],
             "empty": [os.path.join(directory, SNAPSHOT_NAME)]}[source]
    return {"settings": {name: src for name, (_, src) in eff.items()},
            "registry": {"source": source,
                         "file": os.path.join(directory, SNAPSHOT_NAME),
                         "files": files,
                         "projects": len(projects.snapshot_names(view))}}
