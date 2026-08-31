"""Папеты пула: спека job'а и достоверное состояние места.

Модуль ВОЗВРАЩАЕТ ДАННЫЕ и ничего не печатает. Форматирование живёт во
фронтендах (командлеты в bin/ печатают таблицы, mop mcp отдаёт то же самое
модели) — иначе второй фронтенд неизбежно начал бы разбирать чужой текст.
"""
import base64
import os
import re
import time

from . import bus, config, llm, nomad

PROJECT = config.PROJECT

# Значения этой установки — .env поверх дефолтов; см. mop/config.py.
MEM = config.num("MOP_PUPPET_MEM_MB")   # бюджет папета, МБ (на Linux-узлах cgroup-лимит ЖЁСТКИЙ)
HOME = config.get("MOP_HOME")             # $HOME на узлах пула
USER = config.get("MOP_USER")               # под кем идут задачи
# Куда переводить папет, у которого кончилась квота текущей модели
# (решение оператора 27.08: Fable → Opus).
FALLBACK_MODEL = config.get("MOP_FALLBACK_MODEL")
# Префикс НЕ настраивается: на нём стоят глобы сторожа диска (включая
# переходные ~/wk/wk-*), shard_of_name и имена tmux-серверов. Сделать его
# переменной, пока сторож знает оба префикса буквально, — значит развести
# половины одного соглашения.
JOB_PREFIX = "pu-"

# ─── LLM-профили ─────────────────────────────────────────────────────────
# Папет — всегда claude code; профиль меняет ровно одно: КУДА он ходит за
# токенами. Anthropic-совместимый эндпоинт провайдера (ANTHROPIC_BASE_URL),
# ключ (ANTHROPIC_AUTH_TOKEN) и карта имён моделей opus/sonnet/haiku в модели
# провайдера — больше в папет ничего не меняется, поэтому tmux, tail, doctor
# и детект залипаний работают одинаково для любого профиля.
#
# Сами профили — плагины в mop/llm/ (один файл = один профиль, имя файла =
# имя). Здесь только потребление. Дефолт — настройка установки: контора на
# одном провайдере меняет дефолт, а не каждую команду.
# Источник правды по секретам проекта — .env рядом с кодом. Всё, что задаёт
# человек, лежит там; порождаемое само (пароли NATS, токен Nomad, логин
# claude.ai) — не там и туда не попадает.
LOCAL_KEYS_FILE = os.path.join(PROJECT, ".env")
# Подмножество .env, которое уезжает на узлы: только ключи, названные
# профилями. Секреты MCP-серверов установки едут иначе -- их прописывает
# examples/homelab/claude.yml прямо в регистрацию сервера.
SECRETS_FILE = f"{HOME}/.config/mop/secrets.env"    # копия на узле пула

