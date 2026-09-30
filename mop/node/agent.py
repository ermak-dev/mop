"""Агент узла: отвечает на запросы шины про папетов, которые живут здесь.

Заменил собой `alloc exec`. Раньше мастер гонял шелл внутрь аллокации и платил
рукопожатием за каждую пробу; теперь на узле сидит подписчик, а мастер шлёт
ему глагол.

Один на узел, не на папета. Переживает рестарт папета — а спрашивают о папете
чаще всего именно тогда, когда он перезапускается. И живёт вне спеки джоба:
правка спеки не доезжает до работающего папета рестартом аллокации, ей нужна
перерегистрация, а правка агента доезжает одним прогоном плейбука.

Набор глаголов закрыт. Через шину нельзя попросить «выполни шелл»: это был бы
тот же management-токен Nomad, только по другой трубе, — а ради того, чтобы его
с узлов убрать, всё и затевалось. Отсюда же белые списки на `type` и `write`:
произвольная команда в чужой TUI и произвольная запись в $HOME — это два разных
способа получить исполнение кода на узле.

Проект агент пересекает сознательно: он и есть то, что проекты разделяет, и
обслуживает всех жильцов узла. Прав NATS для этого мало — мастер проекта A
законно пишет в свой субъект, но может подставить в поле `name` папета из B.
Поэтому на каждый глагол, называющий папет, сверяем проект из субъекта с
настоящим origin его клона.

Права проверяются дважды: на сервере NATS (кто в какой субъект пишет) и здесь
(какой глагол, каким субъектом и про чьего папета). Право, проверенное в одном
месте, однажды окажется проверенным ни в одном.

Здесь -- глаголы и решение о праве, данные без печати. Программа (подключение,
петля, --check) -- командлет `mop agent`, mop/cli/service/agent.py (#150).
"""
import sys

if __name__ == "__main__":
    # Переход (#150, #172): юнит и проверка deploy зовут уже `mop agent`, но
    # на узлах стоит старый юнит с `python3 -m mop.agent`, пока deploy не
    # перекатит его на обеих установках (mop и rumop). До того этот вход
    # убирать нельзя: агент узла со старым юнитом не поднимется.
    # До импортов пакета (#169): без nats-py импорт шины бросает, и отказ
    # обязан быть строкой командлета, а не трассой из импорта.
    from mop.cli.service import agent as program
    sys.exit(program.main(sys.argv[1:]))

import asyncio  # noqa: E402
import json
import os
import re
import shlex
import socket
import time

from ..common import bus, busnames, config, fsutil, lease, paths, service
from .. import driver, usage
from ..common.domain import CloneFacts, Owner, Verb
from ..driver import Tmux, clone_dir, target_dir, why

HOME = os.path.expanduser("~")

# Драйвер узла, не папета, и берётся он из окружения (юнит агента), а не из
# запроса: иначе мастер проекта A прислал бы своё значение и заставил агента
# исполнить команду не там. Дефолт host — узел, ничего про драйверы не
# знающий, обязан вести себя ровно как раньше.
DRIVER = driver.current()

# Что разрешено отправлять в пейн. Тот же список, что у фронтенда, — но
# проверка здесь настоящая, а там подсказка пользователю.
#
# Escape в списке не ради симметрии: папет, залипший на диалоге, невидим для
# ростера (он показывается занятым или свободным, а сообщения копятся в очереди
# непрочитанными), и единственное лечение — снять диалог, а не ответить на него.
# Ответить значит выбрать из списка, которого не видишь целиком.
SLASH_ALLOWED = ("/model", "/clear", "/compact", "/rc", "/status")
KEYS_ALLOWED = ("Escape",)

# Куда `write` имеет право писать -- paths.WRITABLE под домом ЭТОГО узла
# (#279): клиент называет файл относительно дома пула, дом подставляет
# агент. Токена Nomad в списке нет и не будет: узлы лишились его вместе с
# переездом на шину.

# Ростер тел перечисляет драйвер (у host — по сокетам tmux в /tmp/tmux-<uid>):
# на гипервизоре этот каталог пуст, и знать о нём агенту незачем. Соглашение
# об имени папета живёт там же, в реестре драйверов: это узловой stdlib-модуль,
# а puppets на узел не тянем — он приводит python-nomad, который агенту не
# нужен вовсе.

IDLE_WAIT = 600          # потолок ожидания простоя для notify
IDENTITY_WAIT = 5        # сколько send ждёт профиль владельца у сервера (#167)


def node_name():
    """Имя узла в Nomad. Оно же в субъекте, поэтому берётся из окружения, а не
    угадывается: имя узла и hostname могут расходиться."""
    return os.environ.get("MOP_NODE") or socket.gethostname()


async def puppet_project(name):
    """Чей это папет. Origin клона — авторитет: врапер сносит клон, если origin
    разошёлся с PU_ORIGIN, так что клон и спека не расходятся никогда.

    Пока клона нет (папет грузится) — откат на имя, которое по построению
    согласовано с origin: next_name строит его из того же basename."""
    out, _ = await bsh(name, f"git -C {clone_dir(name)} remote get-url origin 2>/dev/null")
    origin = out.strip().splitlines()[-1] if out.strip() else ""
    if origin:
        return driver.project_of(origin)
    return driver.project_of_name(name)


# ─── локальные пробы ─────────────────────────────────────────────────────
async def bsh(name, script, timeout=20):
    """Шелл внутри тела папета. -> (вывод, код).

    У драйвера host префикс пустой и это тот же шелл на узле; у контейнерного
    драйвера — ssh внутрь. Соблазн оставить пробу снаружи (смонтировать наружу
    tmux-сокет и каталог сессий) ложный: `session.probe` проверяет живость
    через `os.kill(pid, 0)`, а pid тела в namespace хоста означает другой
    процесс или никакой — ломается различение `hung` и `offline`. Плюс tmux
    отказывается соединять клиента с сервером другой версии.

    Скрипт всегда наш, никогда не из запроса, а имя папета попадает в него
    только после driver.valid_name."""
    if not driver.valid_name(name):
        return "", None
    return await driver.sh(script, timeout, prefix=DRIVER.argv(name))


async def tmux_alive(name):
    _, code = await bsh(name, Tmux(name).alive())
    return code == 0


