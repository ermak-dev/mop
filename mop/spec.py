"""Спека джоба папета: врапер, который в ней едет, и ограничение размещения.

Чистые функции без сети (#152): собирает спеку сервис кластера, а читают её
форму и doctor (устарела ли), и ростер (ждёт ли место). Пока всё это жило в
puppets.py рядом с клиентом шины мастера, мастер тащил за собой код сервера,
а имена группы, задачи и ключа меты узла стояли литералами в разных местах.

Врапер живёт в спеке (CLAUDE.md): правка здесь доходит до папета только
перерегистрацией, и любое расхождение собранной спеки с зарегистрированной
spec_is_stale видит у всего пула сразу. Поэтому перенос сверяется со слепком
байт в байт: tests/spec.py.
"""
import base64
import hashlib
import json
import os
import re
import time

from . import config, driver, llm, nomad
from .domain import Project

# Значения этой установки — .env поверх дефолтов; см. mop/config.py.
MEM = config.num("MOP_PUPPET_MEM_MB")   # бюджет папета, МБ: резерв планировщика и мера слотов
MEM_MAX = config.num("MOP_MEM_MB")  # потолок, за которым cgroup всё-таки убивает, если проект не просил своего
HOME = config.get("MOP_HOME")             # $HOME на узлах пула
USER = config.get("MOP_USER")               # под кем идут задачи

# Просьбы проектов (#197): {проект: {настройка: значение}} из их `.mop`. Файл
# кладёт роль cluster из манифестов `mop deploy`: сервис строит спеку под
# учёткой пула и файлов контроллера не видит.
ASKS_FILE = os.path.expanduser("~/.config/mop/project-asks.json")


def read_asks(path=None):
    """Просьбы всех проектов. -> {проект: {настройка: значение}}; в проект
    их переводит domain.Project.of (#204).

    Нет файла или проекта в нём -- пусто, и папет получает значения
    установки: так живёт любой проект до первого прогона deploy после его
    заведения. Битый файл -- падение: его пишет ansible целиком, и
    молчаливый откат на установку спрятал бы поломку прогона."""
    try:
        with open(path or ASKS_FILE) as f:
            return json.load(f)
    except FileNotFoundError:
        return {}


def memory(asks, ceiling, budget):
    """Память папета (#197). -> (резерв, потолок), МБ. Чистая функция.

    Потолок -- просьба проекта (MOP_MEM_MB его `.mop`), иначе установки:
    ceiling -- это MOP_MEM_MB, .env поверх дефолта. Резерв -- бюджет
    установки (budget, MOP_PUPPET_MEM_MB), но не выше потолка: MemoryMaxMB
    ниже MemoryMB Nomad отвергает, а слоты пообещали бы больше тела.

    Просьба, которую нельзя применить, -- ValueError: подмена значением
    установки молча дала бы проекту не то, что он просил."""
    ask = asks.get("MOP_MEM_MB")
    if ask is not None:
        text = str(ask).strip()
        if not text.isdigit() or int(text) <= 0:
            raise ValueError(f"MOP_MEM_MB={ask!r} in the project's .mop is not "
                             f"a whole number of megabytes")
        ceiling = int(text)
    return min(budget, ceiling), ceiling


# Имена внутри спеки. Каждое читается и снаружи — Nomad отдаёт по ним сводку
# группы и задачи, — поэтому литералом в двух местах им не место: разъедутся
# молча, и ростер перестанет видеть очередь.
GROUP = "puppets"        # группа задач папета: по ней JobSummary считает Queued
TASK = "claude"          # имя задачи внутри группы папета
# Ключ меты УЗЛА. Остался прежним при переименовании (#85): его объявляет
# клиент Nomad, и переименовать значит оставить всякую уже
# зарегистрированную спеку без узлов, которые её принимают.
META_PROJECTS = "mop_projects"
# Подстановку `${…}` Nomad вычисляет сам при планировании: здесь она нужна
# именно такой, одним долларом, — это ссылка на мету узла, а не текст.
PROJECTS_TARGET = "${meta." + META_PROJECTS + "}"
# Потолок памяти, который узел готов дать одному папету (#197): его
# MOP_BODY_MEM_CAP_MB, которую `mop deploy` рендерит в meta client.hcl.
META_MEM_CAP = "mop_mem_cap_mb"
MEM_CAP_TARGET = "${meta." + META_MEM_CAP + "}"