# Врапер — собственно задача Nomad: довести узел до "клон есть, claude в
# tmux" и жить, пока жива tmux-сессия. Смерть врапера = рестарт/переезд
# папета силами Nomad; на новом узле врапер сам разворачивает всё заново.
WRAPPER = r"""
set -e
d="$HOME/puppets/$PU_NAME"
if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin)" != "$PU_ORIGIN" ]; then
    rm -rf "$d"
fi
if [ ! -d "$d/.git" ]; then
    mkdir -p "$HOME/puppets"
    git clone -q "$PU_ORIGIN" "$d"
fi
for pat in $${PU_SEED//,/ }; do
    for f in "$HOME/puppet-env/$PU_PROJECT"/$pat; do
        [ -e "$f" ] && cp -a "$f" "$d/" || true
    done
done
mkdir -p "$HOME/.claude"
# ~/.claude.json and ~/.claude/settings.json are NODE-level: every puppet on the
# host edits the same two files, and puppets boot together after a node restart.
# Read-modify-write from N processes through one shared temp path truncated the
# config to 0 bytes three times (2026-08-26/27/28), which parks every claude on
# the "invalid JSON" prompt and reads as a hung pool. So: one lock for the whole
# edit, a temp file per puppet, and a repair pass for whatever a previous race
# (or a hard VM kill) left behind.
edit_json() {  # <file> <jq-program> [jq-args...]
    local file="$1" program="$2"; shift 2
    local tmp="$file.$PU_NAME.tmp"
    exec 9>"$file.lock"
    flock 9
    # `jq empty` is NOT a validity check: a zero-byte file is an empty jq input
    # stream, so it exits 0, every filter over it yields nothing, and the 0-byte
    # config got written straight back (2026-08-28, one node — all six puppets parked
    # on the config prompt while `mop list` showed only "HUNG"). Demand an
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
# A puppet has no human to answer the first-run wizard: without these keys every
# claude sits on the theme prompt, which also reads as a hung puppet.
#
# resumeReturnDismissed is the same class of trap, one level deeper. Resuming a
# LONG conversation (over ~70 minutes or ~100k tokens) asks whether to restore
# it whole, from a summary, or to stop asking -- and the dialog is what the
# "stop asking" answer writes here. On 2026-08-31 a profile switch parked
# pu-cloudpub-1 on exactly that question with 251.5k tokens behind it: no CLI
# flag turns it off, and nobody was there to press a key. The two thresholds
# are also env-tunable (CLAUDE_CODE_RESUME_THRESHOLD_MINUTES,
# CLAUDE_CODE_RESUME_TOKEN_THRESHOLD), but raising a threshold only moves the
# wall further away -- the key removes it.
edit_json "$HOME/.claude.json" '.projects[$d].hasTrustDialogAccepted = true
    | .hasCompletedOnboarding = true
    | .resumeReturnDismissed = true
    | .theme = (.theme // "dark")' --arg d "$d"
# Свой MCP-сервер регистрируем через сам claude: он владелец ~/.claude.json,
# и никаких ручных мерджей. add на существующей записи ошибается -- и это
# устраивает: в записи нет секретов, наличие означает правильность, полную
# сходимость (если сменился путь) делает deploy. Чужие сервера -- дело
# установки (examples/homelab/claude.yml), врапер о них не знает.
claude mcp add --scope user mop -- "$HOME/mop/bin/mop" mcp >/dev/null 2>&1 || true
# playwright-mcp needs a browser on the node; install is idempotent and cached
# in ~/.cache/ms-playwright, so every puppet boot just confirms it is there.
npx -y playwright install chromium >/dev/null 2>&1 || true
# Same repair as above, and for the same reason: an existing-but-unreadable
# file (0 bytes after a torn write) made jq fail silently, so the bypass-mode
# prompt came back and every puppet stopped on it.
[ -f "$HOME/.claude/settings.json" ] || echo '{}' > "$HOME/.claude/settings.json"
edit_json "$HOME/.claude/settings.json" '.skipDangerousModePermissionPrompt = true'
# Встроенный обмен сообщениями убираем совсем. Не «запрещаем на вызове»:
# deny-правила фильтруют САМ СПИСОК инструментов (filterToolsByDenyRules в
# бинаре claude), так что SendMessage и ListAgents до модели не доезжают и
# выбирать между ними и каналом пула ей не приходится. На режим прав фильтр не
# смотрит, поэтому работает и под --dangerously-skip-permissions.
#
# Почему вообще: встроенный механизм находит только сессии ЭТОГО хоста. Пока
# два папета стояли на одном узле, он работал и выглядел исправным; на разных
# узлах он молча не найдёт никого, а тихий отказ в петле мастера хуже громкого.
#
# Слияние, а не присваивание: чужие deny-правила на узле сносить незачем.
#
# AskUserQuestion: у папета нет человека за терминалом. Вызов паркует сессию
# намертво -- это ровно то состояние "needs action", которое doctor лечит
# рестартом. Без инструмента модель решает сама и идёт дальше.
#
# EnterWorktree/ExitWorktree: тихо ломают модель состояния. clone_holds_work
# смотрит в клон папета; ушедший в worktree оставит клон чистым, list покажет
# "free", и мастер задиспатчит поверх живой работы. Отказ молчаливый, а
# цена -- потерянная работа.
edit_json "$HOME/.claude/settings.json" '.permissions.deny =
    ((.permissions.deny // []) + ["SendMessage", "ListAgents",
      "AskUserQuestion", "EnterWorktree", "ExitWorktree"] | unique)'
# CARGO_TARGET_DIR grows without bound - 22 to 49 GB per puppet in practice, and
# five of them filled a node's disk on 2026-08-26, which killed the WSL VM and
# stranded every allocation on it. Boot is the only safe moment to drop
# one: nothing is building yet, and the cache is pure derived data.
TARGET_DIR="$HOME/.cache/target-$PU_NAME"
if [ -d "$TARGET_DIR" ] \
    && [ "$(du -sm "$TARGET_DIR" 2>/dev/null | cut -f1 || echo 0)" -gt 30000 ]; then
    rm -rf "$TARGET_DIR"
fi
# LLM-профиль папета: набор переменных для tmux -e. Статическая часть
# (эндпоинт и карта моделей) приезжает в PU_LLM_ENV из спеки джоба, а КЛЮЧ —
# только с узла: в спеке джоба секретам не место (её видно в UI Nomad).
llm_env=()
if [ -n "$PU_LLM_ENV" ]; then
    while IFS= read -r kv; do
        [ -n "$kv" ] && llm_env+=(-e "$kv")
    done <<< "$(printf '%s' "$PU_LLM_ENV" | base64 -d)"
fi
if [ -n "$PU_LLM_KEY_VAR" ]; then
    keyfile="$HOME/.config/mop/secrets.env"
    key=""
    # sed, а не source: файл с ключами не исполняем
    # Двойной доллар — экранирование интерполяции Nomad: спеку задачи он
    # прогоняет через hcl2 и всякую фигурную подстановку пытается вычислить
    # сам. Неэкранированная подстановка имени переменной ниже, и особенно
    # раскрытие массива llm_env, валят РЕГИСТРАЦИЮ джоба на "Invalid
    # expression" ещё до запуска: [@] для HCL не выражение. Осторожно, это
    # правило действует и на комментарии — Nomad разбирает всю строку.
    [ -f "$keyfile" ] && key=$(sed -n "s/^$${PU_LLM_KEY_VAR}=//p" "$keyfile" | tail -1)
    if [ -z "$key" ]; then
        # Валимся громко: без ключа claude поднимется и будет отбивать каждый
        # ход 401-й, а папет будет читаться как живое и свободное.
        echo "LLM-профиль $PU_LLM: на узле нет ключа $PU_LLM_KEY_VAR в $keyfile — раздай: mop login" >&2
        exit 1
    fi
    llm_env+=(-e "$PU_LLM_AUTH_VAR=$key")
fi

# Креды ШАРДА, а не узла. Без этого папет ходил бы на шину под кредом агента и
# мог бы написать в чужой проект: агент видит, КОГО спрашивают, но не видит,
# КТО спрашивает, и такую подмену не поймал бы. Файл раскатывает deploy/setup.yml
# по одному на шард; если его нет -- валимся ГРОМКО, потому что папет без шины
# читается мастером как живой, но молчащий.
shard_creds="$HOME/.config/mop/bus-$PU_SHARD.json"
if [ ! -f "$shard_creds" ]; then
    echo "нет кредов шарда $PU_SHARD в $shard_creds -- заведи шард: mop deploy $PU_ORIGIN" >&2
    exit 1
fi

# a dedicated tmux SERVER per puppet (-L): with the default server every
# session on the node lives in the cgroup of whichever wrapper started the
# server first, and one task budget OOM-kills all puppets at once
tmux -L "$PU_NAME" kill-session -t "$PU_NAME" 2>/dev/null || true
# env must go through -e: a plain env prefix only reaches the tmux SERVER when
# this wrapper happens to start it, and every later session inherits the first
# wrapper's variables (all puppets ended up sharing one CARGO_TARGET_DIR)
claude_args="--dangerously-skip-permissions"
# Продолжение истории каталога -- РОВНО ОДИН подъём на перерегистрацию спеки.
# Одноразовость здесь не прихоть: рестарт аллокации спеку не перечитывает, а
# ЛЕЧЕНИЕ залипшего папета -- это именно рестарт. Липкий --continue возвращал
# бы вылеченного ровно в тот контекст, на котором он залип, то есть лечил бы
# симптом и воспроизводил болезнь. Поэтому мастер кладёт в спеку разовый
# токен, а врапер гасит его маркером в клоне: совпал -- поднимаемся чисто.
#
# --continue, а НЕ --resume: без ID сессии resume открывает интерактивный
# выбор, и папет паркуется на нём намертво -- выбрать строку ему некому.
marker="$d/.git/mop-continue"
if [ -n "$${PU_CONTINUE:-}" ] \
    && [ "$(cat "$marker" 2>/dev/null)" != "$PU_CONTINUE" ]; then
    printf '%s' "$PU_CONTINUE" > "$marker"
    claude_args="$claude_args --continue"
fi
tmux -L "$PU_NAME" new-session -d -s "$PU_NAME" -c "$d" \
    -e CARGO_TARGET_DIR="$HOME/.cache/target-$PU_NAME" \
    -e CARGO_BUILD_JOBS=1 \
    -e PATH="$d/bin:$PATH" \
    -e MOP_SHARD="$PU_SHARD" \
    -e MOP_BUS_CONFIG="$shard_creds" \
    "$${llm_env[@]}" \
    "$HOME/.local/bin/claude $claude_args"
trap 'tmux -L "$PU_NAME" kill-session -t "$PU_NAME" 2>/dev/null; exit 0' TERM INT
while tmux -L "$PU_NAME" has-session -t "$PU_NAME" 2>/dev/null; do sleep 10 & wait $!; done
"""


