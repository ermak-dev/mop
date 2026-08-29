"""Слейвы пула: спека job'а, LLM-профили и достоверное состояние места.

Модуль ВОЗВРАЩАЕТ ДАННЫЕ и ничего не печатает. Форматирование живёт во
фронтендах (командлеты в bin/ печатают таблицы, mop mcp отдаёт то же самое
модели) — иначе второй фронтенд неизбежно начал бы разбирать чужой текст.
"""
import base64
import os
import re

from . import bus, config, nomad

PROJECT = config.PROJECT

# Значения этой установки — .env поверх дефолтов; см. mop/config.py.
MEM = config.num("MOP_SLAVE_MEM_MB")   # бюджет слейва, МБ (на Linux-узлах cgroup-лимит ЖЁСТКИЙ)
HOME = config.get("MOP_HOME")             # $HOME на узлах пула
USER = config.get("MOP_USER")               # под кем идут задачи
SSH_ALIAS = config.pairs("MOP_SSH_ALIAS")  # узел nomad -> ssh-алиас
# Куда переводить слейв, у которого кончилась квота текущей модели
# (решение оператора 27.08: Fable → Opus).
FALLBACK_MODEL = config.get("MOP_FALLBACK_MODEL")
# Префикс НЕ настраивается: на нём стоят глобы сторожа диска (включая
# переходные ~/wk/wk-*), shard_of_name и имена tmux-серверов. Сделать его
# переменной, пока сторож знает оба префикса буквально, — значит развести
# половины одного соглашения.
JOB_PREFIX = "sl-"

# MCP автоматизации рабочего стола: адреса чужих хостов, не наших узлов.
MCP_PORT = config.get("MCP_PORT")
WINDOWS_MCP_HOST = config.get("WINDOWS_MCP_HOST")
MAC_MCP_HOST = config.get("MAC_MCP_HOST")

