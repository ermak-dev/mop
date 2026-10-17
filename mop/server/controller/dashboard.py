"""Отдача упакованного дашборда тем же origin, что /api/v1 (#627/#644/#695).

Дашборд -- React-приложение, собираемое Vite в web/dist и закоммиченное:
на серверах node нет, пакет везёт собранные файлы с собой
(mop/server/controller/dist), а не рабочий каталог. Ассеты /assets/* раздаёт
стандартный статический сервер (WhiteNoise, WHITENOISE_ROOT = dist): он
знает только проиндексированные при старте файлы, поэтому обход каталога
невозможен, а MIME-типы -- его, не таблица приложения. Приложению остаётся
индекс: он никогда не кэшируется, потому что называет свежие хеши.
"""
import re
from pathlib import Path

from django.http import FileResponse, Http404
from django.views.decorators.http import require_http_methods

DIST = Path(__file__).resolve().parent / 'dist'
INDEX = 'index.html'
# GET и HEAD: страница -- безопасное идемпотентное чтение, тело HEAD снимает
# ASGI-сервер.
READ = ['GET', 'HEAD']
ASSET_CACHE = 'public, max-age=31536000, immutable'
INDEX_CACHE = 'no-store'
# Хеш Django ManifestStaticFilesStorage: 12 шестнадцатеричных знаков перед
# расширением -- собранная статика Admin.
_MANIFEST_HASH = re.compile(r'\.[0-9a-f]{12}\.[A-Za-z0-9]+$')


def immutable(path, url):
    """WhiteNoise: содержимое не меняется под этим URL.

    Ассеты Vite несут хеш в имени, но в своём формате -- признак здесь каталог
    /assets/; собранная статика Admin -- по хешу manifest storage.
    """
    return url.startswith('/assets/') or _MANIFEST_HASH.search(url) is not None


def static_headers(headers, path, url):
    """Точные заголовки кэша: год для ассетов, no-store для копии индекса."""
    if url.startswith('/assets/'):
        headers['Cache-Control'] = ASSET_CACHE
    elif url == '/' + INDEX:
        headers['Cache-Control'] = INDEX_CACHE


@require_http_methods(READ)
def index(request):
    """Индекс приложения: no-store, потому что он называет свежие хеши."""
    path = DIST / INDEX
    if not path.is_file():
        raise Http404('dashboard is not available')
    response = FileResponse(path.open('rb'), content_type='text/html; charset=utf-8')
    response.headers['Cache-Control'] = INDEX_CACHE
    return response