# Врапер делится надвое (docs/DRIVER.md).
#
# Внешний — вот он, и он драйвер-агностичен: узел сам знает, в чём живёт его
# папет, а мастер в момент сборки спеки этого знать не может — Nomad выбирает
# узел уже после регистрации. Поэтому в спеке не может стоять ни `pct exec`,
# ни что-либо ещё про тело.
OUTER = r"""
exec "$HOME/mop/bin/mop" driver run "$PU_NAME"
"""

# Внутренний — собственно задача: довести тело до "клон есть, claude в tmux" и
# жить, пока жива tmux-сессия. Смерть врапера = рестарт/переезд папета силами
# Nomad; в новом теле врапер сам разворачивает всё заново.
#
# Едет в спеке base64 в переменной окружения, и это снимает целый класс
# ловушек: подстановку `${…}` Nomad вычисляет сам во всей строке команды,
# включая комментарии, — отсюда были двойные доллары ниже. В base64 он не
# заглядывает — и поэтому двойные доллары пришлось убрать, а не оставить как
# косметику: они были не защитой, а заменой. Nomad схлопывал `$$` в `$` перед
# запуском, а в base64 схлопывать некому, и `$${p##*/}` доехало бы в тело
# буквально — то есть как PID, приклеенный к мусору. Первый же подъём
# контейнерного папета упал на этом: «syntax error near unexpected token `(`».
#
# Обе ловушки при этом остаются верными и разъезжаются по механизмам: правка
# логики тела доезжает прогоном `mop deploy`, правка логики сессии по-прежнему
# требует перерегистрации джоба.
WRAPPER = r"""
set -e
# Paths come from the job spec (#155), not from this text. An empty one is a
# refusal before the first command: `rm -rf "$d"` and free_dir below over an
# empty string would hit everything in the body.
: "${PU_CLONE:?no PU_CLONE in the task environment}"
: "${PU_TARGET:?no PU_TARGET in the task environment}"
: "${PU_SECRETS:?no PU_SECRETS in the task environment}"
: "${PU_PROJECT_CREDS:?no PU_PROJECT_CREDS in the task environment}"
: "${PU_SECRETS_DIR:?no PU_SECRETS_DIR in the task environment}"
d="$PU_CLONE"
# The old session dies first, before anything touches the directory. It used to
# die at the very bottom, just before the new session was opened -- some 140
# lines and one `npx playwright install` later -- so a retarget removed the
# clone out from under a live claude.
tmux -L "$PU_NAME" kill-session -t "$PU_NAME" 2>/dev/null || true

# ...and whatever outlived it holding the directory as its cwd is killed too.
# A puppet's own MCP server did exactly that (2026-08-31): orphaned by the
# kill above, it kept answering `agents` from memory while every `send` failed
# for the rest of the session, because its cwd pointed at an unlinked inode.
# The same thing was behind the older "fatal: cannot change to '<clone>'".
# The clone may already be gone (the disk sweep can remove it), and the kernel
# marks such a cwd " (deleted)" -- match on the name with that suffix stripped.
free_dir() {
    local dir="$1" cwd pid
    for p in /proc/[0-9]*; do
        pid=${p##*/}
        cwd=$(readlink "$p/cwd" 2>/dev/null) || continue
        cwd=${cwd% (deleted)}
        case "$cwd" in
            "$dir"|"$dir"/*) kill "$pid" 2>/dev/null || true ;;
        esac
    done
}
free_dir "$d"

if [ -d "$d/.git" ] && [ "$(git -C "$d" remote get-url origin)" != "$PU_ORIGIN" ]; then
    rm -rf "$d"
fi
if [ ! -d "$d/.git" ]; then
    mkdir -p "$(dirname "$d")"
    # Зеркало проекта, если тело принесло его с образом: объекты берутся
    # локально, а недостающее -- то, что появилось в origin после сборки
    # образа, -- git дотягивает по сети сам. Клон остаётся полноценным и
    # свежим, отставание зеркала лечится обычным fetch, а не пересборкой.
    #
    # --dissociate не ставим намеренно: он копирует объекты в клон и съедает
    # весь выигрыш. Цена названа: клон зависит от зеркала, и снос зеркала
    # оставит его с битыми alternates -- поэтому зеркало лежит в образе, то
    # есть в том же теле и ровно столько же, сколько сам клон.
    #
    # Нет зеркала -- клонируем как раньше. У драйвера host его не бывает
    # вовсе, и ветка обязана быть тихой: отказ здесь означал бы папета,
    # который не поднимается на обычном узле.
    mirror="$HOME/.cache/mop-mirror/$PU_PROJECT.git"
    if [ -d "$mirror" ]; then
        git clone -q --reference "$mirror" "$PU_ORIGIN" "$d"
    else
        git clone -q "$PU_ORIGIN" "$d"
    fi
fi
# Отказ копирования -- отказ, а не тишина (#62): семя, которое не доехало,
# это папет без .env, читающийся живым. Отсутствие файла при этом штатно --
# у большинства проектов семени нет вовсе, и `[ -e ]` в списке && не роняет
# скрипт; роняет только cp, который не смог.
for pat in ${PU_SEED//,/ }; do
    for f in "$HOME/puppet-env/$PU_PROJECT"/$pat; do
        [ -e "$f" ] && cp -a "$f" "$d/"
    done
done
# Секреты проекта (#127-#129): bootstrap кладёт их в тело при каждом старте,
# отсюда файлы -- в клон по своим путям, переменные -- в сессию. Файлы --
# свежие на каждом подъёме, поверх вчерашних. Переменные читаются строками,
# а не source: файл секретов не исполняем. Они идут в -e ДО наших, чтобы
# проект не мог подменить MOP_PROJECT или кред шины.
sec="$PU_SECRETS_DIR"
if [ -d "$sec/files" ]; then
    cp -a "$sec/files/." "$d/"
fi
project_env=()
if [ -f "$sec/vars.env" ]; then
    while IFS= read -r kv; do
        if [ -n "$kv" ]; then project_env+=(-e "$kv"); fi
    done < "$sec/vars.env"
fi
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
    # `jq empty` is not a validity check: a zero-byte file is an empty jq input
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
# установки (sandbox.yaml), врапер о них не знает.
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
# deny-правила фильтруют сам список инструментов (filterToolsByDenyRules в
# бинаре claude), так что SendMessage и ListAgents до модели не доезжают и
# выбирать между ними и каналом пула ей не приходится. На режим прав фильтр не
# смотрит, поэтому работает и под --dangerously-skip-permissions.
#
# Почему вообще: встроенный механизм находит только сессии этого хоста. Пока
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
TARGET_DIR="$PU_TARGET"
if [ -d "$TARGET_DIR" ] \
    && [ "$(du -sm "$TARGET_DIR" 2>/dev/null | cut -f1 || echo 0)" -gt 30000 ]; then
    rm -rf "$TARGET_DIR"
fi
# LLM-профиль папета: набор переменных для tmux -e. Статическая часть
# (эндпоинт и карта моделей) приезжает в PU_LLM_ENV из спеки джоба, а ключ —
# только с узла: в спеке джоба секретам не место (её видно в UI Nomad).
llm_env=()
if [ -n "$PU_LLM_ENV" ]; then
    while IFS= read -r kv; do
        [ -n "$kv" ] && llm_env+=(-e "$kv")
    done <<< "$(printf '%s' "$PU_LLM_ENV" | base64 -d)"
fi
if [ -n "$PU_LLM_KEY_VAR" ]; then
    keyfile="$PU_SECRETS"
    key=""
    # sed, а не source: файл с ключами не исполняем
    # Доллар здесь одинарный, и так и должно быть: врапер едет в спеке base64,
    # Nomad в него не заглядывает и ничего не подставляет (см. блок над
    # WRAPPER). Двойной доллар, который стоял тут, пока врапер был строкой
    # команды, в теле доехал бы буквально — и имя ключа не подставилось бы.
    [ -f "$keyfile" ] && key=$(sed -n "s/^${PU_LLM_KEY_VAR}=//p" "$keyfile" | tail -1)
    if [ -z "$key" ]; then
        # Валимся громко: без ключа claude поднимется и будет отбивать каждый
        # ход 401-й, а папет будет читаться как живое и свободное.
        echo "LLM profile $PU_LLM: node has no key $PU_LLM_KEY_VAR in $keyfile -- hand out: mop login" >&2
        exit 1
    fi
    llm_env+=(-e "$PU_LLM_AUTH_VAR=$key")
fi

# Креды проекта, а не узла. Без этого папет ходил бы на шину под кредом агента и
# мог бы написать в чужой проект: агент видит, кого спрашивают, но не видит,
# кто спрашивает, и такую подмену не поймал бы. Файл раскатывает deploy/setup.yml
# по одному на проект; если его нет -- валимся громко, потому что папет без шины
# читается мастером как живой, но молчащий.
project_creds="$PU_PROJECT_CREDS"
if [ ! -f "$project_creds" ]; then
    echo "no credentials for project $PU_PROJECT in $project_creds -- set it up: mop project add $PU_ORIGIN" >&2
    exit 1
fi

# a dedicated tmux SERVER per puppet (-L): with the default server every
# session on the node lives in the cgroup of whichever wrapper started the
# server first, and one task budget OOM-kills all puppets at once
# env must go through -e: a plain env prefix only reaches the tmux SERVER when
# this wrapper happens to start it, and every later session inherits the first
# wrapper's variables (all puppets ended up sharing one CARGO_TARGET_DIR)
claude_args="--dangerously-skip-permissions"
# Продолжение истории каталога -- ровно один подъём на перерегистрацию спеки.
# Одноразовость здесь не прихоть: рестарт аллокации спеку не перечитывает, а
# лечение залипшего папета -- это именно рестарт. Липкий --continue возвращал
# бы вылеченного ровно в тот контекст, на котором он залип, то есть лечил бы
# симптом и воспроизводил болезнь. Поэтому мастер кладёт в спеку разовый
# токен, а врапер гасит его маркером в клоне: совпал -- поднимаемся чисто.
#
# --continue, а не --resume: без ID сессии resume открывает интерактивный
# выбор, и папет паркуется на нём намертво -- выбрать строку ему некому.
marker="$d/.git/mop-continue"
if [ -n "${PU_CONTINUE:-}" ] \
    && [ "$(cat "$marker" 2>/dev/null)" != "$PU_CONTINUE" ]; then
    printf '%s' "$PU_CONTINUE" > "$marker"
    claude_args="$claude_args --continue"
fi
# Сколько заданий сборки телу положено: MOP_CORES (#44). На host-узле он
# лежит в node.env; в pve-теле node.env нет, и nproc тела РОВНО то, что
# испечено из MOP_CORES сборкой образа. Зашитая единица врала про машину
# кратно: телу с четырьмя ядрами один поток сборки.
cores="$(nproc)"
mc="$(grep -a '^MOP_CORES=' "$HOME/.config/mop/node.env" 2>/dev/null | tail -1 | cut -d= -f2)"
[ -n "$mc" ] && cores="$mc"
# PATH идёт присваиванием В САМОЙ КОМАНДЕ, а не через -e, и это не стиль.
# tmux кладёт -e в окружение СЕССИИ, и обычные переменные оттуда до панели
# доезжают -- CARGO_TARGET_DIR и MOP_PROJECT ниже приезжают именно так. А PATH
# панели он берёт от своего сервера, и значение из -e просто не применяется.
# Измерено 22.09, tmux 3.4: `new-session -e PATH=/ZZZ:$PATH -e FOO=bar` дал
# процессу FOO=bar и ИСХОДНЫЙ PATH, при том что show-environment показывал оба.
# Цена была тихой: каталог bin/ клона кладётся первым ради того, чтобы у папета
# работала команда проекта (`rug` у rugent), -- и не работала, а правила
# проекта требуют звать её именно так.
#
# Присваивание внутри строки команды, а не префиксом перед `tmux`: префикс
# уехал бы в окружение СЕРВЕРА tmux, а это ровно та ловушка, из-за которой все
# папеты однажды делили один CARGO_TARGET_DIR.
tmux -L "$PU_NAME" new-session -d -s "$PU_NAME" -c "$d" \
    "${project_env[@]}" \
    -e CARGO_TARGET_DIR="$PU_TARGET" \
    -e CARGO_BUILD_JOBS="$cores" \
    -e MOP_PROJECT="$PU_PROJECT" \
    -e MOP_BUS_CONFIG="$project_creds" \
    "${llm_env[@]}" \
    "PATH=$d/bin:$PATH $HOME/.local/bin/claude $claude_args"
trap 'tmux -L "$PU_NAME" kill-session -t "$PU_NAME" 2>/dev/null; exit 0' TERM INT
while tmux -L "$PU_NAME" has-session -t "$PU_NAME" 2>/dev/null; do sleep 10 & wait $!; done
"""