# ─── LLM-профили ─────────────────────────────────────────────────────────
# Слейв — всегда claude code; профиль меняет ровно одно: КУДА он ходит за
# токенами. Anthropic-совместимый эндпоинт провайдера (ANTHROPIC_BASE_URL),
# ключ (ANTHROPIC_AUTH_TOKEN) и карта имён моделей opus/sonnet/haiku в модели
# провайдера — больше в слейв ничего не меняется, поэтому tmux, tail, doctor
# и детект залипаний работают одинаково для любого профиля.
#
# "key" — ИМЯ переменной в .env проекта (локальный источник правды по ключам).
# Сам ключ в спеку джоба НЕ кладём: она видна в UI Nomad и остаётся в его
# состоянии. Вместо этого раздаём на узлы файл
# ~/.config/mop/secrets.env (только с тем, что названо явно), а врапер уже на
# узле подставляет нужное в сессию.
LLM_PROFILES = {
    # штатный Claude: авторизация — логин claude.ai (mop login), env пустой
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
# Источник правды по секретам проекта — .env рядом с кодом. Всё, что задаёт
# человек, лежит там; порождаемое само (пароли NATS, токен Nomad, логин
# claude.ai) — не там и туда не попадает.
LOCAL_KEYS_FILE = os.path.join(PROJECT, ".env")
# Подмножество, которое уезжает на узлы: ключи, названные профилями, плюс то,
# что нужно самому врапёру.
NODE_SECRETS = ("WINDOWS_MCP_TOKEN", "MAC_MCP_TOKEN")
SECRETS_FILE = f"{HOME}/.config/mop/secrets.env"    # копия на узле пула

# Врапер — собственно задача Nomad: довести узел до "клон есть, claude в
# tmux" и жить, пока жива tmux-сессия. Смерть врапера = рестарт/переезд
# слейва силами Nomad; на новом узле врапер сам разворачивает всё заново.
WRAPPER = r"""
set -e
d="$HOME/slaves/$SL_NAME"
if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin)" != "$SL_ORIGIN" ]; then
    rm -rf "$d"
fi
if [ ! -d "$d/.git" ]; then
    mkdir -p "$HOME/slaves"
    git clone -q "$SL_ORIGIN" "$d"
fi
for f in "$HOME/slave-env/$SL_PROJECT"/.env* "$HOME/slave-env/$SL_PROJECT"/.providers; do
    [ -e "$f" ] && cp -a "$f" "$d/" || true
done
mkdir -p "$HOME/.claude"
# ~/.claude.json and ~/.claude/settings.json are NODE-level: every slave on the
# host edits the same two files, and slaves boot together after a node restart.
# Read-modify-write from N processes through one shared temp path truncated the
# config to 0 bytes three times (2026-08-26/27/28), which parks every claude on
# the "invalid JSON" prompt and reads as a hung pool. So: one lock for the whole
# edit, a temp file per slave, and a repair pass for whatever a previous race
# (or a hard VM kill) left behind.
edit_json() {  # <file> <jq-program> [jq-args...]
    local file="$1" program="$2"; shift 2
    local tmp="$file.$SL_NAME.tmp"
    exec 9>"$file.lock"
    flock 9
    # `jq empty` is NOT a validity check: a zero-byte file is an empty jq input
    # stream, so it exits 0, every filter over it yields nothing, and the 0-byte
    # config got written straight back (gamer, 2026-08-28 — all six slaves parked
    # on the config prompt while `mop list` showed only "ЗАВИС"). Demand an
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
# A slave has no human to answer the first-run wizard: without these keys every
# claude sits on the theme prompt, which also reads as a hung slave.
edit_json "$HOME/.claude.json" '.projects[$d].hasTrustDialogAccepted = true
    | .hasCompletedOnboarding = true
    | .theme = (.theme // "dark")' --arg d "$d"
# Desktop-automation MCP servers: win-mcp (Windows host pwate) and mac-mcp (mac
# host), so a slave can drive and verify the GUI clients. HTTP transport, so any
# node that can resolve/reach the hosts gets them; a node that cannot just sees
# the server fail to connect.
# windows-mcp runs on the Windows host (gamer) bound to 0.0.0.0:8000. Off-host
# slaves reach it by the host's LAN IP; a slave running IN the gamer host's own
# mirrored-mode WSL shares that LAN IP, where the Windows bind is only reachable
# via loopback - so it must use 127.0.0.1 instead.
if ip -4 -o addr show 2>/dev/null | grep -q "$SL_WIN_HOST"; then
    WINURL="http://127.0.0.1:$SL_MCP_PORT/mcp"
else
    WINURL="http://$SL_WIN_HOST:$SL_MCP_PORT/mcp"
fi
# Токены НЕ в спеке: она видна в UI Nomad и остаётся в его состоянии. Едут
# тем же файлом, что и ключи провайдеров, и достаются отсюда так же -- sed'ом,
# а не source: файл с секретами не исполняем.
secrets="$HOME/.config/mop/secrets.env"
wintok=$(sed -n "s/^WINDOWS_MCP_TOKEN=//p" "$secrets" 2>/dev/null | tail -1)
mactok=$(sed -n "s/^MAC_MCP_TOKEN=//p" "$secrets" 2>/dev/null | tail -1)
edit_json "$HOME/.claude.json" '.mcpServers["windows-mcp"] = {"type":"http","url":$winurl,"headers":{"Authorization":("Bearer " + $wintok)}}
  | .mcpServers["mac-mcp"] = {"type":"http","url":$macurl,"headers":{"Authorization":("Bearer " + $mactok)}}
  | .mcpServers["playwright"] = {"type":"stdio","command":"npx","args":["-y","@playwright/mcp@latest","--headless","--isolated","--browser","chromium"]}
  | .mcpServers["mop"] = {"type":"stdio","command":$mop,"args":["mcp"]}' \
    --arg winurl "$WINURL" --arg macurl "http://$SL_MAC_HOST:$SL_MCP_PORT/mcp" \
    --arg mop "$HOME/mop/bin/mop" --arg wintok "$wintok" --arg mactok "$mactok"
# playwright-mcp needs a browser on the node; install is idempotent and cached
# in ~/.cache/ms-playwright, so every slave boot just confirms it is there.
npx -y playwright install chromium >/dev/null 2>&1 || true
# Same repair as above, and for the same reason: an existing-but-unreadable
# file (0 bytes after a torn write) made jq fail silently, so the bypass-mode
# prompt came back and every slave stopped on it.
[ -f "$HOME/.claude/settings.json" ] || echo '{}' > "$HOME/.claude/settings.json"
edit_json "$HOME/.claude/settings.json" '.skipDangerousModePermissionPrompt = true'
# Встроенный обмен сообщениями убираем совсем. Не «запрещаем на вызове»:
# deny-правила фильтруют САМ СПИСОК инструментов (filterToolsByDenyRules в
# бинаре claude), так что SendMessage и ListAgents до модели не доезжают и
# выбирать между ними и каналом пула ей не приходится. На режим прав фильтр не
# смотрит, поэтому работает и под --dangerously-skip-permissions.
#
# Почему вообще: встроенный механизм находит только сессии ЭТОГО хоста. Пока
# два слейва стояли на одном узле, он работал и выглядел исправным; на разных
# узлах он молча не найдёт никого, а тихий отказ в петле мастера хуже громкого.
#
# Слияние, а не присваивание: чужие deny-правила на узле сносить незачем.
#
# AskUserQuestion: у слейва нет человека за терминалом. Вызов паркует сессию
# намертво -- это ровно то состояние "требует действия", которое doctor лечит
# рестартом. Без инструмента модель решает сама и идёт дальше.
#
# EnterWorktree/ExitWorktree: тихо ломают модель состояния. clone_holds_work
# смотрит в клон слейва; ушедший в worktree оставит клон чистым, list покажет
# "свободен", и мастер задиспатчит поверх живой работы. Отказ молчаливый, а
# цена -- потерянная работа.
edit_json "$HOME/.claude/settings.json" '.permissions.deny =
    ((.permissions.deny // []) + ["SendMessage", "ListAgents",
      "AskUserQuestion", "EnterWorktree", "ExitWorktree"] | unique)'
# CARGO_TARGET_DIR grows without bound - 22 to 49 GB per slave in practice, and
# five of them filled the gamer node's disk on 2026-08-26, which killed the WSL
# VM and stranded every allocation on it. Boot is the only safe moment to drop
# one: nothing is building yet, and the cache is pure derived data.
TARGET_DIR="$HOME/.cache/target-$SL_NAME"
if [ -d "$TARGET_DIR" ] \
    && [ "$(du -sm "$TARGET_DIR" 2>/dev/null | cut -f1 || echo 0)" -gt 30000 ]; then
    rm -rf "$TARGET_DIR"
fi
# LLM-профиль слейва: набор переменных для tmux -e. Статическая часть
# (эндпоинт и карта моделей) приезжает в SL_LLM_ENV из спеки джоба, а КЛЮЧ —
# только с узла: в спеке джоба секретам не место (её видно в UI Nomad).
llm_env=()
if [ -n "$SL_LLM_ENV" ]; then
    while IFS= read -r kv; do
        [ -n "$kv" ] && llm_env+=(-e "$kv")
    done <<< "$(printf '%s' "$SL_LLM_ENV" | base64 -d)"
fi
if [ -n "$SL_LLM_KEY_VAR" ]; then
    keyfile="$HOME/.config/mop/secrets.env"
    key=""
    # sed, а не source: файл с ключами не исполняем
    # Двойной доллар — экранирование интерполяции Nomad: спеку задачи он
    # прогоняет через hcl2 и всякую фигурную подстановку пытается вычислить
    # сам. Неэкранированная подстановка имени переменной ниже, и особенно
    # раскрытие массива llm_env, валят РЕГИСТРАЦИЮ джоба на "Invalid
    # expression" ещё до запуска: [@] для HCL не выражение. Осторожно, это
    # правило действует и на комментарии — Nomad разбирает всю строку.
    [ -f "$keyfile" ] && key=$(sed -n "s/^$${SL_LLM_KEY_VAR}=//p" "$keyfile" | tail -1)
    if [ -z "$key" ]; then
        # Валимся громко: без ключа claude поднимется и будет отбивать каждый
        # ход 401-й, а слейв будет читаться как живое и свободное.
        echo "LLM-профиль $SL_LLM: на узле нет ключа $SL_LLM_KEY_VAR в $keyfile — раздай: mop login" >&2
        exit 1
    fi
    llm_env+=(-e "$SL_LLM_AUTH_VAR=$key")
fi

# Креды ШАРДА, а не узла. Без этого слейв ходил бы на шину под кредом агента и
# мог бы написать в чужой проект: агент видит, КОГО спрашивают, но не видит,
# КТО спрашивает, и такую подмену не поймал бы. Файл раскатывает deploy/setup.yml
# по одному на шард; если его нет -- валимся ГРОМКО, потому что слейв без шины
# читается мастером как живой, но молчащий.
shard_creds="$HOME/.config/mop/bus-$SL_SHARD.json"
if [ ! -f "$shard_creds" ]; then
    echo "нет кредов шарда $SL_SHARD в $shard_creds -- заведи шард: mop deploy nats" >&2
    exit 1
fi

# a dedicated tmux SERVER per slave (-L): with the default server every
# session on the node lives in the cgroup of whichever wrapper started the
# server first, and one task budget OOM-kills all slaves at once
tmux -L "$SL_NAME" kill-session -t "$SL_NAME" 2>/dev/null || true
# env must go through -e: a plain env prefix only reaches the tmux SERVER when
# this wrapper happens to start it, and every later session inherits the first
# wrapper's variables (all slaves ended up sharing one CARGO_TARGET_DIR)
tmux -L "$SL_NAME" new-session -d -s "$SL_NAME" -c "$d" \
    -e CARGO_TARGET_DIR="$HOME/.cache/target-$SL_NAME" \
    -e CARGO_BUILD_JOBS=1 \
    -e PATH="$d/bin:$PATH" \
    -e MOP_SHARD="$SL_SHARD" \
    -e MOP_BUS_CONFIG="$shard_creds" \
    "$${llm_env[@]}" \
    "$HOME/.local/bin/claude --dangerously-skip-permissions"
trap 'tmux -L "$SL_NAME" kill-session -t "$SL_NAME" 2>/dev/null; exit 0' TERM INT
while tmux -L "$SL_NAME" has-session -t "$SL_NAME" 2>/dev/null; do sleep 10 & wait $!; done
"""


def shard_of(origin):
    """Шард (он же проект) по origin репозитория.

    Basename без .git, и это ЕДИНСТВЕННОЕ определение проекта в системе.
    Соблазн взять хеш от полного origin есть — тогда два `rugent.git` на разных
    хостах не слились бы в один шард. Но имена слейвов уже строятся отсюда же
    (`sl-<проект>-<n>`), и завести рядом второе, более точное понятие «проект»
    значит получить два места, по-разному отвечающих на вопрос «чей это слейв».
    Цена честная и названа: одинаковые basename делят шард ровно так же, как
    уже делят имена. Понадобится развести — сюда добавляется суффикс от
    sha256(origin), и больше никуда."""
    return os.path.basename(origin).removesuffix(".git")


def shard_of_name(name):
    """Шард по имени слейва: sl-<проект>-<n>. Откат для случая, когда клона
    ещё нет, — origin спросить не у кого, а имя уже есть."""
    if not name.startswith(JOB_PREFIX):
        return ""
    return name[len(JOB_PREFIX):].rsplit("-", 1)[0]


def clone_dir(name):
    return f"{HOME}/slaves/{name}"


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
            "Name": "slaves",
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
                "User": USER,
                "Config": {"command": "/bin/bash", "args": ["-c", WRAPPER]},
                "Env": {
                    "SL_NAME": name,
                    "SL_ORIGIN": origin,
                    "SL_PROJECT": project,
                    "SL_SHARD": shard_of(origin),
                    "SL_MCP_PORT": MCP_PORT,
                    "SL_WIN_HOST": WINDOWS_MCP_HOST,
                    "SL_MAC_HOST": MAC_MCP_HOST,
                    "HOME": HOME,
                    "PATH": f"/usr/local/bin:/usr/bin:/bin:{HOME}/.local/bin:{HOME}/.cargo/bin:{HOME}/.nvm/versions/node/v22.12.0/bin",
                    # LLM-профиль: имена и эндпоинт — здесь, ключ — на узле
                    "SL_LLM": llm,
                    "SL_LLM_ENV": base64.b64encode(llm_env.encode()).decode(),
                    "SL_LLM_KEY_VAR": prof.get("key") or "",
                    "SL_LLM_AUTH_VAR": prof.get("auth_var") or "ANTHROPIC_AUTH_TOKEN",
                },
                "Resources": {"CPU": 1000, "MemoryMB": MEM},
                "KillTimeout": 15 * 10**9,
            }],
        }],
    }}


