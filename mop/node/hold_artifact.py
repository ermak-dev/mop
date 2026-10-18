"""Bytes и public pin пассивного artifact; не installer и не enrollment receipt.

Пин вычисляется из настоящего standalone файла без шаблона/подстановок. Root
trust проверяет artifact на месте исполнения; metadata helper не выдаёт bytes
из рабочей копии за доказательство установки и не пишет ничего на узел.
"""
from dataclasses import dataclass
import hashlib
import os
from pathlib import Path, PurePosixPath

from mop.node.hold_anchor import installed_path, read_bytes, same_file


@dataclass(frozen=True)
class Artifact:
    content: bytes

    @property
    def sha256(self):
        return hashlib.sha256(self.content).hexdigest()

    def executable(self, directory):
        """Планируемый абсолютный путь; существование/доверие этим не объявлено."""
        if (type(directory) is not str or not directory.startswith('/')
                or directory.startswith('//') or '..' in PurePosixPath(directory).parts
                or PurePosixPath(directory).as_posix() != directory):
            raise ValueError('canonical absolute anchor installation directory required')
        path = str(PurePosixPath(directory)/('sha256-'+self.sha256)/'hold-anchor')
        return str(installed_path(path, self.sha256))


def artifact():
    """Прочесть bundled source из regular nonsymlink fd, вычислять hash из bytes."""
    source = Path(__file__).with_name('hold_anchor.py')
    descriptor = os.open(source, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    try:
        before = os.fstat(descriptor)
        content = read_bytes(descriptor)
        if not same_file(before, os.stat(source, follow_symlinks=False)):
            raise ValueError('bundled anchor source identity changed during metadata generation')
        return Artifact(content)
    finally:
        os.close(descriptor)
