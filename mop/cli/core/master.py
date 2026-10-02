"""launch claude as a project's master: mop master [git-origin] [claude options]

Without an argument, the origin of the current working copy is used. One
project — one project: a master sees and reaches only its own puppets; anyone
else's don't exist for it.

Only origin belongs to this command; everything else goes to claude
as-is (`mop master --continue` resumes the master's last session). The
command won't mirror claude's own flags: there are dozens of them, and the
list would drift the moment claude ships a new version. Pass a flag that
takes a VALUE after `--` (`mop master -- --model opus`): otherwise the value
is indistinguishable from origin.

You can run as many masters as you like, including several for one project:
an inbox is addressed by master, not by project, so they never get confused.

All this command does is work out the project, check its credentials, and exec
into claude. From there the slice is inherited: mop mcp, spawned by this
session as a child, sees MOP_PROJECT, builds the project's bus credentials from
the server's directory (mop/common/creds.py) and subscribes to its project's inbox.

No ansible on this machine: bus and proxy credentials arrive with
`mop login --server ADDRESS LOGIN`. The server comes from the clone binding,
MOP_SERVER_LAN or --server; without one, a single joined server is selected.
The /master skill is linked from here.

The pool server arrives as an argument, not from a directory config. The
master sits in a working copy of its own project, while the `.mcp.json` that
names the server lives in the mop repository — that is, anywhere except
where masters actually run. The very first run in a foreign directory came up
with no mcp__mop__* at all, and the session could neither see the roster nor
write to a puppet. Registering the server globally is possible, but
~/.claude.json is live state that running sessions rewrite; the argument
touches nothing and applies to this session alone.

The master comes up in the same permission class as the pool it commands.
Puppets live under --dangerously-skip-permissions, and the message channel
checks classes: a puppet's report that reaches a master in ordinary mode gets
parked in the held queue to wait for a human — meaning the loop stops being
automatic on exactly its main signal. The price is named up front: the
master can push, manage its project's puppets and talk to the tracker without
asking on every step. The Nomad token stays on the server.

The session uses the selected server's LLM proxy. Its URL and key come from
the private client.json written by mop login, not from installation .env.
"""
import json
import os
import sys

from mop.cli import lib
from mop.cli.core import _common
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


def main(argv):
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
    session = _common.session_env()
    env = dict(os.environ, MOP_PROJECT=project, **session)
    print(f"master of project {project} ({origin})"
          + (f" + claude {' '.join(passthru)}" if passthru else ""))
    os.execvpe("claude", ["claude", "--dangerously-skip-permissions",
                          "--mcp-config", mcp_config()] + passthru, env)



# Проверка настроек кластера — до первого сетевого вызова (lib.cluster).
main = lib.cluster(main)