def jobs(shard=None):
    """Джобы слейвов. Префикс sl- ловит и sl-cleanup с его периодическими
    детьми; слейвы — те, что врапер пометил origin'ом.

    shard=None -> срез ЭТОГО процесса: `mop master` ставит MOP_SHARD, и мастер
    проекта перестаёт видеть чужих слейвов уже здесь, в ростере. Псевдошард
    admin (оператор вне мастер-шелла) видит всё."""
    listing = nomad.client().jobs.get_jobs(prefix=JOB_PREFIX, meta=True)
    out = [j for j in listing if "origin" in (j.get("Meta") or {})]
    shard = shard or bus.SHARD
    if shard == bus.ADMIN:
        return out
    return [j for j in out if shard_of(j["Meta"]["origin"]) == shard]


def next_name(project):
    taken = {j["ID"] for j in nomad.client().jobs.get_jobs(prefix=f"{JOB_PREFIX}{project}-")}
    n = 1
    while f"{JOB_PREFIX}{project}-{n}" in taken:
        n += 1
    return f"{JOB_PREFIX}{project}-{n}"


# ─── состояние слейва ────────────────────────────────────────────────────
# КАК УЗНАТЬ РЕАЛЬНОЕ СОСТОЯНИЕ СЛЕЙВА
#
# Узел присылает ФАКТЫ, вердикт собираем здесь. Слейв сам ведёт две вещи, и
# они и есть источник правды (обе появляются независимо от моста claude.ai):
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
# Нет файла (старый claude / нет python3) -> None, и мы откатываемся на
# прежнюю tmux-эвристику. Детект протухшего логина остаётся на tmux — в файле
# он не виден.
#
# Пробник живёт в mop/session.py и исполняется агентом НА УЗЛЕ: сокет слейва
# host-local, снаружи к нему не подключиться.
#
# Всё, что ниже, — ЧИСТЫЕ функции над этими фактами. Так вышло не случайно:
# пока состояние собиралось поверх exec, проверить его без живого пула было
# нельзя, и регрессия однажды спряталась именно здесь.
SESSION_STATES = ("idle", "busy", "requires_action", "waiting", "offline")


