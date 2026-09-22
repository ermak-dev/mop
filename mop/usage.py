"""Расход токенов по транскриптам claude. Только stdlib: исполняется в агенте
узла, где пакета `mop` целиком может и не быть в зависимостях.

Откуда берутся цифры. Claude Code пишет каждую сессию в
~/.claude/projects/<slug каталога>/<session>.jsonl, и у каждой записи
type=assistant лежит message.usage с четырьмя счётчиками: input_tokens,
output_tokens, cache_creation_input_tokens, cache_read_input_tokens.
Субагенты пишут рядом, в <session>/subagents/*.jsonl, и это те же деньги.
Сводный ~/.claude/stats-cache.json существует, но обновляется только когда
человек открывает /stats в TUI, — у папета этого не случается никогда, и на
mate он отстал на полгода. Поэтому считаем по транскриптам.

Один ответ API — несколько строк. Ответ с N блоками (thinking, текст, вызовы
инструментов) пишется N строками с одним и тем же message.id и одинаковым
usage. Сложить строки как есть значит завысить расход в два-четыре раза, и
на глаз этого не видно: цифра просто большая. Отсюда дедупликация по id.
"""
import json
import os
import re
import sys
from datetime import datetime, timedelta

KINDS = ("input", "output", "cache_write", "cache_read")
_FIELDS = {"input": "input_tokens", "output": "output_tokens",
           "cache_write": "cache_creation_input_tokens",
           "cache_read": "cache_read_input_tokens"}

PROJECTS = os.path.expanduser("~/.claude/projects")


def slug(path):
    """Имя каталога транскриптов для рабочего каталога: claude заменяет всё,
    кроме букв и цифр, на дефис. /home/u/puppets/pu-x-1 -> -home-u-puppets-pu-x-1."""
    return re.sub(r"[^A-Za-z0-9]", "-", path)


def empty():
    return {k: 0 for k in KINDS}


def _local_date(ts):
    """Дата записи по местному времени узла: так же считает /stats, и оператор
    смотрит на график по своим суткам, а не по UTC."""
    return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone().date()


def transcripts(project_dir):
    """Все .jsonl проекта, включая субагентов, — они лежат этажом ниже."""
    for root, _dirs, files in os.walk(project_dir):
        for f in files:
            if f.endswith(".jsonl"):
                yield os.path.join(root, f)


def read_usage(path, since, seen):
    """Расход одного файла по дням: {date: {kind: n}}. `seen` — общий набор
    id ответов, живёт дольше файла: при --resume claude дописывает тот же
    файл, а при форке сессии копирует историю в новый, и один ответ
    встречается в двух файлах."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Дешёвый фильтр до json.loads: строк с инструментами и текстом
            # в разы больше, чем ответов, и разбирать их незачем.
            if '"usage"' not in line or '"assistant"' not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            if d.get("type") != "assistant":
                continue
            m = d.get("message") or {}
            u = m.get("usage")
            if not isinstance(u, dict):
                continue
            key = m.get("id") or d.get("requestId") or d.get("uuid")
            if key in seen:
                continue
            seen.add(key)
            try:
                day = _local_date(d["timestamp"])
            except (KeyError, ValueError, TypeError):
                continue
            if day < since:
                continue
            row = out.setdefault(day.isoformat(), empty())
            for kind, field in _FIELDS.items():
                row[kind] += int(u.get(field) or 0)
    return out


def scan(project_dir, days, now=None):
    """Расход проекта за последние `days` суток включая сегодня:
    {date: {kind: n}}. Файлы, не менявшиеся с начала окна, не открываются:
    дописать запись в прошлое claude не может."""
    now = now or datetime.now()
    since = (now - timedelta(days=days - 1)).date()
    floor = datetime.combine(since, datetime.min.time()).timestamp()
    seen = set()
    acc = {}
    for path in transcripts(project_dir):
        try:
            if os.path.getmtime(path) < floor:
                continue
        except OSError:
            continue
        merge(acc, read_usage(path, since, seen))
    return acc


def merge(into, rows):
    """Сложить {date: {kind: n}} в накопитель того же вида."""
    for day, row in rows.items():
        acc = into.setdefault(day, empty())
        for k in KINDS:
            acc[k] += int(row.get(k) or 0)
    return into


def total(row):
    return sum(row.get(k) or 0 for k in KINDS)


def sum_days(rows):
    """{date: {kind: n}} -> один {kind: n} за всё окно."""
    acc = empty()
    for row in rows.values():
        for k in KINDS:
            acc[k] += int(row.get(k) or 0)
    return acc


def days_back(days, now=None):
    """Список дат окна по порядку, от старой к сегодняшней, — оси графика
    нужны и пустые дни: провал в расходе виден только на месте."""
    now = now or datetime.now()
    return [(now - timedelta(days=i)).date().isoformat() for i in range(days - 1, -1, -1)]


def main(argv):
    """CLI: usage.py <каталог транскриптов> <дней> -> JSON {дата: {вид: n}}.

    Форма нужна ровно по той же причине, что и у session.py: транскрипты
    контейнерного папета лежат внутри тела, и импортировать этот модуль с
    гипервизора не над чем — там их просто нет. Молчаливый отказ был бы
    худшим: расход показался бы нулевым, а не неизвестным."""
    if len(argv) < 2:
        print("usage: usage.py <projects-dir> <days>", file=sys.stderr)
        return 2
    d, days = argv[0], int(argv[1])
    print(json.dumps(scan(d, days) if os.path.isdir(d) else {}))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
