"""Исполнение orchestra/session.py внутри аллокации Nomad.

Сокет сессии host-local: с управляющей машины к слейву не подключиться, так
что и пробник, и отправитель должны работать НА УЗЛЕ.

Почему модуль ставится на узел, а не возится в каждой команде: команда exec
едет в query-строке URL, а nginx перед Nomad режет URI примерно на 8 КБ. Целый
session.py — 19 КБ в base64, и запрос отбивался 414 ещё до Nomad. Сжатый и без
комментариев он влезает (~4 КБ), но тогда полезную длину делит с текстом
сообщения, и достаточно длинный диспатч снова упирается в лимит.

Поэтому две фазы: обычный вызов — короткая команда `python3 <кэш>/session-<hash>.py`,
и почти весь бюджет URL достаётся сообщению; если файла на узле нет, вызов
возвращает 97, мы ставим модуль и повторяем. Хэш в имени сам инвалидирует кэш
при правке session.py.
"""
import ast
import base64
import gzip
import hashlib
import io
import json
import os
import shlex

from . import nomad, session

CACHE_DIR = "$HOME/.cache/orchestra"
NOT_INSTALLED = 97
_packed = None


def _pack():
    """Исходник session.py без комментариев и докстрингов, сжатый, в base64."""
    global _packed
    if _packed is not None:
        return _packed
    src = io.open(os.path.realpath(session.__file__), encoding="utf-8").read()
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef,
                                 ast.AsyncFunctionDef, ast.ClassDef)):
            continue
        head = node.body[0] if node.body else None
        if (isinstance(head, ast.Expr) and isinstance(head.value, ast.Constant)
                and isinstance(head.value.value, str)):
            node.body = node.body[1:] or [ast.Pass()]
    lean = ast.unparse(ast.fix_missing_locations(tree)).encode()
    blob = base64.b64encode(gzip.compress(lean, 9)).decode()
    _packed = (hashlib.sha256(lean).hexdigest()[:12], blob)
    return _packed


def _path():
    return f"{CACHE_DIR}/session-{_pack()[0]}.py"


def _install_script():
    return (f"mkdir -p {CACHE_DIR} && echo {_pack()[1]} | base64 -d | gzip -d "
            f"> {_path()}.tmp && mv {_path()}.tmp {_path()}")


def _run_script(argv):
    args = " ".join(shlex.quote(str(a)) for a in argv)
    return f"[ -f {_path()} ] || exit {NOT_INSTALLED}; exec python3 {_path()} {args}"


def run(alloc, argv, timeout=30):
    """Запустить session.py на узле, поставив его при первом обращении."""
    out, code = nomad.sh(alloc, _run_script(argv), timeout=timeout)
    if code == NOT_INSTALLED:
        nomad.sh(alloc, _install_script())
        out, code = nomad.sh(alloc, _run_script(argv), timeout=timeout)
    return out, code


def probe(alloc, cwd):
    """Состояние сессии, работающей в cwd на узле: "<status> <alive> <listen>"
    либо "none"."""
    out, _ = run(alloc, ["probe", cwd])
    return out.strip().splitlines()[-1] if out.strip() else ""


def send(alloc, cwd, message, priority="next", from_name="orchestra", wait=0):
    """Сообщение сессии слейва. -> ответ отправителя (msg_id/idle либо error)."""
    argv = ["send", cwd, message, "--priority", priority,
            "--from-name", from_name, "--wait", str(wait)]
    out, _ = run(alloc, argv, timeout=wait + 30 if wait else 30)
    line = out.strip().splitlines()[-1] if out.strip() else ""
    try:
        return json.loads(line)
    except ValueError:
        return {"error": (line or "отправитель на узле промолчал")[:200]}