def facts(node, name):
    """Факты об одном слейве с его узла."""
    return bus.request(node, "state", name=name)


def _session_state(line):
    """Ответ пробника "<status> <alive> <listen>" -> состояние сессии, либо
    None, если файла сессии нет."""
    if not line or line == "none":
        return None
    parts = line.split()
    if len(parts) < 3:
        return None
    status, alive, listen = parts[0], parts[1] == "1", parts[2] == "1"
    if not listen:
        return "hung" if alive else "offline"
    return status if status in SESSION_STATES else None


def _screen_complaint(activity):
    """Жалоба, видимая только на экране. -> (вид, текст) или None.

    Оба случая — один класс: сессия жива, отвечает на ping'и, но ни одного хода
    выдать не может. Для мастер это неотличимо от молчания.
    """
    low = activity.lower()
    # Логин. Варианты экрана: "Not logged in · Run /login", "Login expired ·
    # Please run /login". Проверять до скоринга: у залипшего мид-таск в буфере
    # полно рабочих слов.
    #
    # Строки про Remote Control отсюда убраны вместе с самим --remote-control:
    # слейва больше не ходят на мост claude.ai, и "/rc failed" на их экране
    # означало бы что угодно, только не болезнь. Для профилей с ключом
    # провайдера (glm) логин claude.ai вообще не при делах.
    if "not logged in" in low or "login expired" in low:
        return "login", ("не залогинен" if "not logged in" in low else "логин протух")
    # Квота модели: каждое входящее сообщение мгновенно возвращает "You're out
    # of usage credits…" и сессия падает обратно в idle. Рестарт НЕ лечит —
    # нужен либо другой /model, либо пополнение.
    #
    # Жалоба живёт в скроллбэке и после лечения, поэтому считается актуальной
    # только если ПОСЛЕ неё модель не переключали: иначе вылеченное слейв
    # вечно читалось бы как больное.
    if ("out of usage credits" in low
            and low.rfind("out of usage credits") > low.rfind("set model to")):
        m = re.search(r"keep using ([^\s]+(?: [0-9.]+)?)", activity, re.I)
        return "quota", (f"нет квоты модели: {m.group(1)}" if m else "нет квоты модели")
    return None



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



