"""Печать таблиц. Единственное место, где считаются ширины колонок."""


def table(rows):
    """rows: список кортежей строк -> выровненные строки. Последняя колонка не
    добивается пробелами, иначе хвост таблицы тянет за собой мусорный отступ."""
    rows = [tuple(str(c) for c in r) for r in rows]
    if not rows:
        return []
    width = [max(len(r[i]) for r in rows) for i in range(len(rows[0]) - 1)]
    return ["  ".join(list(c.ljust(w) for c, w in zip(r, width)) + [r[-1]]).rstrip()
            for r in rows]


def ratio(free, total):
    """Свободно из всего -> «N/M» (#243): одна запись на фронтенды CLI.
    Неизвестная часть -- «-» (сервис кластера старше #243 всего не знает),
    обе неизвестны -- «-». Панель питон не импортирует: её ratio
    (web/src/components/format.ts) держится той же записи, tests/render.py
    и format.test.ts это сверяют."""
    if free is None and total is None:
        return "-"
    return f"{'-' if free is None else free}/{'-' if total is None else total}"