async def pane_lines(name):
    """Весь буфер пейна без хвостовых пустых строк, которыми tmux добивает
    видимую часть."""
    out, code = await bsh(name, Tmux(name).buffer())
    if code not in (0, None):
        raise RuntimeError(f"tmux in {name}: {why(out, code)}")
    lines = out.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def clone_probe(d):
    """Шелл-проба клона в каталоге d -> скрипт; вывод читает clone_facts_of.
    Одна на агента и сторожа host-узла (#347): правило работы в клоне
    решает по этим числам, и второй набор проб разошёлся бы с первым.

    `--not --remotes` здесь не случайность: upstream рабочей ветки бывает
    прибит к origin/master, и тогда `@{u}..` считает влитое неотправленным.
    На этих числах стоит решение мастера о диспатче."""
    return (f'cd {d} 2>/dev/null || exit 0; '
            f'echo "cur=$(git branch --show-current 2>/dev/null)"; '
            f'echo "def=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null)"; '
            f'echo "home=$(git config mop.home 2>/dev/null)"; '
            f'echo "origin=$(git remote get-url origin 2>/dev/null)"; '
            f'echo "dirty=$(git status --porcelain 2>/dev/null | wc -l)"; '
            f'echo "ahead=$(git rev-list --count HEAD --not --remotes 2>/dev/null)"; '
            f'echo "owner=$(head -1 {lease.FILE} 2>/dev/null)"')


def clone_facts_of(out):
    """Вывод clone_probe -> CloneFacts.to_dict() либо None (клона нет или
    числа не прочитались)."""
    kv = fsutil.read_kv(out, raw=True)
    if "dirty" not in kv:
        return None
    try:
        dirty, ahead = int(kv.get("dirty") or 0), int(kv.get("ahead") or 0)
    except ValueError:
        return None
    # Кто ведёт задание (#161): сырая запись, живость считает мастер. По
    # шине -- словарём (#204, #266), обратно его читает CloneFacts.from_dict.
    # Дом клона (#272) пишет стадия клона; нет записи -- None, и дом тогда
    # ветка по умолчанию (CloneFacts.home_branch).
    return CloneFacts(branch=kv.get("cur") or "(detached)",
                      default_branch=(kv.get("def") or "").rsplit("/", 1)[-1] or None,
                      home=kv.get("home") or None,
                      origin=kv.get("origin") or None, dirty=dirty, ahead=ahead,
                      owner=Owner.parse(kv.get("owner"))).to_dict()


async def clone_facts(name):
    """Что клон держит: ветка, несохранённое, неотправленное.

    Пробы переехали с `alloc exec` слово в слово, и переписывать их вместе с
    транспортом нельзя."""
    out, _ = await bsh(name, clone_probe(clone_dir(name)))
    return clone_facts_of(out)


async def du_kb(name):
    """Сколько места занимает папет: клон плюс его target-каталог, в КБ.

    Меряется по спросу, без кэша (пока): du по большому target — обход сотен
    тысяч inode, и цену платит каждый спрашивающий. nice обязателен — обмер
    конкурирует за IO с живыми сборками. Отказ du — None, а не ноль: ноль
    это измеренное «пусто», отказ — «не знаю».

    Каталоги отбирает сам шелл в теле (`-d`), а не os.path.isdir здесь: у
    контейнерного папета этих путей на узле нет вовсе, и проверка снаружи
    вернула бы «ничего не занимает» для полного клона."""
    paths = f"{clone_dir(name)} {target_dir(name)}"
    out, code = await bsh(
        name,
        f'p=""; for x in {paths}; do [ -d "$x" ] && p="$p $x"; done; '
        f'[ -n "$p" ] && nice -n 19 du -sx $p 2>/dev/null',
        timeout=120)
    if code is None:
        return None
    kbs = [int(l.split()[0]) for l in out.splitlines() if l[:1].isdigit()]
    return sum(kbs) if kbs else None


# ─── сессия папета: всегда внутри тела ───────────────────────────────────
# session.py ходит к сокету сессии и проверяет живость через os.kill(pid, 0) —
# то и другое имеет смысл только там, где сессия и живёт. Поэтому агент зовёт
# его не импортом, а отдельным процессом через тот же префикс, что и tmux:
# у host это тот же узел, у контейнерного драйвера — ssh внутрь. Модуль для
# того и сделан standalone, с CLI, печатающим в stdout.
def _session_cmd(verb, *args):
    return " ".join(["python3", shlex.quote(DRIVER.SESSION_PY), verb]
                    + [shlex.quote(str(a)) for a in args])


async def session_probe(name):
    """Достоверное состояние сессии: "<status> <alive> <listen>" либо "none".

    Отказ пробы отдаём как "none", а не как исключение: у мастера это значит
    «файла сессии нет», и он уходит на откат по буферу пейна — ровно то же,
    что было, когда пробник не находил сессии."""
    out, code = await bsh(name, _session_cmd("probe", clone_dir(name)))
    if code not in (0, None) or not out.strip():
        return "none"
    return out.strip().splitlines()[-1].strip()


async def session_state(name):
    """Сессия и исход последнего хода: (строка probe, словарь state либо None).

    Один процесс вместо probe: `session.py state` (#222) отдаёт то же, что
    probe, и запись хода, которую пишут хуки (#224). Строку session собираем из
    того же словаря — мастер со старой библиотекой читает только её.

    Старый session.py на узле не знает глагола и сразу выходит с ошибкой —
    тогда probe ровно как раньше. Таймаут — другое дело: тело не ответило, и
    probe прождал бы столько же впустую; ответ "none", как у таймаута probe."""
    out, code = await bsh(name, _session_cmd("state", clone_dir(name)))
    if code is None:
        return "none", None
    st = _last_json(out) if code == 0 else None
    if not isinstance(st, dict) or "status" not in st:
        return await session_probe(name), None
    if st["status"] is None:
        return "none", st
    return f"{st['status']} {int(bool(st.get('alive')))} {int(bool(st.get('listen')))}", st


def _last_json(out):
    """Последняя JSON-строка вывода либо None.

    Последняя, а не первая: ssh в тело волен подмешать сверху свои
    предупреждения (баннер, добавленный ключ хоста), и первая строка тогда
    не JSON. Так читаются и session.py, и usage.py."""
    for line in reversed(out.strip().splitlines()):
        try:
            return json.loads(line)
        except ValueError:
            continue
    return None


async def session_json(name, script, timeout=20):
    """Ответ session.py, который печатает JSON (send, wait-idle)."""
    out, code = await bsh(name, script, timeout)
    if code is None:
        return {"error": f"{name}: the body did not answer in {timeout}s"}
    got = _last_json(out)
    if got is not None:
        return got
    return {"error": out.strip() or f"session.py exit {code}"}


async def facts(name):
    """Всё, что узел знает о папете, одним ответом.

    Агент отдаёт факты, а не вердикт: собирает состояние мастер. Так логика
    «free/busy/HUNG» остаётся в одном месте и, главное, становится
    чистой функцией — её можно проверить без пула, чего про неё не скажешь
    с тех пор, как она жила поверх exec."""
    if not await tmux_alive(name):
        return {"present": False}
    # Экран в факты не входит (#235, #236): вердикт его не читает, а снимать
    # пейн у каждого папета на каждом опросе ростера -- лишний заход в тело.
    (sess, st), clone = await asyncio.gather(session_state(name), clone_facts(name))
    got = {"present": True, "session": sess, "clone": clone}
    if st is not None:
        got["state"] = st
    return got


