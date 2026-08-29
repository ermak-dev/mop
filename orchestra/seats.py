"""Сиденья пула: спека job'а, LLM-профили и достоверное состояние места.

Модуль ВОЗВРАЩАЕТ ДАННЫЕ и ничего не печатает. Форматирование живёт во
фронтендах (bin/player печатает таблицы, bin/orchestra-mcp отдаёт то же самое
модели) — иначе второй фронтенд неизбежно начал бы разбирать чужой текст.
"""
import base64
import os
import re

from . import nomad, remote

MEM = 8192                          # бюджет плеера, МБ (на Linux-узлах cgroup-лимит ЖЁСТКИЙ)
HOME = "/home/ermak"                # $HOME на узлах пула
SSH_ALIAS = {"gamer": "gamer-wsl"}  # имя узла nomad -> ssh-алиас
# Куда переводить сиденье, у которого кончилась квота текущей модели
# (решение оператора 27.08: Fable → Opus).
FALLBACK_MODEL = "opus"
JOB_PREFIX = "wk-"

# ─── LLM-профили ─────────────────────────────────────────────────────────
# Сиденье — всегда claude code; профиль меняет ровно одно: КУДА он ходит за
# токенами. Anthropic-совместимый эндпоинт провайдера (ANTHROPIC_BASE_URL),
# ключ (ANTHROPIC_AUTH_TOKEN) и карта имён моделей opus/sonnet/haiku в модели
# провайдера — больше в сиденье ничего не меняется, поэтому tmux, tail, doctor
# и детект залипаний работают одинаково для любого профиля.
#
# "key" — ИМЯ переменной в ~/.ssh/ai-provider-keys.env (локальный источник
# правды по ключам). Сам ключ в спеку джоба НЕ кладём: она видна в UI Nomad и
# остаётся в его состоянии. Вместо этого раздаём на узлы файл
# ~/.config/orchestra/llm-keys.env (только с теми ключами, которые называет хоть
# один профиль), а врапер уже на узле подставляет нужный в сессию.
LLM_PROFILES = {
    # штатный Claude: авторизация — логин claude.ai (player login), env пустой
    "claude": {"key": None, "env": {}},
    # z.ai GLM coding plan, https://docs.z.ai/devpack/tool/claude
    "glm": {
        "key": "Z_AI_KEY",
        "auth_var": "ANTHROPIC_AUTH_TOKEN",
        "env": {
            "ANTHROPIC_BASE_URL": "https://api.z.ai/api/anthropic",
            # [1m] — окно контекста модели, claude срезает суффикс перед
            # запросом (проверено на z.ai). Без него claude считает незнакомую
            # модель 200-килотокенной и жмёт auto-compact вчетверо раньше,
            # чем нужно: у glm-5.3 контекст 1M.
            "ANTHROPIC_DEFAULT_OPUS_MODEL": "glm-5.3[1m]",
            "ANTHROPIC_DEFAULT_SONNET_MODEL": "glm-5.3[1m]",
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": "glm-5.3-flash",
            # длинные ходы у GLM легко перебивают дефолтный таймаут клиента
            "API_TIMEOUT_MS": "3000000",
        },
    },
}
DEFAULT_LLM = "claude"
LOCAL_KEYS_FILE = "~/.ssh/ai-provider-keys.env"        # источник ключей на этой машине
LLM_KEYS_FILE = f"{HOME}/.config/orchestra/llm-keys.env"  # копия на узле пула