def shard_of(origin):
    """Шард (он же проект) по origin репозитория.

    Basename без .git, и это ЕДИНСТВЕННОЕ определение проекта в системе.
    Соблазн взять хеш от полного origin есть — тогда два одноимённых репозитория
    на разных хостах не слились бы в один шард. Но имена папетов уже строятся отсюда же
    (`pu-<проект>-<n>`), и завести рядом второе, более точное понятие «проект»
    значит получить два места, по-разному отвечающих на вопрос «чей это папет».
    Цена честная и названа: одинаковые basename делят шард ровно так же, как
    уже делят имена. Понадобится развести — сюда добавляется суффикс от
    sha256(origin), и больше никуда."""
    return os.path.basename(origin).removesuffix(".git")


def shard_of_name(name):
    """Шард по имени папета: pu-<проект>-<n>. Откат для случая, когда клона
    ещё нет, — origin спросить не у кого, а имя уже есть."""
    if not name.startswith(JOB_PREFIX):
        return ""
    return name[len(JOB_PREFIX):].rsplit("-", 1)[0]


def clone_dir(name):
    return f"{HOME}/puppets/{name}"


def job_spec(name, origin, profile=None, cont=False):
    """Спека джоба. cont=True — первому подъёму по этой спеке разрешено поднять
    историю каталога (`claude --continue`).

    По умолчанию ЧИСТО, и умолчание выбрано так намеренно: подъём с историей
    нужен ровно там, где работу продолжают под другой моделью, а везде ещё
    (новый папет, рецикл, лечение) чистый старт — половина смысла операции."""
    project = os.path.basename(origin).removesuffix(".git")
    profile = profile or config.get("MOP_DEFAULT_LLM")
    prof = llm.get(profile)
    if prof is None:
        # Протухший Meta.llm у работающего джоба: профиль удалили из реестра,
        # а джоб жив. Отказ обязан звать папета по имени — иначе искать, кто
        # именно не перерегистрируется, придётся по трассе.
        raise RuntimeError(f"{name}: нет LLM-профиля {profile}; есть: "
                           f"{', '.join(llm.profiles())} (mop llm)")
    llm_env = "".join(f"{k}={v}\n" for k, v in prof["env"].items())
    meta = {"origin": origin, "llm": profile}
    env = {
        "PU_NAME": name,
        "PU_ORIGIN": origin,
        "PU_PROJECT": project,
        "PU_SHARD": shard_of(origin),
        "PU_SEED": config.get("MOP_PUPPET_SEED"),
        "HOME": HOME,
        "PATH": config.get("MOP_PUPPET_PATH").replace("{HOME}", HOME),
        # Разовый токен: врапер гасит его маркером в клоне, поэтому историю
        # поднимет только первый подъём по этой спеке, а рестарты — чистые.
        "PU_CONTINUE": str(time.time()) if cont else "",
        # LLM-профиль: имена и эндпоинт — здесь, ключ — на узле
        "PU_LLM": profile,
        "PU_LLM_ENV": base64.b64encode(llm_env.encode()).decode(),
        "PU_LLM_KEY_VAR": prof.get("key") or "",
        "PU_LLM_AUTH_VAR": prof.get("auth_var") or "ANTHROPIC_AUTH_TOKEN",
    }
    return {"Job": {
        "ID": name,
        "Name": name,
        "Datacenters": [nomad.POOL_DC],
        "Type": "service",
        "Meta": meta,
        "TaskGroups": [{
            "Name": "puppets",
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
                "Env": env,
                "Resources": {"CPU": 1000, "MemoryMB": MEM},
                "KillTimeout": 15 * 10**9,
            }],
        }],
    }}