# ─── глаголы ─────────────────────────────────────────────────────────────
async def v_ping(_conn, _req):
    return {"node": node_name()}


async def v_state(_conn, req):
    return await facts(req["name"])


async def v_states(_conn, req):
    """Пачкой: у узла обычно несколько папетов, и спрашивают о них всегда
    вместе. Одна поездка вместо N.

    Чужих молча выбрасываем, а не отвечаем отказом: мастер спрашивает по своему
    ростеру, и если в списке оказался чужой — это ошибка спрашивающего, из-за
    которой не должна пропасть картина по своим."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    got = await asyncio.gather(*(facts(n) for n in names))
    return {"puppets": dict(zip(names, got))}


async def v_sizes(_conn, req):
    """Место папетов пачкой: клон + target, du по спросу.

    Отдельный глагол, а не поле в states: du небыстрый, и воткнуть его в
    быстрый ответ о состояниях — значит читать медленный обмер как «агент
    молчит 20с». Не доехал за таймаут — у мастера прочерк, а не ложный
    диагноз."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    kbs = await asyncio.gather(*(du_kb(n) for n in names))
    return {"sizes": dict(zip(names, kbs))}


async def v_local(_conn, req):
    """Папета, живущие на этом узле, — по сокетам tmux-серверов.

    Ростер без Nomad. Нужен узловому `mop mcp`: токена у него больше нет, и
    список джобов взять неоткуда. Мастер этим глаголом не пользуется — у него
    ростер богаче: аллокации, профиль LLM, репозиторий.

    Перечисляет драйвер: у host это сокеты tmux-серверов в /tmp/tmux-<uid>, а
    на гипервизоре этот каталог пуст — тела там отдельные объекты."""
    names = await DRIVER.bodies()
    alive = [n for n in names if await tmux_alive(n) and await _mine(req, n)]
    got = await asyncio.gather(*(facts(n) for n in alive))
    return {"node": node_name(), "puppets": dict(zip(alive, got))}


# Проверка владельца и запись -- под замком на папета: два `send` в одно
# окно иначе оба увидели бы «ничей» (#161). Агент на узле один, поэтому
# замка в процессе достаточно.
_owner_locks = {}


async def _claim(name, req):
    """Аренда задания перед доставкой. -> (отказ|None, откат|None, заметка|None).

    Откат -- прежняя запись: не доехало сообщение -- возвращаем её, иначе
    неудачный `send` держал бы папета окно диспатча впустую."""
    me = lease.caller(req)[0]
    if not me:
        return None, None, None
    clone = CloneFacts.from_dict(await clone_facts(name))
    owner = clone and clone.owner
    act, note = lease.verdict(owner, me, clone, time.time(), bool(req.get("force")))
    if act == "refuse":
        return f"{name}: {note}", None, None
    if act != "take" or clone is None:
        return None, None, note
    path = f"{clone_dir(name)}/{lease.FILE}"
    mine = Owner(me, int(time.time())).render()
    out, code = await bsh(name, f"printf %s {shlex.quote(mine)} > {shlex.quote(path)}")
    # Таймаут -- не записано (#171): иначе «владелец есть», а файла нет.
    if code != 0:
        return f"{name}: owner not recorded: {why(out, code)}", None, None
    return None, (path, owner, mine), note


async def _unclaim(name, undo):
    """Вернуть прежнюю аренду. -> причина неудачи | None.

    Не вернули (таймаут, #171, или ошибка) -- аренда осталась за мастером,
    чей send не доехал, и следующему откажут, назвав не того владельца (#181):
    звавший обязан это сказать.

    Только если в файле всё ещё наша запись (#189): пока доставка шла, аренду
    мог взять другой мастер (force), и откат вслепую перетёр бы его запись
    прежним владельцем или стёр бы файл -- его работа осталась бы ничьей.
    Чужая запись -- не наш откат: оставляем её и ничего не говорим. Зовут под
    _owner_locks, так что между сравнением и записью чужого claim не бывает."""
    path, owner, mine = undo
    body = owner.render() if owner else ""
    write = (f"printf %s {shlex.quote(body)} > {shlex.quote(path)}"
             if body else f"rm -f {shlex.quote(path)}")
    # $(cat) срезает конечный перевод строки -- сравниваем без него.
    out, code = await bsh(name, f'[ "$(cat {shlex.quote(path)} 2>/dev/null)" = '
                                f'{shlex.quote(mine.rstrip(chr(10)))} ] || exit 0; {write}')
    return why(out, code) if code != 0 else None


async def _gate(name, req):
    """Ворота владения (#40) изменяющего глагола. -> (отказ|None, заметка|None).

    Та же lease.gate, что у сервиса кластера (#267), и та же may_touch, что
    за send: чужая живая аренда -- отказ с именем владельца. Оператор --
    субъект admin, а не поле тела. Зовут под _owner_locks: между проверкой и
    действием чужой claim не вклинится."""
    return lease.gate(name, CloneFacts.from_dict(await clone_facts(name)),
                      lease.caller(req)[0], time.time(), bool(req.get("force")),
                      req.get("_project") == busnames.ADMIN)


async def _gated(name, req, act):
    """act() под замком папета, если ворота пустили. Заметка о забранной
    аренде -- полем owner_note, как у send."""
    async with _owner_locks.setdefault(name, asyncio.Lock()):
        refused, note = await _gate(name, req)
        if refused:
            return {"error": refused}
        out = await act()
    return lease.noted(out, note)


# ─── git identity владельца (#167) ───────────────────────────────────────
# Автор коммита -- человек, прошедший проверку на шине, а не установка и не
# то, что мастер назвал разовым `git -c`. Правило одно: после проверенной
# аренды в клоне identity нового владельца или никакой. Профиля нет или
# сервер молчит -- identity снимается, а не остаётся прежнему: коммиты
# нового владельца иначе шли бы под чужим именем. Названный телом владелец
# (прежний субъект, self-declared) identity не трогает вовсе.
async def owner_profile(conn, name, login):
    """Имя и почта логина у сервиса сервера (глагол identity). -> (профиль
    {name, email} | None, причина | None). Спрашиваем проектом папета: права
    узла на server.rpc -- по любому проекту, а журнал сервера так читается."""
    subject = busnames.server(await puppet_project(name))
    try:
        msg = await bus.arequest(conn, subject, bus.envelope("identity", login=login),
                                 IDENTITY_WAIT)
        got = json.loads(msg.data.decode())
    except Exception as e:
        return None, f"the server did not answer ({type(e).__name__})"
    if not isinstance(got, dict) or got.get("error"):
        return None, f"the server refused: {got.get('error') if isinstance(got, dict) else got}"
    if got.get("name") and got.get("email"):
        return {"name": got["name"], "email": got["email"]}, None
    return None, "no name/email in the profile"