# Врапер — собственно задача Nomad: довести узел до "клон есть, claude в
# tmux" и жить, пока жива tmux-сессия. Смерть врапера = рестарт/переезд
# плеера силами Nomad; на новом узле врапер сам разворачивает всё заново.
WRAPPER = r"""
set -e
d="$HOME/wk/$WK_NAME"
if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin)" != "$WK_ORIGIN" ]; then
    rm -rf "$d"
fi
if [ ! -d "$d/.git" ]; then
    mkdir -p "$HOME/wk"
    git clone -q "$WK_ORIGIN" "$d"
fi
for f in "$HOME/wk-env/$WK_PROJECT"/.env* "$HOME/wk-env/$WK_PROJECT"/.providers; do
    [ -e "$f" ] && cp -a "$f" "$d/" || true
done
mkdir -p "$HOME/.claude"
# ~/.claude.json and ~/.claude/settings.json are NODE-level: every seat on the
# host edits the same two files, and seats boot together after a node restart.
# Read-modify-write from N processes through one shared temp path truncated the
# config to 0 bytes three times (2026-08-26/27/28), which parks every claude on
# the "invalid JSON" prompt and reads as a hung pool. So: one lock for the whole
# edit, a temp file per seat, and a repair pass for whatever a previous race
# (or a hard VM kill) left behind.
edit_json() {  # <file> <jq-program> [jq-args...]
    local file="$1" program="$2"; shift 2
    local tmp="$file.$WK_NAME.tmp"
    exec 9>"$file.lock"
    flock 9
    # `jq empty` is NOT a validity check: a zero-byte file is an empty jq input
    # stream, so it exits 0, every filter over it yields nothing, and the 0-byte
    # config got written straight back (gamer, 2026-08-28 — all six seats parked
    # on the config prompt while `player list` showed only "ЗАВИС"). Demand an
    # actual object, and never install an empty result.
    jq -e 'type == "object"' "$file" >/dev/null 2>&1 || echo '{}' > "$file"
    if jq "$@" "$program" "$file" > "$tmp" && [ -s "$tmp" ]; then
        mv "$tmp" "$file"
    else
        rm -f "$tmp"
    fi
    flock -u 9
    exec 9>&-
}
[ -f "$HOME/.claude.json" ] || echo '{}' > "$HOME/.claude.json"
# A seat has no human to answer the first-run wizard: without these keys every
# claude sits on the theme prompt, which also reads as a hung seat.
edit_json "$HOME/.claude.json" '.projects[$d].hasTrustDialogAccepted = true
    | .hasCompletedOnboarding = true
    | .theme = (.theme // "dark")' --arg d "$d"
# Desktop-automation MCP servers: win-mcp (Windows host pwate) and mac-mcp (mac
# host), so a seat can drive and verify the GUI clients. HTTP transport, so any
# node that can resolve/reach the hosts gets them; a node that cannot just sees
# the server fail to connect.
# windows-mcp runs on the Windows host (gamer) bound to 0.0.0.0:8000. Off-host
# seats reach it by the host's LAN IP; a seat running IN the gamer host's own
# mirrored-mode WSL shares that LAN IP, where the Windows bind is only reachable
# via loopback - so it must use 127.0.0.1 instead.
if ip -4 -o addr show 2>/dev/null | grep -q '192.168.1.151'; then
    WINURL="http://127.0.0.1:8000/mcp"
else
    WINURL="http://192.168.1.151:8000/mcp"
fi
edit_json "$HOME/.claude.json" '.mcpServers["windows-mcp"] = {"type":"http","url":$winurl,"headers":{"Authorization":"Bearer kCgqRS33Yxv4lrSlqV5w6b3qNz0shjj9u4HTInk2DW0"}}
  | .mcpServers["mac-mcp"] = {"type":"http","url":"http://mac:8000/mcp","headers":{"Authorization":"Bearer FeRM5I-lQr_3mJbq1PWG6KzErm-666SA8b2vVwlBj8U"}}
  | .mcpServers["playwright"] = {"type":"stdio","command":"npx","args":["-y","@playwright/mcp@latest","--headless","--isolated","--browser","chromium"]}
  | .mcpServers["orchestra"] = {"type":"stdio","command":"/home/ermak/orchestra/bin/orchestra-mcp"}' \
    --arg winurl "$WINURL"
# playwright-mcp needs a browser on the node; install is idempotent and cached
# in ~/.cache/ms-playwright, so every seat boot just confirms it is there.
npx -y playwright install chromium >/dev/null 2>&1 || true
# Same repair as above, and for the same reason: an existing-but-unreadable
# file (0 bytes after a torn write) made jq fail silently, so the bypass-mode
# prompt came back and every seat stopped on it.
[ -f "$HOME/.claude/settings.json" ] || echo '{}' > "$HOME/.claude/settings.json"
edit_json "$HOME/.claude/settings.json" '.skipDangerousModePermissionPrompt = true'
# CARGO_TARGET_DIR grows without bound - 22 to 49 GB per seat in practice, and
# five of them filled the gamer node's disk on 2026-08-26, which killed the WSL
# VM and stranded every allocation on it. Boot is the only safe moment to drop
# one: nothing is building yet, and the cache is pure derived data.
TARGET_DIR="$HOME/.cache/target-$WK_NAME"
if [ -d "$TARGET_DIR" ] \
    && [ "$(du -sm "$TARGET_DIR" 2>/dev/null | cut -f1 || echo 0)" -gt 30000 ]; then
    rm -rf "$TARGET_DIR"
fi
# LLM-профиль сиденья: набор переменных для tmux -e. Статическая часть
# (эндпоинт и карта моделей) приезжает в WK_LLM_ENV из спеки джоба, а КЛЮЧ —
# только с узла: в спеке джоба секретам не место (её видно в UI Nomad).
llm_env=()
if [ -n "$WK_LLM_ENV" ]; then
    while IFS= read -r kv; do
        [ -n "$kv" ] && llm_env+=(-e "$kv")
    done <<< "$(printf '%s' "$WK_LLM_ENV" | base64 -d)"
fi
if [ -n "$WK_LLM_KEY_VAR" ]; then
    keyfile="$HOME/.config/orchestra/llm-keys.env"
    key=""
    # sed, а не source: файл с ключами не исполняем
    # Двойной доллар — экранирование интерполяции Nomad: спеку задачи он
    # прогоняет через hcl2 и всякую фигурную подстановку пытается вычислить
    # сам. Неэкранированная подстановка имени переменной ниже, и особенно
    # раскрытие массива llm_env, валят РЕГИСТРАЦИЮ джоба на "Invalid
    # expression" ещё до запуска: [@] для HCL не выражение. Осторожно, это
    # правило действует и на комментарии — Nomad разбирает всю строку.
    [ -f "$keyfile" ] && key=$(sed -n "s/^$${WK_LLM_KEY_VAR}=//p" "$keyfile" | tail -1)
    if [ -z "$key" ]; then
        # Валимся громко: без ключа claude поднимется и будет отбивать каждый
        # ход 401-й, а сиденье будет читаться как живое и свободное.
        echo "LLM-профиль $WK_LLM: на узле нет ключа $WK_LLM_KEY_VAR в $keyfile — раздай: player login" >&2
        exit 1
    fi
    llm_env+=(-e "$WK_LLM_AUTH_VAR=$key")
fi

# a dedicated tmux SERVER per worker (-L): with the default server every
# session on the node lives in the cgroup of whichever wrapper started the
# server first, and one task budget OOM-kills all workers at once
tmux -L "$WK_NAME" kill-session -t "$WK_NAME" 2>/dev/null || true
# env must go through -e: a plain env prefix only reaches the tmux SERVER when
# this wrapper happens to start it, and every later session inherits the first
# wrapper's variables (all workers ended up sharing one CARGO_TARGET_DIR)
tmux -L "$WK_NAME" new-session -d -s "$WK_NAME" -c "$d" \
    -e CARGO_TARGET_DIR="$HOME/.cache/target-$WK_NAME" \
    -e CARGO_BUILD_JOBS=1 \
    -e PATH="$d/bin:$PATH" \
    "$${llm_env[@]}" \
    "$HOME/.local/bin/claude --dangerously-skip-permissions"
trap 'tmux -L "$WK_NAME" kill-session -t "$WK_NAME" 2>/dev/null; exit 0' TERM INT
while tmux -L "$WK_NAME" has-session -t "$WK_NAME" 2>/dev/null; do sleep 10 & wait $!; done
"""