def jobs(shard=None):
    """Джобы папетов. Префикс pu- ловит и pu-cleanup с его периодическими
    детьми; папеты — те, что врапер пометил origin'ом.

    shard=None -> срез ЭТОГО процесса: `mop master` ставит MOP_SHARD, и мастер
    проекта перестаёт видеть чужих папетов уже здесь, в ростере. Псевдошард
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


# ─── состояние папета ────────────────────────────────────────────────────
# КАК УЗНАТЬ РЕАЛЬНОЕ СОСТОЯНИЕ ПАПЕТА
#
# Узел присылает ФАКТЫ, вердикт собираем здесь. Папет сам ведёт две вещи, и
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
#   коннект есть + idle ............ free
#   коннект есть + busy ............ busy
#   коннект есть + requires_action . needs action
#   коннект есть + waiting ......... waiting for input
#
# Сами строки состояния — ПО-АНГЛИЙСКИ: их читает не только человек, но и
# модель (mop list, инструменты MCP), а промпты и скиллы пула англоязычны.
# Нет файла (старый claude / нет python3) -> None, и мы откатываемся на
# прежнюю tmux-эвристику. Детект протухшего логина остаётся на tmux — в файле
# он не виден.
#
# Пробник живёт в mop/session.py и исполняется агентом НА УЗЛЕ: сокет папета
# host-local, снаружи к нему не подключиться.
#
# Всё, что ниже, — ЧИСТЫЕ функции над этими фактами. Так вышло не случайно:
# пока состояние собиралось поверх exec, проверить его без живого пула было
# нельзя, и регрессия однажды спряталась именно здесь.
SESSION_STATES = ("idle", "busy", "requires_action", "waiting", "offline")


def facts(node, name):
    """Факты об одном папете с его узла."""
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


# Следы ХОДА: результат инструмента, запуск команд, реплика модели. Индикатор
# «✻ Baked for 49m · done» в этот список НЕ входит намеренно — им подписан и
# тот самый ход, который отказом и кончился.
_TURN_AFTER = re.compile(r"^\s*(⎿|Ran \d|● (?!API Error))", re.M | re.I)


def _outlived(activity, low, mark):
    """Пережита ли жалоба, стоящая в буфере на позиции mark.

    Восстановленная сессия приносит С СОБОЙ весь скроллбэк, включая отказ, на
    котором её когда-то оборвало: pu-cloudpub-1 поднялся с 251.5k токенов
    истории, прекрасно работал под другой моделью — и читался в ростере
    больным, потому что жалоба в буфере была.

    Отличает живого от больного не сама жалоба, а то, ЧТО ПОД НЕЙ. У живого
    ниже лежат следы ходов; у больного — только подпись оборванного хода,
    сводка задач и пустая рамка ввода. Переключение модели считается тем же
    доказательством: жалоба на прежнюю модель к новой не относится."""
    if low.rfind("set model to") > mark:
        return True
    return bool(_TURN_AFTER.search(activity, mark))


def _screen_complaint(activity):
    """Жалоба, видимая только на экране. -> (вид, текст) или None.

    Оба случая — один класс: сессия жива, отвечает на ping'и, но ни одного хода
    выдать не может. Для мастер это неотличимо от молчания.
    """
    low = activity.lower()
    # Модальный диалог claude. Ловим его по ФУТЕРУ, а не по тексту конкретного
    # вопроса: «Enter to confirm · Esc to cancel» стоит под любым выбором, и
    # список вопросов, которые claude умеет задать, нам не принадлежит — он
    # растёт с каждой версией, а список причин залипания расти не должен.
    #
    # Только в ХВОСТЕ экрана: диалог рисуется внизу, а уехавший вверх футер
    # означает уже отвеченный вопрос. Поймано на живом папете 2026-08-31 —
    # выбор, чем поднимать историю (251.5k токенов), и файл сессии при этом
    # показывал живую сессию, так что ростер читал папета здоровым.
    tail = [l for l in activity.splitlines() if l.strip()][-2:]
    if any("enter to confirm" in l.lower() for l in tail):
        what = "resume prompt" if "resume full session as-is" in low else "диалог"
        return "dialog", f"needs action: {what}"
    # Логин. Варианты экрана: "Not logged in · Run /login", "Login expired ·
    # Please run /login". Проверять до скоринга: у залипшего мид-таск в буфере
    # полно рабочих слов.
    #
    # Строки про Remote Control отсюда убраны вместе с самим --remote-control:
    # папета больше не ходят на мост claude.ai, и "/rc failed" на их экране
    # означало бы что угодно, только не болезнь. Для профилей с ключом
    # провайдера (glm) логин claude.ai вообще не при делах.
    if "not logged in" in low or "login expired" in low:
        return "login", ("not logged in" if "not logged in" in low else "login expired")
    # Квота модели и прочие отказы провайдера. Жалоба остаётся в скроллбэке и
    # после лечения — актуальна она только пока её не пережили.
    mark = low.rfind("out of usage credits")
    if mark >= 0 and not _outlived(activity, low, mark):
        m = re.search(r"keep using ([^\s]+(?: [0-9.]+)?)", activity, re.I)
        return "quota", (f"no model quota: {m.group(1)}" if m else "no model quota")
    mark = low.rfind("api error")
    if mark >= 0 and not _outlived(activity, low, mark):
        text = _api_error_text(activity)
        if text:
            return "error", f"error: {text}"
    return None


def _api_error_text(activity):
    """Человекочитаемая часть отказа провайдера, либо None.

    Экран: «● API Error: Request rejected (429) · [1308][Usage limit reached
    for 5 hour. Your limit will reset at …][<request id>]». Мастеру нужен
    ТОЛЬКО средний блок: код и request id ему ничего не говорят, а «Request
    rejected (429)» умалчивает главное — когда квота вернётся.

    Сообщение длинное, и рендер claude переносит его на следующую строку. Где
    оно кончилось, видно по балансу скобок, а не по концу строки: пустые строки
    агент из пейна уже вырезал, поэтому следующий блок экрана начинается сразу
    за жалобой. Склейка нормализует отступ переноса — она врёт, если рендер
    разорвал слово посередине, и тогда в таблицу приедет лишний пробел.
    """
    m = re.search(r"API Error:", activity, re.I)
    if not m:
        return None
    buf, depth, opened = [], 0, False
    for line in activity[m.end():].splitlines()[:6]:
        buf.append(line.strip())
        depth += line.count("[") - line.count("]")
        opened = opened or "[" in line
        if opened and depth <= 0:
            break
    text = " ".join(b for b in buf if b).strip()
    # Блок со словами и есть сообщение: код и request id пробелов не содержат.
    for group in re.findall(r"\[([^\[\]]*)\]", text):
        if " " in group.strip():
            return group.strip()
    return text or None



def _tmux_guess(activity):
    """Древний скоринг по словам в буфере. Работает только там, где файла
    сессии нет (старый claude / нет python3 на узле). -> строка или None."""
    low = activity.lower()
    work = sum(1 for x in ("working", "herding", "garnishing", "finding",
                           "checking", "running") if x in low)
    idle = sum(1 for x in ("резерв", "reserve", "waiting", "idle",
                           "свободен", "await") if x in low)
    if work > idle:
        return "busy"
    if idle > work:
        return "free"
    return None



def _work_branch(clone):
    """Ветка папета, если она не дефолтная, — иначе None.

    Занятость места — это НЕ имя ветки, и здесь оно нужно только чтобы
    показать человеку, где папет сидит."""
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
    отправлено» (поймано на живом папете).

    «uncommitted» и «unpushed» показываем РАЗДЕЛЬНО: это разные состояния и
    разный разговор с агентом. Сложив их в одно «только локально: 3», мастер
    однажды сказал переродившемуся месту «у тебя было 3 локальных коммита»,
    тогда как там лежали 3 несохранённых файла и ноль коммитов."""
    if st == "requires_action":
        return "needs action"
    if st == "waiting":
        return "waiting for input"
    if st in ("offline", "hung"):
        return "HUNG (not responding)"
    if st == "busy":
        branch = _work_branch(clone)
        return f"busy: {branch}" if branch else "busy"

    if not clone:
        return "free"
    dirty, ahead = clone.get("dirty") or 0, clone.get("ahead") or 0
    if dirty or ahead:
        what = ", ".join(p for p in (f"uncommitted: {dirty}" if dirty else "",
                                     f"unpushed: {ahead}" if ahead else "") if p)
        return f"busy: {clone.get('cur')} ({what})"
    cur = clone.get("cur")
    return f"free ({cur})" if cur else "free"


