"""launch claude outside the pool: mop code [claude options]

Same session environment as `mop master`, none of the pool. No project, no
bus credentials, no mop MCP server: this is an ordinary claude session on
the installation's LLM proxy. Use it where the pool has nothing to do with
the job — a scratch checkout, someone else's repository, a shell on a
machine that never got `mop server deploy`.

Everything goes to claude as-is
(`mop code --continue` resumes the last session here). The command won't
mirror claude's own flags: there are dozens of them, and the list would drift
the moment claude ships a new version.

Permissions are skipped, same as `mop master` and the puppets: the session
comes up with --dangerously-skip-permissions added for you. The whole point
of the command is to get to work on a provider without ceremony, and
answering a prompt per shell call is exactly the ceremony. Passing the flag
yourself is harmless — it isn't added twice.

The session runs on the installation's single LLM proxy (MOP_PROXY_URL);
the key is read from this machine's .env: there's no node secrets.env here,
and the local .env is exactly what serves as the source of truth for keys.
"""
import os

from mop.cli import lib
from mop.cli.core import _common


def main(argv):
    # Никакого разбора позиционных: своих аргументов у команды нет, и всё
    # уезжает claude дословно. Отсюда же отсутствие обязательного
    # `--` из `mop master`: там он отделял origin от значения чужого флага,
    # здесь отделять не от чего.
    passthru = argv
    # Тот же источник ключа, что у мастера: местный .env, а не узловой
    # secrets.env — общее в _common.session_env.
    session = _common.session_env()
    env = dict(os.environ, **session)
    # flush до exec: буфер stdout не переживает execvpe, и строка о профиле
    # пропадала бы везде, где вывод не в терминал.
    # Флаг добавляется здесь, а не оставляется пользователю: команда для
    # того и есть, чтобы сесть за работу без церемоний. Свой экземпляр из
    # passthru не дублируем — claude второй раз не нужен.
    skip = "--dangerously-skip-permissions"
    args = ([] if skip in passthru else [skip]) + passthru
    print(f"claude + {' '.join(args)}", flush=True)
    os.execvpe("claude", ["claude"] + args, env)


# Не lib.cluster: пула эта команда не касается, и требовать настроенный
# кластер ради запуска claude в чужом каталоге не за что.