def _work_branch(clone):
    """Ветка слейва, если она не дефолтная, — иначе None.

    Занятость места — это НЕ имя ветки, и здесь оно нужно только чтобы
    показать человеку, где слейв сидит."""
    if not clone:
        return None
    cur, default = clone.get("cur"), clone.get("def")
    return cur if cur and cur != default else None


def _state_from_session(st, clone):
    """Состояние места по достоверному статусу сессии.

    Файл сессии авторитетен про АКТИВНОСТЬ, но не про ЗАНЯТОСТЬ МЕСТА: клон
    переживает смерть сессии. Поэтому у idle спрашиваем клон — пропадёт ли
    что-нибудь, если занять место.

    Пропадает только то, чего нет ни на одной удалённой ветке. Числа считает
    агент через `--not --remotes`, а НЕ через `@{u}..`: upstream рабочей ветки
    бывает прибит к origin/master, и тогда всё невлитое врёт как «не
    отправлено» (так и вышло на rugent-7).

    «Не закоммичено» и «не отправлено» показываем РАЗДЕЛЬНО: это разные
    состояния и разный разговор с агентом. Сложив их в одно «только локально:
    3», мастер однажды сказал переродившемуся месту «у тебя было 3 локальных
    коммита», тогда как там лежали 3 несохранённых файла и ноль коммитов."""
    if st == "requires_action":
        return "требует действия"
    if st == "waiting":
        return "ждёт ввода"
    if st in ("offline", "hung"):
        return "ЗАВИС (не отвечает)"
    if st == "busy":
        branch = _work_branch(clone)
        return f"занят: {branch}" if branch else "занят"

    if not clone:
        return "свободен"
    dirty, ahead = clone.get("dirty") or 0, clone.get("ahead") or 0
    if dirty or ahead:
        what = ", ".join(p for p in (f"не закоммичено: {dirty}" if dirty else "",
                                     f"не отправлено: {ahead}" if ahead else "") if p)
        return f"занят: {clone.get('cur')} ({what})"
    # Чисто и всё на origin — терять нечего, место переиспользуемо. Ветку
    # показываем справочно: диспатч всё равно обязан начать с переключения на
    # интеграционную ветку, иначе новая ветка тикета уедет от оставшейся здесь.
    cur = clone.get("cur")
    return f"свободен ({cur})" if cur else "свободен"


