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
import dataclasses
import hashlib
import json
import os
import re
import time

from ..common import config, domain, llm, paths, soul, state
from .. import driver
from . import grants, nomad
from ..common.domain import JobMeta, Project

# Значения этой установки — .env поверх дефолтов; см. mop/common/config.py.
MEM = config.num("MOP_PUPPET_MEM_MB")   # бюджет папета, МБ: резерв планировщика и мера слотов
MEM_MAX = config.num("MOP_MEM_MB")  # потолок, за которым cgroup всё-таки убивает, если проект не просил своего
HOME = config.get("MOP_HOME")             # $HOME на узлах пула
USER = config.get("MOP_USER")               # под кем идут задачи

# Просьбы проектов (#197): {проект: {настройка: значение}} из их `.mop`. Файл
# кладёт роль cluster из манифестов `mop server deploy`: сервис строит спеку под
# учёткой пула и файлов контроллера не видит.
ASKS_FILE = paths.local("project-asks.json")


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
GROUP = state.GROUP     # группа задач папета: по ней JobSummary считает Queued (state.queued)
TASK = "claude"          # имя задачи внутри группы папета
# Ключ меты УЗЛА. Остался прежним при переименовании (#85): его объявляет
# клиент Nomad, и переименовать значит оставить всякую уже
# зарегистрированную спеку без узлов, которые её принимают.
META_PROJECTS = "mop_projects"
# Подстановку `${…}` Nomad вычисляет сам при планировании: здесь она нужна
# именно такой, одним долларом, — это ссылка на мету узла, а не текст.
PROJECTS_TARGET = "${meta." + META_PROJECTS + "}"
# Потолок памяти, который узел готов дать одному папету (#197): его
# MOP_BODY_MEM_CAP_MB, которую `mop server deploy` рендерит в meta client.hcl.
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
# логики тела доезжает прогоном `mop server deploy`, правка логики сессии по-прежнему
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
# --- role startup (#583) ---
# Джоб с тройкой души в Meta стартует как РОЛЬ, а не как простой папет:
# бандл, кред принципала и расписку готовности доставил лаунчер (#590),
# и врата сверяют их с ожиданием самого джоба ДО того, как что-либо тронет
# тело: неверная, неполная или подменённая роль не имеет права откатиться
# на легаси-путь и подняться бездушным папетом на креде проекта (в этом
# весь смысл #579: роль без своего бандла не стартует). Проверка стоит до
# стадии клона нарочно: CLONE_SH убивает сессии и освобождает каталог, и
# отказавший старт ничего не должен телу.
#
# Ветка выбирается по ЛЮБОМУ непустому ключу транспорта PU_SOUL*, а не по
# одному имени роли: транспорт (#582) везёт все четыре ключа на обоих путях
# -- легаси-джобы везут их ПУСТЫМИ, -- поэтому один непустой ключ с пустыми
# спутниками -- не легаси-джоб, а сломанная запись. Честных раскладок две:
# «все четыре пустые» (легаси) и «все четыре на месте» (роль); всё прочее --
# громкий отказ: наполовину доставленная тройка не должна молча поднимать
# легаси.
#
# Расписка говорит, что лаунчер закончил; байты говорят, ЧТО закончил. Бандл
# перечитывается тем же правилом, что и регистрация (soul.load: ровно
# soul.yaml и промпт, без симлинков наружу и лишних файлов), пересчитывается
# digest'ом из Meta джоба, и МЕТКА роли бандла сверяется с меткой джоба:
# digest закрепляет байты, а самозваный манифест со своим честно посчитанным
# digest'ом и согласованно лгущей распиской прошёл бы проверку одного
# digest'а. Кред обязан быть принципала (grant-<id> по soul.is_grant -- одна
# грамматика, не копия), никогда проектный: агент видит, кого спрашивают,
# а не кто спрашивает, и чужой кред здесь был бы тихой подменой личности.
#
# Стадия клона ниже (CLONE_SH) РАЗРУШИТЕЛЬНА: убивает сессии и переклонирует
# при чужом origin. К моменту этого врапера #590 клон уже подготовил --
# bootstrap сыгран, identity хозяина поставлена, расписка написана. Поэтому
# ролевой старт ВАЛИДИРУЕТ готовый клон и на расхождении отказывает, а не
# готовит заново: переклон здесь стёр бы поставленную identity и охранные
# маркеры, пока расписка ready.json вне клона продолжала бы говорить «готово»
# -- до первого хода модели роль после такого второго прогона не доезжает.
#
# Питон едет цитированным heredoc: одинарный доллар, без подстановки -- та
# же дисциплина, что у поездки base64 (см. блок над WRAPPER).
# PYTHONDONTWRITEBYTECODE не оставляет __pycache__ в теле.
role_env=()
case "${PU_SOUL:-}${PU_SOUL_DIGEST:-}${PU_SOUL_INC:-}${PU_SOUL_DIR:-}" in
"")
    # Легаси-конвенция: все четыре ключа транспорта пусты (#582).
    is_role=0
    ;;