def clone_dir(name):
    return f"{HOME}/wk/{name}"


def job_spec(name, origin, llm=DEFAULT_LLM):
    project = os.path.basename(origin).removesuffix(".git")
    prof = LLM_PROFILES[llm]
    llm_env = "".join(f"{k}={v}\n" for k, v in prof["env"].items())
    return {"Job": {
        "ID": name,
        "Name": name,
        "Datacenters": [nomad.POOL_DC],
        "Type": "service",
        "Meta": {"origin": origin, "llm": llm},
        "TaskGroups": [{
            "Name": "wk",
            "Count": 1,
            "RestartPolicy": {
                "Attempts": 3,
                "Interval": 1800 * 10**9,
                "Delay": 15 * 10**9,
                "Mode": "delay",
            },
            "Tasks": [{
                "Name": nomad.TASK,
                "Driver": "raw_exec",
                "User": "ermak",
                "Config": {"command": "/bin/bash", "args": ["-c", WRAPPER]},
                "Env": {
                    "WK_NAME": name,
                    "WK_ORIGIN": origin,
                    "WK_PROJECT": project,
                    "HOME": HOME,
                    "PATH": f"/usr/local/bin:/usr/bin:/bin:{HOME}/.local/bin:{HOME}/.cargo/bin:{HOME}/.nvm/versions/node/v22.12.0/bin",
                    # LLM-профиль: имена и эндпоинт — здесь, ключ — на узле
                    "WK_LLM": llm,
                    "WK_LLM_ENV": base64.b64encode(llm_env.encode()).decode(),
                    "WK_LLM_KEY_VAR": prof.get("key") or "",
                    "WK_LLM_AUTH_VAR": prof.get("auth_var") or "ANTHROPIC_AUTH_TOKEN",
                },
                "Resources": {"CPU": 1000, "MemoryMB": MEM},
                "KillTimeout": 15 * 10**9,
            }],
        }],
    }}


