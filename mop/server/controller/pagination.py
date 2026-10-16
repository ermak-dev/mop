"""Ограниченная страница уже авторизованного каталога; cursor не является правом."""
import hashlib
from django.core import signing
from django.utils.crypto import salted_hmac
from rest_framework.exceptions import APIException, ValidationError
from .runtime import installation_snapshot


class CursorReset(APIException):
    status_code = 410
    default_detail = 'catalogue scope or cursor changed; restart pagination'
    default_code = 'reset_required'


def project_page(actor, rows, parameters):
    if set(parameters) - {'page_size', 'cursor'}:
        raise ValidationError('unknown query fields')
    binding = salted_hmac('mop.project-cursor.binding', str(actor.credential_id), algorithm='sha256').hexdigest()
    scope = hashlib.sha256('\0'.join(str(row.id) + ':' + str(row.revision) for row in rows).encode()).hexdigest()
    epoch = installation_snapshot()['epoch']
    cursor = parameters.get('cursor')
    offset, size = 0, 100
    if cursor:
        if len(cursor) > 2048:
            raise CursorReset()
        try:
            data = signing.loads(cursor, salt='mop.project-cursor.v1', max_age=300)
            if (type(data) is not dict or data.get('binding') != binding or data.get('scope') != scope
                    or data.get('epoch') != epoch or type(data.get('offset')) is not int
                    or type(data.get('size')) is not int):
                raise CursorReset()
            offset, size = data['offset'], data['size']
        except (signing.BadSignature, TypeError, ValueError):
            raise CursorReset() from None
    if 'page_size' in parameters:
        try:
            requested = int(parameters['page_size'])
        except (TypeError, ValueError):
            raise ValidationError('page_size must be within 1..500') from None
        if cursor and requested != size:
            raise CursorReset()
        size = requested
    if not 1 <= size <= 500 or not 0 <= offset <= len(rows):
        raise CursorReset() if cursor else ValidationError('page_size must be within 1..500')
    page = tuple(rows[offset:offset + size])
    following = None
    if offset + size < len(rows):
        following = signing.dumps({'binding': binding, 'scope': scope, 'epoch': epoch,
            'offset': offset + size, 'size': size}, salt='mop.project-cursor.v1', compress=True)
    return page, following