# Страж владельца (#313): `git -c user.*` старше .git/config, и identity
# владельца в клоне ничего не стоила -- папеты по привычке подписывались
# чужим именем. Хуки клона сверяют автора и коммитера с локальной почтой.
# Вторая строка -- метка: свой хук узнаётся по ней, чужой не трогается.
OWNER_GUARD = "# mop owner guard (#313)"
GUARDED_HOOKS = ("pre-commit", "pre-merge-commit")   # чистое слияние pre-commit не зовёт


def owner_hook():
    """Текст хука-стража. Чистая. Локальная почта клона (`--local`: -c её не
    видит) есть, а у автора или коммитера (`git var`: видит -c) другая --
    отказ. Локальной нет -- пропуск: владелец без профиля коммитит с -c."""
    return f"""#!/bin/sh
{OWNER_GUARD}
want=$(git config --local user.email 2>/dev/null)
[ -n "$want" ] || exit 0
for who in GIT_AUTHOR_IDENT GIT_COMMITTER_IDENT; do
    got=$(git var "$who" 2>/dev/null | sed -n 's/.*<\\(.*\\)>.*/\\1/p')
    if [ -n "$got" ] && [ "$got" != "$want" ]; then
        echo "commit as the clone's owner: drop -c user.* — the owner's identity is in the clone ($(git config --local user.name) <$want>)" >&2
        exit 1
    fi
done
exit 0
"""


def _hooks_dir(clone):
    """Шелл: каталог хуков клона в $hooks; клона нет -- выход без ошибки."""
    return (f"hooks=$(git -C {shlex.quote(clone)} rev-parse --absolute-git-dir 2>/dev/null)"
            f"/hooks; [ \"$hooks\" != /hooks ] || exit 0; ")


def _ours(f):
    """Шелл: условие «хук f -- наш» (метка второй строкой)."""
    return f"sed -n 2p {f} | grep -qxF {shlex.quote(OWNER_GUARD)}"


def identity_script(clone, path, mine, profile):
    """Скрипт: identity владельца в клон (repo-local) или снять её. Чистая.

    Только пока в файле аренды наша запись (как _unclaim, #189): пока шла
    доставка и вопрос серверу, аренду мог забрать другой, и его identity
    перетирать нельзя. Снятие отсутствующего ключа -- не ошибка.

    Вместе с identity -- страж владельца (#313), хуки GUARDED_HOOKS. Чужой
    хук (без метки) не перезаписывается; core.hooksPath задан -- git
    .git/hooks не читает, и ставить туда незачем. Оба пропуска -- строкой в
    вывод: её несёт заметка мастеру. Снятие identity снимает только свои."""
    q = shlex.quote
    guard = f'[ "$(cat {q(path)} 2>/dev/null)" = {q(mine.rstrip(chr(10)))} ] || exit 0; '
    git = f"git -C {q(clone)} config --local"
    if profile:
        install = "".join(
            f'f="$hooks/{h}"; if [ -e "$f" ] && ! {_ours(chr(34) + "$f" + chr(34))}; then '
            f'echo "owner guard skipped: the project has its own {h}"; else '
            f'printf %s {q(owner_hook())} > "$f.mop" && chmod 755 "$f.mop" && '
            f'mv -f "$f.mop" "$f" || exit 1; fi; ' for h in GUARDED_HOOKS)
        return guard + (f"{git} user.name {q(profile['name'])} && "
                        f"{git} user.email {q(profile['email'])} || exit 1; "
                        + _hooks_dir(clone)
                        + f'if [ -n "$(git -C {q(clone)} config core.hooksPath)" ]; then '
                        f'echo "owner guard skipped: core.hooksPath is set"; exit 0; fi; '
                        f'mkdir -p "$hooks"; ' + install + "exit 0")
    remove = "".join(f'f="$hooks/{h}"; [ -f "$f" ] && {_ours(chr(34) + "$f" + chr(34))} '
                     f'&& rm -f "$f"; ' for h in GUARDED_HOOKS)
    return guard + (f"{git} --unset-all user.name; {git} --unset-all user.email; "
                    + _hooks_dir(clone) + remove + "exit 0")


async def _follow_owner(conn, name, login, undo):
    """Identity клона -- за проверенным владельцем. -> заметка мастеру."""
    profile, missing = await owner_profile(conn, name, login)
    path, _, mine = undo
    async with _owner_locks.setdefault(name, asyncio.Lock()):
        out, code = await bsh(name, identity_script(clone_dir(name), path, mine, profile))
    if code != 0:
        return f"git identity not set: {why(out, code)}"
    if profile:
        # Пропуск стража (#313) -- тем же текстом, что сказал скрипт.
        skipped = "; ".join(l.strip() for l in out.splitlines() if "skipped" in l)
        return f"commits as {profile['name']} <{profile['email']}>" + (
            f"; {skipped}" if skipped else "")
    return f"no git identity for {login} ({missing}): the clone has none, commit with git -c"


async def v_send(conn, req):
    """Сообщение в сессию папета. -> {msg_id} либо {error}.

    `notify=true` не блокирует ответ: подписку на простой держит фоновая
    задача здесь, на узле, рядом с сокетом, и, дождавшись, публикует в инбокс
    мастера. Отсюда push без опроса — и без потока внутри MCP-сервера, который
    раньше ждал простоя, сидя в аллокации."""
    name = req["name"]
    wait = min(max(int(req.get("wait") or 0), 0), IDLE_WAIT)
    async with _owner_locks.setdefault(name, asyncio.Lock()):
        refused, undo, note = await _claim(name, req)
    if refused:
        return {"error": refused}
    # Цель — клон, а не сокет: session.py резолвит сессию сам, внутри тела, где
    # только и лежат её файлы. Снаружи сокет контейнерного папета не виден.
    out = await session_json(name, _session_cmd(
        "send", clone_dir(name), req["message"],
        "--priority", req.get("priority", "next"),
        "--from-name", req.get("from_name", "mop"),
        "--wait", wait), timeout=wait + 20)
    if out.get("error"):
        # Под тем же замком, что и claim (#189): иначе между сравнением и
        # записью отката вклинился бы чужой send.
        failed = None
        if undo:
            async with _owner_locks.setdefault(name, asyncio.Lock()):
                failed = await _unclaim(name, undo)
        if failed:
            out["error"] += f"; owner not restored: {failed}"
        return out
    me, verified = lease.caller(req)
    if undo and verified:
        # После доставки: не доехало -- аренда откатилась, и identity не
        # наша. Коммит папет делает не в первую секунду хода.
        note = "; ".join(filter(None, (note, await _follow_owner(conn, name, me, undo))))
    if note:
        out["owner_note"] = note
    await _event(conn, "send", name, text=f"from {req.get('from_name', 'mop')}")
    if req.get("notify") and not wait:
        # Куда отвечать, говорит сам мастер: инбокс адресуется мастером, а не
        # проектом, иначе два терминала в одном проекте получали бы вести друг
        # друга. Без reply_to ждать бессмысленно — некому сказать.
        if req.get("reply_to"):
            asyncio.create_task(_watch_idle(conn, name, req["reply_to"]))
        else:
            out["notify"] = "no reply_to given — nothing to notify"
    return out