def _state_from_clone(clone):
    """Последний откат: одна лишь ветка клона."""
    if not clone:
        return "клона ещё нет"
    branch = _work_branch(clone)
    return f"занят: {branch}" if branch else "свободен"


def slave_state(f):
    """Занятость места по фактам с узла. ЧИСТАЯ функция: см. блок выше.

    «Свободен» означает, что в клоне нет несохранённой работы, а не что сессия
    молчит. На этом стоит решение мастера о диспатче, и ломать условие нельзя.
    """
    if not f or f.get("error"):
        return f"ЗАВИС ({(f or {}).get('error', 'нет ответа')[:40]})"
    if not f.get("present"):
        return "ЗАВИС (нет tmux-сессии)"

    activity = f.get("screen") or ""
    if not activity.strip():
        return "свободен"

    complaint = _screen_complaint(activity)
    if complaint:
        kind, text = complaint
        if kind != "login":
            return text
        branch = _work_branch(f.get("clone"))
        return f"{text}: {branch}" if branch else text

    st = _session_state(f.get("session"))
    if st is not None:
        return _state_from_session(st, f.get("clone"))

    guess = _tmux_guess(activity)
    if guess:
        return guess

    return _state_from_clone(f.get("clone"))


# ─── сводки для фронтендов ───────────────────────────────────────────────
def _collect():
    """Ростер Nomad плюс состояние с узлов, ОДНИМ заходом.

    Общая часть list и doctor. Раньше каждый ходил на узлы сам и платил по
    четыре рукопожатия exec'а за слейва — на десяти слейвах это сорок
    последовательных подключений, и столько же ещё раз, если следом звали
    doctor. Теперь: один запрос в Nomad за ростером и по одному запросу на
    УЗЕЛ за всеми его слейвами, параллельно по одному соединению.

    -> [{job, alloc, state}]; state=None там, где спрашивать некого.
    """
    items, by_node = [], {}
    for j in sorted(jobs(), key=lambda j: j["ID"]):
        alloc, err = None, None
        try:
            alloc = nomad.latest_alloc(j["ID"])
        except Exception as e:
            err = nomad.describe_error(e)
        item = {"job": j, "alloc": alloc, "state": None, "error": err}
        if alloc and alloc["ClientStatus"] == "running":
            by_node.setdefault(alloc["NodeName"], []).append(item)
        items.append(item)

    # Шина легла целиком — ростер всё равно показываем. Он приходит из Nomad и
    # к шине отношения не имеет; уронить `list` вместе с ней значит оставить
    # мастера без единственной картины пула ровно тогда, когда что-то сломалось.
    try:
        answers = bus.request_many({
            node: {"verb": "states", "names": [i["job"]["ID"] for i in its]}
            for node, its in by_node.items()})
    except bus.BusError as e:
        answers = {node: bus.BusError(str(e)) for node in by_node}

    for node, its in by_node.items():
        answer = answers.get(node)
        # Молчащий агент — ОТДЕЛЬНАЯ болезнь, не "слейв завис": слейв при этом
        # может прекрасно работать, и рестартить его нельзя.
        if isinstance(answer, Exception):
            for i in its:
                i["state"] = f"АГЕНТ МОЛЧИТ ({answer})"
            continue
        got = (answer or {}).get("slaves") or {}
        for i in its:
            i["state"] = slave_state(got.get(i["job"]["ID"]))
    return items