def task_env(name, origin, profile, prof, cont=False, mem=0):
    """Окружение задачи папета. Одно место и для job_spec, и для версии
    шаблона: current_version зовёт его с пустым профилем, не спрашивая реестр
    профилей (#174) — набор ключей от профиля не зависит. mem -- потолок
    памяти папета, МБ (spec.memory)."""
    llm_env = "".join(f"{k}={v}\n" for k, v in prof["env"].items())
    project = driver.project_of(origin)
    env = {
        "PU_NAME": name,
        "PU_ORIGIN": origin,
        "PU_PROJECT": project,
        "PU_SEED": config.get("MOP_PUPPET_SEED"),
        # Пути в теле (#155): врапер их не собирает, а читает. Одно место на
        # мастера, агента и врапер — mop/driver; копия в тексте врапера
        # расходилась бы с ними молча, узнать было бы только по симптому.
        "PU_CLONE": driver.clone_dir(name),
        "PU_TARGET": driver.target_dir(name),
        "PU_SECRETS": driver.SECRETS_FILE,
        "PU_PROJECT_CREDS": driver.project_creds(project),
        "PU_SECRETS_DIR": driver.project_secrets_dir(project),
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
        # Потолок памяти папета (#197): на host его держит cgroup задачи
        # (MemoryMaxMB), в pve-теле -- `pct --memory`, который ставит ensure
        # драйвера из этой переменной на каждом подъёме.
        "PU_MEM_MB": str(mem),
    }
    # Что `mop driver run` переливает в тело: всё окружение спеки, кроме
    # самого врапера. Списком здесь, а не копией там (#155).
    env["PU_CARRY"] = ",".join(env)
    # Внутренний врапер — в спеке, как и был, но base64: узел исполняет
    # его в теле, каким бы оно ни было. Утащить его в пакет на узле
    # значило бы молча поменять инвариант «правка сессии доезжает
    # перерегистрацией».
    env["PU_WRAPPER"] = base64.b64encode(WRAPPER.encode()).decode()
    return env