async def _watch_idle(conn, name, reply_to):
    """Дождаться простоя и сказать мастеру, который об этом попросил.

    Ожидание держит session.py в теле (глагол wait-idle): подписка идёт к
    сокету сессии, а он host-local внутри тела. Метка нужна, когда узел ждёт
    сразу нескольких папетов — иначе два инбокса отберут друг у друга путь
    <pid>.sock."""
    r = await session_json(name, _session_cmd(
        "wait-idle", clone_dir(name), IDLE_WAIT, name[-8:]), timeout=IDLE_WAIT + 20)
    if r.get("error"):
        return await _tell_master(
            conn, reply_to, f"mop: gave up waiting for {name} to idle: {r['error']}")
    state = r.get("state")
    await _tell_master(conn, reply_to, f"mop: puppet {name} — {state}" if state
                       else f"mop: {name} did not report idle within {IDLE_WAIT}s")
    await _event(conn, "idle", name, text=state or f"no idle within {IDLE_WAIT}s")


async def _tell_master(conn, reply_to, text):
    try:
        await conn.publish(reply_to, json.dumps(
            {"node": node_name(), "text": text}, ensure_ascii=False).encode())
    except Exception:
        pass


async def _event(conn, kind, name=None, **fields):
    """Запись в журнал проекта (mop.<проект>.events, #66).

    Агент публикует то, что делает сам: доставил сообщение, дождался
    простоя, напечатал команду, снёс тело, поднялся. Смену состояния внутри
    сессии он не видит — её по-прежнему спрашивает мастер, — но по этим
    событиям дашборд сдвигает опрос вперёд и держит журнал. Проект берётся у
    самого папета, а не из субъекта запроса: оператор пишет из admin, а
    событие принадлежит проекту. Отказ — тишина: журнал вторичен, и ронять
    глагол из-за него нельзя."""
    project = await puppet_project(name) if name else bus.ADMIN
    try:
        await conn.publish(bus.events(project), json.dumps(
            {"event": kind, "node": node_name(), "name": name, "project": project,
             "at": time.time(), **fields}, ensure_ascii=False).encode())
    except Exception:
        pass


async def v_tail(_conn, req):
    return {"lines": await pane_lines(req["name"])}


async def v_disk(_conn, _req):
    """Место в хранилище тел. Узловой факт: давление оценивает мастер
    (mop gc), здесь только цифра.

    Меряет драйвер: у host хранилище тел — это $HOME со всеми клонами и
    target-каталогами, а на гипервизоре в $HOME не лежит ни одного папета, и
    `df $HOME` там отвечал бы про совершенно постороннюю файловую систему.
    Молча: число выглядит правдоподобно, а `mop gc` принимает по нему решение
    о сносе."""
    return await DRIVER.capacity()


# Дисковый сторож узла (#358): скрипт кладёт на узел deploy (роль bus), его
# пороги -- узловые настройки из node.env. Зовёт его doctor, а не только
# periodic pu-cleanup: исполнение остаётся здесь, узел знает локальное --
# tmux-сессии, клоны, тела.
SWEEP = "/usr/local/bin/pu-sweep"
SWEEP_KNOBS = ("MOP_SWEEP_FREE_MIN_GB", "MOP_SWEEP_MAX_TARGET", "MOP_SWEEP_STALE_DAYS")
# Последняя строка pu-sweep -- для машины, всё выше -- для человека.
_SWEEP_TOTALS = re.compile(r"pu_sweep_freed_kb=(\d+) pu_sweep_free_gb=(\d+)")
# Одно подметание на узел за раз: два doctor подряд иначе мели бы одни и те
# же пути наперегонки.
_sweep_lock = asyncio.Lock()


def sweep_report(rc, out, err, min_gb):
    """Вывод pu-sweep -> {freed_kb, free_gb, min_gb, warnings} | {error}.
    Чистая функция (tests/agent.py).

    Итоговой строки нет у узла с телами-контейнерами (уходит после яруса 0)
    и у отказа сторожа (ecryptfs без монтирования): тогда цифры None --
    «не знаю», а не ноль. Предупреждения -- строки stderr с «!»: ими скрипт
    говорит об отказах внутри подметания."""
    if rc != 0:
        last = (err.strip() or out.strip()).splitlines()[-1:] or ["no output"]
        return {"error": f"pu-sweep exit {rc}: {last[0].strip()}"}
    m = _SWEEP_TOTALS.search(out)
    return {"freed_kb": int(m[1]) if m else None,
            "free_gb": int(m[2]) if m else None,
            "min_gb": min_gb,
            "warnings": [ln.strip()[1:].strip() for ln in err.splitlines()
                         if ln.strip().startswith("!")]}


async def v_sweep(_conn, req):
    """Подмести диск узла: pu-sweep, ярусы и предохранители -- его. dry --
    только отчёт, ничего не сносится (MOP_SWEEP_DRY).

    Пороги -- из настроек узла, а не из запроса: сколько места держать
    свободным, решает машина, а не просящий."""
    if _sweep_lock.locked():
        return {"error": "a sweep is already running on this node"}
    async with _sweep_lock:
        env = {**os.environ, "HOME": HOME, **{k: config.get(k) for k in SWEEP_KNOBS}}
        env.pop("MOP_SWEEP_DRY", None)
        if req.get("dry"):
            env["MOP_SWEEP_DRY"] = "1"
        proc = await asyncio.create_subprocess_exec(
            SWEEP, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env=env)
        out, err = await proc.communicate()
    return sweep_report(proc.returncode, out.decode(errors="replace"),
                        err.decode(errors="replace"), int(config.get("MOP_SWEEP_FREE_MIN_GB")))


