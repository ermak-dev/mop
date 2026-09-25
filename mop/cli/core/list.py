"""pool puppets: who's where, busy or free, what LLM it runs on

"free" means the clone has no unsaved work, not that the session is silent:
the dispatch decision rests on this.
"""
from mop.cli import lib
from mop.common import puppets

# Ширины фиксированные, и это плата за потоковый вывод: выровнять по самой
# длинной строке можно только собрав их все, то есть промолчав до конца обмера.
# Значение шире своей колонки не режется, а сдвигает хвост строки — как в df.
# Последняя колонка (origin) не добивается вовсе.
WIDTHS = (15, 7, 8, 46, 10, 7, 7)


def place(row):
    """Колонка «место»: клон + target папета, обмер спросом (du без кэша).
    Прочерк — обмер не доехал: не ноль, ноль был бы «измерено и пусто»."""
    kb = row.disk_kb
    if kb is None:
        return "-"
    return f"{kb / 2**20:.0f} GB" if kb >= 2**20 else f"{kb / 2**10:.0f} MB"


def line(r):
    s = r.render()
    cells = (s["name"], s["node"], s["alloc_status"], s["state"], s["owner"], place(r),
             s["llm"])
    return "  ".join(c.ljust(w) for c, w in zip(cells, WIDTHS)) + "  " + s["origin"]


def main(argv):
    if argv:
        lib.usage(__doc__)
    # Печатаем по мере готовности: состояния всего пула приходят за десятые
    # доли секунды, а обмер места — секунды, и собирать таблицу целиком значит
    # молчать всё это время. Порядок здесь — по готовности, а не по имени.
    rows = []
    for r in puppets.puppet_rows_stream():
        print(line(r), flush=True)
        rows.append(r)
    if not rows:
        print("no puppets")
    print("pool:")
    print("\n".join(lib.pool_lines()))



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