def _state_from_clone(clone):
    """Последний откат: одна лишь ветка клона."""
    if not clone:
        return "no clone yet"
    branch = _work_branch(clone)
    return f"busy: {branch}" if branch else "free"


def puppet_state(f):
    """Занятость места по фактам с узла. ЧИСТАЯ функция: см. блок выше.

    «Свободен» означает, что в клоне нет несохранённой работы, а не что сессия
    молчит. На этом стоит решение мастера о диспатче, и ломать условие нельзя.
    """
    if not f or f.get("error"):
        return f"HUNG ({(f or {}).get('error', 'no answer')[:40]})"
    if not f.get("present"):
        return "HUNG (no tmux session)"

    activity = f.get("screen") or ""
    if not activity.strip():
        return "free"

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


def is_free(state):
    """Свободен ли папет по строке состояния из puppet_state.

    По ПРЕФИКСУ: у свободного места состояние обычно несёт ещё и ветку —
    «free (master)». Точное сравнение врало и до переезда на шину:
    таблица показывала свободные места, а подсказка под ней уверяла, что
    свободных нет. На этом ответе стоит и решение о диспатче, и выбор жертвы
    для рецикла."""
    return state.startswith("free")


# ─── сводки для фронтендов ───────────────────────────────────────────────
def _collect():
    """Ростер Nomad плюс состояние с узлов, ОДНИМ заходом.

    Общая часть list и doctor. Раньше каждый ходил на узлы сам и платил по
    четыре рукопожатия exec'а за папета — на десяти папетах это сорок
    последовательных подключений, и столько же ещё раз, если следом звали
    doctor. Теперь: один запрос в Nomad за ростером и по одному запросу на
    УЗЕЛ за всеми его папетами, параллельно по одному соединению.

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

    # Место папетов — ОТДЕЛЬНЫЙ поезд с щедрым таймаутом: du небыстрый, и
    # вплавить его в states значило бы читать медленный обмер как «агент
    # молчит». Не доехало — в колонке прочерк, список состояний цел.
    try:
        sizes = bus.request_many({
            node: {"verb": "sizes", "names": [i["job"]["ID"] for i in its]}
            for node, its in by_node.items()}, timeout=45)
    except bus.BusError as e:
        sizes = {node: bus.BusError(str(e)) for node in by_node}

    for node, its in by_node.items():
        answer = answers.get(node)
        # Молчащий агент — ОТДЕЛЬНАЯ болезнь, не "папет завис": папет при этом
        # может прекрасно работать, и рестартить его нельзя.
        if isinstance(answer, Exception):
            for i in its:
                i["state"] = f"AGENT SILENT ({answer})"
            continue
        got = (answer or {}).get("puppets") or {}
        sanswer = sizes.get(node)
        sgot = ({} if isinstance(sanswer, Exception)
                else ((sanswer or {}).get("sizes") or {}))
        for i in its:
            i["state"] = puppet_state(got.get(i["job"]["ID"]))
            i["disk_kb"] = sgot.get(i["job"]["ID"])
    return items


def puppet_rows():
    """Папета как ДАННЫЕ: [{name, node, alloc_status, state, llm, origin,
    disk_kb}]. disk_kb — клон плюс target, обмеряется спросом; None — du не
    доехал, это прочерк, а не ноль."""
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
            "llm": meta.get("llm", config.get("MOP_DEFAULT_LLM")),
            "origin": meta.get("origin", "?"),
            "disk_kb": item.get("disk_kb"),
        })
    return rows


def pool():
    """Узлы пула как ДАННЫЕ: [{name, status, free_mb, total_mb, slots, error}].

    Только датацентр пула: джобы папетов объявляют его, и планировщик на узлы
    других dc не смотрит вовсе. Показать такой узел свободными слотами —
    пообещать то, чего планировщик не даст: управляляющая машина в control
    однажды так светилась тремя слотами, пока два папета стояли в queued."""
    out = []
    for n in nomad.client().nodes.get_nodes():
        if n.get("Datacenter") != nomad.POOL_DC:
            continue
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
#   not logged in/expired  -> раздать креды на пул, затем restart папета
#   pending/failed/lost    -> alloc stop: Nomad пересоздаёт сразу, минуя
#                             restart-backoff (до 30 мин)
#   no model quota/error   -> печать /model в пейн; рестарт квоту не вернёт
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
    queued = (job.get("JobSummary", {}).get("Summary", {}).get("puppets") or {}).get("Queued", 0)
    if queued:
        return {"name": name, "alloc": None, "action": None,
                "diagnosis": "queued — нет свободных слотов в пуле"}
    return {"name": name, "alloc": None, "action": None, "diagnosis": "нет аллокации"}


def _action_for(state):
    """Лечение для состояния. False — состояние здоровое, проблемы нет."""
    # Молчит АГЕНТ, а не папет. Рестарт папета тут ничего не лечит и вполне
    # может убить живую работу в клоне: про сам папет мы в этот момент не
    # знаем ничего. Показать — да, трогать — нет.
    if state.startswith("AGENT SILENT"):
        return None
    if state.startswith("HUNG"):
        return "restart"
    # Сессия жива, но упёрлась в запрос действия и сама не сдвинется.
    # "waiting for input" НЕ трогаем: это бывает и нормальным межходовым
    # состоянием, автолечить его опасно — только показываем.
    if state.startswith("needs action"):
        # Рестарт лечит и залипание на диалоге: разрешение на подъём истории
        # разовое, и врапер погасил его маркером ещё до запуска claude — папет
        # поднимется чисто и спрашивать будет не о чем.
        return "restart"
    if state.startswith(("not logged in", "login expired")):
        return "login+restart"
    if state.startswith(("no model quota", "error")):
        return "model"
    return False


# ─── рецикл ───────────────────────────────────────────────────────────────
def wipe(node, name):
    """Глагол wipe напрямую, без остановки джоба. Агент сам откажет, если
    tmux-сессия жива: голый wipe — для уже остановленного папета, полный
    цикл (стоп → снос → подъём) — recycle."""
    r = bus.request(node, "wipe", name=name, timeout=600)
    if "error" in r:
        raise RuntimeError(r["error"])
    return r


def _wait_stopped(name):
    """Дождаться, пока последняя аллокация перестанет быть running.

    Останов джоба убивает задачу через KillTimeout (15с) и вместе с врапером —
    tmux-сессию. Сносить рабочую копию под живой сессией нельзя, поэтому ждём
    именно терминального статуса аллокации, а не «джоб dead» в API: между
    ними сидит остановка задачи на узле."""
    for _ in range(45):
        alloc = nomad.latest_alloc(name)
        if not alloc or alloc["ClientStatus"] != "running":
            return
        time.sleep(2)
    raise RuntimeError(f"аллокация {name} не останавливается — узел жив?")


def recycle(name):
    """Пересоздать папета на чистой рабочей копии. -> {node}.

    Клон НЕ переклонируется: сбрасывается на месте глаголом wipe (reset
    отслеживаемого + clean -xdff, который выметает и игнорируемое, но
    щадит подсеянное врапером: .env*, .providers), target-каталог
    удаляется целиком — он и есть почти весь объём. Первая
    сборка после рецикла долгая, поэтому это крайняя мера, а не гигиена.

    Порядок ОБЯЗАТЕЛЕН: остановить джоб → дождаться терминала → wipe →
    перерегистрировать спеку. Между решением «свободен» и сносом папету
    успевает прилететь задача (mop send идёт мимо мастера, у пула несколько
    мастеров), и остановленный джоб — единственное состояние, в котором
    сессии гарантированно нет. Перерегистрация, а не alloc_restart: врапер
    живёт в спеке джоба, рестарт аллокации поднял бы старую."""
    job = nomad.get_job(name)
    meta = job.get("Meta") or {}
    origin = meta.get("origin")
    if not origin:
        raise RuntimeError(f"у {name} нет origin в Meta — это не папет?")
    llm = meta.get("llm", config.get("MOP_DEFAULT_LLM"))
    alloc = nomad.latest_alloc(name)
    node = alloc["NodeName"] if alloc else None
    if not node:
        raise RuntimeError(f"у {name} нет аллокации — рециклить нечего")

    nomad.deregister(name, purge=False)
    _wait_stopped(name)
    try:
        wipe(node, name)
    except RuntimeError as e:
        raise RuntimeError(f"{e}; джоб остановлен — после починки узла "
                           f"повтори: mop recycle {name}")
    nomad.register(job_spec(name, origin, llm))
    return {"node": node}


# ─── ввод в TUI папета ───────────────────────────────────────────────────
# Всё здесь адресуется УЗЛОМ, а не аллокацией: alloc exec умер вместе со своей
# адресацией, и агент подписан на субъект узла.
def type_command(node, name, command):
    """Напечатать слэш-команду в tmux-пейн папета и вернуть экран после неё.

    Печатью, а не сообщением по каналу: слэш-команды через канал не проходят
    (сообщение кладётся в очередь с skipSlashCommands), а у папета с
    исчерпанной квотой любой ход падает, не начавшись — слэш-команду же
    исполняет сам TUI, ход на неё не тратится.

    Белый список команд проверяет АГЕНТ: проверка на этой стороне осталась бы
    подсказкой пользователю, а не правом."""
    r = bus.request(node, "type", name=name, command=command, timeout=45)
    if "error" in r:
        raise RuntimeError(r["error"])
    return r.get("screen") or ""


def press_enter(node, name):
    """Подтвердить диалог. Только увидев его: слепой Enter на папет без
    диалога отправил бы пустой ход."""
    return type_command(node, name, "")


def switch_model(node, name, model):
    """Перевести папет на другую модель, напечатав /model в его tmux-пейн.

    `/model` не переключает молча — он спрашивает «Switch model?» с уже
    выделенным «Yes». Подтверждаем вторым Enter, но ТОЛЬКО увидев диалог."""
    out = type_command(node, name, f"/model {model}")
    if "switch model?" in out.lower():
        out = press_enter(node, name)
    if "switch model?" in out.lower():
        raise RuntimeError("диалог смены модели не закрылся")


def pane_lines(node, name):
    """Весь буфер tmux-пейна папета (история + экран)."""
    r = bus.request(node, "tail", name=name)
    if "error" in r:
        raise RuntimeError(f"tmux в {name}: {r['error']}")
    return r.get("lines") or []
