"""Изолированный installed MCP client; пути только из scope/read-only profile.

Не создаёт identity/token и не меняет empty-to-active сервер #748. До проверки
immutable entry импортируется только stdlib; никакого node API или checkout.
"""
import os
from pathlib import Path
import stat
import sys

_BOOTSTRAP = ('mop/__init__.py', 'mop/common/__init__.py',
              'mop/common/installed_content.py', 'mop/client/__init__.py',
              'mop/client/body_entry.py')


def installed_entry():
    if (not sys.flags.isolated or not sys.flags.no_site or not sys.flags.dont_write_bytecode
            or os.geteuid() == 0):
        raise RuntimeError('isolated immutable installed body MCP UID entry required')
    release = Path(__file__).absolute().parents[2]
    if os.path.lexists(release/'.git'):
        raise RuntimeError('installed body MCP checkout entry is forbidden')
    # Только immutable bootstrap предшествует common verifier import. Никакой
    # node composition: MCP — client, UID/native ownership остаются у broker.
    for name in _BOOTSTRAP:
        filename = release/name
        fd = os.open('/', os.O_RDONLY|os.O_DIRECTORY|os.O_CLOEXEC)
        try:
            for part in filename.parts[1:-1]:
                child = os.open(part, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC, dir_fd=fd)
                os.close(fd)
                fd = child
                value = os.fstat(fd)
                if value.st_uid != 0 or value.st_mode & 0o022:
                    raise RuntimeError('immutable root-owned body MCP ancestors required')
            leaf = os.open(filename.name, os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK|os.O_CLOEXEC, dir_fd=fd)
            try:
                value = os.fstat(leaf)
                if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_uid != 0
                        or value.st_mode & 0o022 or not 0 < value.st_size <= 1048576):
                    raise RuntimeError('immutable root-owned body MCP bootstrap source required')
            finally:
                os.close(leaf)
        finally:
            os.close(fd)
    sys.dont_write_bytecode = True
    sys.pycache_prefix = '/dev/null/mop-no-bytecode'
    if str(release) not in sys.path:
        sys.path.insert(0, str(release))
    return release


def main(argv):
    release = installed_entry()
    os.environ.clear()
    os.chdir('/')
    import argparse
    from mop.common.installed_content import mcp_profile, scope_launch
    parser = argparse.ArgumentParser(prog='mop host-body-mcp', allow_abbrev=False)
    parser.add_argument('--scope', required=True)
    parser.add_argument('--deadline', required=True)
    args = parser.parse_args(argv)
    launch_id = scope_launch(args.scope)
    body_root, content = mcp_profile(release)
    if Path(sys.executable).resolve() != content.tools['python']:
        raise RuntimeError('body MCP interpreter differs from immutable installed runtime')
    # Product и dependency trees уже целиком hashed/immutable. -S не исполняет
    # .pth/site, а launcher не читает .env и не наследует installation selectors.
    sys.path.insert(1, str(content.directories['python_root']))
    from mop.common.management_context import body_deadline
    end = body_deadline(args.deadline)
    body = body_root/launch_id
    os.environ.update(HOME=str(body), PATH='/usr/bin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8')
    from mop.cli._mcp import serve_body
    return serve_body(body/'r/m/bootstrap', body/'r/m/context', end.isoformat())


if __name__ == '__main__':
    try:
        raise SystemExit(main(sys.argv[1:]))
    except (RuntimeError, ValueError, OSError) as error:
        # Только safe installed profile phrases: private handoff/provider bytes
        # здесь не читаются и не попадают в stderr до MCP ownership.
        sys.stderr.write('Installed body MCP entry refused: '+str(error)+'\n')
        raise SystemExit(1)
