"""Проверки единственного владельца чистых правил management gateway #647."""
import json
import sys
import _lib  # noqa: F401
from mop.server import management_policy as p

# HYPOTHESIS: ранний return маскирует второй ключ; PATCH возвращает старые bytes.
# SOLUTION: полный обход и материализованное тело после удаления echo-поля.
# STATUS: FIXED — see #647

def main():
    failures = []
    def check(name, value):
        if not value:
            failures.append(name)
    result = p.guard_management_write('PUT', '/v8/management/config',
        b'{"secret-key":"managed-by-mop","management":{"secret-key":"replacement"}}', lambda: 'key')
    check('placeholder must not hide a second replacement', result.get('status') == 403)
    for suffix, body in [('config', b'{"management":"private-hash"}'),
                         ('config.yaml', b'management: private-hash')]:
        check('malformed management is not readable: ' + suffix,
              p.redact_management_read('/v8/management/' + suffix, body) is None)
    result = p.guard_management_write('PATCH', '/v8/management/config',
        b'{"management":{"secret-key":"managed-by-mop","allow-remote":false}}', lambda: 'key')
    check('PATCH must serialize removed key', result['action'] == 'inject'
          and 'secret-key' not in json.loads(result['body'])['management'])
    for body in (b'\x5b1, 2\x5d', b'a: &loop\n  again: *loop\nmanagement: *loop\n',
                 b'[' * 1000 + b']' * 1000):
        try:
            verdict = p.guard_management_write('PUT', '/v8/management/config.yaml', body, lambda: 'key')
        except (RecursionError, ValueError):
            verdict = {}
        check('invalid or recursive YAML is a bounded refusal', verdict.get('status') == 400)
    from unittest.mock import patch
    from types import SimpleNamespace
    calls = []
    fake = SimpleNamespace(checkpw=lambda *args: calls.append(args) or True)
    expensive = '$2b$31$' + 'a' * 53
    with patch.dict(sys.modules, {'bcrypt': fake}):
        result = p.guard_management_write('PUT', '/v8/management/config',
            json.dumps({'management': {'secret-key': expensive}}).encode(), lambda: 'key')
    check('untrusted bcrypt cost must not consume an unbounded executor slot',
          result.get('status') == 403 and not calls)
    calls.clear()
    current = '$2b$12$' + 'a' * 53
    with patch.dict(sys.modules, {'bcrypt': fake}):
        p.guard_management_write('PUT', '/v8/management/config',
            json.dumps({'entries': [{'secret-key': current} for _ in range(30)]}).encode(), lambda: 'key')
    check('repeated hash echoes need at most two bcrypt checks', len(calls) <= 2)
    body = b'{"management":{"secret-key":"private-hash"}}'
    redacted = p.redact_management_read('/v8/management/credentials', body)
    check('non-config JSON cannot carry a management hash either', b'private-hash' not in redacted)
    prefix, suffix = b'{"management":{"secret-key":""},"a":"', b'"}'
    body = prefix + b'x' * (p.PANEL_MAX - len(prefix) - len(suffix)) + suffix
    check('redaction expansion remains bounded', p.redact_management_read('/v8/management/config', body) is None)
    check('allowed HTTP response header names are case insensitive',
        p.response_headers({'x-cpa-version':'fixture', 'set-cookie':'private'}) == {'X-Cpa-Version':'fixture'})
    for name in failures:
        print('FAIL: ' + name)
    return bool(failures)

if __name__ == '__main__':
    sys.exit(main())