*)
    # Всё прочее -- либо роль, либо сломанная запись: неполный набор в
    # любом направлении (имя без digest, digest без имени, путь без
    # инкарнации) -- громкий отказ, а не молчаливый легаси-подъём.
    if [ -z "${PU_SOUL:-}" ] || [ -z "${PU_SOUL_DIGEST:-}" ] \
        || [ -z "${PU_SOUL_INC:-}" ] || [ -z "${PU_SOUL_DIR:-}" ]; then
        echo "role transport is partial: PU_SOUL='${PU_SOUL:-}', PU_SOUL_DIGEST='${PU_SOUL_DIGEST:-}', PU_SOUL_INC='${PU_SOUL_INC:-}', PU_SOUL_DIR='${PU_SOUL_DIR:-}' -- a role job carries all four, a legacy job none" >&2
        exit 1
    fi
    # Имя узла -- из собственных поверенных кредов лаунчера (#590 его
    # везёт; hostname контейнера называет тело, а не узел). Ролевой старт
    # без него -- отказ, а не догадка: MOP_NODE -- это КТО в сессии, и
    # угаданное имя -- ложь.
    : "${PU_NODE:?no PU_NODE for a role job -- the launcher carries the node name}"
    for f in "$PU_SOUL_DIR/ready.json" "$PU_SOUL_DIR/bus.json"; do
        [ -f "$f" ] || { echo "role $PU_SOUL: $f is missing -- the delivery did not finish" >&2; exit 1; }
    done
    is_role=1
    ;;
esac
# Готовый клон #590 -- валидация, а не подготовка заново: bootstrap уже
# сыгран, identity хозяина уже поставлена, и вторая разрушительная
# подготовка стёрла бы их до первого хода модели. Чужой origin или
# отсутствие .git у РОЛЕВОГО старта -- отказ; легаси-путь CLONE_SH ниже
# не тронут -- там клон переживает папетов и врапер готовит его сам.
if [ "$is_role" = 1 ]; then
    if [ ! -d "$d/.git" ]; then
        echo "role $PU_SOUL: $d has no .git -- the prepared clone is gone; the launcher (#590) prepares it, the wrapper does not re-clone a role" >&2
        exit 1
    fi
    clone_origin="$(git -C "$d" remote get-url origin 2>/dev/null || true)"
    if [ "$clone_origin" != "$PU_ORIGIN" ]; then
        echo "role $PU_SOUL: the prepared clone tracks '$clone_origin', the job pins '$PU_ORIGIN' -- a role startup validates the clone, it does not re-prepare it" >&2
        exit 1
    fi
