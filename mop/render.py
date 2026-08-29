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