def job_spec(name, origin, profile=None, cont=False):
    """Спека джоба. cont=True — первому подъёму по этой спеке разрешено поднять
    историю каталога (`claude --continue`).

    По умолчанию чисто, и умолчание выбрано так намеренно: подъём с историей
    нужен ровно там, где работу продолжают под другой моделью, а везде ещё
    (новый папет, рецикл, лечение) чистый старт — половина смысла операции."""
    profile = llm.resolve(profile)
    prof = llm.get(profile)
    if prof is None:
        # Протухший Meta.llm у работающего джоба: профиль удалили из реестра,
        # а джоб жив. Отказ обязан звать папета по имени — иначе искать, кто
        # именно не перерегистрируется, придётся по трассе.
        raise RuntimeError(f"{name}: no LLM profile {profile}; available: "
                           f"{', '.join(llm.profiles())} (mop llm)")
    meta = {"origin": origin, "llm": profile}
    project = Project.of(origin, asks=read_asks())
    try:
        reserve, ceiling = memory(project.asks, MEM_MAX, MEM)
    except ValueError as e:
        raise RuntimeError(f"{name}: {e} ({project.name})")
    env = task_env(name, origin, profile, prof, cont, ceiling)
    meta[SPEC_META] = template_version(env)
    return {"Job": {
        "ID": name,
        "Name": name,
        "Datacenters": [nomad.POOL_DC],
        "Type": "service",
        "Meta": meta,
        "Constraints": [project_constraint(project.name), memory_constraint(ceiling)],
        "TaskGroups": [{
            "Name": GROUP,
            "Count": 1,
            "RestartPolicy": {
                "Attempts": 3,
                "Interval": 1800 * 10**9,
                "Delay": 15 * 10**9,
                "Mode": "delay",
            },
            "Tasks": [{
                "Name": TASK,
                "Driver": "raw_exec",
                "User": USER,
                "Config": {"command": "/bin/bash", "args": ["-c", OUTER]},
                "Env": env,
                "Resources": {"CPU": 1000, "MemoryMB": reserve, "MemoryMaxMB": ceiling},
                "KillTimeout": 15 * 10**9,
            }],
        }],
    }}


