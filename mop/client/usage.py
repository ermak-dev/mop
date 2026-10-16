"""CLI/stat adapter #642 над публичным DRF query DTO #656.

query — явный bound HTTPS client #642: callable(start, end, limit, after).
Здесь нет выбора credential/server, HTTP дубля, ORM, NATS или transcript fallback.
"""
from datetime import date, datetime
import re
from uuid import UUID
from mop.common.usage_contract import COUNTERS


class UsageQueryError(ValueError):
    """Некорректный/неограниченный DTO не превращается в частичный успех."""


def _invalid():
    raise UsageQueryError('invalid or unbounded proxy usage response')


def _integer(value):
    if type(value) is not int or not 0 <= value <= 2**63-1:
        _invalid()
    return value


def _uuid(value):
    if type(value) is not str:
        _invalid()
    try:
        return str(UUID(value))
    except ValueError:
        _invalid()


def _label(value, maximum):
    if type(value) is not str or not 0 < len(value) <= maximum or any(ord(c) < 32 for c in value):
        _invalid()
    return value


def _row(row, first, last):
    if type(row) is not dict:
        _invalid()
    _uuid(row.get('id'))
    _uuid(row.get('source_id'))
    try:
        day = date.fromisoformat(row.get('date'))
    except (ValueError, TypeError):
        _invalid()
    if not first <= day < last:
        _invalid()
    for field, maximum in (('model', 256), ('alias', 256), ('provider', 128)):
        _label(row.get(field), maximum)
    key = row.get('key_id')
    if key is not None and (type(key) is not str or not re.fullmatch('key_[0-9a-f]{64}', key)):
        _invalid()
    requests = _integer(row.get('requests'))
    for name in ('failed_requests', 'partial_events'):
        if _integer(row.get(name)) > requests:
            _invalid()
    counters = row.get('counters')
    if type(counters) is not dict or set(counters) != set(COUNTERS):
        _invalid()
    for metric in counters.values():
        if type(metric) is not dict or set(metric) != {'value', 'known_events', 'unknown_events'}:
            _invalid()
        known, unknown = _integer(metric['known_events']), _integer(metric['unknown_events'])
        value = metric['value']
        if known + unknown != requests or (known == 0) != (value is None):
            _invalid()
        if value is not None and (type(value) is not str or not re.fullmatch(r'0|[1-9][0-9]{0,99}', value)):
            _invalid()
    return row


def _source(source):
    if (type(source) is not dict or set(source) != {'id', 'profile', 'installed_version', 'source_complete',
            'unresolved_reads', 'gap_reads', 'last_read_at', 'status'} or source['source_complete'] is not False
            or source['status'] not in ('reading', 'unknown', 'committed', 'gap', 'not_observed')):
        _invalid()
    _uuid(source['id'])
    _label(source['profile'], 64)
    _label(source['installed_version'], 32)
    if _integer(source['unresolved_reads']) > 1:
        _invalid()
    _integer(source['gap_reads'])
    if source['last_read_at'] is not None:
        try:
            stamp = datetime.fromisoformat(source['last_read_at'])
            if stamp.tzinfo is None or stamp.utcoffset().total_seconds() != 0:
                _invalid()
        except (TypeError, ValueError, AttributeError):
            _invalid()
    return dict(source)


def stat(query, *, start, end, group_by='day', max_pages=20):
    """Материализованный bounded stat; принимает тот же query, что dashboard.

    Структурные категории не складываются в grand total. Неполное coverage
    остаётся явным даже когда часть значений известна. Денежного биллинга нет.
    """
    dimensions = {'day': ('date',), 'key': ('source_id', 'key_id'),
                  'model': ('provider', 'model', 'alias')}
    if group_by not in dimensions:
        raise UsageQueryError('user, project and puppet usage attribution is unavailable for shared proxy keys')
    try:
        first, last = date.fromisoformat(start), date.fromisoformat(end)
    except (ValueError, TypeError):
        _invalid()
    if not 0 < (last-first).days <= 366 or type(max_pages) is not int or not 1 <= max_pages <= 100:
        _invalid()
    cursor, cursors, ids, groups, sources = '', set(), set(), {}, {}
    for _ in range(max_pages):
        page = query(start=start, end=end, limit=100, after=cursor)
        if (type(page) is not dict or type(page.get('schema_version')) is not int or page['schema_version'] != 1
                or page.get('source') != 'cliproxy' or page.get('source_complete') is not False
                or page.get('period') != {'start': start, 'end': end, 'timezone': 'UTC', 'end_exclusive': True}
                or page.get('attribution') != {'user': 'unavailable', 'project': 'unavailable', 'puppet': 'unavailable'}
                or type(page.get('items')) is not list or len(page['items']) > 100
                or type(page.get('sources')) is not list or len(page['sources']) > 100 or 'next' not in page):
            _invalid()
        for raw_source in page['sources']:
            source = _source(raw_source)
            sources[source['id']] = source
        if len(sources) > 100:
            _invalid()
        for raw in page['items']:
            row = _row(raw, first, last)
            if row['id'] in ids:
                _invalid()
            ids.add(row['id'])
            key = tuple(row[field] for field in dimensions[group_by])
            target = groups.setdefault(key, {'dimensions': {field: row[field] for field in dimensions[group_by]},
                'requests': 0, 'failed_requests': 0, 'partial_events': 0,
                'counters': {name: {'value': None, 'known_events': 0, 'unknown_events': 0} for name in COUNTERS}})
            for name in ('requests', 'failed_requests', 'partial_events'):
                target[name] += row[name]
            for name in COUNTERS:
                dest, metric = target['counters'][name], row['counters'][name]
                if metric['value'] is not None:
                    dest['value'] = str(int(dest['value'] or '0') + int(metric['value']))
                dest['known_events'] += metric['known_events']
                dest['unknown_events'] += metric['unknown_events']
        cursor = page.get('next')
        if cursor is None:
            break
        if type(cursor) is not str or not re.fullmatch('[0-9a-f]{32}', cursor) or cursor in cursors:
            _invalid()
        cursors.add(cursor)
    else:
        raise UsageQueryError('usage query page budget exhausted; narrow the period')
    return {'schema_version': 1, 'source': 'cliproxy', 'source_complete': False,
        'period': page['period'], 'attribution': page['attribution'],
        'sources': list(sources.values()), 'group_by': group_by,
        'groups': [groups[key] for key in sorted(groups, key=lambda key: tuple(str(value) for value in key))]}