def jobs():
    """Джобы плееров. Префикс wk- ловит и wk-cleanup с его периодическими
    детьми; плееры — те, что врапер пометил origin'ом."""
    listing = nomad.client().jobs.get_jobs(prefix=JOB_PREFIX, meta=True)
    return [j for j in listing if "origin" in (j.get("Meta") or {})]


def next_name(project):
    taken = {j["ID"] for j in nomad.client().jobs.get_jobs(prefix=f"{JOB_PREFIX}{project}-")}
    n = 1
    while f"{JOB_PREFIX}{project}-{n}" in taken:
        n += 1
    return f"{JOB_PREFIX}{project}-{n}"


# ─── пробы клона ─────────────────────────────────────────────────────────
def _kv(out):
    """Ответ вида k=v построчно -> словарь. Форма ответа у всех проб одна."""
    return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)


def clone_branch(alloc, name):
    """Ветки клона: (текущая, дефолтная); (None, <причина>) если недоступно."""
    d = clone_dir(name)
    try:
        out, _ = nomad.sh(alloc,
            f'echo "cur=$(git -C {d} branch --show-current 2>/dev/null)"; '
            f'echo "def=$(git -C {d} rev-parse --abbrev-ref origin/HEAD 2>/dev/null)"')
    except Exception:
        return None, "exec недоступен"
    kv = _kv(out)
    if "def" not in kv:
        return None, "exec недоступен"
    if not kv["def"]:
        return None, "клона ещё нет"
    return kv["cur"] or "(detached)", kv["def"].rsplit("/", 1)[-1]


