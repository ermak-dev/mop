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

Чья это работа (#244). Сообщение мастера приходит папету конвертом
`<cross-session-message ... from-name="<логин>.<хост>-<pid>">`, и с #213
логин — первый токен адреса. Ответ приписывается логину последнего конверта,
увиденного до него в том же транскрипте; до первого конверта (руками через
attach, слэш-команда, старт сессии) и у адреса без логина (до #213
«хост-pid», «mop») — «-». Субагент наследует логин хода, который его
запустил: toolUseId из <agent>.meta.json — id вызова инструмента в
транскрипте родителя.
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
NOBODY = "-"

# Конверт в начале доставленного текста. Claude Code ставит перед ним одну
# строку («Another Claude session sent a message:»), поэтому допускаем её, но
# не больше: тот же текст внутри реплики или результата инструмента (папет
# читал исходник) — не доставка.
_ENVELOPE = re.compile(r'^(?:[^\n<]*\n)?<cross-session-message\b([^>]*)>')
_FROM_NAME = re.compile(r'\bfrom-name="([^"]*)"')


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


def login_of(address):
    """Адрес мастера на шине -> логин: первый токен до точки (#213). Адрес без
    логина (до #213 «хост-pid», «mop», пусто) — NOBODY."""
    login, dot, _rest = (address or "").partition(".")
    return login if dot and login else NOBODY


def _envelope_address(d):
    """Адрес отправителя, если запись — доставленное сообщение, иначе None.

    Доставка — это user с текстом-строкой (у живого сообщения есть и origin
    с name) или attachment queued_command (пришло посреди хода). Постановка
    в очередь (queue-operation) — ещё не доставка, а конверт внутри вызова
    или результата инструмента — просто текст."""
    t = d.get("type")
    if t == "user":
        origin = d.get("origin")
        if isinstance(origin, dict) and origin.get("kind") == "peer" and origin.get("name"):
            return origin["name"]
        text = (d.get("message") or {}).get("content")
    elif t == "attachment":
        a = d.get("attachment") or {}
        text = a.get("prompt") if a.get("type") == "queued_command" else None
    else:
        return None
    m = _ENVELOPE.match(text) if isinstance(text, str) else None
    if not m:
        return None
    name = _FROM_NAME.search(m.group(1))
    return name.group(1) if name else ""


def read_usage(path, since, seen, login=NOBODY, spawns=None):
    """Расход одного файла по логину и дню: {login: {date: {kind: n}}}.

    `seen` — общий набор id ответов, живёт дольше файла: при --resume claude
    дописывает тот же файл, а при форке сессии копирует историю в новый, и
    один ответ встречается в двух файлах. Считается первое появление — с тем
    логином, что стоял там.

    login — с чьим логином файл начинается (у субагента — логин
    запустившего хода). spawns, если дан, пополняется {id вызова
    инструмента: логин}: по нему субагенты находят свой ход."""
    out = {}
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            # Дешёвый фильтр до json.loads: строк с инструментами и текстом
            # в разы больше, чем ответов и конвертов, и разбирать их незачем.
            if '"assistant"' not in line and "cross-session-message" not in line:
                continue
            try:
                d = json.loads(line)
            except ValueError:
                continue
            address = _envelope_address(d)
            if address is not None:
                login = login_of(address)
                continue
            if d.get("type") != "assistant":
                continue
            m = d.get("message") or {}
            if spawns is not None and isinstance(m.get("content"), list):
                for b in m["content"]:
                    if isinstance(b, dict) and b.get("type") == "tool_use" and b.get("id"):
                        spawns[b["id"]] = login
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
            row = out.setdefault(login, {}).setdefault(day.isoformat(), empty())
            for kind, field in _FIELDS.items():
                row[kind] += int(u.get(field) or 0)
    return out


def _spawned_by(path):
    """toolUseId субагента из соседнего <agent>.meta.json, либо None."""
    try:
        with open(path[:-len(".jsonl")] + ".meta.json") as fh:
            got = json.load(fh).get("toolUseId")
    except (OSError, ValueError, AttributeError):
        return None
    return got if isinstance(got, str) else None


def scan(project_dir, days, now=None):
    """Расход проекта за последние `days` суток включая сегодня:
    {date: {kind: n}}."""
    return scan_all(project_dir, days, now)[0]


def scan_all(project_dir, days, now=None):
    """Расход проекта за окно двумя срезами: ({date: {kind: n}},
    {login: {date: {kind: n}}}). Первый — сумма второго по логинам.

    Файлы, не менявшиеся с начала окна, не открываются: дописать запись в
    прошлое claude не может. Субагенты читаются после сессий: os.walk идёт
    сверху вниз, и вызов, запустивший субагента, к его разбору уже увиден.
    Субагент, чей вызов не нашёлся (нет meta, родитель вне окна, вложенный
    субагент, прочитанный раньше своего родителя), — NOBODY."""
    now = now or datetime.now()
    since = (now - timedelta(days=days - 1)).date()
    floor = datetime.combine(since, datetime.min.time()).timestamp()
    seen, spawns = set(), {}
    acc, by = {}, {}
    for path in transcripts(project_dir):
        try:
            if os.path.getmtime(path) < floor:
                continue
        except OSError:
            continue
        sub = os.path.basename(os.path.dirname(path)) == "subagents"
        login = spawns.get(_spawned_by(path), NOBODY) if sub else NOBODY
        for who, rows in read_usage(path, since, seen, login, spawns).items():
            merge(acc, rows)
            merge(by.setdefault(who, {}), rows)
    return acc, by


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
    """CLI: usage.py <каталог транскриптов> <дней> -> JSON {дата: {вид: n}};
    с третьим аргументом --by-login -> {"usage": то же, "by_login": {логин:
    {дата: {вид: n}}}} (#244). Флаг, а не новая форма по умолчанию: агент
    узла и usage.py в теле приезжают разными путями, и старый агент читает
    ответ без флага.

    Форма нужна ровно по той же причине, что и у session.py: транскрипты
    контейнерного папета лежат внутри тела, и импортировать этот модуль с
    гипервизора не над чем — там их просто нет. Молчаливый отказ был бы
    худшим: расход показался бы нулевым, а не неизвестным."""
    if len(argv) < 2:
        print("usage: usage.py <projects-dir> <days>", file=sys.stderr)
        return 2
    d, days = argv[0], int(argv[1])
    total, by = scan_all(d, days) if os.path.isdir(d) else ({}, {})
    if argv[2:3] == ["--by-login"]:
        print(json.dumps({"usage": total, "by_login": by}))
    else:
        print(json.dumps(total))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
