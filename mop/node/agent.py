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
import base64
import json
import os
import re
import shlex
import socket
import time

from ..common import bus, busnames, config, fsutil, lease, paths, service, soul
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
RESOLVE_WAIT = 5         # сколько глагол актёра ждёт резолвер сервера (#584)
SOUL_FACT_TTL = 10       # кэш soul-факта ростера: только читающие глаголы (#584)
STARTUP = "initial_identity"   # единственный глагол канала подъёма (#584/#590)
MARK = "mop-soul-inc"          # маркер identity инкарнации (агент, .git/)
READY = "ready.json"          # доказательство готовности 590/583 (souls/)


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
    На этих числах стоит решение мастера о диспатче.

    Проба fail-closed (#564): отказ обязательной команды роняет её (exit 1),
    а не маскируется нулём -- и отказ отличает код возврата, не пустой
    stdout: git может напечатать число и отказаться. `git status | wc -l`
    на побитом репозитории печатает пустоту -- и она читалась нулём грязи
    (код конвейера -- это код wc, а не git-а, поэтому считаем после захвата:
    `$(...)` несёт код git); пустой `rev-list` от `2>/dev/null` -- нулём
    неотправленного. Уборка получала «чистый клон» там, где git не мог
    прочесть репозиторий вовсе (мусорный HEAD, битый index). Обязательных
    команд три: status, проверка состояния и rev-list. Проверка состояния --
    `rev-parse --verify` HEAD с кодом как приговором (#564): 0 -- HEAD
    доказан (born), 1 -- HEAD законно нет (unborn, единственный случай,
    когда rev-list по HEAD отказывает законно и считаются все коммиты),
    прочее -- репозиторий не читается, unknown; «любой отказ проверки =
    unborn» -- ошибка, оборачивающая сбой в ложное разрешение. Остальные
    команды (branch, config, remote) необязательны: их отказ -- пустое
    поле, а не неизвестный клон."""
    return (f'cd {d} 2>/dev/null || exit 0; '
            f'echo "cur=$(git branch --show-current 2>/dev/null)"; '
            f'echo "def=$(git rev-parse --abbrev-ref origin/HEAD 2>/dev/null)"; '
            f'echo "home=$(git config mop.home 2>/dev/null)"; '
            f'echo "origin=$(git remote get-url origin 2>/dev/null)"; '
            f'st=$(git status --porcelain 2>/dev/null) || exit 1; '
            f'dirty=0; [ -n "$st" ] && dirty=$(printf \'%s\\n\' "$st" | wc -l); '
            f'git rev-parse --verify -q --end-of-options HEAD >/dev/null 2>&1; '
            f'case $? in '
            f'0) ahead=$(git rev-list --count HEAD --not --remotes 2>/dev/null) || exit 1;; '
            f'1) ahead=$(git rev-list --count --all 2>/dev/null) || exit 1;; '
            f'*) exit 1;; esac; '
            f'[ -n "$ahead" ] || exit 1; '
            f'echo "dirty=$dirty"; echo "ahead=$ahead"; '
            f'echo "owner=$(head -1 {lease.FILE} 2>/dev/null)"')


def clone_facts_of(out):
    """Вывод clone_probe -> CloneFacts.to_dict() либо None (клона нет или
    числа не прочитались).

    Пустые dirty/ahead -- не нули, а неполные данные (#564): обязательная
    команда не ответила, и клон обязан читаться неизвестным -- у
    потребителя это «keep», а не «clean». До #564 пустой ahead читался
    нулём (`or 0`), и побитый клон получал разрешение на уборку."""
    kv = fsutil.read_kv(out, raw=True)
    if "dirty" not in kv:
        return None
    try:
        dirty, ahead = int(kv.get("dirty") or -1), int(kv.get("ahead") or -1)
    except ValueError:
        return None
    if dirty < 0 or ahead < 0:
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
    """Ответ session.py, который печатает JSON (send, wait-idle).

    Тишина тела (код None) и ответ без итоговой JSON-строки (разрыв
    транспорта: мусор ssh, обрыв соединения) -- исход НЕОПРЕДЕЛЁННЫЙ, и
    оба помечены timeout (#review574-7): v_send отличает «не доехало
    достоверно» (структурированный {"error": …} от session.py) от «не
    известно, доехало ли» -- второе не откатывает ни аренду, ни identity:
    сообщение могло дойти, и откат испортил бы чужой ход. Подтверждённый
    отказ -- только структурированный ответ session.py, угадывание по
    строкам мусора -- не способ."""
    out, code = await bsh(name, script, timeout)
    if code is None:
        return {"error": f"{name}: the body did not answer in {timeout}s",
                "timeout": True}
    got = _last_json(out)
    if got is not None:
        return got
    return {"error": (out.strip() or f"session.py exit {code}")
            + " — no parsable answer, delivery unknown",
            "timeout": True}


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


async def v_state(conn, req):
    out = await facts(req["name"])
    soul_fact = await _soul_fact(conn, req["_project"], req["name"], out)
    if soul_fact is not None:
        out["soul"] = soul_fact
    return out


async def v_states(conn, req):
    """Пачкой: у узла обычно несколько папетов, и спрашивают о них всегда
    вместе. Одна поездка вместо N.

    Чужих молча выбрасываем, а не отвечаем отказом: мастер спрашивает по своему
    ростеру, и если в списке оказался чужой — это ошибка спрашивающего, из-за
    которой не должна пропасть картина по своим."""
    names = [n for n in (req.get("names") or []) if await _mine(req, n)]
    got = await asyncio.gather(*(facts(n) for n in names))
    souls = await asyncio.gather(*(_soul_fact(conn, req["_project"], n, f)
                                   for n, f in zip(names, got)))
    return {"puppets": {n: dict(f, soul=s) if s is not None else f
                        for n, f, s in zip(names, got, souls)}}


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

# Поколения захвата аренды по пути файла (#573): монотонный счётчик в
# процессе агента. Файл аренды пишет только агент под замком на папета,
# поэтому захваты одного пути строго пронумерованы по порядку записи.
# Счётчик живёт, пока живёт процесс; перезапуск его сбрасывает, но это
# ничьих откатов не рвёт: undo живёт в том же процессе, что и захват.
_lease_generations = {}

# Захваты того же пути (#573): поколение -> [исход, прежняя запись,
# предшественник]. Исход -- "live" (доставка идёт), "settled" (доставка
# доехала -- откат больше не приедет) либо "failed" (доставка упала;
# отмечается ВСЕГДА, и когда откат исполняется, и когда чужой захват
# поверх сделал запись невозможной -- иначе поздний откат не узнает, что
# запись провалившегося восстанавливать нельзя). Прежняя запись -- байты
# файла на момент захвата: ровно их вернёт откат, если под ним нет
# невосстановленных провалов. Предшественник -- поколение, чья запись
# физически лежала в файле на момент захвата: откат исполнен -- держателем
# снова стал он, захват поверх его записи -- предшественник именно он,
# каким бы ни был номер соседнего поколения. Причинная цепочка по
# предшественникам заменяет арифметику номеров: откат спускается по
# ссылкам, проваленные звенья перепрыгивает, на живом останавливается.
# Вернуть активного предшественника и откатить аллокатор -- разные вещи:
# счётчик монотонен, исходы решает этот реестр. База -- не аудит всех
# отправок: реестр хранит контекст возможных откатов (live и их цепочки)
# и подтверждённого держателя файла -- и ничего сверх того.
_lease_claims = {}

# Держатель файла аренды по пути (#573): поколение, чья запись лежит в
# файле сейчас, либо None (файла нет или в нём чужие/внешние байты -- их
# никакой захват не восстановит, и откат обязан вернуть именно их).
# Пишется под тем же замком, что и сам файл: claim делает держателем себя,
# успешная доставка оставляет держателя-захвата подтверждённой базой,
# исполненный откат -- предшественника, на котором остановился.
_lease_holder = {}


def _predecessor(claims, generation):
    """Поколение-предшественник захвата либо None. Ссылка из реестра:
    чья запись лежала в файле на момент захвата."""
    return claims.get(generation, (None, None, None, None))[2]


def _prune_claims(claims, holder=None):
    """Убрать из реестра недостижимое: аудит каждого send тут не нужен.

    Дойти до записи отката может только цепочка от захвата, чей откат ещё
    возможен, -- от live. Значит, живут live-захваты и их предшественники
    по ссылкам; завершённый (доставка доехала -- settled) и проваленный
    захват без живой ссылки сверху отката больше не дождётся -- он никому
    не нужен. Держатель файла живёт всегда: его запись в файле -- подтвер-
    ждённая база, экран для чужих откатов и «прежде» будущих захватов.
    Зовут под замком на папета, сразу после перехода реестра в новое
    состояние: до этого момента запись могла ещё понадобиться."""
    keep = set()
    for g, s in claims.items():
        if s[0] == "live":
            while g is not None and g not in keep:
                keep.add(g)
                g = _predecessor(claims, g)
    if holder is not None:
        keep.add(holder)  # сам держатель, без его цепочки: его «прежде»
        # уже никому не вернуть -- откатов к нему нет, файл и есть база
    for g in list(claims):
        if g not in keep:
            del claims[g]


def _settle(path, generation):
    """Успешная доставка завершает захват (#573): откат ему больше не
    приедет, и реестр не копит историю завершённых отправок. Захват ещё
    держит файл -- он и остаётся подтверждённой базой: держателем, экраном
    чужих откатов и «прежде» будущих захватов; «прежде» нового захвата
    читается из фактов, реестр хранит только контекст возможного отката."""
    claims = _lease_claims.setdefault(path, {})
    if generation in claims:
        claims[generation][0] = "settled"
        if _lease_holder.get(path) == generation:
            _prune_claims(claims, generation)
            return
        _prune_claims(claims, _lease_holder.get(path))


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
    # Поколение захвата (#573): один логин в одну секунду пишет байт в байт
    # ту же строку, и равенство строк в откате не отличает свой захват от
    # чужого успешного. Счётчик монотонен на путь аренды в этом процессе:
    # файл аренды пишет только агент, замок сериализует его записи, поэтому
    # «наш захват» -- это строго нумерованное поколение этого пути. Файл не
    # меняется, читатели прежние -- никакого перехода записей при rollout.
    # Прежний владелец -- из фактов: их читаем мы же под этим же замком,
    # пишет файл тоже только агент, устареть раньше нас некому. Предше-
    # ственник -- держатель файла на момент захвата: чья запись лежала в
    # файле, тот и предшественник -- ссылка, а не соседний номер поколения.
    generation = _lease_generations[path] = _lease_generations.get(path, 0) + 1
    claims = _lease_claims.setdefault(path, {})
    # Четвёртое поле (#574) -- прежняя identity клона на момент установки
    # нашей: снимок ставит шелл установки, провалившаяся доставка вернёт
    # именно её, не спрашивая сервер заново. Начало -- «не трогали»
    # (self-declared владелец, захват без установки): восстановления нет;
    # реальный снимок (профиль либо None «снять») пишет v_send после
    # установки -- один и тот же маркер не спутаешь ни с профилем, ни с None.
    claims[generation] = ["live", owner.render() if owner else "",
                          _lease_holder.get(path), _NO_IDENTITY_TOUCH]
    _lease_holder[path] = generation
    return None, (path, owner, mine, generation), note


async def _unclaim(name, undo):
    """Вернуть прежнюю аренду. -> причина неудачи | None.

    Не вернули (таймаут, #171, или ошибка) -- аренда осталась за мастером,
    чей send не доехал, и следующему откажут, назвав не того владельца (#181):
    звавший обязан это сказать.

    Только если в файле всё ещё наша запись (#189): пока доставка шла, аренду
    мог взять другой мастер (force), и откат вслепую перетёр бы его запись
    прежним владельцем или стёр бы файл -- его работа осталась бы ничьей.
    Чужая запись -- не наш откат: оставляем её и ничего не говорим. Зовут под
    _owner_locks, так что между сравнением и записью чужого claim не бывает.
    Равных строк мало (#573): один логин в одну секунду пишет ту же строку,
    и потому «наш» захват -- поколение, а не байты. Провал отмечается в
    реестре исходов ВСЕГДА -- и когда откат исполняется, и когда чужой
    захват поверх сделал запись невозможной: исход захвата -- факт о нём
    самом, а не о том, писал ли кто-то поверх. Запись же условна: файл
    трогает только откат, под которым нет живых захватов. Что восстанав-
    ливать -- решает причинная цепочка предшественников: у каждого захвата
    предшественник -- тот, чья запись лежала в файле на момент захвата
    (откат исполнен -- держателем стал он, и захват поверх его записи
    ссылается на него, каким бы ни был соседний номер). Проваленные звенья
    цепочка перепрыгивает: их записи недействительны, действительны байты
    «прежде» того, на ком цепочка остановилась. Счётчик поколений при этом
    монотонен: вернуть активного предшественника -- не то же самое, что
    уменьшить аллокатор."""
    path, _owner, mine, generation = undo  # «прежде» -- в реестре, не в undo
    claims = _lease_claims.setdefault(path, {})
    ours = claims.get(generation)
    if ours is None:
        # Реестр -- хозяин захвата: нет записи -- нет и нашего отката.
        # В одном процессе undo не переживает своей записи в реестре.
        return None
    # Критическая секция -- здесь: caller проверил замок, но между его
    # предчеком и входом чужой захват мог подняться. Проверка ДО записи:
    # после записи файла проверять поздно. Зовут под _owner_locks, и
    # реестр под тем же замком пишут только _claim и исполненные откаты --
    # между этими двумя строками чужого захвата не бывает. Живой или
    # завершённый захват поверх -- файла не трогаем: live ещё ждёт своего
    # исхода, settled уже доехал и держит файл; наш провал отмечаем, его
    # запись не наша -- поздний откат по ссылке предшественника перепрыг-
    # нет её.
    if any(s[0] != "failed" for g, s in claims.items() if g > generation):
        ours[0] = "failed"
        _prune_claims(claims, _lease_holder.get(path))
        return None
    # Причинная цепочка вниз: предшественник провален -- его запись недей-
    # ствительна (его откат не дошёл до файла или уже перепрыгнут), спус-
    # каемся к ЕГО предшественнику. Остановились (нет предшественника или
    # он жив) -- возвращаем «прежде» остановившегося: эти байты и были
    # последними действительными до нашей цепочки.
    g = generation
    while _predecessor(claims, g) is not None:
        prev = _predecessor(claims, g)
        if claims[prev][0] != "failed":
            break
        g = prev
    body = claims[g][1]  # «прежде» того, на ком цепочка остановилась
    write = (f"printf %s {shlex.quote(body)} > {shlex.quote(path)}"
             if body else f"rm -f {shlex.quote(path)}")
    # $(cat) срезает конечный перевод строки -- сравниваем без него.
    # Ветка «в файле не наше» отвечает своей меткой: rc одинаков с записью,
    # а исход разный -- файл уже чужой, держателя из реестра не назначаем.
    out, code = await bsh(name, f'[ "$(cat {shlex.quote(path)} 2>/dev/null)" = '
                                f'{shlex.quote(mine.rstrip(chr(10)))} ] '
                                f'|| {{ echo "not ours"; exit 0; }}; {write}')
    # Переход реестра подтверждает сама запись, не решение о ней: bsh упал
    # или молчит (#171) -- физический файл всё ещё держит нашу запись, и
    # реестр обязан ждать того же. Пометить захват проваленным здесь --
    # объявить восстановленным то, что не восстановлено; причина неудачи
    # уходит с отказом (#181), захват остаётся живым до подтверждения --
    # ровно как не доехавший send держит аренду до ручного разбора.
    if code == 0:
        ours[0] = "failed"
        # «Не наше» в файле -- он уже внешний: держателя из реестра не
        # называем, следующие захваты прочитают факты с настоящего файла.
        _lease_holder[path] = None if "not ours" in out else _predecessor(claims, g)
        _prune_claims(claims, _lease_holder.get(path))
    return why(out, code) if code != 0 else None


def _identity_of(claims, generation):
    """Прежний снимок захвата из реестра (#574). -> снимок | «поля нет»-маркер.

    Маркер MISSING -- identity при этом захвате не трогали (self-declared
    владелец, захват без установки): восстановления тоже нет. Снимок из
    четвёртого поля -- {'identity': …, 'hooks': …} от шелла установки."""
    got = claims.get(generation)
    if got is None or len(got) < 4:
        return _NO_IDENTITY_TOUCH
    return got[3]


class _NoIdentityTouch:
    """Маркер «identity не трогали»: не None (None -- «снять»)."""

    def __repr__(self):
        return "identity-untouched"


_NO_IDENTITY_TOUCH = _NoIdentityTouch()


async def _restore_identity(name, undo, claims, generation):
    """Вернуть прежнюю identity клона после подтверждённого провала (#574).
    -> причина неудачи | None. Зовут под _owner_locks, ДО _unclaim.

    Что восстанавливать -- снимок в реестре, а не новый вопрос серверу:
    удалённый профиль к моменту отката мог измениться, а прежнее состояние
    обязано вернуться согласованным -- той парой и теми хуками, что стояли
    в клоне на момент установки нашей. Цепочка та же, что у файла (#573):
    проваленные звенья перепрыгиваем, восстанавливаем снимок звена, на
    котором остановились. Экран тот же: живой или завершённый захват поверх
    -- identity не трогаем, это уже чужая пара. Снимок не читан -- прежнее
    состояние неизвестно: «восстановить пустоту» нельзя, отказ с причиной
    (#181); клон остаётся под последней поставленной identity, и разбирать
    это оператору, а не молча перезаписывать. Ошибка шелла -- неудача
    отката с причиной, без строк снимка."""
    path, _owner, mine, _gen = undo
    g = generation
    while _predecessor(claims, g) is not None:
        prev = _predecessor(claims, g)
        if claims[prev][0] != "failed":
            break
        g = prev
    snap = _identity_of(claims, g)
    if snap is _NO_IDENTITY_TOUCH:
        return None
    script = _restore_script(clone_dir(name), path, mine, snap)
    if script is None:
        return "git identity not restored: the prior state snapshot is unreadable"
    out, code = await bsh(name, script)
    if code != 0:
        return f"git identity not restored: {_why_marked(out, code)}"
    return None


async def _restore_identity_and_lease(name, undo):
    """Согласованный откат провалившейся доставки (#574): identity, потом
    аренда. -> причина неудачи | None. Зовут под _owner_locks.

    Identity -- первой: её скрипт стоит на страже «в файле наша запись»
    (#189), и откат аренды до неё оставил бы чужие байты -- скрипт молча
    вышел бы, не восстановив пару. Провал файла -- не повод бросать
    identity: обе причины уйдут с отказом (#181)."""
    path, _owner, _mine, generation = undo
    claims = _lease_claims.setdefault(path, {})
    ours = claims.get(generation)
    if ours is None:
        return None
    # Тот же экран, что у файла (#573): чужой захват поверх -- чужая пара.
    if not any(s[0] != "failed" for g, s in claims.items() if g > generation):
        failed = await _restore_identity(name, undo, claims, generation)
        if failed:
            return failed
    return await _unclaim(name, undo)


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


def _hooks_var(clone):
    """Шелл: каталог хуков клона в $hooks. -> (присваивание, шлюз).

    Присваивание создаёт и временный файл $t с trap EXIT (review-3 п.4):
    его же заполняют геттеры конфига и сверки. Определение каталога --
    rev-parse, и его исход один: rc 0 с непустым ответом -- каталог; любое
    иное -- НЕ «отсутствие хуков», а неизвестный каталог (review-5): текст
    «not a git repository» в stderr не документ отсутствия (операционный
    отказ даёт тот же текст), и даже действительно не-git каталог нельзя
    безопасно подготавливать для repo-local identity. $hfail включает шлюз:
    установка и откат отказывают ДО записи и ДО доставки -- ложный A в
    снимке затем удалял исходный хук при откате."""
    q = shlex.quote
    assign = (
        't=$(mktemp "${TMPDIR:-/tmp}/mop574.XXXXXXXX") || exit 2; '
        "trap 'rm -f \"$t\"' EXIT; "
        f'hooks=$(git -C {q(clone)} rev-parse --absolute-git-dir 2>"$t"); '
        'hr=$?; hfail=; '
        'if [ $hr -eq 0 ] && [ -n "$hooks" ]; then hooks=$hooks/hooks; '
        'else hfail=1; fi; ')
    gate = ('if [ -n "$hfail" ]; then '
            'echo "git hooks directory lookup failed: '
            'the prior state is unknown"; exit 1; fi; ')
    return assign, gate


def _ours(f):
    """Шелл: условие «хук f -- наш» (метка второй строкой)."""
    return f"sed -n 2p {f} | grep -qxF {shlex.quote(OWNER_GUARD)}"


# Снимок прежнего состояния клона (#574, review-2 п.3/4). Формат строки:
# mop574-<ключ>\t<флаг><base64>, где флаг P -- ключ есть (значение -- b64),
# A -- ключа нет, X -- прочитать не удалось (операционная ошибка: git не
# открыл конфиг). Значение ВСЕГДА base64: literal '-' валиден и не может
# служить сентинелом, многострочные значения несут чужие строки в вывод.
# Свои хуки стража -- теми же флагами: P=O (наш: режим и тело b64), F
# (чужой), A (не было). Нет строки -- секция не читана (старый/чужой
# шелл): «не читано» -- не «пусто», восстановление такую секцию не примет.
def _snap_flagged(out, marks):
    """Строки кодированного снимка из вывода шелла. -> {метка: значение}.

    Значение: ('P', строка) | ('A', None) | ('X', None). Чужие флаги и
    битый base64 -- ('X', None): нечитанное состояние, не пустота."""
    got = {}
    for line in out.splitlines():
        for key, mark in marks:
            if not line.startswith(mark + "\t"):
                continue
            tail = line[len(mark) + 1:]
            flag, b64 = tail[:1], tail[1:]
            if flag == "A":
                got[key] = ("A", None)
            elif flag == "P":
                try:
                    got[key] = ("P", base64.b64decode(b64).decode())
                except (ValueError, UnicodeDecodeError):
                    got[key] = ("X", None)
            else:
                got[key] = ("X", None)
    return got


def _snap_lines(out):
    """Снимок identity: {ключ: ('P'|'A'|'X', значение|None)}."""
    return _snap_flagged(out, [(k, f"mop574-{k}") for k in ("name", "email")])


def _hook_lines(out):
    """Снимок своих хуков. -> {хук: (A|F|O, режим, тело)}.

    O -- режим и тело (строка mop574-hook-<хук>\tO\t<режим>\t<b64>); A и F
    -- состояние безpayload. Нераспознанная строка -- None: снимок этого
    хука повреждён, восстановление его не примет."""
    got = {}
    for line in out.splitlines():
        for h in GUARDED_HOOKS:
            mark = f"mop574-hook-{h}"
            if not line.startswith(mark + "\t"):
                continue
            state, _, tail = line[len(mark) + 1:].partition("\t")
            if state == "A":
                got[h] = ("A", None, None)
            elif state == "F":
                got[h] = ("F", None, None)
            elif state == "X":
                # Нечитанный элемент (review-3 п.1): отказ stat/base64 --
                # не пустота, и восстановление его не примет.
                got[h] = ("X", None, None)
            elif state == "O":
                mode, _, b64 = tail.partition("\t")
                try:
                    got[h] = ("O", mode, base64.b64decode(b64).decode())
                except (ValueError, UnicodeDecodeError):
                    got[h] = None
            else:
                got[h] = None
    return got


def _snapshot(out):
    """Полный снимок прежнего состояния клона. -> {'identity': …, 'hooks': …}.

    Секция None -- не читана (меток нет) или битая: восстановление обязано
    отказаться, а не «восстановить пустоту». identity читана, когда есть
    оба ключа и ни один не 'X'; hooks -- когда есть каждый страж и ни один
    не None и не 'X' (X -- нечитанный элемент, review-3 п.1)."""
    ident = _snap_lines(out)
    ident = (ident if set(ident) == {"name", "email"}
             and all(v[0] != "X" for v in ident.values()) else None)
    hooks = _hook_lines(out)
    hooks = (hooks if set(hooks) == set(GUARDED_HOOKS)
             and all(v is not None and v[0] != "X" for v in hooks.values())
             else None)
    return {"identity": ident, "hooks": hooks}


def _why_marked(out, code):
    """Причина отказа шелла без строк снимка (#review574-5, review-2 п.4).

    Метки mop574-* несут прежнюю пару и тела хуков. Значения в них
    кодированы base64, но кадр самой метки из вывода вырезается целиком:
    диагностика -- про отказ, не про прежнее состояние клона."""
    clean = "\n".join(l for l in out.splitlines() if not l.startswith("mop574-"))
    return why(clean, code)


def _unset(git, key, back=""):
    """Шелл: снять ключ, терпя его отсутствие (#review574-3).

    Код 5 -- ключа нет, это успех снятия; 255 (config.lock занят) и прочие
    ненулевые -- отказ записи: остановиться ДО установки нового, откатив
    применённое (back), -- сообщение под неподтверждённой подготовкой не
    уходит."""
    fail = f"{{ {back}exit 1; }}" if back else "exit 1"
    return f"{git} --unset-all user.{key} || [ $? -eq 5 ] || {fail}; "


def _restore_script(clone, path, mine, snap):
    """Скрипт восстановления прежней пары и своих хуков из снимка. Чистая.

    Только пока в файле аренды наша запись (#189). Каждый ключ -- по своему
    присутствию в снимке: был -- вернуть значение (пустое включительно),
    не был -- снять. Свои хуки -- по состоянию: A -- снести наши (их не
    было), O -- воссоздать с прежним режимом и телом, F -- не трогать
    (чужой и тогда, и теперь). Секция не читана (identity или hooks) --
    восстановление невозможно: вернуть «пустоту» вместо прежней пары или
    молча оставить свои хуки хуже, чем отказаться с причиной (review-2,
    п.3).

    Успех -- только подтверждённый (review-3 п.3): снятие созданного нами
    хука обязано пройти (не «тихо не сработало»), и в конце скрипт сверяет
    результат со снимком -- ровно как компенсация установки. Любой отказ --
    exit 1 с причиной: причина едет мастеру, аренда остаётся у пославшего,
    «восстановлено» не говорится, пока не восстановлено."""
    if snap["identity"] is None or snap["hooks"] is None:
        return None
    q = shlex.quote
    parts = [f'[ "$(cat {q(path)} 2>/dev/null)" = {q(mine.rstrip(chr(10)))} ] '
             f"|| exit 0; "]
    git = f"git -C {q(clone)} config --local"
    for key in ("name", "email"):
        flag, value = snap["identity"][key]
        parts.append(_unset(git, key))
        if flag == "P":
            parts.append(f"{git} user.{key} {q(value)} || exit 1; ")
    hooks_assign, hooks_gate = _hooks_var(clone)
    # Неизвестный каталог (review-4) -- отказ отката с причиной, не
    # «восстановление» в /hooks: xуки там не те, сверка соврёт.
    parts.append(hooks_assign + hooks_gate)
    for h in GUARDED_HOOKS:
        v = h.replace("-", "_")
        f = f"$hooks/{h}"
        state, mode, body = snap["hooks"][h]
        if state == "O":
            b64 = base64.b64encode(body.encode()).decode()
            parts.append(f'if [ -e {f} ] && ! {_ours(chr(34) + f + chr(34))}; then '
                         f'echo "hook {h} is not ours anymore, kept"; else '
                         f'printf %s {q(b64)} | base64 -d > {f}.mop && '
                         f'chmod {mode} {f}.mop && mv -f {f}.mop {f} || exit 1; fi; ')
        elif state == "A":
            # Снять именно наш (метка): чужой не трогаем. Отказ снятия --
            # отказ отката (review-3 п.3), не «exit 0 поверх живого хука».
            parts.append(f'if [ -f {f} ]; then if ! {_ours(chr(34) + f + chr(34))}; '
                         f'then echo "hook {h} is not ours anymore, kept"; '
                         f'elif ! rm -f {f}; then '
                         f'echo "hook {h} removal failed"; exit 1; fi; fi; ')
    # Сверка с снимком (review-3 п.3): пара -- как в снимке (отсутствие --
    # код 1 пустым stderr), созданное -- снято, воссозданное -- теми байтами
    # и режимом. Недостоверное чтение -- тоже отказ. Временный файл $t и
    # trap уже создал каталог-блок _hooks_var.
    parts.append('c=ok; ')
    for key in ("name", "email"):
        flag, value = snap["identity"][key]
        want = q(value) if flag == "P" else None
        parts.append(
            f'e=$({git} user.{key} 2>"$t"); r=$?; '
            + (f'if [ $r -ne 0 ] || [ "$e" != {want} ]; then c=bad; fi; '
               if flag == "P" else
               f'if [ $r -ne 1 ] || [ -s "$t" ] || [ -n "$e" ]; then c=bad; fi; '))
    for h in GUARDED_HOOKS:
        v = h.replace("-", "_")
        f = f"$hooks/{h}"
        state, mode, body = snap["hooks"][h]
        if state == "O":
            b64 = base64.b64encode(body.encode()).decode()
            parts.append(f'[ -f {f} ] || c=bad; '
                         f'[ "$(stat -c %a {f})" = {q(mode)} ] || c=bad; '
                         f'[ "$(base64 -w0 {f})" = {q(b64)} ] || c=bad; ')
        elif state == "A":
            parts.append(f'[ -f {f} ] && c=bad; ')
    parts.append('[ "$c" = ok ] || { echo "the prior state is not confirmed '
                 'restored"; exit 1; }; exit 0')
    return "".join(parts)


def identity_script(clone, path, mine, profile):
    """Скрипт: identity владельца в клон (repo-local) или снять её. Чистая.

    Только пока в файле аренды наша запись (как _unclaim, #189): пока шла
    доставка и вопрос серверу, аренду мог забрать другой, и его identity
    перетирать нельзя.

    Прежнее состояние печатается ДО первой записи кодированными метками
    mop574-* (review-2 п.3/4): присутствие -- флагом (P/A/X), значение --
    base64: literal '-' валиден и не сентинел, многострочные значения не
    вытекают в диагностику. Чтение различает «ключа нет» (git rc 1, пустой
    вывод) от «не прочиталось» (rc 1 непустой stderr): второе -- X, и
    установка отказывает, не «восстанавливая» неизвестное.

    Установка с компенсацией (review-2 п.2): точка отказа возвращает ТОЧНЫЕ
    bytes/режим/присутствие каждого затронутого элемента (свой хук -- телом
    из b64, не одним chmod) и подтверждает возврат сверкой; код 1 --
    «отказала, прежнее вернулось», код 2 -- «прежнее НЕ вернулось»: тогда
    аренду прежнему владельцу не отдают, обе причины едут мастеру. Снятие
    отсутствующего ключа (код 5) -- успех, отказ записи (config.lock) --
    ошибка до доставки.

    Вместе с identity -- страж владельца (#313), хуки GUARDED_HOOKS. Чужой
    хук (без метки) не перезаписывается; core.hooksPath задан -- git
    .git/hooks не читает, и ставить туда незачем. Оба пропуска -- строкой в
    вывод: её несёт заметка мастеру. Снятие identity снимает только свои."""
    q = shlex.quote
    guard = f'[ "$(cat {q(path)} 2>/dev/null)" = {q(mine.rstrip(chr(10)))} ] || exit 0; '
    git = f"git -C {q(clone)} config --local"
    # Каталог хуков: любое иное определение, кроме rc 0 с непустым ответом,
    # -- неизвестный каталог (review-5), отказ ДО записи и ДО доставки, не
    # «отсутствие хуков»: stderr-текст не документ отсутствия.
    hooks_assign, hooks_gate = _hooks_var(clone)
    # Чтение с различением исходов (review-3 п.1): отсутствие -- ТОЛЬКО
    # документированный код 1 геттера с пустым stderr; любой другой
    # ненулевой код (молчаливый 128 в том числе) -- операционный отказ (X):
    # пустой stderr не доказательство отсутствия. Значение сразу в base64-
    # переменную: назад пишем её же. Временный файл $t и trap уже создал
    # каталог-блок _hooks_var: файл живёт до конца -- его же читает сверка.
    read = "".join(
        f'e_{k}=$({git} user.{k} 2>"$t"); r_{k}=$?; '
        f'if [ $r_{k} -eq 1 ] && [ ! -s "$t" ]; then '
        f'f_{k}=A; v_{k}=; printf \'mop574-{k}\\tA\\n\'; '
        f'elif [ $r_{k} -ne 0 ]; then f_{k}=X; v_{k}=; '
        f'printf \'mop574-{k}\\tX\\n\'; '
        f'else f_{k}=P; v_{k}=$(printf %s "$e_{k}" | base64 -w0); '
        f'printf \'mop574-{k}\\tP%s\\n\' "$v_{k}"; fi; '
        for k in ("name", "email"))
    v = [h.replace("-", "_") for h in GUARDED_HOOKS]
    # Операционный отказ чтения (X) -- ДО любой записи: прежнее состояние
    # неизвестно, «восстанавливать пустоту» нельзя, установка не начинается.
    # Сюда же -- X снимка хуков (review-3 п.1): отказ stat/base64 -- не
    # «пустые режим и тело», а нечитанный элемент. Ворота -- ПОСЛЕ всех
    # обязательных чтений (read и snap_hooks ставят f_*/st_*; ворота ДО
    # snap_hooks смотрели несуществующие переменные и пропускали X,
    # review-5 п.2) и ДО первой записи (set_new).
    gate_x = ('if [ "$f_name" = X ] || [ "$f_email" = X ]; then '
              'echo "git config read failed: the prior identity is unknown"; '
              'exit 1; fi; '
              + "".join(
                  f'if [ "$st_{x}" = X ]; then '
                  f'echo "hook snapshot read failed: '
                  f'the prior state is unknown"; exit 1; fi; '
                  for x in v))
    # Снимок своих хуков: A/F/O, у O -- режим и тело (b64-переменная);
    # исходные байты хранит переменная, компенсация пишет её же. Отказ
    # чтения (stat/base64) -- X, не «O с пустотой» (review-3 п.1).
    snap_hooks = "".join(
        f'f_{x}="$hooks/{h}"; '
        f'if [ -e "$f_{x}" ]; then '
        f'if {_ours(chr(34) + "$f_" + x + chr(34))}; then '
        f'm_{x}=$(stat -c %a "$f_{x}"); r1_{x}=$?; '
        f'b_{x}=$(base64 -w0 "$f_{x}" 2>/dev/null); r2_{x}=$?; '
        f'if [ $r1_{x} -eq 0 ] && [ $r2_{x} -eq 0 ]; then st_{x}=O; '
        f'printf \'mop574-hook-{h}\\tO\\t%s\\t%s\\n\' "$m_{x}" "$b_{x}"; '
        f'else st_{x}=X; printf \'mop574-hook-{h}\\tX\\n\'; fi; '
        f'else st_{x}=F; printf \'mop574-hook-{h}\\tF\\n\'; fi; '
        f'else st_{x}=A; printf \'mop574-hook-{h}\\tA\\n\'; fi; '
        for h, x in zip(GUARDED_HOOKS, v))
    # Компенсация с подтверждением (review-2 п.2): вернуть точные
    # bytes/mode/presence, сверить с напечатанным снимком; исходы 1/2.
    # Сверка -- с той же дисциплиной чтения (review-3 п.1): «отсутствует»
    # доказывает только rc 1 с пустым stderr, «равно прежнему» -- только
    # rc 0; иной код геттера -- не подтверждение, c=bad.
    back = "".join(
        f'{git} --unset-all user.{k} || :; '
        f'[ "$f_{k}" = P ] && {git} user.{k} "$e_{k}" || :; '
        for k in ("name", "email"))
    back += "".join(
        f'[ "$st_{x}" = A ] && [ -f "$f_{x}" ] '
        f'&& {_ours(chr(34) + "$f_" + x + chr(34))} && rm -f "$f_{x}" || :; '
        f'[ "$st_{x}" = O ] && printf %s "$b_{x}" | base64 -d > "$f_{x}.c" '
        f'&& chmod "$m_{x}" "$f_{x}.c" && mv -f "$f_{x}.c" "$f_{x}" || :; '
        for x in v)
    verify = (
        'c=ok; '
        + "".join(
            f'q_{k}=$({git} user.{k} 2>"$t"); qr_{k}=$?; '
            f'if [ "$f_{k}" = A ] && {{ [ $qr_{k} -ne 1 ] || [ -s "$t" ]; }}; '
            f'then c=bad; fi; '
            f'[ "$f_{k}" = P ] && {{ [ $qr_{k} -ne 0 ] '
            f'|| [ "$q_{k}" != "$e_{k}" ]; }} && c=bad; '
            for k in ("name", "email"))
        + "".join(
            f'[ "$st_{x}" = A ] && [ -f "$f_{x}" ] && c=bad; '
            f'[ "$st_{x}" = O ] && {{ [ -f "$f_{x}" ] || c=bad; '
            f'[ "$(stat -c %a "$f_{x}")" = "$m_{x}" ] || c=bad; '
            f'[ "$(base64 -w0 "$f_{x}")" = "$b_{x}" ] || c=bad; }}; '
            for x in v)
        + '[ "$c" = ok ] && exit 1 || exit 2; ')
    if profile:
        set_new = (f"{git} user.name {q(profile['name'])} && "
                   f"{git} user.email {q(profile['email'])} "
                   f"|| {{ {back}{verify}exit 1; }}; ")
        manage = 'mkdir -p "$hooks"; ' + "".join(
            f'if [ -e "$f_{x}" ] && ! {_ours(chr(34) + "$f_" + x + chr(34))}; then '
            f'echo "owner guard skipped: the project has its own {h}"; else '
            f'printf %s {q(owner_hook())} > "$f_{x}.mop" && chmod 755 "$f_{x}.mop" && '
            f'mv -f "$f_{x}.mop" "$f_{x}" || {{ {back}{verify}exit 1; }}; fi; '
            for h, x in zip(GUARDED_HOOKS, v))
    else:
        set_new = _unset(git, "name", back + verify) + _unset(git, "email", back + verify)
        manage = "".join(
            f'[ -f "$f_{x}" ] && {_ours(chr(34) + "$f_" + x + chr(34))} '
            f'&& rm -f "$f_{x}"; ' for x in v)
    skip = (f'if [ -n "$(git -C {q(clone)} config core.hooksPath)" ]; then '
            f'echo "owner guard skipped: core.hooksPath is set"; '
            f'else {manage}fi; ')
    return (guard + hooks_assign + hooks_gate + read + snap_hooks + gate_x
            + set_new + skip + "exit 0")


async def _install_identity(conn, name, login, undo):
    """Identity клона -- за проверенным владельцем, ДО доставки (#574).
    -> (заметка|None, причина|None, снимок|None, компенсация безопасна?).

    Зовут под замком папета вслед за _claim: захват, identity, доставка --
    и только тогда чужой send. Отказ -- ДО доставки: задание не уходит под
    неверной identity. Коды шелла (review-2 п.2): 0 -- установлено; 1 --
    установка отказала, компенсация подтверждена сверкой (аренду можно
    откатывать, причина едет мастеру без строк снимка); 2 -- установка
    отказала, прежнее состояние НЕ восстановлено: аренда остаётся за
    пославшим, и обе причины -- отказ и несостоявшийся возврат -- обязаны
    дойти до мастера, «восстановлено» не говорится. Снимок не читан -- к
    отказу по п.3: прежнее состояние неизвестно, доставлять под ним нельзя."""
    profile, missing = await owner_profile(conn, name, login)
    path, _owner, mine, _generation = undo  # поколение -- только откату (#573)
    out, code = await bsh(name, identity_script(clone_dir(name), path, mine, profile))
    snap = _snapshot(out)
    if code == 2:
        return (None,
                f"git identity not set: {_why_marked(out, code)}; "
                "the prior state is not confirmed restored",
                None, False)
    if code not in (0, 1):
        # Код вне контракта (0/1/2) -- исход неизвестен: компенсация могла
        # начаться и оборваться, прежнее состояние не подтверждено. Аренда
        # остаётся за пославшим -- как при транспортно-неопределённом исходе
        # доставки: «восстановлено» не говорится.
        return (None,
                f"git identity not set: {_why_marked(out, code)}; "
                "the install shell ended outside its contract, the prior state "
                "is unconfirmed",
                None, False)
    if code != 0:
        # Код 1: точка отказа откатила применённое внутри шелла и сверила
        # возврат (review-2 п.2); здесь -- только причина, без прежней пары.
        return None, f"git identity not set: {_why_marked(out, code)}", None, True
    if snap["identity"] is None or snap["hooks"] is None:
        # Код 0 без ПОЛНОГО снимка (review-3 п.2): установка реально
        # исполнилась (identity уже New), но кадры потеряны или неполны --
        # прежнее состояние неизвестно, и код 0 НЕ доказывает компенсацию,
        # которой не было. Вслепую вернуть аренду прежнему владельцу --
        # оставить клон под чужой парой: контекст держим за пославшим,
        # причину -- с именем снимка, разбирать оператору.
        return (None,
                "git identity not set: the prior state snapshot is "
                "unreadable, the lease stays with the sender",
                None, False)
    if profile:
        # Пропуск стража (#313) -- тем же текстом, что сказал скрипт.
        skipped = "; ".join(l.strip() for l in out.splitlines() if "skipped" in l)
        return (f"commits as {profile['name']} <{profile['email']}>"
                + (f"; {skipped}" if skipped else ""), None, snap, True)
    return (f"no git identity for {login} ({missing}): the clone has none, "
            f"commit with git -c", None, snap, True)


async def _prepare(conn, name, req):
    """Захват аренды и identity владельца ДО доставки (#574). -> (отказ |
    None, откат | None, заметка | None). Зовут под замком папета.

    Захват -> identity -> передача: под одним замком, окно -- вопрос
    профилю (IDENTITY_WAIT) и шелл установки. Ошибка подготовки -- отказ ДО
    доставки: задание не уходит под неверной identity; компенсация
    подтвердилась -- откат захвата едет с той же причиной (#181), не
    подтвердилась -- аренда остаётся за пославшим, «восстановлено» не
    говорится (review-2, п.2)."""
    me, verified = lease.caller(req)
    refused, undo, note = await _claim(name, req)
    if refused:
        return refused, None, None
    if undo and verified:
        inote, failed, snap, safe = await _install_identity(conn, name, me, undo)
        if failed:
            if safe:
                undone = await _unclaim(name, undo)
                if undone:
                    failed = f"{failed}; owner not restored: {undone}"
            return f"{name}: {failed}", None, None
        note = "; ".join(filter(None, (note, inote)))
        # Снимок в реестр: прежняя пара и свои хуки тем же шеллом
        # установки, до записи новой.
        _lease_claims.setdefault(undo[0], {})[undo[3]][3] = snap
    return None, undo, note


async def initial_identity(conn, name, owner, inc):
    """Первая аренда и identity хозяина в теле роли ДО первого хода модели
    (docs/SOUL.md, шов для #590). -> {resumed | owner_note} либо {error}.
    Успех ставит маркер инкарнации.

    Та же транзакция, что у dispatch (#574): замок папета, захват, снимок,
    установка, откат при провале. owner -- хозяин-человек из резолвера,
    не из тела. Резюм доказывает маркер ЭТОЙ инкарнации, не аренда:
    клон могли снести и поднять заново тем же хозяином, и свежее тело без
    маркера обязано пройти полный путь, а не «аренда та же -- значит
    готово». Маркер жив -- возобновление: ни аренду, ни git не трогаем, их
    мог законно переставить поздний dispatch. Маркера нет, а аренда
    чужая -- отказ: работа в клоне не наша."""
    async with _owner_locks.setdefault(name, asyncio.Lock()):
        marker, mwhy = await _read_marker(name)
        if marker is None:
            if mwhy and "absent" not in mwhy:
                return {"error": mwhy}
            clone = CloneFacts.from_dict(await clone_facts(name))
            held = clone and clone.owner and clone.owner.user
            if held:
                return {"error": f"{name}: lease is held by {held}, not {owner}"}
            refused, undo, note = await _prepare(conn, name, {"_caller": owner})
            if refused:
                return {"error": refused}
            if undo:
                _settle(undo[0], undo[3])
        else:
            if marker["inc"] != inc:
                return {"error": f"{name}: the body carries incarnation "
                                 f"{marker['inc']}, this registration is {inc}"}
            note = None
        failed = await _write_marker(name, inc)
        if failed:
            return {"error": failed}
    return {"resumed": True} if note is None and marker else \
        ({"owner_note": note} if note else {})


async def v_send(conn, req):
    """Сообщение в сессию папета. -> {msg_id} либо {error}.

    `notify=true` не блокирует ответ: подписку на простой держит фоновая
    задача здесь, на узле, рядом с сокетом, и, дождавшись, публикует в инбокс
    мастера. Отсюда push без опроса — и без потока внутри MCP-сервера, который
    раньше ждал простоя, сидя в аллокации.

    Порядок #574: захват -> identity -> передача, и ВСЕ ТРИ под одним
    замком (review-2, п.1): проверка файла в шелле -- check-then-use, между
    cat-сравнением и session.py владение успевало уйти, и сообщение
    доставлялось под чужой identity. Взаимное исключение -- сам замок
    папета: session.py send уходит с --wait 0 и потолком 20с, окно --
    короткое и ограниченное; ожидание простоя (до IDLE_WAIT) -- ВНЕ замка,
    отдельным глаголом session.py wait-idle, и ошибка ожидания -- не
    недоставка уже доставленного. Гонки #573/#189 остаются инвариантами
    координации реестра -- последовательность захватов теперь сериализована
    самим замком, и откат чужой записи поверх невозможен конструктивно."""
    name = req["name"]
    wait = min(max(int(req.get("wait") or 0), 0), IDLE_WAIT)
    # Актёр роли (#584): обычный send -- сообщение соседу, без аренды и без
    # identity; аренду берёт только dispatch, решённый субъектом и
    # резолвером в handle (_dispatch), а не телом. Аренда и identity
    # dispatch -- хозяина-человека из резолвера (_caller), как у человека.
    actor = req.get("_actor")
    leased = not actor or req.get("_dispatch") is True
    out = {}
    async with _owner_locks.setdefault(name, asyncio.Lock()):
        refused, undo, note = await _prepare(conn, name, req) if leased \
            else (None, None, None)
        if refused:
            return {"error": refused}
        # Цель — клон, а не сокет: session.py резолвит сессию сам, внутри
        # тела, где только и лежат её файлы. Снаружи сокет контейнерного
        # папета не виден. Передача -- под тем же замком: bounded (--wait 0,
        # потолок 20с), чужой send не вклинивается между подготовкой и
        # стартом тела.
        out = await session_json(name, _session_cmd(
            "send", clone_dir(name), req["message"],
            "--priority", req.get("priority", "next"),
            "--from-name", req.get("from_name", "mop"),
            "--wait", 0), timeout=20)
        if out.get("error"):
            indeterminate = out.get("timeout")
            # Откат -- в том же замке (мы в нём и стоим, #189): между
            # сравнением и записью чужой claim не вклинится. Предчека
            # поколения нет: провал обязан попасть в реестр исходов (#573)
            # даже тогда, когда записи отката не будет -- чужой живой захват
            # поверх не отменяет того, что наш захват провалился.
            failed = None
            if undo and not indeterminate:
                failed = await _restore_identity_and_lease(name, undo)
            if failed:
                out["error"] += f"; owner not restored: {failed}"
            return out
        # Доставка доехала -- откат этому захвату больше не приедет:
        # завершаем его и держим реестр контекстом возможных откатов, без
        # истории завершённых отправок (#573).
        if undo:
            _settle(undo[0], undo[3])
    if note:
        out["owner_note"] = note
    # Простой ждём ВНЕ замка (#574): подписка -- control-кадр, сессию не
    # занимает, и чужой send не стоит за нашей 600-секундной паузой. Ошибка
    # ожидания -- не недоставка: msg_id стоит, откатов нет.
    if wait:
        r = await session_json(name, _session_cmd(
            "wait-idle", clone_dir(name), wait, name[-8:]), timeout=wait + 20)
        if r.get("error"):
            out["idle"] = None
            out["idle_error"] = r["error"]
        else:
            out["idle"] = r.get("state")
    # Аудит -- фактический актёр и его хозяин из резолвера, не поле тела.
    await _event(conn, "send", name, text=f"from {req.get('from_name', 'mop')}",
                 **({"actor": actor, "owner": req.get("_caller"),
                     "dispatch": req.get("_dispatch") is True} if actor else {}))
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
    (mop server gc), здесь только цифра.

    Меряет драйвер: у host хранилище тел — это $HOME со всеми клонами и
    target-каталогами, а на гипервизоре в $HOME не лежит ни одного папета, и
    `df $HOME` там отвечал бы про совершенно постороннюю файловую систему.
    Молча: число выглядит правдоподобно, а `mop server gc` принимает по нему решение
    о сносе."""
    return await DRIVER.capacity()


# Дисковый сторож узла (#358): скрипт кладёт на узел deploy (роль bus), его
# пороги -- узловые настройки из node.env. Зовёт его doctor (по расписанию
# -- таймер mop-doctor на сервере, #359; periodic pu-cleanup снят, #361):
# исполнение остаётся здесь, узел знает локальное -- tmux-сессии, клоны, тела.
SWEEP = "/usr/local/bin/pu-sweep"
SWEEP_KNOBS = ("MOP_SWEEP_FREE_MIN_GB", "MOP_SWEEP_MAX_TARGET", "MOP_SWEEP_STALE_DAYS")
# Последняя строка pu-sweep -- для машины, всё выше -- для человека.
_SWEEP_TOTALS = re.compile(r"pu_sweep_freed_kb=(\d+) pu_sweep_free_gb=(\d+)")
# Ярус 0 (тела-контейнеры) итоговой строки не даёт: сколько места вернёт снос
# тела, с узла не видно. Зато видно, сколько тел уйдёт, -- этой строкой
# заканчивает `mop driver sweep`, и без неё doctor не отличил бы узел с
# брошенными телами от чистого (#363).
SWEEP_BODIES = "pu_sweep_bodies"
_SWEEP_BODIES = re.compile(SWEEP_BODIES + r"=(\d+)")
# Одно подметание на узел за раз: два doctor подряд иначе мели бы одни и те
# же пути наперегонки.
_sweep_lock = asyncio.Lock()


def sweep_report(rc, out, err, min_gb):
    """Вывод pu-sweep -> {freed_kb, free_gb, min_gb, warnings, bodies} | {error}.
    Чистая функция (tests/agent.py).

    Итоговой строки нет у узла с телами-контейнерами (уходит после яруса 0)
    и у отказа сторожа (ecryptfs без монтирования): тогда цифры None --
    «не знаю», а не ноль. bodies -- сколько брошенных тел снесёт (снёс)
    ярус 0; у host-узла и у отказа яруса 0 -- None. Предупреждения -- строки
    stderr с «!»: ими скрипт говорит об отказах внутри подметания."""
    if rc != 0:
        last = (err.strip() or out.strip()).splitlines()[-1:] or ["no output"]
        return {"error": f"pu-sweep exit {rc}: {last[0].strip()}"}
    m = _SWEEP_TOTALS.search(out)
    b = _SWEEP_BODIES.search(out)
    return {"freed_kb": int(m[1]) if m else None,
            "free_gb": int(m[2]) if m else None,
            "min_gb": min_gb,
            "warnings": [ln.strip()[1:].strip() for ln in err.splitlines()
                         if ln.strip().startswith("!")],
            "bodies": int(b[1]) if b else None}


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
        # Со сроком (#373): зависший pu-sweep держал замок до рестарта юнита,
        # и каждый следующий sweep получал «already running». exec -- чтобы
        # убит по сроку был сам pu-sweep, а не обёрточный sh: dash не
        # подменяет себя последней командой, и сирота жил бы дальше.
        timeout = float(config.get("MOP_SWEEP_TIMEOUT"))
        out, err, rc = await driver.sh(f"exec {shlex.quote(SWEEP)}", timeout,
                                       env=env, split=True)
    if rc is None:
        return {"error": f"pu-sweep did not finish in {config.get('MOP_SWEEP_TIMEOUT')}s"}
    return sweep_report(rc, out, err, int(config.get("MOP_SWEEP_FREE_MIN_GB")))


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


def write_targets(project, requested, live, owners, is_container,
                  puppet_home=None):
    """Куда писать файлы глагола `write` (#312, #387). Чистая.
    -> (тела, писать ли копию узла, отказ|None, названные, но не живые).

    Меток аренды больше нет (#387): write без адреса пишет копию узла и
    живые тела без оговорок -- узел -- одного сервера, и «чужой кред» в
    теле оказаться не может. project -- проект субъекта (admin --
    оператор); requested -- `bodies` запроса либо None; live -- живые тела
    (у host -- папеты узла); owners -- {тело: проект}.

    Адресная запись: только названные тела, каждое -- папет проекта
    просящего (admin -- любое); копия узла -- только у host, где тело и
    есть узел. Без адреса: admin -- копия узла и все живые тела, проект --
    свои живые тела, у host -- свой узел, когда на нём только свои."""
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
    if admin:
        return (list(live) if is_container else []), True, None, []
    if is_container:
        return [n for n in live if owners.get(n) == project], False, None, []
    alone = all(owners.get(n) == project for n in live)
    return [], alone, None, []


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
    requested = req.get("bodies")
    project = req.get("_project")
    live = await DRIVER.bodies()
    named = [n for n in dict.fromkeys(list(live) + list(requested or []))
             if driver.valid_name(n)]
    owners = {} if project == bus.ADMIN else dict(zip(named, await asyncio.gather(
        *(puppet_project(n) for n in named))))
    bodies, node_copy, refused, absent = write_targets(
        project, requested, live, owners, driver.separate_bodies(DRIVER))
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
    failed = {}
    for name, r in zip(bodies, answers):
        if r.get("error"):
            # Отказ по одному телу не отменяет остальных.
            failed[name] = r["error"]
            written.append(f"{name} FAILED — {r['error']}")
        else:
            written += [f"{name}:{p}" for p in r.get("written") or []]
    written += [f"{name} NOT LIVE" for name in absent]
    # Отказы тел -- полями (#366): по ним судят bus.results_from и реестр
    # кредитов; строки в written остаются для старых клиентов, разбирать
    # их текст нельзя -- правка формулировки молча сделала бы отказ успехом.
    return {"written": written, "failed": failed, "absent": list(absent)}


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
    #
    # Факты -- целиком (#372): CloneFacts.to_dict() с def/home/owner. Урезанный
    # {dirty, ahead, cur} терял дом клона, и правило «клон не на своей ветке
    # -- работа» (#266, #272) у уборки не срабатывало никогда.
    work = {}
    for n in names:
        c = await clone_facts(n)
        if c:
            work[n] = c
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
# `write` из NODE убран. С #387 список белый список -- один secrets.env, и
# его везёт только сервис кластера (#391): узлам незачем принимать файлы от
# чужих сессий.
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


# ─── делегированный актёр (#584) ─────────────────────────────────────────
# Роль входит своим принципалом grant-<id> (docs/SOUL.md): rpc.<grant-id>
# для глаголов node_verbs и dispatch.<id> -- для диспетчера. ACL шины
# пускает в субъект только этот принципал, но его права на глагол, на цель
# и сама живость привязки -- у сервера: узел спрашивает резолвер
# bootstrap-сервиса (server.rpc, узловой кред) перед любым эффектом, каждый
# раз, без кэша. Молчание, отказ, ответ не о том -- отказ: отозванная
# привязка не действует и при живом соединении.
DISPATCH = "dispatch"     # глагол в теле dispatch-субъекта и в резолвере
# Канал обычного RPC актёра -- busnames.ROLE_CHANNEL (#603): локальная
# копия убрана, расхождение ловил бы только живой забор.
# Поля тела, которыми легаси-путь называет власть: у актёра власть -- его
# субъект и резолвер, тело их не пишет.
ACTOR_STRIPPED = ("owner", "holder", "force")


def actor_of(subject):
    """Субъект -> (канал, принципал | None). role.<grant-id> -- актёр
    обычного глагола (#603), dispatch.<id> -- диспетчер; токен канала без
    правильного принципала -- канал без актёра (отказ в handle). Логин
    человека принципалом не бывает: префикс grant- зарезервирован, и
    принципал на старом rpc-пути -- не человек и не актёр: проволочный
    забор, чтобы старый агент смешанной раскатки не исполнил глагол без
    резолвера. Канал подъёма -- не субъект актёра: глагол один, и его
    держит узловой кред тела, а не принципал (#590)."""
    tokens = (subject or "").split(".")
    channel = tokens[4] if tokens[2:3] == ["node"] and len(tokens) >= 5 else None
    if channel == busnames.DISPATCH_CHANNEL:
        # Полный принципал grant-<id> (#603): голый hex на проводе не
        # появляется, и токен субъекта -- уже готовое имя.
        user = tokens[5] if len(tokens) > 5 else ""
        return DISPATCH, (user if soul.is_grant(user) else None)
    if channel == busnames.ROLE_CHANNEL:
        user = tokens[5] if len(tokens) > 5 else ""
        return busnames.ROLE_CHANNEL, (user if soul.is_grant(user) else None)
    if channel == "startup":
        return "startup", None
    # Принципал на человеческом rpc-пути: субъект роли есть, этот -- не
    # его. #603 закрыл caller() для гранта на rpc, и узел читает забор по
    # последнему токену субъекта тем же правилом, что и шина: принципал
    # там -- не человек и не актёр, до резолвера отказ (смешанная раскатка
    # не исполняет глагол без резолвера).
    tail = tokens[5] if len(tokens) > 5 else ""
    if soul.is_grant(tail):
        return "fenced", tail
    return "rpc", None


def source_dispatch(got):
    """Право источника на dispatch из ответа резолвера: source_dispatch
    либо capabilities.dispatch; иного вида нет -- None, то есть отказ."""
    if "source_dispatch" in got:
        return got["source_dispatch"]
    caps = got.get("capabilities")
    return caps.get("dispatch") if isinstance(caps, dict) else None


def actor_verdict(got, principal, project, verb, target=None):
    """Ответ резолвера -> причина отказа | None. Чистая: решает узел, а не
    вера в то, что сервер ответил о том, о чём спросили.

    Пускает только явное allow о том же принципале в том же проекте, с
    хозяином-человеком; названная цель -- найдена, та же и в том же
    проекте; dispatch -- источник с dispatch и цель с receives_dispatch
    (право цели -- отдельная проверка от свободы клона, её решает аренда)."""
    if not isinstance(got, dict):
        return f"the resolver answered {type(got).__name__}, not a verdict"
    if got.get("allow") is not True:
        return f"refused by the server: {got.get('reason') or got.get('error') or 'not allowed'}"
    if got.get("principal") != principal:
        return f"the resolver answered about {got.get('principal')}, not {principal}"
    if got.get("project") != project:
        return f"principal {principal} is not in project {project}"
    for key, what in (("owner", "human owner"), ("job", "job")):
        if not (isinstance(got.get(key), str) and got[key]):
            return f"principal {principal} has no {what}"
    if target is not None:
        t = got.get("target")
        if not (isinstance(t, dict) and t.get("found") is True):
            return f"puppet {target} has no binding"
        if t.get("job") != target:
            return f"the resolver answered about {t.get('job')}, not {target}"
        if t.get("project") != project:
            return foreign(target, project)
    if verb == DISPATCH:
        if source_dispatch(got) is not True:
            return f"role {got.get('role')} does not dispatch"
        if target is None or got["target"].get("receives_dispatch") is not True:
            return f"puppet {target} does not receive dispatch"
    return None


def resolver_subject(conn, project):
    """Субъект вопроса к резолверу сервера -- узловой квалифицированный
    (#581): анонимный server.rpc сервисный маршрут не пускает. Узел --
    из кредов ОТКРЫТОГО соединения (conn.creds, #603): тот же снимок, что
    рукопожатию; дефолтный config() на узле пуст или чужой. -> субъект
    | None (кред не узловой -- спрашивать нечем)."""
    creds = getattr(conn, "creds", None)
    user = creds().get("user") if callable(creds) else \
        (creds or {}).get("user") if isinstance(creds, dict) else None
    if isinstance(user, str) and user.startswith(busnames.NODE_PREFIX):
        return busnames.server(project, caller=busnames.node_user(
            user[len(busnames.NODE_PREFIX):]))
    return None


async def resolve_actor(conn, project, principal, verb, target=None):
    """Принципал, глагол, цель -> (ответ резолвера | None, отказ | None).

    Свежий запрос к bootstrap-сервису проекта узловым кредом: ни Nomad,
    ни каталога ролей узел не держит. Проект -- субъектом запроса, не
    полем тела: сервер берёт его оттуда. Принципал -- полным именем
    grant-<id> на обоих каналах. Общий шов для подъёма роли (#590)."""
    fields = {"principal": principal, "service": "node", "verb_name": verb}
    if target is not None:
        fields["target"] = target
    subj = resolver_subject(conn, project)
    if subj is None:
        return None, "no node credentials to ask the resolver"
    try:
        msg = await bus.arequest(conn, subj,
                                 bus.envelope("resolve", **fields), RESOLVE_WAIT)
        got = json.loads(msg.data.decode())
    except Exception as e:
        return None, f"the resolver did not answer ({type(e).__name__})"
    why = actor_verdict(got, principal, project, verb, target)
    return (None, why) if why else (got, None)


async def target_binding(conn, project, name, soul_inc=None):
    """Привязка цели из реестра сервера. -> (цель | None, причина | None).

    Свежий вопрос без принципала (target-вид резолвера): тот же сервис,
    тот же узловой кред. Инкарнация в вопросе -- контракт by_job #581:
    точная запись приходит С состоянием (неактивная, отозванная), и
    отвергает её вызывающий; без инкарнации ответ -- только живая.
    Проверяем и форму тройки: битый digest или инкарнация -- не
    готовность, а причина; «примерно та» привязка прав не даёт. Кэш --
    только у читающего глагола (_soul_fact), мутации зовут свежо."""
    fields = {"service": "node", "target": name}
    if soul_inc is not None:
        fields["soul_inc"] = soul_inc
    subj = resolver_subject(conn, project)
    if subj is None:
        return None, "no node credentials to ask the resolver"
    try:
        msg = await bus.arequest(conn, subj,
                                 bus.envelope("resolve", **fields), RESOLVE_WAIT)
        got = json.loads(msg.data.decode())
    except Exception as e:
        return None, f"the resolver did not answer ({type(e).__name__})"
    if not isinstance(got, dict) or got.get("found") is not True:
        why = got.get("reason") if isinstance(got, dict) else None
        if soul_inc is not None and isinstance(got, dict) \
                and (got.get("revoked_at") or got.get("active") is False):
            return None, (f"binding of {name} {soul_inc} is revoked"
                          if got.get("revoked_at") else
                          f"binding of {name} {soul_inc} is not active")
        if why:
            return None, f"{name}: {why}"
        return None, f"puppet {name} has no binding"
    if soul_inc is not None and got.get("revoked_at"):
        return None, f"binding of {name} {soul_inc} is revoked"
    if soul_inc is not None and got.get("active") is False:
        return None, f"binding of {name} {soul_inc} is not active"
    if got.get("job") != name:
        return None, f"the resolver answered about {got.get('job')}, not {name}"
    if got.get("project") != project:
        return None, foreign(name, project)
    if not (isinstance(got.get("owner"), str) and got["owner"]):
        return None, f"puppet {name} has no human owner"
    if not (isinstance(got.get("role"), str) and soul.NAME.match(got["role"])):
        return None, f"puppet {name} has a malformed role {got.get('role')!r}"
    if not (isinstance(got.get("soul_digest"), str)
            and soul.HEX64.match(got["soul_digest"])):
        return None, f"puppet {name} has a malformed soul digest"
    import uuid as _uuid
    inc = got.get("soul_inc")
    try:
        canonical = str(_uuid.UUID(inc)) == inc if isinstance(inc, str) else False
    except ValueError:
        canonical = False
    if not canonical:
        return None, f"puppet {name} has a malformed soul incarnation"
    return got, None


# Кэш soul-факта ростера (#584): (проект, имя, инкарнация, sessionId) ->
# (годен до, факт). Ключ -- проверенная инкарнация и идентификатор живой
# сессии из структурной записи state: переиспользованное имя и новая
# сессия не наследуют готовность прежней. Только читающие глаголы и
# только на TTL: мутации резолвер спрашивают свежо.
_soul_cache = {}


async def _read_marker(name):
    """Маркер identity инкарнации. -> (словарь | None, причина | None).

    Маркер пишет агент канала подъёма под замком папета, в .git/ -- рядом
    с файлом аренды: клон остаётся чистым для git. Маркер доказывает
    только то, что агент сам поставил identity этой инкарнации, и ничью
    готовность сам по себе не доказывает (готовность -- ready-файл 590).
    Битый и нечитаемый маркер -- причина, не готовность."""
    out, code = await bsh(
        name, f'cat {shlex.quote(os.path.join(clone_dir(name), ".git", MARK))} 2>/dev/null')
    if not (out or "").strip():
        # Нет файла (rc 1) и пустой вывод -- маркера нет; таймаут и мусор
        # вместо кода -- нечитаемо: «нет» и «не знаю» -- разные ответы.
        if code in (1, 0, None):
            return None, f"{name}: the identity marker is absent"
        return None, f"{name}: the identity marker is unreadable"
    try:
        got = json.loads(out.strip().splitlines()[-1])
    except ValueError:
        return None, f"{name}: the identity marker is malformed"
    if not (isinstance(got, dict) and isinstance(got.get("inc"), str) and got["inc"]):
        return None, f"{name}: the identity marker carries no incarnation"
    return got, None


async def _write_marker(name, inc):
    """Маркер identity этой инкарнации: {"inc": ...} в .git/, как файл
    аренды. Пишет агент под замком папета."""
    path = os.path.join(clone_dir(name), ".git", MARK)
    body = json.dumps({"inc": inc})
    out, code = await bsh(name, f"printf %s {shlex.quote(body)} > {shlex.quote(path)}")
    return None if code == 0 else f"{name}: the identity marker is not recorded: {why(out, code)}"




def soul_dir(name, inc):
    """Каталог инкарнации роли в теле -- опубликованный helper #603
    (driver.soul_dir): формула та же, что была здесь локальной копией;
    узел читает каталог, который пишет лаунчер #590."""
    return driver.soul_dir(name, inc)


async def _read_ready(name, inc):
    """Доказательство готовности 590/583 -> (словарь | None, причина |
    None). Обёртка пишет его последним и отдельным файлом в каталог
    инкарнации (souls/<name>/<inc>/) после проверки бандла, до старта
    модели; узел читает и сверяет, но не создаёт: готовность -- факт
    обёртки, узел её лишь предъявляет. Битое -- не готовность."""
    path = os.path.join(soul_dir(name, inc), READY)
    try:
        got = json.loads(open(path).read() or "null")
    except (OSError, ValueError):
        return None, f"{name}: the ready proof is unreadable or absent"
    if not isinstance(got, dict):
        return None, f"{name}: the ready proof is malformed"
    return got, None


def _session_live(fact):
    """Живая сессия и её идентификатор -> (жива, id | None).

    Только структурная запись state (#222/#468): alive === True,
    listen === True и непустой sessionId -- его session.py даёт лишь
    слушающей живой сессии. Строка «<status> <alive> <listen>» статусы
    не перечисляет (idle/busy/waiting/shell -- всё живые), записи нет --
    fail closed: неугаданное состояние не готовность, и никакой роли не
    позволено «примерно живой» быть."""
    st = fact.get("state")
    if not isinstance(st, dict):
        return False, None
    sid = st.get("sessionId")
    if st.get("alive") is not True or st.get("listen") is not True \
            or not (isinstance(sid, str) and sid):
        return False, None
    return True, sid


async def _soul_fact(conn, project, name, fact):
    """Тройка роли для ростера. -> факт либо None (нет живой сессии -- нет
    и факта). Готовность -- рантайм-доказательство, а не факт регистрации:
    проверенная привязка И живая сессия И маркер этой инкарнации, что
    агент поставил под замком. Чего-то нет -- не готовность, а причина.

    Отказ привязки -- не отказ глагола: состояние папета -- факт узла,
    тройка роли -- знание сервера; незнание уезжает битым фактом с
    причиной, а ростер остаётся цел."""
    live, sid = _session_live(fact)
    if not fact.get("present") or not live:
        return None
    key = (project, name, sid)
    hit = _soul_cache.get(key)
    if hit and hit[0] > time.time():
        return hit[1]
    got, why = await target_binding(conn, project, name)
    if got is None:
        soul_fact = {"ready": False, "why": why}
    else:
        marker, mwhy = await _read_marker(name)
        ready, rwhy = (await _read_ready(name, got["soul_inc"])) if marker is not None \
            and marker["inc"] == got["soul_inc"] else (None, None)
        if marker is None:
            soul_fact = {"ready": False, "why": mwhy}
        elif marker["inc"] != got["soul_inc"]:
            soul_fact = {"ready": False,
                         "why": f"{name}: the identity marker names incarnation "
                                f"{marker['inc']}, the binding names {got['soul_inc']}"}
        elif ready is None:
            soul_fact = {"ready": False, "why": rwhy}
        elif ready.get("role") != got["role"] or ready.get("soul_inc") != got["soul_inc"] \
                or ready.get("soul_digest") != got["soul_digest"]:
            soul_fact = {"ready": False,
                         "why": f"{name}: the ready proof names another soul "
                                f"({ready.get('role')}, {ready.get('soul_inc')}, "
                                f"{ready.get('soul_digest')})"}
        else:
            soul_fact = {"ready": True, "role": got["role"],
                         "soul_inc": got["soul_inc"], "soul_digest": got["soul_digest"]}
    _soul_cache[(project, name, sid)] = (time.time() + SOUL_FACT_TTL, soul_fact)
    return soul_fact


async def v_startup(conn, req):
    """Глагол канала подъёма: первая аренда и identity до первого хода
    модели (#590 зовёт обёрткой; субъект -- узловой кред тела). Хозяин и
    тройка -- из привязки сервера, тело называет джоб и точную инкарнацию:
    расхождение (переиспользованное имя, старая инкарнация) -- отказ.
    Цель -- джоб этого узла: канал подъёма не обслуживает соседей.
    Тройка -- только успешному ответу: ошибка не ходит с готовностью."""
    name = req.get("name")
    if not driver.valid_name(name):
        return {"error": driver.bad_name(name)}
    if not await local_puppet(name):
        return {"error": f"{name}: not a job of this node"}
    got, why = await target_binding(conn, req["_project"], name,
                                    soul_inc=req.get("soul_inc"))
    if got is None:
        return {"error": why}
    if req.get("soul_inc") != got["soul_inc"]:
        return {"error": f"{name}: this body is incarnation {got['soul_inc']}, "
                         f"the request names {req.get('soul_inc')!r}"}
    out = await initial_identity(conn, name, got["owner"], got["soul_inc"])
    if not out.get("error"):
        # Расписка identity -- ВЕРХНЕГО УРОВНЯ (#590): identity_ready +
        # тройка роли, не вложенный soul.ready (тот публикует отдельный
        # факт узла после живого доказательства, _soul_fact). Лаунчер
        # сверяет её с Meta до метки готовности.
        out.update(identity_ready=True, role=got["role"],
                   soul_inc=got["soul_inc"], soul_digest=got["soul_digest"])
    return out


async def local_puppet(name):
    """Джоб этого узла? Канал подъёма -- факт того же узла, и соседние
    тела не его дело. Но свежая роль ещё НЕ в списках драйвера/tmux:
    подъём происходит до первого процесса в теле, и по сокетам свежий
    старт выглядел бы чужим. Членство -- по клональному факту: клон
    поднял лаунчер ДО identity (#247); нет клона -- нет и джоба."""
    clone = await clone_facts(name)
    if clone and clone.get("origin"):
        return True
    return name in [n for n in await DRIVER.bodies() if n == name]


async def _admit_actor(conn, req, actor, channel):
    """Права актёра на запрос. -> отказ | None; пустил -- в req каркасные
    поля актёра. Глагол, который проверили, и есть тот, что исполнят:
    dispatch-субъект исполняет только dispatch (как send с арендой),
    rpc-субъект -- только свой глагол, «dispatch» телом -- не глагол."""
    verb = req.get("verb")
    if channel == DISPATCH:
        if verb != DISPATCH:
            return f"subject {DISPATCH} carries verb {DISPATCH} only, not {verb}"
        req["verb"] = verb = "send"
    why = refusal(verb, False, req["_project"])
    if why:
        return why
    named = VERBS[verb].named
    if named and not await _mine(req, req.get("name") or ""):
        return foreign(req.get("name"), req["_project"])
    got, why = await resolve_actor(conn, req["_project"], actor,
                                   DISPATCH if channel == DISPATCH else verb,
                                   req.get("name") if named else None)
    if why:
        return why
    for key in ACTOR_STRIPPED:
        req.pop(key, None)
    # Власть -- хозяин-человек из резолвера (_caller): аренда, ворота (#40)
    # и git-подпись те же, что у него самого; два мастера одного человека --
    # один человек, как по прежней политике. Актёр -- отдельно, для аудита.
    # from-name -- джоб актёра: стабильный адрес папета для отчётов.
    req.update(_caller=got["owner"], _actor=actor, _job=got["job"],
               _dispatch=channel == DISPATCH, from_name=got["job"])
    return None


# ─── петля ───────────────────────────────────────────────────────────────
async def handle(conn, msg, public, startup=False):
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
    # Кто просит (#207) -- из субъекта, поверх тела: каркасные ключи ('_')
    # тело не пишет, все до одного (service.trusted, #580). Публичный
    # канал -- папетов: владельца он не несёт, и названный в теле не в счёт.
    channel, actor = (None, None) if public else actor_of(msg.subject)
    req = service.trusted(req, None if public else busnames.caller(msg.subject))
    # Одно правило на всех (#173): mop.<проект>.node.<узел>.<канал>.
    req["_project"] = service.project_from_subject(msg.subject)
    if public:
        req.pop("owner", None)

    verb = req.get("verb")
    if startup:
        # Канал подъёма (#584): узловой кред тела, глагол один, и никакой
        # другой субъект его не отдаёт.
        why = (None if verb == STARTUP
               else f"the startup channel carries {STARTUP} only, not {verb}")
    elif verb == STARTUP:
        why = f"verb {STARTUP} is available to the wrapper's startup channel only"
    elif channel == "fenced":
        why = f"delegated principals use the {busnames.ROLE_CHANNEL} subject, not the human rpc path"
    elif actor:
        why = await _admit_actor(conn, req, actor, channel)
        verb = req.get("verb")
    elif channel in (busnames.ROLE_CHANNEL, DISPATCH):
        # Канал есть, актёра нет: токен не принципал (или его нет вовсе) --
        # отказ до таблицы и до легаси-человека, не проваливаясь в него.
        why = f"the {channel} subject needs a delegated principal token"
    else:
        why = refusal(verb, public, req["_project"])
    if why is None and not actor and not startup and VERBS[verb].named \
            and not await _mine(req, req.get("name") or ""):
        # Главная проверка проектирования. Прав NATS тут мало: мастер проекта A
        # законно пишет в свой субъект, но может назвать папета из B.
        why = foreign(req.get("name"), req["_project"])
    if why:
        out = {"error": why}
    else:
        try:
            out = await (v_startup(conn, req) if startup
                         else VERBS[verb].fn(conn, req))
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

    async def on_startup(msg):
        asyncio.create_task(handle(conn, msg, public=False, startup=True))

    subs = busnames.agent_subscriptions(node)
    # dispatch (#584): свой субъект с принципалом актёра, права узла его
    # покрывают (natsconf.node_permissions: node.<узел>.>). Маска роли --
    # опубликованный помощник #603 (node_role, «role.>»); маски dispatch у
    # #603 нет -- точечные субъекты строит dispatch_subject (полный
    # принципал), а маску подписки собираем из его же констант.
    dispatch_mask = (f"{busnames.ROOT}.{busnames.ANY}.node.{node}."
                     f"{busnames.DISPATCH_CHANNEL}.{busnames.ANY}")
    subs["rpc"] = subs["rpc"] + [dispatch_mask,
                                 busnames.node_role(busnames.ANY, node),
                                 busnames.node(busnames.ANY, node, "startup")]
    for subj in subs["rpc"][:-1]:
        await conn.subscribe(subj, cb=on_rpc)
    await conn.subscribe(subs["rpc"][-1], cb=on_startup)
    for subj in subs["msg"]:
        await conn.subscribe(subj, cb=on_msg)
    await _event(conn, "up", text=f"agent on {node}")
    return subs["rpc"] + subs["msg"]