fi
role_gate() {
    PYTHONDONTWRITEBYTECODE=1 python3 - "$PU_SOUL_DIR" "$PU_SOUL" \
        "$PU_SOUL_DIGEST" "$PU_SOUL_INC" <<'PY'
import json, os, sys

root, role, want_digest, inc = sys.argv[1:5]
def die(why):
    sys.stderr.write(f"role {role}: {why}\n")
    raise SystemExit(1)

# Расписка: доказательство лаунчера, написанное последним, называет именно
# эту тройку.
try:
    with open(os.path.join(root, "ready.json")) as f:
        receipt = json.load(f)
except (OSError, ValueError) as e:
    die(f"the ready receipt does not read: {e}")
for key, want in (("ready", True), ("role", role), ("soul_inc", inc),
                  ("soul_digest", want_digest)):
    if receipt.get(key) != want:
        die(f"the ready receipt {key} is {receipt.get(key)!r}, the job pins {want!r}")

# Бандл: ровно soul.yaml и промпт, перечитан и пересчитан правилом
# регистрации. Импорт идёт через копию пакета в теле ($HOME/mop) -- то же
# дерево, на котором работает агент узла.
bundle = os.path.join(root, "bundle")
sys.path.insert(0, os.path.join(os.environ.get("HOME", ""), "mop"))
try:
    from mop.common import soul
except Exception as e:  # noqa: BLE001 -- любая причина -- отказ старта
    die(f"the body has no mop package for the bundle gate: {e}")
try:
    loaded = soul.load(bundle)
except ValueError as e:
    die(f"the bundle does not verify: {e}")
# И метка тоже, а не только digest: digest закрепляет байты, а бандл-
# самозванец со своим digest'ом и лгущей распиской проходит проверку
# одного digest'а. Роль, названная джобом, -- та, что обязана загрузиться.
if loaded.role != role:
    die(f"the bundle is role {loaded.role!r}, the job pins {role!r}")
if loaded.digest != want_digest:
    die(f"the bundle hashes to {loaded.digest[:12]}, the job pins {want_digest[:12]}")

# Кред -- самого принципала. Грамматика grant'а живёт в soul.is_grant --
# одно место, не копия здесь.
try:
    with open(os.path.join(root, "bus.json")) as f:
        creds = json.load(f)
except (OSError, ValueError) as e:
    die(f"the principal credential does not read: {e}")
if not soul.is_grant(creds.get("user")):
    die(f"the credential user {creds.get('user')!r} is not grant-<id>")
for key in ("url", "password"):
    if not (isinstance(creds.get(key), str) and creds[key]):
        die(f"the credential has no {key}")
print(os.path.join(bundle, loaded.manifest.prompt))
PY
}
role_prompt=""
# Кред сессии выбирается ЗДЕСЬ один раз: легаси -- проекта, роль --
# принципала. Строка запуска ниже передаёт его одним -e дословно, и второй
# записи MOP_BUS_CONFIG в врапере нет (#583: дубль -e у tmux молча
# перетирал бы первый, а выбранным должен быть не «последний», а тот,
# что выбран явно).
session_bus_config="$PU_PROJECT_CREDS"
if [ "$is_role" = 1 ]; then
    role_prompt="$(role_gate)" || exit 1
    [ -n "$role_prompt" ] || { echo "role $PU_SOUL: the gate named no prompt" >&2; exit 1; }
    # Окружение ролевой сессии: каталог бандла (MCP читает роль через
    # soul.load ровно по bundle/), имя папета, имя узла. MOP_SOUL_DIR
    # называет bundle/, а не корень инкарнации: bus.json/ready.json --
    # дело лаунчера и узла, а не модели.
    role_env+=(-e "MOP_SOUL_DIR=$PU_SOUL_DIR/bundle" \
               -e "MOP_PUPPET=$PU_NAME" \
               -e "MOP_NODE=$PU_NODE")
    session_bus_config="$PU_SOUL_DIR/bus.json"
fi
# --- end of role startup (#583) ---
"""
# --- подготовка клона: одна константа на врапер и на `mop driver run` (#247) ---
WRAPPER = WRAPPER + driver.CLONE_SH + r"""# Отказ копирования -- отказ, а не тишина (#62): семя, которое не доехало,
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
# --- claude hooks (#223) ---
# Состояние папета -- исход хода из хуков Claude Code, а не догадка по экрану
# tmux: 24.09 два папета умерли на "Login expired" посреди хода, и ростер
# полтора часа читал их простаивающими. Хук зовёт `session.py hook` (#222).
#
# Корень пакета в теле -- тот же, откуда внешний врапер запускает `mop`
# (OUTER); другая сторона этого соглашения -- driver.SESSION_PY обоих
# драйверов. Разойдутся -- хуки молча замолчат: tests/spec.py сверяет корни.
mop_root="$HOME/mop"
# Свой файл, а не ~/.claude/settings.json: тот на host-узле делят все claude
# машины, и хук стрелял бы в сессиях оператора и мастера.
hooks="$HOME/.config/mop/claude-hooks.json"
# Охрана не обсуждается: stdout хуков SessionStart/UserPromptSubmit уходит в
# контекст модели, а выход 2 из Stop заставляет claude продолжить ход. Старый
# session.py без глагола hook выходит 2 -- и папет крутился бы вечно.
#
# Пишем через свой временный файл и mv: на host-узле папеты делят $HOME и
# стартуют вместе -- та же гонка, что трижды обнуляла конфиг (edit_json выше).
# Замок не нужен: содержимое у всех одно, mv атомарен.
#
# Хуки -- наблюдение за папетом, а не условие его подъёма: не записался файл
# -- claude встаёт без --settings, а врапер говорит об этом в лог задачи.
# Упасть здесь значило бы уронить папета из-за того, что должно лишь сообщать
# о нём. Внутри if set -e не срабатывает -- на этом и стоит развилка.
# Статус-строка (#455) -- в тот же файл и тем же jq: claude папета зовёт
# глагол statusline того же session.py (#453). Охрана здесь -- НЕ копия
# хуковой: stdout хука -- шум в контексте модели, а stdout статус-строки --
# сама строка, и глушить её значило бы убрать статус-строку целиком.
# Гасятся stderr и код выхода: старый session.py без глагола печатает usage
# и выходит 2, отсутствующий -- ненулевой; без охраны строка гасла бы на
# каждом ходу до раскатки пакета по узлам.
# Роль (#583) пишет настройки в СВОЙ каталог .config/mop/claude/<папет>/
# (ролевой старт выше), легаси -- в общий файл: содержимое одно и тот же
# jq, разница только в пути и цене ошибки. Путь выбирается ЗДЕСЬ, один
# jq-блок на обе ветки: копия блока расходилась бы с первой правкой хука,
# а ролевый старт не должен читать «could not write» об общем файле,
# который он и не собирался трогать.
settings=""
role_mcp_config=""
if [ "$is_role" = 1 ]; then
    hooks="$HOME/.config/mop/claude/$PU_NAME/settings.json"
    role_mcp_config="$HOME/.config/mop/claude/$PU_NAME/mcp.json"