async def v_wipe(conn, req):
    """Снести рабочую копию папета и восстановить её из git; target — целиком.

    Это половина рецикла (вторая — перерегистрация джоба у мастера). Клон не
    переклонируется — дорого и незачем: reset откатывает отслеживаемое,
    `clean -xdff` выметает и untracked, и игнорируемое (внутриклоновые
    кэши, node_modules), но `-e` защищает подсеянное врапером — список
    живёт в MOP_PUPPET_SEED и у врапера, и здесь один. target-каталог —
    чисто производные данные, он удаляется rm -rf и тем самым снимается
    почти весь объём.

    Что именно сносится, решает драйвер: у host тело — сам узел, снести его
    нельзя, и сносится всё, что папет в нём нажил; у контейнерного драйвера
    уходит целиком контейнер, а следующий подъём делает новый.

    Предохранитель: живая tmux-сессия — отказ. Агент не судит, свободен ли
    папет, но «сессия жива» — факт, и снос под живой сессией недопустим
    независимо от того, что решил мастер."""
    name = req["name"]
    if not driver.valid_name(name):
        return {"error": driver.bad_name(name)}

    async def act():
        if await tmux_alive(name):
            return {"error": f"{name}: tmux session is alive — stop the job first"}
        # Событие до сноса: после него origin клона спрашивать уже не у кого.
        await _event(conn, "wipe", name)
        # Ветка мастера (#257): host-клон после сноса встаёт на неё, у
        # контейнерного драйвера её поставит свежий клон по мете джоба.
        return await DRIVER.destroy(name, branch=req.get("branch"))
    # Замок держится весь снос: send в сносимый клон всё равно не доехал бы.
    return await _gated(name, req, act)


async def v_type(conn, req):
    """Напечатать слэш-команду в пейн и вернуть экран после неё.

    Печатью, а не сообщением по каналу: слэш-команды через канал не проходят
    (сообщение кладётся в очередь с skipSlashCommands), а у папета с
    исчерпанной квотой любой ход падает, не начавшись, — слэш-команду же
    исполняет сам TUI, ход на неё не тратится.

    Перед вводом чистим строку (C-u): в пейне мог остаться недобитый текст,
    и тогда команда склеилась бы с ним в мусор."""
    name, command = req["name"], (req.get("command") or "").strip()
    # Имя -- до шелла: bsh на кривом имени отдаёт тот же None, что таймаут,
    # и отказ назвал бы не ту причину (#171).
    if not driver.valid_name(name):
        return {"error": driver.bad_name(name)}
    return await _gated(name, req, lambda: _type(conn, name, command))


async def _type(conn, name, command):
    """Ввод в пейн, когда ворота уже пустили."""
    if command in KEYS_ALLOWED:
        # Голая клавиша: ни очистки строки, ни Enter следом — Escape снимает
        # диалог, а Enter после него отправил бы пустой ход.
        out, code = await bsh(name, Tmux(name).press(command))
        if code is None:
            # Таймаут -- не нажато (#171); ненулевой код отвечает как раньше.
            return {"error": why(out, code)}
        return {"screen": out} if code == 0 else {"error": out.strip()}
    if command.split()[0:1] and command.split()[0] not in SLASH_ALLOWED:
        return {"error": f"only allowed: {', '.join(SLASH_ALLOWED + KEYS_ALLOWED)}"}
    if "'" in command:
        return {"error": "quote in command: command goes to the shell as one line"}
    out, code = await bsh(name, Tmux(name).type(command))
    if code is None:
        return {"error": why(out, code)}
    if code != 0:
        return {"error": out.strip() or f"tmux exit {code}"}
    await _event(conn, "type", name, text=command)
    return {"screen": out}


async def v_clone(_conn, req):
    """Факты клона папета, и при мёртвой сессии тоже (#40): по ним сервис
    кластера решает, можно ли рестарт, update, delete. state их не даёт,
    пока tmux не жив, -- а работа в клоне от этого не пропадает."""
    return {"clone": await clone_facts(req["name"])}


def write_targets(project, requested, live, owners, marks, carries_mark, is_container,
                  node_mark):
    """Куда писать файлы глагола `write` (#312). Чистая.
    -> (тела, писать ли копию узла, отказ|None, названные, но не живые).

    Раньше -- всегда в копию узла и во все живые тела: раздача кредита a
    перетирала кредит b соседа, а `mop login` мастера проекта X -- кредит в
    телах проекта Y на том же узле. Копия узла -- то, что драйвер сеет в
    каждое новое тело, и последний писавший побеждал для всех будущих.

    project -- проект субъекта (admin -- оператор); requested -- `bodies`
    запроса либо None; live -- живые тела (у host -- папеты узла); owners --
    {тело: проект}; marks -- {тело: метка аренды} ("" -- нет; нечитаемая --
    не пустая: метка только оберегает от записи); carries_mark -- везёт ли
    запись метку (так пишет раздача кредита, `mop login` -- никогда);
    node_mark -- метка на самом узле (у host тело и есть узел).

    Адресная запись (раздача кредита): только названные тела, каждое --
    папет проекта просящего (admin -- любое); копия узла -- только у host,
    где тело и есть узел (второй кредит профиля на host-узле отказывает
    сервис кластера). Без адреса (`mop login`): проект -- только свои тела
    без аренды и не копию узла (её посеют чужим папетам); admin -- копию
    узла и тела без аренды. Аренда -- правда, метка -- подсказка: она
    только не даёт перетереть, и ничего не приписывает (#308)."""
    admin = project == bus.ADMIN
    if requested is not None:
        bad = [n for n in requested if not driver.valid_name(n)]
        if bad:
            return [], False, driver.bad_name(bad[0]), []
        foreign_ = [n for n in requested if not admin and owners.get(n) != project]
        if foreign_:
            return [], False, foreign(foreign_[0], project), []
        if not is_container:
            # У host тело -- сам узел: копия узла и есть его файл, и до tmux
            # (bootstrap) папет обслужен ею, хоть и не «живой».
            return [], True, None, []
        return [n for n in requested if n in live], False, None, [
            n for n in requested if n not in live]
    if admin and carries_mark:
        # ПЕРЕХОД (#312): раздача кредита от сервера до #312 -- без `bodies`,
        # с меткой. Пишется, как было; уходит, когда сервер и все агенты
        # пройдут этот выпуск.
        return list(live), True, None, []
    free = [n for n in live if not marks.get(n)]
    if admin:
        return (free if is_container else []), (is_container or not node_mark), None, []
    if is_container:
        return [n for n in free if owners.get(n) == project], False, None, []
    alone = all(owners.get(n) == project for n in live)
    return [], alone and not node_mark, None, []


async def _mark_of(name, path):
    """Метка аренды в теле: имя кредита, "" -- нет; не прочиталась -- "?",
    и тело не перетирается."""
    out, code = await bsh(name, f"cat {shlex.quote(path)} 2>/dev/null; true")
    return out.strip() if code == 0 else "?"


