"""Явные synthetic phase bytes; ни machine client, ни installation inputs (#746)."""
import base64
from datetime import datetime, timedelta, timezone
import hashlib
from uuid import uuid4

from mop.node.host_scope import Scope
from mop.node.http_execution import Admission, Work


def selected(action='prepare_body'):
    deadline = datetime.now(timezone.utc) + timedelta(seconds=60)
    work = Work(*(str(uuid4()) for _ in range(7)), 1, 1, 1,
                'builder', 'a'*64, str(uuid4()), action, deadline)
    admission = Admission(work, str(uuid4()), str(uuid4()), 1, deadline-timedelta(seconds=5))
    scope = Scope(work.epoch, work.node_id, work.executor_epoch, work.target_id,
                  work.incarnation_id, work.operation_id)
    image = str(uuid4())
    version = str(uuid4())
    delivery = dict(schema_version=1, work=work.wire(), phase=action,
        bootstrap_revision=str(uuid4()), bootstrap_digest='b'*64,
        image_revision=image, image_digest=hashlib.sha256(('image-revision:'+image).encode()).hexdigest(),
        display_name='pu-fixture-1', image_policy=dict(policy_id=str(uuid4()), cpu=100, memory_mb=64, disk_mb=64),
        selection=dict(policy_id=str(uuid4()), digest='c'*64,
            source=dict(origin='https://example.invalid/fixture.git', commit='d'*40, ref='refs/heads/main'),
            desired_model='fixture-model', model_config_digest=hashlib.sha256(b'{}').hexdigest(),
            consumers=[dict(purpose='model_config', version_id=str(uuid4())),
                       dict(purpose='source_credential', version_id=version)]),
        git_name='Fixture Owner', git_email='fixture@example.invalid', role=None, bootstrap=None,
        consumers=[dict(purpose='source_credential', version_id=version, name='opaque-not-a-path',
                        kind='credential', value=base64.b64encode(b'approved-phase-secret').decode())])
    if action != 'prepare_body':
        delivery['consumers'] = []
    if action == 'bootstrap_body':
        # #751: retained digest -- checksum всего canonical envelope, не
        # фиктивный pin и не SHA(tasks||variables). Root по-прежнему opaque.
        from mop.common.bootstrap_envelope import bounded_content, encode
        raw = encode(bounded_content(b'- command: never-root-executed\n', b'opaque: project-yaml\n',
            'fixture', 'e'*40, ()))
        delivery['bootstrap_digest'] = hashlib.sha256(raw).hexdigest()
        delivery['bootstrap'] = dict(envelope=base64.b64encode(raw).decode())
    return admission, scope, delivery
