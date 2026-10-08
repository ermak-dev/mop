"""Файловые примитивы: приватная запись и KEY=VALUE (#153).

Раньше -- пять копий записи кредов и три разборщика KEY=VALUE, и копии
разошлись: одни писали атомарно, другие усекали файл на месте, одни чинили
права лежащего файла, другие оставляли чужие. Здесь одна реализация.

Только STDLIB, без импортов из пакета: этим пользуются и config (которого
импортирует всё остальное), и агент на узле.
"""
import ctypes
import errno
import json
import os
import secrets


def _single_entry(name):
    """Имя записи В КАТАЛОГЕ ПО fd: строго одна запись (#542).

    Пустое имя, '.', '..', с '/' или NUL уводит запись из каталога-якоря:
    '/' разрешается относительно fd (path traversal), абсолютный путь fd
    игнорирует вовсе, NUL обрезает c_char_p в renameat2. Отказ ДО всякого
    syscall -- ValueError, а не надежда на errno от самого syscall."""
    if (not isinstance(name, str) or not name or "/" in name or "\0" in name
            or name in (".", "..")):
        raise ValueError(f"not a single directory entry: {name!r}")


def _write(path, data, private, owner=None, dir_fd=None):
    """Временный файл рядом, fsync, rename. Читатель видит либо прежний файл
    целиком, либо новый целиком: nats-server перечитывает users.conf в любой
    момент, а обрыв посреди записи кредов оставлял усечённый файл.

    owner -- (uid, gid) файла, отданный до первого байта (#234): не выходит
    -- OSError, и на месте остаётся прежний файл, а не чужой.

    dir_fd -- писать в каталог по его fd, path -- ОДНО имя записи в нём
    (см. _single_entry): путь подменяют (rmtree + чужой mkdir), а fd держит
    каталог, на котором стоит попытка (#542)."""
    raw = data.encode() if isinstance(data, str) else data
    # Имя с солью, а не `path.tmp`: два писателя одного файла не пишут в
    # один временный, а брошенный после обрыва не мешает следующей записи.
    if dir_fd is None:
        tmp = f"{path}.{secrets.token_hex(4)}.tmp"
    else:
        _single_entry(path)
        tmp = f".{path}.{secrets.token_hex(4)}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL,
                 0o600 if private else 0o666, dir_fd=dir_fd)
    try:
        if private:
            # 0600 до первого байта и независимо от umask: mode у open
            # umask только урезает.
            os.fchmod(fd, 0o600)
        if owner is not None:
            st = os.fstat(fd)
            if (st.st_uid, st.st_gid) != tuple(owner):
                os.fchown(fd, *owner)
        with os.fdopen(fd, "wb") as f:
            fd = None
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
    except BaseException:
        if fd is not None:
            os.close(fd)
        try:
            os.unlink(tmp, dir_fd=dir_fd)
        except FileNotFoundError:
            pass
        raise


def write_private(path, data, owner=None):
    """Файл с секретом: ровно 0600, атомарно. Каталог -- забота вызывающего:
    у каждого своё правило, каким ему быть. owner -- как у _write."""
    _write(path, data, private=True, owner=owner)


def write_private_dirfd(dirfd, name, data):
    """Приватный файл В КАТАЛОГЕ ПО ЕГО fd, не по пути (#542).

    Тонкая обёртка того же _write (dir_fd): имя каталога не используется
    вовсе -- путь подменяют (rmtree + чужой mkdir), а fd держит якорь.
    Подмена между записями ломается здесь же ENOENT -- отвязанный каталог
    новых имён не даёт."""
    _write(name, data, private=True, dir_fd=dirfd)


_RENAME_NOREPLACE = 1 << 0


def _renameat2():
    """Загруженный renameat2 (lazy): недоступен -- OSError ENOSYS (#542).

    Загрузка при первом вызове, не при импорте модуля: fsutil тянет и
    config на клиенте, и агент на узле, и наличие libc-символа не должно
    решать, импортируется ли пакет вообще. Платформа без флага --
    именованное ограничение вызывающему, не молчаливый fallback."""
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        fn = libc.renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS,
                      "renameat2 is not available on this platform") from None
    fn.restype = ctypes.c_int
    fn.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int,
                   ctypes.c_char_p, ctypes.c_uint]
    return fn


def rename_noreplace(src_dirfd, src_name, dst_dirfd, dst_name):
    """Атомарный перенос БЕЗ перезаписи занятого имени (#542).

    renameat2(RENAME_NOREPLACE): имя назначения занято (файл, каталог,
    symlink) -- False, цель не тронута; свободно -- True и перенесено
    атомарно. Это единственный способ заявить имя без окна «проверил --
    затем положил»: между stat и rename имя занимает чужой, и обычный
    os.rename молча затёр бы его. Имена -- строго single entry (см.
    _single_entry): '/' уводит перенос из якоря, NUL обрезает c_char_p.
    Недоступность renameat2 (ENOSYS/EINVAL -- ядро или ФС без флага)
    поднимает OSError -- вызывающий обязан отказать, а не откатываться
    к проверке-затем-rename."""
    _single_entry(src_name)
    _single_entry(dst_name)
    fn = _renameat2()
    ctypes.set_errno(0)
    got = fn(src_dirfd, os.fsencode(src_name), dst_dirfd,
             os.fsencode(dst_name), _RENAME_NOREPLACE)
    err = ctypes.get_errno()
    if got == 0:
        return True
    if err == errno.EEXIST:
        return False
    raise OSError(err, os.strerror(err))


def write_atomic(path, data):
    """Не секрет, но и усечённым его видеть нельзя (реестр проектов, лимиты):
    атомарно, права -- по umask, как у обычного open."""
    _write(path, data, private=False)


def make_private_dir(path):
    """Каталог 0700, в том числе уже лежащий: mode у makedirs действует только
    на новый последний уровень и урезается umask'ом."""
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def read_kv(text, raw=False):
    """KEY=VALUE построчно -> dict; повтор ключа -- последний.

    Диалекта два, и оба нужны:
      .env (по умолчанию) -- .env, node.env, secrets.env: пустые строки и
          `#`-комментарии пропускаются, пробелы по краям ключа и значения
          срезаются, с краёв значения -- и кавычки. Формат намеренно
          примитивен: его читают и питон, и ansible, и sed.
      raw -- vars.env секретов проекта и вывод пробы агента: значение байт в
          байт, как записано. vars.env пишем мы сами (write_kv), и значение
          с пробелами или кавычками обязано вернуться тем же.
    Строка без `=` пропускается в обоих."""
    out = {}
    for line in text.splitlines():
        if not raw:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
        k, sep, v = line.partition("=")
        if not sep:
            continue
        if raw:
            out[k] = v
        else:
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def read_json(path, default=None):
    """JSON файла либо default: файла нет, не читается или не JSON (#317).
    Форму (словарь, нужные ключи) проверяет вызывающий -- у каждого своя."""
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_kv(values):
    """dict -> текст KEY=VALUE, по строке на ключ, ключи по порядку."""
    return "".join(f"{k}={values[k]}\n" for k in sorted(values))