# Слово, которым узел объявляет, что обслуживает любой проект. Так говорят про
# себя узлы, где тело равно узлу: им нечего готовить заранее. Узел, чьи тела —
# контейнеры, перечисляет проекты поимённо — те, чьи образы на нём собраны.
ANY_PROJECT = "any"


def project_constraint(project):
    """Ограничение размещения: узел обязан уметь обслужить этот проект.

    До появления тел вопрос не стоял — любой узел пула умел любого папета. У
    узла-гипервизора это перестало быть правдой: тело клонируется из образа
    проекта, и папет проекта, чей образ там не собран, не поднимется никогда.
    Планировщик об этом не знал и ставил такого папета туда при первом же
    давлении; поймано на живом пуле (pu-rugent-6 на hyper), и лечилось руками.

    Регулярное выражение по списку через запятую, а не set_contains, потому
    что `any` обязано быть словом целиком: узлу общего назначения нечего
    перечислять, а новый проект заводится после прогона deploy и в перечне
    заведомо не окажется. Якоря не украшение — без них `mop` совпадёт с
    `mop2`, а `op` с `mop`, и оба промаха молчаливы."""
    return {"LTarget": PROJECTS_TARGET, "Operand": "regexp",
            "RTarget": f"(^|,)({ANY_PROJECT}|{re.escape(project)})(,|$)"}