else
    hooks="$HOME/.config/mop/claude-hooks.json"
fi
if mkdir -p "$(dirname "$hooks")" \
    && jq -n --arg cmd "python3 $mop_root/mop/session.py hook >/dev/null 2>&1 || true" \
           --arg status "python3 $mop_root/mop/session.py statusline 2>/dev/null || true" '
        {hooks: (["SessionStart", "UserPromptSubmit", "Stop", "StopFailure",
                  "PostModelSwitch"]
            | map({key: ., value: [{hooks: [{type: "command", command: $cmd,
                                             timeout: 5}]}]})
            | from_entries),
         statusLine: {type: "command", command: $status}}' > "$hooks.$PU_NAME.tmp" \
    && mv "$hooks.$PU_NAME.tmp" "$hooks"; then
    settings="--settings $hooks"
    if [ "$is_role" = 1 ]; then
        # Свой MCP-конфиг роли: та же пара (mop, mcp), но своим файлом --
        # пользовательский регист ~/.claude.json узла ролевая сессия не
        # трогает. Запись не удалась -- отказ: роль без своих настроек --
        # не «папет без хуков», а чужая сессия без наблюдения.
        if printf '%s' "{\"mcpServers\": {\"mop\": {\"command\": \"$mop_root/bin/mop\", \"args\": [\"mcp\"]}}}" > "$role_mcp_config"; then
            :
        else
            echo "role $PU_SOUL: could not write $role_mcp_config -- refusing to start the role" >&2
            exit 1
        fi
    fi
else
    rm -f "$hooks.$PU_NAME.tmp"
    if [ "$is_role" = 1 ]; then
        echo "role $PU_SOUL: could not write $hooks -- refusing to start the role" >&2
        exit 1
    fi
    echo "claude hooks: could not write $hooks -- $PU_NAME starts without them" >&2
fi
# --- end of claude hooks ---
# Свой MCP-сервер регистрируем через сам claude: он владелец ~/.claude.json,
# и никаких ручных мерджей. add на существующей записи ошибается -- и это
# устраивает: в записи нет секретов, наличие означает правильность, полную
# сходимость (если сменился путь) делает deploy. Чужие сервера -- дело
# установки (sandbox.yaml), врапер о них не знает.
# Только легаси-путь (#583): ролевая сессия едет со своим --mcp-config и
# пользовательского регистра не трогает.
if [ "$is_role" = 0 ]; then
    claude mcp add --scope user mop -- "$mop_root/bin/mop" mcp >/dev/null 2>&1 || true
