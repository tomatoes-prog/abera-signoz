"""Shared host updates under the core's reviewed UPDATE ownership.

No free-form command or SQL comes from a review. A different storage schema
needs a new reviewed adapter in this repository and compatibility tests.
"""
from copy import deepcopy
import hashlib
import re
import subprocess
import tarfile
from .backup import stop, start, digest
from .host import RuntimeFailure, api
from .model import read_json, write_json


def command(args, *, timeout=900):
    result = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        raise RuntimeFailure('Reviewed host maintenance command failed; inspect private logs')
    return result.stdout


def validate(controller, request):
    reviewed = request['context'].get('maintenanceReview')
    if not reviewed:
        return None
    if (reviewed['unit'] != 'host:' + controller.env['HOST_INSTANCE_ID']
            or reviewed['pilot'] != request['subscription']['id']):
        raise RuntimeFailure('Maintenance targets another host or pilot')
    procedure = reviewed['procedure']
    if (procedure.get('adapter') != 'signoz-host-v1' or procedure.get('unitKind') != 'host'
            or procedure.get('storageCompatibility') != 'no-schema-change'
            or not procedure.get('compatibilityEvidence')
            or not set(procedure.get('components', [])) <= {'controller', 'clickhouse', 'keeper'}):
        raise RuntimeFailure('Unsupported shared transition; implement and review its migration adapter')
    gate = controller.core.get_item(Key={'pk': 'CAPACITY#abera-signoz#MAINTENANCE', 'sk': 'LOCK'}, ConsistentRead=True).get('Item', {})
    if gate.get('operationId') != request['operation']['id'] or gate.get('unit') != reviewed['unit']:
        raise RuntimeFailure('Shared maintenance no longer owns the admission gate')
    tenants = controller.host.state['tenants']
    members = {item['subscriptionId']: item for item in reviewed['members']}
    if set(members) != {item['subscriptionId'] for item in tenants}:
        raise RuntimeFailure('Actual host tenants differ from reviewed inventory')
    for tenant in tenants:
        member = members[tenant['subscriptionId']]
        sub = controller.subscription(tenant['subscriptionId'])
        allowed_operation = request['operation']['id'] if tenant['subscriptionId'] == reviewed['pilot'] else None
        if (sub.get('activeOperationId') != allowed_operation or sub['customerId'] != member['customerId']
                or sub['currentVersion'] != member['currentVersion']
                or sub['currentVersion'] not in procedure.get('compatibleApplicationVersions', [])):
            raise RuntimeFailure('A neighbouring tenant changed or is incompatible with shared storage')
    images = request['release']['imageUris']
    if set(images) != {'app', 'collector', 'clickhouse', 'keeper', 'admin'} or any(
            not re.fullmatch(r'[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}', image) for image in images.values()):
        raise RuntimeFailure('All release images must be pinned by digest')
    return reviewed


def checkpoint(controller, request):
    operation = request['operation']['id']
    token = hashlib.sha256(operation.encode()).hexdigest()[:32]
    path = controller.host.root / 'maintenance' / (token + '.json')
    progress = read_json(path) if path.exists() else {'operationId': operation, 'backups': {}, 'stage': 'reviewed'}
    return path, progress


