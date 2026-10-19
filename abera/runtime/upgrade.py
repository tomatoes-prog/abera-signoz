"""Tenant SQLite migrations with sealed source snapshots and verified recovery.

signoz-host-v2 deliberately keeps ClickHouse and Keeper unchanged. It is not a
SQL down migration: compensation restores the source data and source binaries.
"""
from copy import deepcopy
from contextlib import closing
import hashlib
import json
import re
import sqlite3

from . import cloud_backup, lifecycle, maintenance
from .backup import confined_remove, restore, stop, tenant_for, verify_sqlite
from .host import RuntimeFailure
from .model import read_json, write_json


def requested(request):
    context = request.get('context', {})
    review = context.get('maintenanceReview') or context.get('maintenanceAcceptance', {}).get('review', {})
    return review.get('procedure', {}).get('adapter') == 'signoz-host-v2'


def checkpoint(controller, request):
    token = hashlib.sha256(request['operation']['id'].encode()).hexdigest()[:32]
    path = controller.host.root / 'maintenance' / (token + '-tenant-upgrade.json')
    identity = {'operationId': request['operation']['id'], 'subscriptionId': request['subscription']['id'],
                'release': request['release'], 'fromVersion': request['operation'].get('fromVersion'),
                'review': request['context'].get('maintenanceReview'),
                'acceptance': request['context'].get('maintenanceAcceptance')}
    fingerprint = hashlib.sha256(json.dumps(identity, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    progress = read_json(path) if path.exists() else {'fingerprint': fingerprint, 'stage': 'reviewed', 'backups': {}}
    if progress.get('fingerprint') != fingerprint:
        raise RuntimeFailure('Reviewed upgrade changed while its checkpoint exists')
    return path, progress


def validate(controller, request):
    controller.fence(request)
    images = request['release']['imageUris']
    lifecycle.release_images(controller.host, request['release'])  # pins shared storage exactly
    if request['release']['version'] != '0.3.0' or not re.fullmatch(r'0\.2\.[0-9]+', request['operation'].get('fromVersion', '')):
        raise RuntimeFailure('Unsupported verified-restore application transition')
    reviewed = request['context'].get('maintenanceReview')
    if reviewed:
        return maintenance.validate(controller, request)
    accepted = request['context'].get('maintenanceAcceptance', {})
    reviewed, result = accepted.get('review', {}), accepted.get('result', {})
    procedure = reviewed.get('procedure', {})
    sid = request['subscription']['id']
    members = {m['subscriptionId']: m for m in reviewed.get('members', [])}
    if (not accepted.get('operationId') or not accepted.get('releaseRevision') or not accepted.get('releaseContentSha256')
            or accepted.get('targetVersion') != '0.3.0' or reviewed.get('unit') != 'host:' + controller.env['HOST_INSTANCE_ID']
            or procedure.get('adapter') != 'signoz-host-v2' or procedure.get('storageCompatibility') != 'verified-restore'
            or procedure.get('components') != ['controller'] or not procedure.get('compatibilityEvidence')
            or sid not in members or not result.get('verified') or result.get('unit') != reviewed['unit']
            or set(result.get('members', [])) != set(members)
            or {t['subscriptionId'] for t in controller.host.state['tenants']} != set(members)):
        raise RuntimeFailure('Tenant migration requires accepted maintenance for this host')
    if (result.get('controllerImage') != images['admin'] or result.get('clickhouseImage') != images['clickhouse']
            or result.get('keeperImage') != images['keeper']
            or result.get('appImage') != images['app'] or result.get('collectorImage') != images['collector']
            or maintenance.command(['docker', 'inspect', '--format', '{{.Config.Image}}', 'abera-controller']).strip() != images['admin']):
        raise RuntimeFailure('Accepted shared runtime differs from the running controller or storage')
    sub = controller.subscription(sid)
    if members[sid]['customerId'] != sub['customerId'] or sub['currentVersion'] != request['operation']['fromVersion']:
        raise RuntimeFailure('Accepted tenant identity or source version changed')
    gate = controller.core.get_item(Key={'pk': 'CAPACITY#abera-signoz#MAINTENANCE', 'sk': 'LOCK'}, ConsistentRead=True).get('Item')
    if gate:
        raise RuntimeFailure('Another shared maintenance owns the host admission gate')
    return reviewed


def prepare(controller, request):
    reviewed = validate(controller, request)
    path, progress = checkpoint(controller, request)
    if progress['stage'] != 'reviewed':
        return progress
    sid = request['subscription']['id']
    if 'before' not in progress:
        progress['before'] = deepcopy(controller.host.state)
        write_json(path, progress)
    before = progress['before']
    source = next(t for t in before['tenants'] if t['subscriptionId'] == sid)
    if source.get('productVersion') != request['operation']['fromVersion']:
        raise RuntimeFailure('Installed tenant version differs from reviewed source')
    if not source.get('ready') or source.get('state') not in {'ACTIVE', 'SUSPENDED'}:
        raise RuntimeFailure('Migration requires an initialized tenant and preserved application identity')
    source_images(source, before)
    tenants = before['tenants'] if request['context'].get('maintenanceReview') else [source]
    for tenant in tenants:
        stop(controller.host, tenant)
    for tenant in tenants:
        member = tenant['subscriptionId']
        if member in progress['backups']:
            continue
        validate(controller, request)
        sub = controller.subscription(member)
        if tenant.get('productVersion') != sub['currentVersion']:
            raise RuntimeFailure('Snapshot source version differs from the installed subscription')
        child = deepcopy(request)
        child['subscription'].update(id=member, customerId=sub['customerId'])
        child['release']['version'] = tenant['productVersion']
        child['context']['dataGeneration'] = sub.get('dataGeneration', 1)
        receipt = controller.backup(child, final=True)
        if (not receipt.get('verified') or not receipt.get('manifestVersionId')
                or receipt.get('productVersion') != tenant['productVersion']
                or not receipt.get('runtimeManifest', {}).get('sealed')):
            raise RuntimeFailure('Migration requires a sealed versioned backup of the source release')
        progress['backups'][member] = receipt
        write_json(path, progress)
    progress['stage'] = 'backed-up'
    write_json(path, progress)
    return progress


def source_images(source, before):
    images = {key: source.get(key, before.get(key)) for key in ('appImage', 'collectorImage')}
    if any(not isinstance(value, str) or not re.fullmatch(r'[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}', value)
           for value in images.values()):
        raise RuntimeFailure('Source image identity is unavailable')
    return {**images, 'productVersion': source['productVersion']}


def migration_snapshot(controller, tenant, path):
    """Inspect a stopped, consistent SQLite copy after the app applies migrations."""
    stage = path.parent / (path.stem + '-schema')
    if stage.exists():
        confined_remove(stage, path.parent)
    stage.mkdir(exist_ok=True)
    stop(controller.host, tenant)
    controller.host.compose('cp', f"app-{tenant['slot']}:/var/lib/signoz/.", str(stage))
    database = stage / 'signoz.db'
    verify_sqlite(database)
    with closing(sqlite3.connect(f'file:{database.as_posix()}?mode=ro', uri=True)) as db:
        applied = {str(row[0]).lstrip('0') for row in db.execute('SELECT name FROM migration')}
        if not {'133', '134', '135'} <= applied:
            raise RuntimeFailure('Target SQLite migrations 133-135 are not all applied')
    confined_remove(stage, path.parent)
    return ['133', '134', '135']


def apply_images(controller, sid, images):
    state = controller.host.state
    tenant = next(t for t in state['tenants'] if t['subscriptionId'] == sid)
    stop(controller.host, tenant)
    tenant.update(images, state='ACTIVE')  # only private app/collector; ingress remains stopped
    controller.host.save(state)
    controller.host.up(sid, ingress=False)


def verify(controller, request):
    reviewed = validate(controller, request)
    path, progress = checkpoint(controller, request)
    if progress['stage'] not in {'backed-up', 'applying', 'verified', 'opening', 'finalized'}:
        raise RuntimeFailure('Migration has no completed source snapshot or requires recovery')
    if request['context'].get('maintenanceReview'):
        helper_path, _ = maintenance.checkpoint(controller, request)
        if not maintenance.ensure_controller(controller, request, helper_path, reviewed):
            return None
    sid = request['subscription']['id']
    if progress['stage'] in {'backed-up', 'applying'}:
        progress['stage'] = 'applying'
        write_json(path, progress)
        try:
            apply_images(controller, sid, lifecycle.release_images(controller.host, request['release']))
            validate(controller, request)
            progress['sqliteMigrations'] = migration_snapshot(controller, tenant_for(controller.host, sid), path)
            controller.host.isolated_probe()
            progress['stage'] = 'verified'
            write_json(path, progress)
        except Exception:
            stop(controller.host, tenant_for(controller.host, sid))
            progress['stage'] = 'recovery-required'
            write_json(path, progress)
            raise
    images = request['release']['imageUris']
    return {'unit': reviewed['unit'], 'members': sorted(m['subscriptionId'] for m in reviewed['members']),
            'verified': True, 'adapter': 'signoz-host-v2', 'sqliteMigrations': progress['sqliteMigrations'],
            'backupVersions': {s: r['manifestVersionId'] for s, r in progress['backups'].items()},
            'sourceVersion': request['operation']['fromVersion'], 'targetVersion': '0.3.0',
            'appImage': images['app'], 'collectorImage': images['collector'],
            'controllerImage': images['admin'], 'clickhouseImage': images['clickhouse'], 'keeperImage': images['keeper']}


def resume_members(controller, request, progress):
    sid = request['subscription']['id']
    source = progress['before']['tenants']
    members = source if request['context'].get('maintenanceReview') else [t for t in source if t['subscriptionId'] == sid]
    members = sorted(members, key=lambda t: t['subscriptionId'] == sid)  # pilot ingress opens last
    for original in members:
        validate(controller, request)
        member = original['subscriptionId']
        sub = controller.subscription(member)
        lifecycle.reconcile(controller.host, member, sub['billingRevision'], sub['entitlementContext']['terms'])
        active = original['state'] == 'ACTIVE' and sub.get('desiredEntitlement', 'ACTIVE') == 'ACTIVE'
        lifecycle.set_state(controller.host, member, 'ACTIVE' if active else 'SUSPENDED')


def finalize(controller, request):
    validate(controller, request)
    path, progress = checkpoint(controller, request)
    if progress['stage'] not in {'verified', 'opening', 'finalized'}:
        raise RuntimeFailure('Cannot reopen ingress before migration verification')
    progress['stage'] = 'opening'
    write_json(path, progress)
    resume_members(controller, request, progress)
    progress['stage'] = 'finalized'
    write_json(path, progress)


def compensate(controller, request):
    validate(controller, request)
    path, progress = checkpoint(controller, request)
    if progress['stage'] in {'opening', 'finalized'}:
        stop(controller.host, tenant_for(controller.host, request['subscription']['id']))
        raise RuntimeFailure('Ingress may have reopened; preserve new data for reviewed recovery')
    if 'before' not in progress:
        return {'restored': False, 'dataChanged': False}
    sid = request['subscription']['id']
    source = next(t for t in progress['before']['tenants'] if t['subscriptionId'] == sid)
    if progress['stage'] not in {'reviewed', 'backed-up', 'restored'}:
        receipt = progress['backups'].get(sid)
        if not receipt or receipt['productVersion'] != source['productVersion']:
            raise RuntimeFailure('Source recovery snapshot is unavailable; tenant stays stopped')
        stop(controller.host, tenant_for(controller.host, sid))
        progress['stage'] = 'restoring'
        write_json(path, progress)
        sub = controller.subscription(sid)
        directory = cloud_backup.download(controller.s3, receipt, controller.host.root, subscription_id=sid,
            customer_id=sub['customerId'], environment=controller.env['ENVIRONMENT'], bucket=controller.env['BACKUP_BUCKET'],
            account=controller.env['AWS_ACCOUNT_ID'], region=controller.env['AWS_REGION'])
        # A failed SQLite migration must not prevent recovery by demanding a new
        # healthy target snapshot. The immutable source snapshot already exists;
        # the warm ledger merge still preserves current counters/accepted batches.
        restored = restore(controller.host, sid, directory, resume=False, preserve_current=False)
        if not restored.get('restored'):
            raise RuntimeFailure('Source data restoration was not confirmed')
        apply_images(controller, sid, source_images(source, progress['before']))
        validate(controller, request)
        controller.host.isolated_probe()
        progress['stage'] = 'restored'
        progress['restoredBackupId'] = receipt['backupId']
        write_json(path, progress)
    if progress.get('restoredBackupId'):
        confined_remove(controller.host.root / 'backups' / sid / progress['restoredBackupId'], controller.host.root / 'backups')
    resume_members(controller, request, progress)
    return {'restored': progress['stage'] == 'restored', 'sourceVersion': source['productVersion'],
            'backupVersionId': progress.get('backups', {}).get(sid, {}).get('manifestVersionId')}