def memory_constraint(ceiling):
    """Ограничение размещения: потолок узла не ниже потолка папета (#197).

    Без него просьба проекта больше, чем машина готова дать телу, молча
    переподписывала бы гипервизор. `>=` в Nomad 1.10 сравнивает численно,
    если обе стороны -- целые (scheduler/feasible.go, checkOrder; зеркало --
    nomad_order), иначе лексически; поэтому справа -- целое строкой, а deploy
    отвергает потолок узла не из одного целого. Узел без ключа в meta
    ограничение не проходит: папет не встаёт туда, где потолка не знают."""
    return {"LTarget": MEM_CAP_TARGET, "Operand": ">=", "RTarget": str(int(ceiling))}


def ceiling_of(job):
    """Потолок папета из зарегистрированной спеки: по ограничению, с которым
    её и размещает Nomad. None -- спека до #197, потолка узла она не
    спрашивает."""
    for c in job.get("Constraints") or []:
        if (c or {}).get("LTarget") == MEM_CAP_TARGET:
            return int(c["RTarget"])
    return None


_GO_INT = re.compile(r"[+-]?[0-9]+")


def nomad_order(op, left, right):
    """Порядок, как его считает Nomad 1.10 (checkOrder): обе стороны целые
    -- как целые, обе float -- как float, иначе лексически. Чистая функция.

    Зеркало нужно диагнозу unserved: иначе он и планировщик разошлись бы
    ровно на тех значениях, где лексический порядок врёт ("9" > "10").
    float -- приближение strconv.ParseFloat: пробелы по краям Go не
    принимает, python принимает, поэтому их отсекаем сами."""
    import operator
    cmp = {"<": operator.lt, "<=": operator.le, ">": operator.gt, ">=": operator.ge}[op]
    if _GO_INT.fullmatch(left) and _GO_INT.fullmatch(right):
        return cmp(int(left), int(right))
    try:
        if left == left.strip() and right == right.strip():
            return cmp(float(left), float(right))
    except ValueError:
        pass
    return cmp(left, right)


def unserved(project, nodes, ceiling=None):
    """Некуда ли поставить папета проекта. -> bool. Чистая функция;
    почему -- placement_gap."""
    return bool(placement_gap(project, nodes, ceiling))


