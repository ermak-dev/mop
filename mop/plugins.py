"""Обход каталога плагинов: один файл в пакете — один плагин.

Так устроены два реестра, драйверы узла (mop/driver/) и LLM-профили
(mop/llm/), и так же командлеты в bin/: обнаружение списком каталога,
таблицы регистрации нет. Контракт у каждого реестра свой и проверяется его
функцией; здесь только сам обход. Только stdlib: реестр драйверов живёт на
узле рядом с агентом.
"""
import importlib
from pathlib import Path


def discover(package_file, package, contract):
    """{имя плагина: contract(имя, модуль)} по файлам рядом с package_file.

    Файлы с подчёркиванием — не плагины (сам __init__, приватные помощники).
    Контракт зовётся при загрузке, чтобы кривой плагин упал громко и с именем
    файла, а не молча пропал из перечня."""
    out = {}
    for path in sorted(Path(package_file).parent.glob("*.py")):
        if path.stem.startswith("_"):
            continue
        mod = importlib.import_module(f".{path.stem}", package)
        out[path.stem] = contract(path.stem, mod)
    return out
