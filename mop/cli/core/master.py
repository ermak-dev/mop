"""launch claude as a project's master: mop master [git-origin] [claude options]

Without an argument, the origin of the current working copy is used. A
master sees and reaches only its own project's puppets; anyone else's don't
exist for it.

Only origin belongs to this command; everything else goes to claude as-is
(`mop master --continue` resumes the master's last session). A flag that
takes a VALUE goes after `--` (`mop master -- --model opus`): otherwise the
value is indistinguishable from origin. Several masters of one project are
fine: inboxes are addressed per master.

No ansible on this machine: bus and proxy credentials arrive with
`mop login --server ADDRESS LOGIN`, started automatically if either record
is missing; without a terminal or an unambiguous server it refuses before
launching claude.

The master runs in the same permission class as its pool: it can push,
manage the project's puppets and talk to the tracker without asking on
every step. The Nomad token stays on the server. The session uses the
selected server's LLM proxy; its URL and key come from the private
client.json written by mop login.
"""
import json
import os
import sys

from mop.cli import lib
from mop.cli.core import _common
from mop.cli.pool import login
from mop.common import config, puppets

# Скилл едет в пакете (#351): у клиента, поставленного из git, config.PROJECT
# -- site-packages, и skills/ рядом с ним нет. В клоне skills/master --
# переходный симлинк сюда же, и прежние ссылки ~/.claude/skills/master
# резолвятся в тот же файл.
SKILL = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.realpath(__file__)))), "skills", "master")
SKILL_LINK = os.path.expanduser("~/.claude/skills/master")


# Скилл /master -- интерфейс мастер-сессии, как ~/bin/mop. Сам он живёт в
# репозитории, снаружи только симлинк: правка через ~/.claude попадала бы в
# тот же файл, но проходила бы мимо истории. Ставим здесь, а не плейбуком:
# машина оператора ansible не видит.
def link_skill():
    if os.path.realpath(SKILL_LINK) == SKILL:
        return
    os.makedirs(os.path.dirname(SKILL_LINK), exist_ok=True)
    if os.path.lexists(SKILL_LINK):
        os.remove(SKILL_LINK)
    os.symlink(SKILL, SKILL_LINK)
    print(f"skill /master -> {SKILL}")


# --strict-mcp-config не добавляем: у оператора есть свои серверы, и мастер-шелл
# не повод их отбирать.
def mcp_command(bin_dir=lib.BIN, python=sys.executable):
    """Чем мастер-сессия поднимает свой MCP-сервер -> (команда, аргументы).
    Чистая функция (#351). У клона -- его bin/mop: сервер остаётся на дереве
    той копии, из которой подняли мастера. У пакета bin/ нет, и его зовёт
    тот же интерпретатор, что запустил mop, -- в venv пакета он и найдёт."""
    argv = lib.self_argv(bin_dir, python)
    return argv[0], [*argv[1:], "mcp"]


def mcp_config():
    command, args = mcp_command()
    return json.dumps({"mcpServers": {"mop": {
        "type": "stdio",
        "command": command,
        "args": args,
    }}})


def claude_args(args):
    """Разделить свои аргументы и чужие. -> (origin | None, опции для claude).

    Чужое узнаётся по дефису, а `--` отдаёт claude весь хвост дословно — без
    него флаг со значением неотличим от origin, и «--model opus» тихо ушёл бы
    искать проект «opus»."""
    if "--" in args:
        cut = args.index("--")
        args, tail = args[:cut], args[cut + 1:]
    else:
        tail = []
    mine = [a for a in args if not a.startswith("-")]
    return mine, [a for a in args if a.startswith("-")] + tail


def claude_argv(passthru, settings_path=None):
    """Аргументы запуска claude -> список. Чистая функция (#419): сборка
    argv в одном месте, как code.claude_argv. Настройки отзыва -- один
    --settings до пасстру (почему одним -- там же), settings_path --
    memfd-путь подготовленных настроек."""
    settings = ["--settings", settings_path] if settings_path \
        else list(_common.FEEDBACK_SETTINGS)
    return ["claude", "--dangerously-skip-permissions",
            "--mcp-config", mcp_config(),
            *settings, *passthru]


def _main(argv):
    mine, passthru = claude_args(argv)
    if len(mine) > 1:
        lib.usage(__doc__)
    # Origin, в котором нет ни хоста, ни пути, — почти наверняка значение
    # чужого флага, приехавшее сюда позиционно. Отказ обязан назвать причину
    # и выход: иначе дальше будет «нет кредов проекта opus», и искать
    # опечатку придётся в совсем другом месте.
    hint = (f"Pass claude options that take a value after --: "
            f"mop master -- {' '.join(passthru + mine)}" if mine else __doc__)
    origin = lib.origin(mine[0] if mine else None, hint)
    project = puppets.project_of(origin)
    if not lib.project_ready(project):
        # Курица и яйцо: у нового проекта ещё нет пользователя в конфиге NATS,
        # и папет к шине не подключится. Говорим прямо, а не запускаем ansible
        # за спиной оператора: на узел ведёт одна дорога, и это deploy на
        # сервере; сюда его плоды привозит join.
        lib.usage(f"no bus credentials on this machine.\n"
                  f"Log in as yourself: mop login <login>. The project "
                  f"must be registered (mop project add {origin}) and yours "
                  f"in the server's identity provider (mop server user, or the directory)")
    link_skill()

    # URL и ключ -- из joined-конфига выбранного сервера; `mop code`
    # использует тот же источник без MCP и папетов.
    env = _common.launch_env(project=project)
    # Настройки отзыва (#419): слияние пользовательского --settings с
    # feedbackDrafts=off, итог -- memfd-путём; отказ -- явно (как у
    # `mop code`, общее в _common.feedback_settings).
    try:
        value, passthru = _common.feedback_settings(passthru)
    except ValueError as e:
        lib.usage(str(e))
    path, _fd = _common.feedback_fd(value)
    print(f"master of project {project} ({origin})"
          + (f" + claude {' '.join(passthru)}" if passthru else ""))
    os.execvpe("claude", claude_argv(passthru, path)[1:], env)


# Вход -- до lib.cluster: без client.json тот не найдёт выбранный сервер.
_launch = lib.cluster(_main)


def main(argv):
    if len(claude_args(argv)[0]) > 1:
        lib.usage(__doc__)
    login.ensure()
    return _launch(argv)
