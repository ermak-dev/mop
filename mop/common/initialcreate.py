"""Конечный словарь первого запуска (#721), не произвольный workflow.

Узел обязан отдельно связать каждый новый action. Наличие слова в протоколе
не объявляет physical capability и не разрешает unknown-action fallback.
Бюджет эффекта включает уборку; SQL/HTTP timeout к нему не относится.
"""
PHASES = ('prepare_body', 'bootstrap_body', 'initial_identity', 'scheduler_register',
          'native_exec', 'confirm_ready', 'activate_grant')
PLANNED = frozenset(('prepare_body', 'bootstrap_body', 'initial_identity'))
REGISTERED = frozenset(('native_exec', 'confirm_ready'))
MACHINE = PLANNED | REGISTERED
# (максимальный эффект, обязательный резерв уборки), секунды.
BUDGETS = {'prepare_body': (60, 20), 'bootstrap_body': (300, 20),
           'initial_identity': (20, 10), 'native_exec': (60, 20),
           'confirm_ready': (60, 10), 'scheduler_register': (20, 10), 'activate_grant': (20, 10)}
CONFIRMATIONS = {'prepare_body': 'body-prepared', 'bootstrap_body': 'body-bootstrapped',
                 'initial_identity': 'initial-identity-applied', 'native_exec': 'native-started'}
# Только точные серверные version refs, не имена секретов от машины.
CONSUMERS = frozenset(('source_credential', 'bootstrap_input', 'model_config', 'model_credential'))
PHASE_CONSUMERS = {'prepare_body': frozenset(('source_credential',)),
                  'bootstrap_body': frozenset(('bootstrap_input',)),
                  'initial_identity': frozenset(),
                  'native_exec': frozenset(('model_config', 'model_credential')),
                  'confirm_ready': frozenset()}


def lease_seconds(action):
    """Непродлеваемая lease одной фазы; неизвестное действие не допускается."""
    effect, cleanup = BUDGETS[action]
    return effect + cleanup


def source_ref(value):
    """Конечная fully qualified Git ref, без refspec, reflog и path escapes."""
    import re
    return (type(value) is str and re.fullmatch(r'refs/(heads|tags)/[A-Za-z0-9_./-]{1,240}', value) is not None
            and '..' not in value and not value.endswith('.')
            and all(part and not part.startswith('.') and not part.endswith('.lock') for part in value.split('/')))


def layout_metadata(*, display_name, image_revision, image_policy, image_digest):
    """Логические пины layout: CPU MHz, memory/disk MiB, не physical receipt.

    int32 -- положительный диапазон ProjectPolicySnapshot в PostgreSQL.
    image_digest адресует UUID ImageRevision, а не checksum установленных bytes.
    """
    from . import credentials as p
    import hashlib
    import re
    if (type(display_name) is not str or not 1 <= len(display_name) <= 64
            or re.fullmatch(r'pu-[A-Za-z0-9][A-Za-z0-9._-]*-[0-9]+', display_name) is None):
        raise p.CredentialError('canonical initial launch display name required')
    p.identifier(image_revision)
    p.exact(image_policy, ('policy_id', 'cpu', 'memory_mb', 'disk_mb'))
    p.identifier(image_policy['policy_id'])
    if any(type(image_policy[key]) is not int or not 1 <= image_policy[key] < 2**31
           for key in ('cpu', 'memory_mb', 'disk_mb')):
        raise p.CredentialError('positive int32 initial launch resources required')
    if image_digest != hashlib.sha256(('image-revision:'+image_revision).encode()).hexdigest():
        raise p.CredentialError('logical initial launch image revision pin changed')
    return dict(display_name=display_name, image_revision=image_revision, image_policy=dict(image_policy))