def prepare(controller, request):
    reviewed = validate(controller, request)
    path, progress = checkpoint(controller, request)
    if progress['stage'] != 'reviewed':
        return progress
    if 'before' not in progress:
        progress['before'] = deepcopy(controller.host.state)
        write_json(path, progress)
    # Fence ingress for the entire unit before taking its mutually consistent
    # snapshots. Capture independently verifies each tenant's native restore.
    for tenant in controller.host.state['tenants']:
        stop(controller.host, tenant)
    for member in reviewed['members']:
        sid = member['subscriptionId']
        if sid in progress['backups']: continue
        sub = controller.subscription(sid)
        child = deepcopy(request)
        child['subscription'].update(id=sid, customerId=sub['customerId'])
        child['release']['version'] = sub['currentVersion']
        child['context']['dataGeneration'] = sub.get('dataGeneration', 1)
        receipt = controller.backup(child, final=True)
        if not receipt.get('verified') or not receipt.get('manifestVersionId'):
            raise RuntimeFailure('Shared maintenance requires version-pinned verified tenant backups')
        progress['backups'][sid] = receipt
        write_json(path, progress)
    controller.host.compose('stop', 'clickhouse', 'keeper')
    stage = path.parent / (path.stem + '-storage')
    stage.mkdir(parents=True, exist_ok=True)
    for service in ('clickhouse', 'keeper'):
        archive = stage / (service + '.tar.gz')
        if not archive.exists():
            target = stage / service
            target.mkdir(exist_ok=True)
            controller.host.compose('cp', service + ':/var/lib/clickhouse/.', str(target))
            with tarfile.open(archive, 'w:gz') as stream:
                stream.add(target, arcname=service)
        # Storage files are cold and checksummed. This is integrity evidence;
        # tenant receipts separately carry actual native restore verification.
        checksum = digest(archive)
        object_key = 'recovery/_maintenance/' + path.stem + '/' + archive.name
        controller.s3.upload_file(str(archive), controller.env['BACKUP_BUCKET'], object_key,
            ExtraArgs={'ServerSideEncryption': 'aws:kms', 'SSEKMSKeyId': controller.env['DATA_KEY_ARN'], 'Metadata': {'sha256': checksum}})
        obj = controller.s3.head_object(Bucket=controller.env['BACKUP_BUCKET'], Key=object_key)
        if not obj.get('VersionId') or obj['VersionId'] == 'null' or obj.get('Metadata', {}).get('sha256') != checksum:
            raise RuntimeFailure('Cold shared storage backup is not versioned and checksummed')
        progress.setdefault('sharedStorage', {})[service] = {'key': object_key, 'versionId': obj['VersionId'], 'sha256': checksum,
                                                           'integrityVerified': True, 'restoreAcceptance': 'required-for-this-transition'}
        write_json(path, progress)
    progress['stage'] = 'backed-up'
    write_json(path, progress)
    return progress


def verify(controller, request):
    reviewed = validate(controller, request)
    path, progress = checkpoint(controller, request)
    if progress['stage'] == 'reviewed':
        raise RuntimeFailure('Shared maintenance has not completed backups')
    images = request['release']['imageUris']
    if progress['stage'] == 'backed-up':
        state = controller.host.state
        for name in ('clickhouse', 'keeper'):
            if name in reviewed['procedure']['components']:
                state[name + 'Image'] = images[name]
            elif state[name + 'Image'] != images[name]:
                raise RuntimeFailure('Release changes an undeclared shared component')
        controller.host.save(state)
        controller.host.render()
        controller.host.compose('up', '-d', '--wait', '--wait-timeout', '180', 'keeper', 'clickhouse')
        progress['stage'] = 'storage-updated'
        write_json(path, progress)
    target_admin = images['admin']
    running_admin = command(['docker', 'inspect', '--format', '{{.Config.Image}}', 'abera-controller']).strip()
    if running_admin != target_admin:
        if 'controller' not in reviewed['procedure']['components']:
            raise RuntimeFailure('Release changes an undeclared controller')
        # A durable helper replaces the controller. The new controller resumes
        # this same queued hook, so the core never accepts an unobserved restart.
        helper = 'abera-controller-upgrade-' + path.stem[:12]
        exists = subprocess.run(['docker', 'inspect', helper], capture_output=True, text=True).returncode == 0
        if not exists:
            command(['docker', 'run', '-d', '--name', helper, '--network', 'host',
                '-v', '/var/run/docker.sock:/var/run/docker.sock', '-v', '/opt/abera:/opt/abera',
                '--entrypoint', 'python3', target_admin, '-m', 'abera.runtime.controller_update', target_admin, path.stem])
        return None
    for tenant in controller.host.state['tenants']:
        if tenant['state'] == 'ACTIVE': start(controller.host, tenant)
        else: stop(controller.host, tenant)
    for tenant in controller.host.state['tenants']:
        if tenant['state'] == 'ACTIVE':
            api(f"http://127.0.0.1:{25800+tenant['slot']}", 'GET', '/api/v1/health')
    controller.host.isolated_probe()
    progress['stage'] = 'verified'
    write_json(path, progress)
    return {'unit': reviewed['unit'], 'members': sorted(progress['backups']), 'verified': True,
            'backupVersions': {sid: value['manifestVersionId'] for sid, value in progress['backups'].items()},
            'sharedStorage': progress['sharedStorage'], 'controllerImage': target_admin,
            'clickhouseImage': controller.host.state['clickhouseImage'], 'keeperImage': controller.host.state['keeperImage']}
