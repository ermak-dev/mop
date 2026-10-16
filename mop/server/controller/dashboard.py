"""Отдача упакованного дашборда тем же origin, что /api/v1 (#627/#644).

Дашборд -- React-приложение, собираемое Vite в web/dist и закоммиченное:
на серверах node нет, пакет везёт собранные файлы с собой
(mop/server/controller/dist), а не рабочий каталог. Один владелец отдачи:
index никогда не кэшируется (он называет свежие хеши), ассеты с хешем в
имени -- год immutable. Имя ассета плоское: ни подкаталогов, ни «..», ни
скрытых файлов -- обход каталога невозможен по построению, как в
mop/server/web.py #297.
"""
import os
from pathlib import Path

from django.http import FileResponse, Http404
from django.views.decorators.http import require_http_methods

DIST = Path(__file__).resolve().parent / 'dist'
ASSETS = '/assets/'
INDEX = 'index.html'
# GET и HEAD: страница и ассеты -- безопасные идемпотентные чтения,
# тело HEAD снимает ASGI-сервер.
READ = ['GET', 'HEAD']
ASSET_CACHE = 'public, max-age=31536000, immutable'
INDEX_CACHE = 'no-store'
# Тот же конечный словарь, что у прежнего сервиса (#297): незнакомое
# расширение -- поток байтов, а не угаданный тип.
CONTENT_TYPES = {'.js': 'text/javascript; charset=utf-8',
                 '.css': 'text/css; charset=utf-8',
                 '.svg': 'image/svg+xml', '.woff2': 'font/woff2', '.woff': 'font/woff',
                 '.png': 'image/png', '.map': 'application/json',
                 '.json': 'application/json; charset=utf-8', '.ico': 'image/x-icon',
                 '.html': 'text/html; charset=utf-8'}


def asset_name(path):
    """Плоское имя файла под assets/, либо None: без обхода каталога.

    Чистая функция: слеш, бэкслеш, процент и точка в начале -- отказ.
    """
    if not path.startswith(ASSETS):
        return None
    name = path[len(ASSETS):]
    if not name or '/' in name or '\\' in name or '%' in name or name.startswith('.') \
            or name in ('.', '..'):
        return None
    return name


def content_type(name):
    """Тип по расширению; незнакомое -- поток байтов."""
    return CONTENT_TYPES.get(os.path.splitext(name)[1].lower(), 'application/octet-stream')


def _serve(path, cache_control, content_type):
    if not path.is_file():
        raise Http404('dashboard asset is not available')
    response = FileResponse(path.open('rb'), content_type=content_type)
    response.headers['Cache-Control'] = cache_control
    return response


@require_http_methods(READ)
def index(request):
    """Индекс приложения: no-store, потому что он называет свежие хеши."""
    return _serve(DIST / INDEX, INDEX_CACHE, 'text/html; charset=utf-8')


@require_http_methods(READ)
def asset(request, name):
    """Ассет с хешем в имени: год immutable, имя плоское."""
    if asset_name(ASSETS + (name or '')) is None:
        raise Http404('dashboard asset is not available')
    return _serve(DIST / 'assets' / name, ASSET_CACHE, content_type(name))
