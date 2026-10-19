"""Спека нового запуска из явных public pins, без authority старой установки.

RegisteredLayout/MachineConfig/BundlePin и параметры планировщика передаёт
composition root. Импорт и рендер не читают legacy config, grants или Nomad
client; исторические job_spec/respec остаются в spec до удаления в #645.
"""
import hashlib
import re


# Новый путь #643 не использует старый WRAPPER/OUTER и не делает upgrade
# живого задания. Root связывает capabilities до регистрации нового job.
API_LAUNCH_CAPABILITIES = frozenset(('addressed_launch_v1', 'session_scope_v1',
    'scope_retirement_v1', 'external_image_v1', 'bootstrap_ssh_v1', 'initial_create_anchor_v1'))

# Идентификатор конечного installed-протокола, больше не shell-программа.
# Сохранённый dispatched_job не перерендеривается после смены шаблона.
API_WRAPPER = 'initial-create-anchor-v1'


def anchor_pin(executable, digest):
    """Доверенный installed content-addressed executable, не runtime/launch тела.

    Установка обязана проверить bytes/owner/mode до enrollment; renderer не
    читает файловую систему узла и не выдаёт желаемый hash за такую проверку.
    """
    from pathlib import PurePosixPath
    if (type(executable) is not str or len(executable) > 512
            or type(digest) is not str or not re.fullmatch('[0-9a-f]{64}', digest)
            or not re.fullmatch(r'/[A-Za-z0-9_./-]+', executable)
            or '..' in PurePosixPath(executable).parts
            or PurePosixPath(executable).as_posix() != executable
            or PurePosixPath(executable).parent.name != 'sha256-'+digest):
        raise ValueError('explicit installed content-addressed anchor pin required')
    return executable, digest


def api_job_spec(*, layout, machine, bundle, nomad_node_id, datacenter, user,
                 memory_mb, cpu, restore_generation, capabilities=frozenset(),
                 anchor_executable='', anchor_digest=''):
    """Новая регистрация из public Root ports, не JSON команды пользователя.

    Installed anchor только удерживает allocation, никогда не запускает native.
    Allocation restart повторяет hold, не bootstrap/startup. Единственный запуск
    native принадлежит node-owned native_exec. Установку immutable anchor и
    фактический image receipt связывает Root, не Nomad spec. Capabilities поступают от проверенных adapters, не из роли
    или поля запроса. Наличие флага не создаёт отсутствующий endpoint/SessionApi.
    Пока Root не связан, default gate отказывает ещё до регистрации задания.
    """
    from uuid import UUID
    from mop.driver.launch import BundlePin, MachineConfig, RegisteredLayout, LaunchResponse
    from mop.server.operations.scheduler import addressed_metadata
    if (type(capabilities) is not frozenset or not API_LAUNCH_CAPABILITIES <= capabilities):
        raise ValueError('new body launch capabilities are not bound')
    if type(layout) is not RegisteredLayout or type(machine) is not MachineConfig:
        raise ValueError('trusted registered layout and machine configuration required')
    registration = layout.registration
    if type(bundle) is BundlePin:
        # Пин одобренного определения: байты сверит узел (#643).
        if (bundle.role, bundle.digest) != (registration.work.role, registration.work.bundle_digest):
            raise ValueError('approved definition pin does not match registration')
        driver = bundle.driver
    else:
        LaunchResponse(registration, bundle, None).check(registration)
        driver = bundle.manifest.runtime.driver
    if driver == 'isolating' and machine.driver != 'pve':
        raise ValueError('isolating role requires a separate body namespace')
    if type(nomad_node_id) is not str or str(UUID(nomad_node_id)) != nomad_node_id:
        raise ValueError('explicit enrolled scheduler node identity required')
    if (type(datacenter) is not str or not re.fullmatch('[A-Za-z0-9_-]{1,64}', datacenter)
            or type(user) is not str or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', user)
            or type(memory_mb) is not int or not 128 <= memory_mb < 2**31
            or type(cpu) is not int or not 1 <= cpu < 2**31):
        raise ValueError('bounded registered scheduler resources required')
    if type(restore_generation) is not UUID:
        raise ValueError('typed restore generation required')
    anchor_pin(anchor_executable, anchor_digest)
    w = registration.work
    env = dict(MOP_TARGET_ID=w.target_id,
        MOP_INCARNATION_ID=w.incarnation_id, MOP_EXECUTOR_EPOCH=w.executor_epoch,
        MOP_GENERATION=str(w.generation), MOP_BOOTSTRAP_REVISION=registration.bootstrap_revision)
    # Те же адресные ключи, по которым stop/delete узнают свой job (#643);
    # пины запуска -- дополнительные ключи того же префикса.
    meta = addressed_metadata(w.epoch, restore_generation, w)
    meta.update({'mop.bootstrap_revision': registration.bootstrap_revision,
        'mop.bootstrap_digest': registration.bootstrap_digest,
        'mop.image_digest': registration.image_digest, 'mop.launch_schema': '1',
        'mop.launch_template': hashlib.sha256(API_WRAPPER.encode()).hexdigest(),
        'mop.anchor_digest': anchor_digest})
    return {'Job': dict(ID=w.target_id, Name=registration.display_name,
        Datacenters=[datacenter], Type='service', Meta=meta,
        Constraints=[dict(LTarget='${node.unique.id}', Operand='=', RTarget=nomad_node_id)],
        TaskGroups=[dict(Name='body', Count=1, Tasks=[dict(Name='launch', Driver='raw_exec',
            User=user, Config=dict(command=anchor_executable, args=['hold',
                '--sha256', anchor_digest, '--target', w.target_id,
                '--incarnation', w.incarnation_id, '--executor-epoch', w.executor_epoch,
                '--generation', str(w.generation), '--bootstrap-revision', registration.bootstrap_revision]), Env=env,
            Resources=dict(CPU=cpu, MemoryMB=memory_mb, MemoryMaxMB=memory_mb),
            KillTimeout=15*10**9)])])}