async def v_write(_conn, req):
    """Атомарная запись файла из белого списка, 600.

    Заменяет ту ветку раздачи кредов, что ездила шеллом в аллокацию. Список
    закрыт: без него это была бы произвольная запись в $HOME, то есть
    исполнение кода через ~/.bashrc. Куда -- решает write_targets (#312)."""
    files = []
    for path, b64 in req.get("files") or []:
        full = paths.writable(HOME, path)
        if not full:
            return {"error": f"agent is not allowed to write to {path}"}
        files.append((full, bus.file_data(b64)))
    mark_path = paths.writable(HOME, paths.CRED_MARK)
    carries_mark = any(p == mark_path for p, _ in files)
    requested = req.get("bodies")
    project = req.get("_project")
    live = await DRIVER.bodies()
    named = [n for n in dict.fromkeys(list(live) + list(requested or []))
             if driver.valid_name(n)]
    owners = {} if project == bus.ADMIN else dict(zip(named, await asyncio.gather(
        *(puppet_project(n) for n in named))))
    marks, node_mark = {}, ""
    if requested is None and not (project == bus.ADMIN and carries_mark):
        if driver.separate_bodies(DRIVER):
            marks = dict(zip(live, await asyncio.gather(*(_mark_of(n, mark_path)
                                                          for n in live))))
        else:
            try:
                with open(mark_path) as f:
                    node_mark = f.read().strip()
            except OSError:
                node_mark = ""
    bodies, node_copy, refused, absent = write_targets(
        project, requested, live, owners, marks, carries_mark, driver.separate_bodies(DRIVER),
        node_mark)
    if refused:
        return {"error": refused}

    # Копия узла -- то, что драйвер сеет в каждое новое тело при подъёме
    # (MOP_BODY_SEED); у host она же и есть тело.
    written = []
    if node_copy:
        for path, data in files:
            try:
                driver.write_private(path, data)
            except Exception as e:
                return {"error": f"{path}: {e}"}
            written.append(path)

    # ...И в живые тела: иначе протухший логин лечился бы только рестартом
    # папета -- ценой его работы. Все тела разом и все файлы тела одним
    # вызовом (#137): по очереди это стоило ~4 с на файл.
    answers = await asyncio.gather(*(DRIVER.push_many(name, files) for name in bodies))
    for name, r in zip(bodies, answers):
        if r.get("error"):
            # Отказ по одному телу не отменяет остальных.
            written.append(f"{name} FAILED — {r['error']}")
        else:
            written += [f"{name}:{p}" for p in r.get("written") or []]
    written += [f"{name} NOT LIVE" for name in absent]
    return {"written": written}


async def v_junk(_conn, req):
    """Что стоит на этом узле, БЕЗ фильтров: {node, driver, bodies,
    templates}.

    От `local` отличается тем, ради чего и заведён: тот показывает папетов
    (живая сессия, свой проект), а этот — объекты. Мусор по определению не
    имеет живой сессии и не принадлежит никому, так что фильтры `local`
    отсеяли бы ровно то, что ищут. Поэтому глагол админский: он рассказывает
    про чужие проекты тоже, а сопоставлять с Nomad всё равно некому, кроме
    управляющей машины.

    `templates` у host пуст: сборочных тел там не бывает вовсе, глагола у
    драйвера нет (#276), и пустой список честнее выдуманного."""
    names = await DRIVER.bodies()
    templates = driver.hypervisor_verb(DRIVER, "templates")
    # Работу в клоне спрашиваем ЗДЕСЬ, а не оставляем решать по имени. Тело
    # без tmux-сессии `facts` описывает как {present: False} и про клон молчит
    # — верно для узла, до которого не достучаться, но сирота на гипервизоре
    # жива и отвечает по ssh. Без этого уборка сносила бы тела, не спросив,
    # есть ли в них несохранённое: 22.09 она так снесла два контейнера чужих
    # проектов, и повезло, что пустых.
    work = {}
    for n in names:
        c = await clone_facts(n)
        if c:
            work[n] = {"dirty": c.get("dirty"), "ahead": c.get("ahead"),
                       "cur": c.get("cur")}
    return {"node": node_name(),
            "driver": driver.current_name(),
            "bodies": names,
            "work": work,
            "templates": await templates() if templates else []}


async def v_usage(_conn, req):
    """Расход токенов папетов этого узла по дням: {usage: {папет: {дата:
    {input, output, cache_write, cache_read}}}}, окно — `days` суток включая
    сегодня, по местному времени узла.

    Считается по транскриптам <projects_dir>/<slug клона>/ (подробности и
    ловушка с дублями — в usage.py), и считается внутри тела: у контейнерного
    папета этих файлов на узле нет вовсе, а отсутствие файлов неотличимо от
    нулевого расхода — счёт снаружи показал бы ноль там, где папет сжёг
    миллионы.

    Папета перечисляет драйвер (bodies), а не listdir клонов: на гипервизоре
    каталога клонов нет. Имя в slug'е искалечено (точки и подчёркивания стали
    дефисами), и обратно в имя папета, которое сверяется с проектом, его не
    собрать, — поэтому идём от имени к каталогу, а не наоборот.

    Чужих папетов выбрасываем молча, как states: мастер проекта видит расход
    своего проекта, оператор — всего узла.

    Рядом — by_login: {папет: {логин: {дата: {...}}}}, тот же расход по
    логину мастера, чьё сообщение вызвало ход (#244). usage не меняется: его
    читают mop stat и дашборд. Сумма by_login по логинам за папет и день —
    ровно строка usage. Старый usage.py в теле флага --by-login не знает и
    печатает прежнюю форму; тогда весь расход папета за «-»."""
    days = min(max(int(req.get("days") or 7), 1), 366)
    names = [n for n in await DRIVER.bodies() if await _mine(req, n)]
    # usage.py лежит рядом с session.py: SESSION_PY и называет то место, куда
    # пакет mop приехал внутри тела.
    usage_py = os.path.join(os.path.dirname(DRIVER.SESSION_PY), "usage.py")

    async def one(name):
        d = f"{DRIVER.projects_dir(name)}/{usage.slug(clone_dir(name))}"
        out, code = await bsh(name, " ".join(
            ["python3", shlex.quote(usage_py), shlex.quote(d), str(days), "--by-login"]), 120)
        got = (_last_json(out) or {}) if code in (0, None) else {}
        if "by_login" in got and "usage" in got:
            return got["usage"], got["by_login"]
        return got, ({usage.NOBODY: got} if got else {})

    got = await asyncio.gather(*(one(n) for n in names))
    return {"node": node_name(), "usage": {n: g[0] for n, g in zip(names, got)},
            "by_login": {n: g[1] for n, g in zip(names, got)}}