def clone_holds_work(alloc, name):
    """Есть ли в клоне что терять: ((не закоммичено, не отправлено), ветка)
    или (None, причина).

    Занятость места — это НЕ имя ветки. Имя врёт в обе стороны: у cloudpub
    origin/HEAD = dev, а работают на beta3.0, и чистое запушенное сиденье
    читалось как «занят: beta3.0»; наоборот, место с умершим мид-таском
    агентом сидит на bug/NNNN, и если решать по одной лишь активности сессии,
    оно читается свободным (wk-rugent-4 на bug/1063 попал в list как
    «свободен» — PM, восстановившийся по одному только list, задиспатчил бы
    поверх чужого дерева).

    Единственный вопрос, который на самом деле задаёт PM перед диспатчем:
    пропадёт ли что-нибудь, если занять это место. Пропадает только то, чего
    нет ни на одной удалённой ветке.

    Считаем через `--not --remotes`, а НЕ через `@{u}..`: upstream у рабочей
    ветки бывает прибит к origin/master, и тогда всё, что ещё не влито, врёт
    как «не отправлено». Так и вышло на wk-rugent-7: коммит 0eaeae15 лежал на
    origin/bug/1048 и был влит в master мержем 4b663ce1, а `@{u}..` показывал
    единицу и место читалось занятым.

    Оговорка: remote-tracking ref'ы в клоне могут быть протухшими (на том же
    wk-rugent-7 последний fetch отставал на двое суток). Fetch тут не делаем —
    это сетевая операция на каждое место в каждом list; в сомнительном случае
    смотреть глазами через tail.

    Возвращаем ДВА числа раздельно, а не сумму: «не закоммичено» и «не
    отправлено» — разные состояния и разный разговор с агентом. Сложив их в
    одно «только локально: 3», PM сказал переродившемуся месту «у тебя было
    3 локальных коммита», тогда как там лежали 3 НЕсохранённых файла и ноль
    коммитов.
    """
    d = clone_dir(name)
    try:
        out, _ = nomad.sh(alloc,
            f'cd {d} 2>/dev/null || exit 0; '
            f'echo "cur=$(git branch --show-current 2>/dev/null)"; '
            f'echo "dirty=$(git status --porcelain 2>/dev/null | wc -l)"; '
            f'echo "ahead=$(git rev-list --count HEAD --not --remotes 2>/dev/null)"')
    except Exception:
        return None, "exec недоступен"
    kv = _kv(out)
    if "dirty" not in kv:
        return None, "exec недоступен"
    try:
        dirty, ahead = int(kv.get("dirty") or 0), int(kv.get("ahead") or 0)
    except ValueError:
        return None, "нечитаемый ответ git"
    return (dirty, ahead), kv.get("cur") or "(detached)"


# ─── состояние сессии ────────────────────────────────────────────────────
# КАК УЗНАТЬ РЕАЛЬНОЕ СОСТОЯНИЕ СИДЕНЬЯ (файл сессии + сокет)
#
# Сиденье само ведёт две вещи, которые и есть источник правды о его состоянии
# (обе появляются независимо от моста claude.ai):
#
# 1) ФАЙЛ АКТИВНОСТИ ~/.claude/sessions/<pid>.json: cwd (по нему матчим — он
#    стабилен, в отличие от name), status (idle|busy|requires_action|waiting|
#    offline), pid, messagingSocketPath. Слабость: после грязной смерти
#    процесса файл остаётся с протухшим status.
# 2) СОКЕТ ЖИВОСТИ: успешный connect -> процесс жив и слушает. Строже файла:
#    ловит смерть мгновенно, без всяких таймстампов.
#
# Итог: сокет = живость, файл = активность.
#   нет коннекта + pid мёртв ....... offline (файл протух, не верим)
#   нет коннекта + pid жив ......... hung    (сессия рушится/подвисла)
#   коннект есть + idle ............ свободен
#   коннект есть + busy ............ занят
#   коннект есть + requires_action . требует действия
#   коннект есть + waiting ......... ждёт ввода
# Нет файла (старый claude / нет python3) -> None, и вызывающий откатывается
# на прежнюю tmux-эвристику. Детект протухшего логина остаётся на tmux — в
# файле он не виден.
#
# Пробник и знание о формате файла живут в orchestra/session.py и уезжают на
# узел исходником: сокет сиденья host-local, снаружи к нему не подключиться.
SESSION_STATES = ("idle", "busy", "requires_action", "waiting", "offline")


def session_status(alloc, name):
    """idle|busy|requires_action|waiting|offline|hung, либо None если файла
    сессии нет."""
    try:
        line = remote.probe(alloc, clone_dir(name))
    except Exception:
        return None
    if not line or line == "none":
        return None
    parts = line.split()
    if len(parts) < 3:
        return None
    status, alive, listen = parts[0], parts[1] == "1", parts[2] == "1"
    if not listen and not alive:
        return "offline"
    if not listen and alive:
        return "hung"
    return status if status in SESSION_STATES else None