fi
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
# --- feedback drafts (#420) ---
# У папета нет человека, которому показать черновик feedback: SendFeedback
# (Claude Code 2.1.287) пишет черновики в ~/.claude/feedback/drafts/ и
# заметку о них -- и на host-узле ~/.claude делят все claude машины. Политика
# feedbackDrafts: notify | quiet | off (умолчание notify); off -- restrictive,
# им же гасится подсказка «покажи черновики /feedback». Deny-правило -- то же
# место, что SendMessage (#223): deny фильтрует список инструментов до модели,
# на режим прав он не смотрит и работает под --dangerously-skip-permissions.
# Слияние, а не присваивание: чужие deny-правила на узле сносить незачем.
# DISABLE_FEEDBACK_COMMAND гасит /feedback-команду в панели. Ничего больше --
# ни Remote Control, ни автообновлений -- не выключается: инструмент один.
edit_json "$HOME/.claude/settings.json" '.feedbackDrafts = "off"'
edit_json "$HOME/.claude/settings.json" '.permissions.deny =
    ((.permissions.deny // []) + ["SendFeedback"] | unique)'
export DISABLE_FEEDBACK_COMMAND=1
# --- end of feedback drafts ---
# CARGO_TARGET_DIR grows without bound - 22 to 49 GB per puppet in practice, and
# five of them filled a node's disk on 2026-08-26, which killed the WSL VM and
# stranded every allocation on it. Boot is the only safe moment to drop
# one: nothing is building yet, and the cache is pure derived data.
TARGET_DIR="$PU_TARGET"
if [ -d "$TARGET_DIR" ] \
    && [ "$(du -sm "$TARGET_DIR" 2>/dev/null | cut -f1 || echo 0)" -gt 30000 ]; then
    rm -rf "$TARGET_DIR"
fi
# LLM-окружение папета: набор переменных для tmux -e. Статическая часть
# (эндпоинт и включённый каталог моделей) приезжает в PU_LLM_ENV из спеки
# джоба, а ключ — только с узла: в спеке джоба секретам не место (её видно
# в UI Nomad).
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
        echo "no $PU_LLM_KEY_VAR in $keyfile -- the cluster service hands out the LLM proxy key" >&2
        exit 1
    fi
    llm_env+=(-e "$PU_LLM_AUTH_VAR=$key")
    # --- корень сервера для TLS (#557) ---
    # Папет ходит на публичный /v1 сервера (#557), и корень закрепления
    # приезжает тем же файлом ключей узла ($keyfile), что и ключ: строка
    # MOP_PROXY_CA (base64 PEM, кладёт proxykey.blob) декодируется в файл
    # под $HOME тела и едет claude переменной NODE_EXTRA_CA_CERTS -- как
    # мастера через tls.pem выбранного сервера (#412). Сломанная или
    # пустая строка валит врапер громко, как отсутствие ключа: тело без
    # корня поднялось бы живым и умирало на TLS без причины в журнале.
    # Строки нет -- у установки настоящий сертификат, системное доверие,
    # переменной не едет. Доллар одинарный: врапер едет в спеке base64.
    ca_line=$(sed -n "s/^MOP_PROXY_CA=//p" "$keyfile" | tail -1)
    if [ -n "$ca_line" ]; then
        ca_file="$HOME/.config/mop/tls.pem"
        mkdir -p "$(dirname "$ca_file")"
        if ! printf '%s' "$ca_line" | base64 -d > "$ca_file" 2>/dev/null \
            || [ ! -s "$ca_file" ]; then
            echo "broken MOP_PROXY_CA in $keyfile -- the TLS root does not decode" >&2
            exit 1
        fi
        llm_env+=(-e "NODE_EXTRA_CA_CERTS=$ca_file")
    fi
    # --- конец корня сервера (#557) ---
fi

# Креды проекта, а не узла. Без этого папет ходил бы на шину под кредом агента и
# мог бы написать в чужой проект: агент видит, кого спрашивают, но не видит,
# кто спрашивает, и такую подмену не поймал бы. Файл раскатывает deploy/setup.yml
# по одному на проект; если его нет -- валимся громко, потому что папет без шины
# читается мастером как живой, но молчащий.
# Легаси-путь только (#583): ролевой старт выше уже потребовал кред
# принципала и в сессию едет он -- проекта в ролевом теле не будет вовсе.
project_creds="$PU_PROJECT_CREDS"
if [ "$is_role" = 0 ] && [ ! -f "$project_creds" ]; then
    echo "no credentials for project $PU_PROJECT in $project_creds -- set it up: mop project add $PU_ORIGIN" >&2
    exit 1
fi

# a dedicated tmux SERVER per puppet (-L): with the default server every
# session on the node lives in the cgroup of whichever wrapper started the
# server first, and one task budget OOM-kills all puppets at once
# env must go through -e: a plain env prefix only reaches the tmux SERVER when
# this wrapper happens to start it, and every later session inherits the first
# wrapper's variables (all puppets ended up sharing one CARGO_TARGET_DIR)
#
# Аргументы claude собираются МАССИВОМ, а не склейкой строк (#583): имя файла
# промпта из бандла может нести пробелы и метасимволы (грамматика soul его
# этому не препятствует), и склеенный argv либо рвался бы на пробеле, либо
# выполнял бы подстановку шелла. Каждый путь -- отдельный элемент, а в
# командную строку панели (tmux принимает одну строку) массив уезжает через
# printf %q: один элемент argv -- одно закавыченное слово.
claude_argv=(--dangerously-skip-permissions)
[ -n "$settings" ] && claude_argv+=("--settings" "$hooks")
[ -n "$role_mcp_config" ] && claude_argv+=("--mcp-config" "$role_mcp_config")
# Промпт роли -- на КАЖДЫЙ старт, включая --continue (#583): флаг живёт в
# командной строке, не в сессии, и рестарт без него поднял бы роль без её
# души -- тихо, на прежнем месте. Путь дал врат-скрипт из проверенного
# бандла, байты за ним уже сверены digest'ом.
[ -n "${role_prompt:-}" ] && claude_argv+=("--append-system-prompt-file" "$role_prompt")
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
    claude_argv+=("--continue")
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
    -e MOP_BUS_CONFIG="$session_bus_config" \
    "${role_env[@]}" \
    "${llm_env[@]}" \
    "PATH=$d/bin:$PATH $HOME/.local/bin/claude $(printf ' %q' "${claude_argv[@]}")"
trap 'tmux -L "$PU_NAME" kill-session -t "$PU_NAME" 2>/dev/null; exit 0' TERM INT
while tmux -L "$PU_NAME" has-session -t "$PU_NAME" 2>/dev/null; do sleep 10 & wait $!; done
"""


# Каталог моделей прокси (#449): переменная, которая его включает. Живёт
# здесь, а не в llm.env(): папет -- слой спеки, и выбор «модели называет
# код или каталог» -- решение этого слоя. Мастерская сессия остаётся на
# llm.env() как есть.
DISCOVERY = "CLAUDE_CODE_ENABLE_GATEWAY_MODEL_DISCOVERY"


def gateway_env():
    """Окружение LLM сессии ПАПЕТА -> {переменная: значение}.

    Собирается из llm.env(), но не квашится им целиком (#449): слоты
    ANTHROPIC_DEFAULT_*_MODEL сбрасываются, каталог включается. Статическая
    карта навязывала папету имена, которых за прокси нет (400 unknown
    provider), пока мастер выбирал модели из живого каталога; с discovery
    папет выбирает из того же /v1/models. Адрес и таймаут -- из llm.env()
    без изменений, ключ врапер по-прежнему берёт на узле."""
    env = {k: v for k, v in llm.env().items()
           if not k.startswith("ANTHROPIC_DEFAULT_")}
    env[DISCOVERY] = "1"
    return env


def task_env(name, origin, cont=False, mem=0, soul=None, soul_digest=None,
             soul_inc=None):
    """Окружение задачи папета. Одно место и для job_spec, и для версии
    шаблона. mem -- потолок памяти папета, МБ (spec.memory). Тройка роли
    (#582) -- несекретный транспорт для тела: пустые значения без роли.
    Половина тройки -- сломанная запись, а не «роль без digest» (тот же
    договор, что у JobMeta), и врапер на ней обязан отказать, не откатиться
    на легаси-путь (#583)."""
    env_llm = "".join(f"{k}={v}\n" for k, v in gateway_env().items())
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
        # LLM (#390): эндпоинт и модели -- здесь, ключ -- на узле, в
        # secrets.env, его раздаёт сервис кластера (#391).
        "PU_LLM": "proxy",
        "PU_LLM_ENV": base64.b64encode(env_llm.encode()).decode(),
        "PU_LLM_KEY_VAR": llm.KEY,
        "PU_LLM_AUTH_VAR": llm.AUTH_VAR,
        # Потолок памяти папета (#197): на host его держит cgroup задачи
        # (MemoryMaxMB), в pve-теле -- `pct --memory`, который ставит ensure
        # драйвера из этой переменной на каждом подъёме.
        "PU_MEM_MB": str(mem),
        # Тройка роли транспорта (#582, для #590/#583): несекретные имя,
        # digest и инкарнация из Meta джоба. Meta в теле приезжает не у
        # всякого драйвера и не с гарантированной формой ключей, поэтому
        # врапер читает их из окружения, собранного сервером. Каталог
        # бандла -- по имени и инкарнации (driver.soul_dir): PU_NODE
        # добавит #590 из кредов узла ПОСЛЕ размещения, сервер имени узла
        # не знает.
        "PU_SOUL": soul or "",
        "PU_SOUL_DIGEST": soul_digest or "",
        "PU_SOUL_INC": soul_inc or "",
        "PU_SOUL_DIR": driver.soul_dir(name, soul_inc) if soul else "",
    }
    triple = (soul, soul_digest, soul_inc)
    if any(v is not None for v in triple) and not all(v is not None for v in triple):
        # Половина тройки -- сломанная запись, а не «роль без digest»
        # (договор JobMeta): такой папет не поднимать, и врапер на ней
        # обязан отказать, не откатиться на легаси-путь (#583).
        raise ValueError("the role triple comes whole (soul, soul_digest, "
                         "soul_inc) or not at all")
    # Что `mop driver run` переливает в тело: всё окружение спеки, кроме
    # самого врапера. Списком здесь, а не копией там (#155).
    env["PU_CARRY"] = ",".join(env)
    # Внутренний врапер — в спеке, как и был, но base64: узел исполняет
    # его в теле, каким бы оно ни было. Утащить его в пакет на узле
    # значило бы молча поменять инвариант «правка сессии доезжает
    # перерегистрацией».
    env["PU_WRAPPER"] = base64.b64encode(WRAPPER.encode()).decode()
    return env


def job_spec(name, origin, cont=False, branch=None, node=None,
             soul=None, soul_digest=None, soul_inc=None):
    """Спека джоба. cont=True — первому подъёму по этой спеке разрешено поднять
    историю каталога (`claude --continue`). node — узел, к которому спека
    привязана (#289): перерегистрация стоящего папета, см. node_constraint.

    Тройка роли (#582, #580) — целиком или никак: JobMeta проверяет её
    грамматику, здесь она едет Meta как есть, а бандл закреплён в хранилище
    сервера — isolating-роль добавляет ограничение размещения по
    способности драйвера (isolate_constraint), роли «any» размещение не
    меняют: имя роли -- не привилегия (#579). В окружение задачи тройку
    кладёт task_env (#582): врапер читает её оттуда (#583).

    По умолчанию чисто, и умолчание выбрано так намеренно: подъём с историей
    нужен ровно там, где работу продолжают под другой моделью, а везде ещё
    (новый папет, рецикл, лечение) чистый старт — половина смысла операции."""
    meta = JobMeta(origin, branch, soul=soul, soul_digest=soul_digest,
                   soul_inc=soul_inc)
    project = Project.of(origin, asks=read_asks())
    try:
        reserve, ceiling = memory(project.asks, MEM_MAX, MEM)
    except ValueError as e:
        raise RuntimeError(f"{name}: {e} ({project.name})")
    env = task_env(name, origin, cont, ceiling, soul=soul,
                   soul_digest=soul_digest, soul_inc=soul_inc)
    meta = dataclasses.replace(meta, spec_version=template_version(env)).to_meta()
    role_bundle = pinned_bundle(soul, soul_digest)
    return {"Job": {
        "ID": name,
        "Name": name,
        "Datacenters": [nomad.POOL_DC],
        "Type": "service",
        "Meta": meta,
        "Constraints": [project_constraint(project.name), memory_constraint(ceiling)]
                       + ([node_constraint(node)] if node else [])
                       + ([isolate_constraint()] if role_bundle is not None
                          and role_bundle.manifest.runtime.driver == "isolating" else []),
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


# Временный re-export для исторических callers до #645 (#714). Новый код
# импортирует launch_spec напрямую: импорт spec всё ещё читает старую установку.
# Шаблон принадлежит launch_spec; менять этот alias не значит менять рендер.
from .launch_spec import API_LAUNCH_CAPABILITIES, API_WRAPPER, api_job_spec  # noqa: E402,F401


def respec(name, meta, cont=False, node=None):
    """Спека по мете джоба (JobMeta): одна дорога перерегистрации на сервере
    (#265). Её берут глагол update сервиса кластера (перерегистрация,
    рецикл, лечение doctor'а) и сборка образа, поднимающая снятых папетов.
    Пока дорог было несколько, одна из них теряла ветку мастера (#256):
    после сборки образа папет поднимался на origin/HEAD. Тройка роли --
    та же мета (#582): respec переживает её, как ветку, имя и digest
    не пересобирает -- у живой инкарнации свой бандл (#579). И в окружении
    задачи тоже (task_env): иначе перерегистрация молча превратила бы
    роль в легаси-папета, а врапер обязан на этом отказать (#583).

    node — узел стоящего папета (#289): update держит его на месте. Сборка
    образа узла не называет: тела снесены, держать некого."""
    return job_spec(name, meta.origin, cont=cont, branch=meta.branch, node=node,
                    soul=meta.soul, soul_digest=meta.soul_digest,
                    soul_inc=meta.soul_inc)


# ── роль: бандл и размещение (#582) ──────────────────────────────────────
# Хранилище бандлов сервера (#581): корень по образцу grants._root_dir
# (рядом с каталогом кредов папетов), но своим проводом -- спека не тянет
# bootstrap на импорте. Кто и когда пишет бандл -- регистрация в кластере.
SOUL_STORE = os.path.join(os.path.dirname(paths.local("puppets")), grants.STORE)


def pinned_bundle(role, digest, store=None):
    """Закреплённый бандл роли по дайджесту (#579): тройка в Meta без
    бандла в хранилище -- громкий отказ, а не тихий папет без души: спека
    размещает роль и закрепляет промпт, «примерной» души не бывает.
    -> Bundle | None (легаси-джоб: тройки нет)."""
    if role is None and digest is None:
        return None
    got = grants.bundle(digest, root=store or SOUL_STORE)
    if got is None:
        raise RuntimeError(
            f"soul {role}: bundle {digest} is not in the server store; "
            "the role must be registered before the job")
    bundle = soul.from_manifest(got["manifest"], got["prompt"])
    if bundle.manifest.soul != role:
        raise RuntimeError(
            f"soul {role}: bundle {digest} pins another role "
            f"'{bundle.manifest.soul}'")
    return bundle


def isolate_constraint():
    """Ограничение размещения изолирующей роли (#579, #582): тело обязано
    быть отдельным объектом, и это спрашивается у драйвера узла
    способностью (is_container в контракте), а не именем роли. Кандидаты
    отдаёт реестр драйверов (container_drivers): имя роли в спеке ничего
    не значит, а пустой список означает «в пуле нет изоляции вообще» --
    и это отказ job_spec'а до Nomad, а не тихо непланируемый джоб."""
    capable = driver.container_drivers()
    if not capable:
        raise RuntimeError(
            "an isolating role demands a driver with separate bodies, "
            f"the pool has none: {', '.join(driver.drivers()) or '?'}")
    return {"LTarget": "${meta.mop_driver}", "Operand": "regexp",
            "RTarget": "^(%s)$" % "|".join(capable)}


def node_constraint(node):
    """Ограничение размещения: ровно этот узел (#289).

    Перерегистрация -- новое размещение, и без узла в спеке Nomad ставит
    папета по свободной памяти: 26.09 `mop update pu-mop-6` увёз его с hyper
    на gpu, а тело с клоном и разговором осталось на hyper бесхозным (так же
    возникли бесхозные pu-mop-4/5). У папета на контейнерном узле переезд --
    потеря несохранённой работы. Отказ Nomad по месту на этом узле -- папет
    читается `pending` с причиной в mop list, а не тихо переезжает."""
    return {"LTarget": "${node.unique.name}", "Operand": "=", "RTarget": node}

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


# Версия шаблона спеки в Meta джоба (#174). Врапер живёт в спеке, и джоб,
# зарегистрированный прежним mop, работает прежним врапером до перерегистрации
# (CLAUDE.md) — а обе проверки spec_is_stale ниже он проходит. После #155 так
# жил пятый папет чужого проекта, и ничто его не выдавало.
SPEC_META = domain.SPEC_META


def template_version(env):
    """Короткий хеш того, что общее у спек всех папетов: врапер, внешняя
    команда, НАБОР переменных, имена группы и задачи, форма ограничения.
    Значения конкретного папета (имя, origin, профиль, cont) в него не входят:
    иначе устаревшим читался бы каждый второй. Окружение LLM -- значением, а
    не набором ключей (#449): оно едет в PU_LLM_ENV, и правка его значения без
    смены версии оставила бы стоящих папетов на старом окружении навсегда
    свежими -- та же тихая ошибка, что и #174.
    Ролевые ключи (#583) в набор НЕ входят: роль -- данные конкретного
    папета, как имя и origin, и ролевой папет не «свежее» легаси соседом."""
    c = project_constraint("x")
    m = memory_constraint(0)
    shape = {"wrapper": WRAPPER, "outer": OUTER,
             "env": sorted(k for k in env if not k.startswith("PU_SOUL")),
             "llm": gateway_env(),
             "group": GROUP, "task": TASK,
             "constraint": [c["LTarget"], c["Operand"]],
             "memory": [m["LTarget"], m["Operand"]]}
    return hashlib.sha256(json.dumps(shape, sort_keys=True).encode()).hexdigest()[:12]


def current_version():
    """Версия шаблона, который собрал бы сегодняшний mop. Один набор
    переменных для всех папетов (#390): окружение LLM не зависит от
    профиля."""
    return template_version(task_env("pu-spec-1", "spec"))


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
    if JobMeta.from_job(job).spec_version != current_version():
        return True
    task = job["TaskGroups"][0]["Tasks"][0]
    script = (task.get("Config") or {}).get("args") or ["", ""]
    if "driver run" not in script[-1]:
        return True
    return not any((c or {}).get("LTarget") == PROJECTS_TARGET
                   for c in (job.get("Constraints") or []))