def placement_gap(project, nodes, ceiling=None):
    """Почему папета проекта некуда поставить. Чистая функция.

    nodes -- [{status, eligible, meta}] узлов пула. Узел годится, если он
    ready, открыт для планирования и его meta.mop_projects проходит то же
    выражение, что уезжает в Nomad (project_constraint): иначе диагноз и
    планировщик разошлись бы на `mop2` против `mop`. Узел без ключа в meta
    ограничение не проходит -- так же отвечает и Nomad (#118).

    ceiling -- потолок папета (ceiling_of его спеки), None -- спека до #197.
    -> False, "image" (проект не обслуживает ни один узел) или "memory"
    (обслуживают, но потолок каждого ниже потолка папета, #197)."""
    rx = re.compile(project_constraint(project)["RTarget"])
    serving = [n for n in nodes
               if n.get("status") == "ready" and n.get("eligible", True)
               and META_PROJECTS in (n.get("meta") or {})
               and rx.search(n["meta"][META_PROJECTS])]
    if not serving:
        return "image"
    if ceiling is not None and not any(
            META_MEM_CAP in n["meta"]
            and nomad_order(">=", n["meta"][META_MEM_CAP], str(int(ceiling)))
            for n in serving):
        return "memory"
    return False


def queued(job):
    """Сколько аллокаций джоба ждёт места у планировщика."""
    return (job.get("JobSummary", {}).get("Summary", {}).get(GROUP) or {}).get("Queued", 0)


# Версия шаблона спеки в Meta джоба (#174). Врапер живёт в спеке, и джоб,
# зарегистрированный прежним mop, работает прежним врапером до перерегистрации
# (CLAUDE.md) — а обе проверки spec_is_stale ниже он проходит. После #155 так
# жил пятый папет чужого проекта, и ничто его не выдавало.
SPEC_META = "mop_spec"


def template_version(env):
    """Короткий хеш того, что общее у спек всех папетов: врапер, внешняя
    команда, НАБОР переменных, имена группы и задачи, форма ограничения.
    Значения конкретного папета (имя, origin, профиль, cont) в него не входят:
    иначе устаревшим читался бы каждый второй."""
    c = project_constraint("x")
    m = memory_constraint(0)
    shape = {"wrapper": WRAPPER, "outer": OUTER, "env": sorted(env),
             "group": GROUP, "task": TASK,
             "constraint": [c["LTarget"], c["Operand"]],
             "memory": [m["LTarget"], m["Operand"]]}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]


def current_version():
    """Версия шаблона, который собрал бы сегодняшний mop. Без реестра
    профилей: удалённый MOP_DEFAULT_LLM иначе ронял бы spec_is_stale, а ростер
    глотает падение как «спека свежая» — та самая тихая ошибка (#174)."""
    return template_version(task_env("pu-spec-1", "spec", "", {"env": {}}))


def spec_is_stale(job):
    """Опасна ли эта спека на узле-гипервизоре. Чистая функция.

    Джоб, зарегистрированный до раскола врапера и до ограничения размещения,
    несёт старый врапер — тот, что разворачивает папета прямо на узле, — и не
    несёт ограничения, которое не пустило бы его на гипервизор. Там это значит
    попытку завести клон и tmux на самом гипервизоре: падает, уходит в
    бесконечный рестарт и оставляет за собой каталоги.

    Ограничение живёт в спеке, а спека сама не перечитывается: всё, что
    зарегистрировано раньше, защиты не имеет. Узнать об этом можно было только
    по симптому — в логе задачи на узле, куда мастер проекта не смотрит.
    Поймано на pu-cloudpub-1, лечится `mop update <имя>`.

    И спека прежнего шаблона (#174): без версии в Meta или с другой."""
    if (job.get("Meta") or {}).get(SPEC_META) != current_version():
        return True
    task = job["TaskGroups"][0]["Tasks"][0]
    script = (task.get("Config") or {}).get("args") or ["", ""]
    if "driver run" not in script[-1]:
        return True
    return not any((c or {}).get("LTarget") == PROJECTS_TARGET
                   for c in (job.get("Constraints") or []))