def _responds(alloc):
    """Отвечает ли аллокация вообще. -> причина зависания или None."""
    try:
        out, code = nomad.sh(alloc, "timeout 3s echo 'test' 2>/dev/null")
    except Exception as e:
        return f"ЗАВИС ({str(e)[:30]})"
    if code != 0 or not out.strip():
        return "ЗАВИС (не отвечает)"
    return None


def screen(alloc, name, lines=10):
    """Последние непустые строки tmux-пейна сиденья."""
    out, _ = nomad.sh(alloc,
        f"tmux -L {name} capture-pane -t {name} -p -S - | grep -v '^$' | tail -{lines}")
    return out


def _screen_complaint(activity):
    """Жалоба, видимая только на экране. -> (вид, текст) или None.

    Оба случая — один класс: сессия жива, отвечает на ping'и, но ни одного хода
    выдать не может. Для PM это неотличимо от молчания.
    """
    low = activity.lower()
    # Логин. Варианты экрана: "Not logged in · Run /login", "Login expired ·
    # Please run /login". Проверять до скоринга: у залипшего мид-таск в буфере
    # полно рабочих слов.
    #
    # Строки про Remote Control отсюда убраны вместе с самим --remote-control:
    # сиденья больше не ходят на мост claude.ai, и "/rc failed" на их экране
    # означало бы что угодно, только не болезнь. Для профилей с ключом
    # провайдера (glm) логин claude.ai вообще не при делах.
    if "not logged in" in low or "login expired" in low:
        return "login", ("не залогинен" if "not logged in" in low else "логин протух")
    # Квота модели: каждое входящее сообщение мгновенно возвращает "You're out
    # of usage credits…" и сессия падает обратно в idle. Рестарт НЕ лечит —
    # нужен либо другой /model, либо пополнение.
    #
    # Жалоба живёт в скроллбэке и после лечения, поэтому считается актуальной
    # только если ПОСЛЕ неё модель не переключали: иначе вылеченное сиденье
    # вечно читалось бы как больное.
    if ("out of usage credits" in low
            and low.rfind("out of usage credits") > low.rfind("set model to")):
        m = re.search(r"keep using ([^\s]+(?: [0-9.]+)?)", activity, re.I)
        return "quota", (f"нет квоты модели: {m.group(1)}" if m else "нет квоты модели")
    return None


def _state_from_session(alloc, name, st):
    """Состояние места по достоверному статусу сессии.

    Файл сессии авторитетен про АКТИВНОСТЬ, но не про ЗАНЯТОСТЬ МЕСТА: клон
    переживает смерть сессии. Поэтому у idle спрашиваем клон — пропадёт ли
    что-нибудь, если занять место."""
    if st == "requires_action":
        return "требует действия"
    if st == "waiting":
        return "ждёт ввода"
    if st in ("offline", "hung"):
        return "ЗАВИС (не отвечает)"
    if st == "busy":
        cur, default = clone_branch(alloc, name)
        return f"занят: {cur}" if (cur and cur != default) else "занят"

    held, cur = clone_holds_work(alloc, name)
    if held is None:
        return "свободен"
    dirty, ahead = held
    if dirty or ahead:
        what = ", ".join(p for p in (f"не закоммичено: {dirty}" if dirty else "",
                                     f"не отправлено: {ahead}" if ahead else "") if p)
        return f"занят: {cur} ({what})"
    # Чисто и всё на origin — терять нечего, место переиспользуемо. Ветку
    # показываем справочно: диспатч всё равно обязан начать с переключения на
    # интеграционную ветку, иначе новая ветка тикета уедет от оставшейся здесь.
    return f"свободен ({cur})" if cur else "свободен"