def slave_rows():
    """Слейва как ДАННЫЕ: [{name, node, alloc_status, state, llm, origin}]."""
    rows = []
    for item in _collect():
        job, alloc = item["job"], item["alloc"]
        meta = job.get("Meta") or {}
        rows.append({
            "name": job["ID"],
            "node": alloc["NodeName"] if alloc else "-",
            "alloc_status": (item["error"] or (alloc["ClientStatus"] if alloc
                                               else job.get("Status", "?"))),
            "state": item["state"] or "-",
            "llm": meta.get("llm", DEFAULT_LLM),
            "origin": meta.get("origin", "?"),
        })
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
#   не залогинен/протух    -> раздать креды на пул, затем restart слейва
#   pending/failed/lost    -> alloc stop: Nomad пересоздаёт сразу, минуя
#                             restart-backoff (до 30 мин)
#   нет квоты модели       -> печать /model в пейн; рестарт квоту не вернёт
#   queued без аллокации   -> мест в пуле нет, лечится не отсюда
#   агент узла молчит      -> отсюда никак: лечится юнитом на самом узле
def diagnose():
    """Проблемы пула как ДАННЫЕ: [{name, alloc, diagnosis, action}]."""
    issues = []
    for item in _collect():
        job, alloc = item["job"], item["alloc"]
        if not alloc or alloc["ClientStatus"] in ("lost", "unknown", "failed",
                                                  "pending"):
            issues.append(_placement_issue(job, alloc))
            continue
        action = _action_for(item["state"])
        if action is not False:
            issues.append({"name": job["ID"], "alloc": alloc,
                           "diagnosis": item["state"], "action": action})
    return issues