def parse_delivery(value, work):
    """Строгий отдельный response; Registration.matches/old LaunchResponse неизменны."""
    from . import credentials as p
    import hashlib
    import re
    p.exact(value, ('schema_version', 'work', 'phase', 'bootstrap_revision', 'bootstrap_digest',
        'image_digest', 'display_name', 'image_revision', 'image_policy',
        'selection', 'git_name', 'git_email', 'role', 'bootstrap', 'consumers'))
    layout_metadata(**{key: value[key] for key in ('display_name', 'image_revision', 'image_policy', 'image_digest')})
    phase = value['phase']
    if type(work) is not dict:
        raise p.CredentialError('exact expected initial launch work required')
    p.exact(value['work'], work)
    if (type(value['schema_version']) is not int or value['schema_version'] != 1
            or type(phase) is not str or phase not in MACHINE
            or any(type(value['work'][key]) is not type(item) or value['work'][key] != item for key, item in work.items())
            or phase != work.get('action')):
        raise p.CredentialError('exact finite initial launch phase and work required')
    p.identifier(value['bootstrap_revision'])
    selected = value['selection']
    p.exact(selected, ('policy_id', 'digest', 'source', 'desired_model', 'model_config_digest', 'consumers'))
    p.identifier(selected['policy_id'])
    for digest in (value['bootstrap_digest'], value['image_digest'], selected['digest'], selected['model_config_digest']):
        if type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest):
            raise p.CredentialError('initial launch digest required')
    p.exact(selected['source'], ('origin', 'commit', 'ref'))
    if (type(selected['source']['commit']) is not str or not re.fullmatch('[0-9a-f]{40}|[0-9a-f]{64}', selected['source']['commit'])
            or not source_ref(selected['source']['ref'])):
        raise p.CredentialError('exact initial launch source required')
    for text, limit in ((selected['source']['origin'], 2048), (selected['desired_model'], 128),
                        (value['git_name'], 256), (value['git_email'], 256)):
        if type(text) is not str or not 1 <= len(text) <= limit or any(ord(c) < 32 or ord(c) == 127 for c in text):
            raise p.CredentialError('bounded initial launch input text required')
    refs = selected['consumers']
    if type(refs) is not list or len(refs) > 32:
        raise p.CredentialError('bounded initial launch consumer selection required')
    pins = set()
    for item in refs:
        p.exact(item, ('purpose', 'version_id'))
        p.identifier(item['version_id'])
        if type(item['purpose']) is not str or item['purpose'] not in CONSUMERS or item['version_id'] in {version for _, version in pins}:
            raise p.CredentialError('unique finite initial launch consumer required')
        pins.add((item['purpose'], item['version_id']))
    if sum(purpose == 'model_config' for purpose, _ in pins) != 1:
        raise p.CredentialError('exact model configuration selection required')
    role, bootstrap = value['role'], value['bootstrap']
    if phase in ('initial_identity', 'native_exec'):
        p.exact(role, ('manifest', 'prompt'))
        from .soul import from_manifest
        bundle = from_manifest(role['manifest'], p.binary(role['prompt']))
        if (bundle.role, bundle.digest) != (work['role'], work['bundle_digest']):
            raise p.CredentialError('approved initial launch role pin changed')
    elif role is not None:
        raise p.CredentialError('role bytes outside initial identity or native phase')
    if phase == 'bootstrap_body':
        from .bootstrap_envelope import BootstrapError, decode_base64
        p.exact(bootstrap, ('envelope',))
        try:
            decode_base64(bootstrap['envelope'], value['bootstrap_digest'])
        except BootstrapError as error:
            raise p.CredentialError(str(error)) from None
    elif bootstrap is not None:
        raise p.CredentialError('bootstrap bytes outside bootstrap phase')
    consumers = value['consumers']
    if type(consumers) is not list or len(consumers) > 32:
        raise p.CredentialError('bounded phase consumer delivery required')
    delivered = set()
    for item in consumers:
        p.exact(item, ('purpose', 'version_id', 'name', 'kind', 'value'))
        p.identifier(item['version_id'])
        if type(item['purpose']) is not str:
            raise p.CredentialError('finite phase consumer purpose required')
        pair = (item['purpose'], item['version_id'])
        if pair not in pins or pair in delivered or pair[0] not in PHASE_CONSUMERS[phase]:
            raise p.CredentialError('exact phase consumer delivery required')
        raw = p.binary(item['value'])
        if pair[0] == 'model_config' and hashlib.sha256(raw).hexdigest() != selected['model_config_digest']:
            raise p.CredentialError('selected model configuration digest mismatch')
        if item['kind'] not in ('file', 'variable', 'credential') or type(item['name']) is not str or not 1 <= len(item['name']) <= 256:
            raise p.CredentialError('bounded selected consumer metadata required')
        delivered.add(pair)
    if delivered != {pair for pair in pins if pair[0] in PHASE_CONSUMERS[phase]}:
        raise p.CredentialError('phase consumer delivery is incomplete')
    return value