def _tmux_guess(activity):
    """Древний скоринг по словам в буфере. Работает только там, где файла
    сессии нет (старый claude / нет python3 на узле). -> строка или None."""
    low = activity.lower()
    work = sum(1 for x in ("working", "herding", "garnishing", "finding",
                           "checking", "running") if x in low)
    idle = sum(1 for x in ("резерв", "reserve", "waiting", "idle",
                           "свободен", "await") if x in low)
    if work > idle:
        return "занят"
    if idle > work:
        return "свободен"
    return None


def _state_from_branch(alloc, name):
    """Последний откат: одна лишь ветка клона."""
    cur, default = clone_branch(alloc, name)
    if cur is None:
        return default
    return "свободен" if cur == default else f"занят: {cur}"


def seat_state(alloc, name):
    """Занятость места: реальное состояние сессии, с откатами на tmux и ветку."""
    dead = _responds(alloc)
    if dead:
        return dead

    try:
        activity = screen(alloc, name)
        if not activity.strip():
            return "свободен"

        complaint = _screen_complaint(activity)
        if complaint:
            kind, text = complaint
            if kind != "login":
                return text
            cur, default = clone_branch(alloc, name)
            return f"{text}: {cur}" if cur and cur != default else text

        st = session_status(alloc, name)
        if st is not None:
            return _state_from_session(alloc, name, st)

        guess = _tmux_guess(activity)
        if guess:
            return guess
    except Exception:
        pass  # любой сбой проб -> откат на ветку

    return _state_from_branch(alloc, name)


# ─── сводки для фронтендов ───────────────────────────────────────────────
def seat_rows():
    """Сиденья как ДАННЫЕ: [{name, node, alloc_status, state, llm, origin}]."""
    rows = []
    for j in jobs():
        meta = j.get("Meta") or {}
        row = {"name": j["ID"], "node": "-", "alloc_status": j.get("Status", "?"),
               "state": "-", "llm": meta.get("llm", DEFAULT_LLM),
               "origin": meta.get("origin", "?")}
        try:
            a = nomad.latest_alloc(j["ID"])
            if a:
                row["node"] = a["NodeName"]
                row["alloc_status"] = a["ClientStatus"]
                if a["ClientStatus"] == "running":
                    row["state"] = seat_state(a, j["ID"])
        except Exception as e:
            row["alloc_status"] = nomad.describe_error(e)
        rows.append(row)
    return rows