def _placement_issue(job, alloc):
    name = job["ID"]
    if alloc and alloc["ClientStatus"] in ("pending", "failed"):
        return {"name": name, "alloc": alloc, "action": "stop",
                "diagnosis": f"аллок {alloc['ClientStatus']} (restart-backoff?)"}
    if alloc:
        return {"name": name, "alloc": alloc, "action": "stop",
                "diagnosis": f"аллок {alloc['ClientStatus']}"}
    queued = (job.get("JobSummary", {}).get("Summary", {}).get("slaves") or {}).get("Queued", 0)
    if queued:
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": "queued — нет свободных слотов в пуле"}
    return {"name": name, "alloc": None, "action": None, "diagnosis": "нет аллокации"}


def _action_for(state):
    """Лечение для состояния. False — состояние здоровое, проблемы нет."""
    # Молчит АГЕНТ, а не слейв. Рестарт слейва тут ничего не лечит и вполне
    # может убить живую работу в клоне: про сам слейв мы в этот момент не
    # знаем ничего. Показать — да, трогать — нет.
    if state.startswith("АГЕНТ МОЛЧИТ"):
        return None
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


# ─── ввод в TUI слейва ───────────────────────────────────────────────────
# Всё здесь адресуется УЗЛОМ, а не аллокацией: alloc exec умер вместе со своей
# адресацией, и агент подписан на субъект узла.
def type_command(node, name, command):
    """Напечатать слэш-команду в tmux-пейн слейва и вернуть экран после неё.

    Печатью, а не сообщением по каналу: слэш-команды через канал не проходят
    (сообщение кладётся в очередь с skipSlashCommands), а у слейва с
    исчерпанной квотой любой ход падает, не начавшись — слэш-команду же
    исполняет сам TUI, ход на неё не тратится.

    Белый список команд проверяет АГЕНТ: проверка на этой стороне осталась бы
    подсказкой пользователю, а не правом."""
    r = bus.request(node, "type", name=name, command=command, timeout=45)
    if "error" in r:
        raise RuntimeError(r["error"])
    return r.get("screen") or ""


def press_enter(node, name):
    """Подтвердить диалог. Только увидев его: слепой Enter на слейв без
    диалога отправил бы пустой ход."""
    return type_command(node, name, "")


def switch_model(node, name, model):
    """Перевести слейв на другую модель, напечатав /model в его tmux-пейн.

    `/model` не переключает молча — он спрашивает «Switch model?» с уже
    выделенным «Yes». Подтверждаем вторым Enter, но ТОЛЬКО увидев диалог."""
    out = type_command(node, name, f"/model {model}")
    if "switch model?" in out.lower():
        out = press_enter(node, name)
    if "switch model?" in out.lower():
        raise RuntimeError("диалог смены модели не закрылся")


def pane_lines(node, name):
    """Весь буфер tmux-пейна слейва (история + экран)."""
    r = bus.request(node, "tail", name=name)
    if "error" in r:
        raise RuntimeError(f"tmux в {name}: {r['error']}")
    return r.get("lines") or []