# ─── права: одна таблица ─────────────────────────────────────────────────
# Кому глагол дан (#150). Раньше это были четыре параллельных набора, и новый
# глагол правился в четырёх местах.
#
# PUBLIC -- и не-мастеру, субъектом .msg. Папет имеет право написать соседу и
# посмотреть, кто чем занят; печатать в чужой TUI и писать файлы -- не имеет.
#
# MASTER -- только субъектом .rpc: мастеру проекта и оператору.
#
# NODE -- глагол узла, а не проекта: только оператору (`mop.admin.*`). Место на
# диске -- факт про хост со всеми его жильцами, и мастеру проекта соседняя
# нагрузка не показывается.
#
# `write` из NODE убран. Он лежал там из-за «мастер проекта A перезапишет
# креды проекта B». Из трёх файлов WRITABLE (paths.WRITABLE) два собираются не
# из проекта мастера, а из машины — .credentials.json из логина claude.ai
# управляющей машины, secrets.env из .env самого mop (`puppets.LOCAL_KEYS_FILE`
# — это PROJECT репозитория mop, а не проекта): их мастер любого проекта везёт
# байт в байт то же, что вёз бы оператор. Третий, метку кредита (#284), кладёт
# реестр кредитов сервера (mop/server/credreg.py), и хотя `write` принимает её
# от любого мастера, чужая метка безвредна: приписывание верит только аренде в
# мете джоба. С #284 credreg.tick отдаёт в note_turn лишь ход, чья метка равна
# кредиту держателя аренды, и ход с несогласной меткой пропускает; с #308
# note_turn и имя берёт из аренды, а несогласный ход пишет в журнал. Ценой
# запрета было `mop login` из мастер-шелла: он отбивался по каждому узлу, и
# doctor оставался без единственного лечения протухшего логина. Папета это не
# касается: `write` не PUBLIC, а креды puppet-<проект> в субъект .rpc не пишут
# вовсе.
#
# named -- глагол называет конкретного папета, и проект запроса обязан сойтись
# с настоящим проектом папета. Verb -- общий с сервисом кластера (#204);
# acting у агента не бывает.
PUBLIC, MASTER, NODE = "public", "master", "node"
VERBS = {
    "ping":   Verb(v_ping,   PUBLIC, False),
    "local":  Verb(v_local,  PUBLIC, False),
    "state":  Verb(v_state,  PUBLIC, True),
    "states": Verb(v_states, PUBLIC, False),
    "sizes":  Verb(v_sizes,  MASTER, False),
    "send":   Verb(v_send,   PUBLIC, True),
    "tail":   Verb(v_tail,   PUBLIC, True),
    "type":   Verb(v_type,   MASTER, True),
    "write":  Verb(v_write,  MASTER, False),
    "disk":   Verb(v_disk,   NODE,   False),
    "wipe":   Verb(v_wipe,   MASTER, True),
    "usage":  Verb(v_usage,  MASTER, False),
    "junk":   Verb(v_junk,   NODE,   False),
    "clone":  Verb(v_clone,  MASTER, True),
    "sweep":  Verb(v_sweep,  NODE,   False),
}
# Прежние наборы -- выводом из таблицы, для тех, кто их читает.
PUBLIC_VERBS = tuple(v for v, d in VERBS.items() if d.scope == PUBLIC)
ADMIN_VERBS = tuple(v for v, d in VERBS.items() if d.scope == NODE)
NAMED_VERBS = tuple(v for v, d in VERBS.items() if d.named)


def refusal(verb, public, project):
    """Отказ по глаголу, субъекту и проекту, либо None. Проверку владельца
    папета (named) делает handle: она спрашивает клон.

    Нестроковый глагол -- неизвестный (#168): `[1]` как ключ таблицы бросал
    TypeError, и запрос оставался без ответа."""
    spec = VERBS.get(verb) if isinstance(verb, str) else None
    if spec is None:
        return f"no such verb {verb}; available: {', '.join(sorted(VERBS))}"
    if public and spec.scope != PUBLIC:
        # Не «нет прав», а прямо: глагол существует, но не в этом субъекте.
        return f"verb {verb} is available to the master only"
    if spec.scope == NODE and project != busnames.ADMIN:
        return f"verb {verb} is node-level, not given to project {project}"
    return None


def foreign(name, project):
    return f"puppet {name} is not in project {project}"


async def _mine(req, name):
    """Принадлежит ли папет проекту, из чьего субъекта пришёл запрос."""
    return req.get("_project") == bus.ADMIN or await puppet_project(name) == req.get("_project")


# ─── петля ───────────────────────────────────────────────────────────────
async def handle(conn, msg, public):
    """Разбор и три проверки: глагол существует, субъект его допускает, папет
    принадлежит спрашивающему проекту.

    Проект берём из субъекта (`mop.<проект>.node.<узел>.<канал>`), а не из тела
    запроса: тело пишет отправитель, субъект — права NATS."""
    try:
        req = json.loads(msg.data.decode())
    except ValueError:
        return await msg.respond(
            json.dumps({"error": "request is not JSON"}, ensure_ascii=False).encode())
    if not isinstance(req, dict):
        # До таблицы (#168): `[1]` падал на req["_project"], и задача умирала
        # без ответа -- проситель ждал таймаут.
        return await msg.respond(
            json.dumps({"error": "request is not a JSON object"}).encode())
    # Одно правило на всех (#173): mop.<проект>.node.<узел>.<канал>.
    req["_project"] = service.project_from_subject(msg.subject)
    # Кто просит (#207) -- из субъекта, поверх тела. Публичный канал --
    # папетов: владельца он не несёт, и названный в теле не в счёт.
    req.pop("_caller", None)
    caller = None if public else busnames.caller(msg.subject)
    if caller:
        req["_caller"] = caller
    if public:
        req.pop("owner", None)

    verb = req.get("verb")
    why = refusal(verb, public, req["_project"])
    if why is None and VERBS[verb].named and not await _mine(req, req.get("name") or ""):
        # Главная проверка проектирования. Прав NATS тут мало: мастер проекта A
        # законно пишет в свой субъект, но может назвать папета из B.
        why = foreign(req.get("name"), req["_project"])
    if why:
        out = {"error": why}
    else:
        try:
            out = await VERBS[verb].fn(conn, req)
        except Exception as e:
            out = {"error": f"{verb}: {e}"}
    try:
        await msg.respond(json.dumps(out, ensure_ascii=False).encode())
    except Exception:
        pass


async def attach(conn, node):
    """Подписать агента узла на его субъекты и объявить подъём. -> [субъекты].

    cb обязан быть корутиной — nats-py отвергает обычную функцию. И каждый
    запрос уходит в свою задачу: последовательная обработка означала бы, что
    одно долгое ожидание простоя запирает весь узел.

    Маска по проекту: агент обслуживает всех жильцов узла, а кто из какого
    проекта — решает уже проверка в handle. Общий all.msg — для тех, кто не
    знает состава пула. Те же субъекты, что в правах узла (natsconf, #144)."""
    async def on_rpc(msg):
        asyncio.create_task(handle(conn, msg, public=False))

    async def on_msg(msg):
        asyncio.create_task(handle(conn, msg, public=True))

    subs = busnames.agent_subscriptions(node)
    for subj in subs["rpc"]:
        await conn.subscribe(subj, cb=on_rpc)
    for subj in subs["msg"]:
        await conn.subscribe(subj, cb=on_msg)
    await _event(conn, "up", text=f"agent on {node}")
    return subs["rpc"] + subs["msg"]