def pool():
    """Узлы как ДАННЫЕ: [{name, status, free_mb, total_mb, slots, error}]."""
    out = []
    for n in nomad.client().nodes.get_nodes():
        if n["Status"] != "ready":
            out.append({"name": n["Name"], "status": n["Status"]})
            continue
        try:
            free, total = nomad.node_capacity(n)
            out.append({"name": n["Name"], "status": "ready", "free_mb": free,
                        "total_mb": total, "slots": free // MEM})
        except Exception as e:
            out.append({"name": n["Name"], "status": "ready",
                        "error": nomad.describe_error(e)})
    return out


# ─── диагностика ─────────────────────────────────────────────────────────
# Категории и лечение:
#   залип/не отвечает      -> restart аллокации (клон и ветка переживают)
#   не залогинен/протух    -> раздать креды на пул, затем restart сиденья
#   pending/failed/lost    -> alloc stop: Nomad пересоздаёт сразу, минуя
#                             restart-backoff (до 30 мин)
#   нет квоты модели       -> печать /model в пейн; рестарт квоту не вернёт
#   queued без аллокации   -> мест в пуле нет, лечится не отсюда
def diagnose():
    """Проблемы пула как ДАННЫЕ: [{name, alloc, diagnosis, action}]."""
    issues = []
    for j in sorted(jobs(), key=lambda j: j["ID"]):
        a = nomad.latest_alloc(j["ID"])
        if not a or a["ClientStatus"] in ("lost", "unknown", "failed", "pending"):
            issues.append(_placement_issue(j, a))
            continue
        state = seat_state(a, j["ID"])
        action = _action_for(state)
        if action is not False:
            issues.append({"name": j["ID"], "alloc": a, "diagnosis": state,
                           "action": action})
    return issues


def _placement_issue(job, alloc):
    name = job["ID"]
    if alloc and alloc["ClientStatus"] in ("pending", "failed"):
        return {"name": name, "alloc": alloc, "action": "stop",
                "diagnosis": f"аллок {alloc['ClientStatus']} (restart-backoff?)"}
    if alloc:
        return {"name": name, "alloc": alloc, "action": "stop",
                "diagnosis": f"аллок {alloc['ClientStatus']}"}
    queued = (job.get("JobSummary", {}).get("Summary", {}).get("wk") or {}).get("Queued", 0)
    if queued:
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": "queued — нет свободных слотов в пуле"}
    return {"name": name, "alloc": None, "action": None, "diagnosis": "нет аллокации"}


def _action_for(state):
    """Лечение для состояния. False — состояние здоровое, проблемы нет."""
    if state.startswith("ЗАВИС"):
        return "restart"
    # Сессия жива, но упёрлась в запрос действия и сама не сдвинется.
    # "ждёт ввода" (waiting) НЕ трогаем: это бывает и нормальным межходовым
    # состоянием, автолечить его опасно — только показываем.
    if state == "требует действия":
        return "restart"
    if state.startswith(("не залогинен", "логин протух")):
        return "login+restart"
    if state.startswith("нет квоты модели"):
        return "model"
    return False


def type_command(alloc, name, command):
    """Напечатать команду в tmux-пейн сиденья и вернуть экран после неё.

    Печатью, а не сообщением по каналу: слэш-команды через канал не проходят
    (сообщение кладётся в очередь с skipSlashCommands), а у сиденья с
    исчерпанной квотой любой ход падает, не начавшись — слэш-команду же
    исполняет сам TUI, ход на неё не тратится.

    Перед вводом чистим строку (C-u): в пейне мог остаться недобитый текст,
    и тогда команда склеилась бы с ним в мусор."""
    return _tmux(alloc, name,
        f"tmux -L {name} send-keys -t {name} C-u; sleep 0.3; "
        f"tmux -L {name} send-keys -t {name} '{command}'; sleep 0.3; "
        f"tmux -L {name} send-keys -t {name} Enter; sleep 2; ")


def press_enter(alloc, name):
    """Подтвердить диалог. Только увидев его: слепой Enter на сиденье без
    диалога отправил бы пустой ход."""
    return _tmux(alloc, name, f"tmux -L {name} send-keys -t {name} Enter; sleep 2; ")


def _tmux(alloc, name, script):
    out, code = nomad.sh(alloc, script + f"tmux -L {name} capture-pane -p -t {name}")
    if code not in (0, None):
        raise RuntimeError(out.strip() or f"tmux exit {code}")
    return out


def switch_model(alloc, name, model):
    """Перевести сиденье на другую модель, напечатав /model в его tmux-пейн.

    Именно печатью, а не сообщением по каналу: обработка входящего сообщения —
    это ход, а ход у сиденья с исчерпанной квотой падает, не начавшись. Слэш-
    команда же исполняется самим TUI, ход на неё не тратится.

    Перед вводом чистим строку (C-u): в пейне мог остаться недобитый текст.

    `/model` не переключает молча — он спрашивает «Switch model?» с уже
    выделенным «Yes». Подтверждаем вторым Enter, но ТОЛЬКО увидев диалог:
    слепой Enter на сиденье без диалога отправил бы пустой ход."""
    out = type_command(alloc, name, f"/model {model}")
    if "switch model?" in out.lower():
        out = press_enter(alloc, name)
    if "switch model?" in out.lower():
        raise RuntimeError("диалог смены модели не закрылся")


def pane_lines(alloc, name):
    """Весь буфер tmux-пейна (история + экран), без хвостовых пустых строк,
    которыми tmux добивает видимую часть."""
    out, code = nomad.sh(alloc, f"tmux -L {name} capture-pane -p -t {name} -S -")
    if code not in (0, None):
        raise RuntimeError(f"tmux в {name}: {out.strip() or f'exit {code}'}")
    lines = out.splitlines()
    while lines and not lines[-1].strip():
        lines.pop()
    return lines
