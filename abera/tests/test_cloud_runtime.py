import copy
import hashlib
import json
import zipfile
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from types import SimpleNamespace

import pytest
from boto3.dynamodb.types import TypeSerializer

from abera.runtime import cloud_backup
from abera.runtime import daemon
from abera.runtime.daemon import Controller, dynamo, plain
from abera.runtime.host import RuntimeFailure
from abera.runtime.lifecycle import release_images
from abera.runtime.model import write_json
from abera.tools.lifecycle_smoke import LocalS3


def snapshot(path):
    path.mkdir()
    with zipfile.ZipFile(path/'database.zip', 'w') as z:
        z.writestr('metadata/example.sql', 'synthetic fixture')
    (path/'files.tar.gz').write_bytes(b'synthetic application snapshot')
    (path/'application-secrets.json').write_text('{}')
    files = {p.name: {'bytes': p.stat().st_size, 'sha256': hashlib.sha256(p.read_bytes()).hexdigest()} for p in path.iterdir()}
    write_json(path/'manifest.json', {'schemaVersion': 1, 'backupId': 'backup-test', 'verified': True,
        'capturedAt': 1700000000, 'files': files, 'productVersion': '0.1.0', 'compatibilityGeneration': 'signoz-0.1', 'sealed': True})
    return path


def test_s3_retry_reuses_exact_versions_and_recovery_checks_owner(tmp_path):
    s3 = LocalS3()
    path = snapshot(tmp_path/'capture')
    request = {'subscription': {'id': 'tenant-123', 'customerId': 'customer-123'}, 'context': {'dataGeneration': 1},
               'release': {'version': '0.1.1'}, 'environment': 'dev'}
    first = cloud_backup.upload(s3, 'backups', 'kms-key', path, request, '123456789012', 'us-east-2')
    second = cloud_backup.upload(s3, 'backups', 'kms-key', path, request, '123456789012', 'us-east-2')
    assert first == second and len(s3.objects) == 4
    assert first['productVersion'] == '0.1.0'  # UPDATE captures the previous release.
    identity = dict(subscription_id='tenant-123', customer_id='customer-123', environment='dev', bucket='backups', account='123456789012', region='us-east-2')
    restored = cloud_backup.download(s3, first, tmp_path/'restore', **identity)
    assert (restored/'database.zip').read_bytes() == (path/'database.zip').read_bytes()
    with pytest.raises(RuntimeFailure, match='foreign'):
        cloud_backup.download(s3, first, tmp_path/'foreign', **(identity | {'customer_id': 'another'}))
    cloud_backup.purge(s3, first, subscription_id='tenant-123', bucket='backups')
    cloud_backup.purge(s3, first, subscription_id='tenant-123', bucket='backups')
    assert not s3.objects


def test_immutable_backup_key_conflict_is_not_overwritten():
    s3 = LocalS3()
    s3.put_object(Bucket='backups', Key='key', Body=b'first', Metadata={'sha256': 'one'})
    with pytest.raises(RuntimeFailure, match='different data'):
        cloud_backup.existing(s3, 'backups', 'key', 'two')
    assert len(s3.objects) == 1


def test_job_numbers_are_dynamo_safe_and_lossless():
    value = {'status': 'DONE', 'metadata': {'seconds': 1.25, 'bytes': 20}}
    encoded = dynamo(value)
    TypeSerializer().serialize(encoded)
    assert encoded['metadata']['seconds'] == Decimal('1.25')
    assert plain(encoded) == value


def test_deadline_and_operation_ownership_fence_before_mutation():
    controller = Controller.__new__(Controller)
    controller.heartbeat = lambda: setattr(controller, 'lease', True)
    controller.subscription = lambda sid: {'activeOperationId': 'current'}
    request = {'subscription': {'id': 'tenant-123'}, 'operation': {'id': 'old'}, 'context': {}}
    with pytest.raises(RuntimeFailure, match='deadline'): controller.fence(request)
    request['context']['deadlineAt'] = (datetime.now(timezone.utc)+timedelta(minutes=1)).isoformat()
    with pytest.raises(RuntimeFailure, match='no longer owns'): controller.fence(request)
    request['operation']['id'] = 'current'
    assert controller.fence(request)['activeOperationId'] == 'current'


def test_shared_storage_cannot_change_during_a_tenant_update():
    images = {k: 'repo/'+k+'@sha256:'+'a'*64 for k in ['app','collector','clickhouse','keeper','admin']}
    host = SimpleNamespace(state={'local': False, 'clickhouseImage': images['clickhouse'], 'keeperImage': images['keeper']})
    release = {'version': '0.1.1', 'imageUris': images}
    assert release_images(host, release) == {'appImage': images['app'], 'collectorImage': images['collector'], 'productVersion': '0.1.1'}
    changed = copy.deepcopy(release); changed['imageUris']['clickhouse'] = images['clickhouse'].replace('a'*64, 'b'*64)
    with pytest.raises(RuntimeFailure, match='maintenance'): release_images(host, changed)
    changed['imageUris']['clickhouse'] = 'repo/clickhouse:latest'
    with pytest.raises(RuntimeFailure, match='digests'): release_images(host, changed)


def test_restore_does_not_resume_before_fresh_operation_and_payment_check(monkeypatch, tmp_path):
    controller = Controller.__new__(Controller)
    controller.host = SimpleNamespace(root=tmp_path)
    controller.s3 = object()
    controller.env = dict(ENVIRONMENT='dev', BACKUP_BUCKET='backups', AWS_ACCOUNT_ID='123456789012', AWS_REGION='us-east-2')
    events = []
    monkeypatch.setattr(cloud_backup, 'download', lambda *args, **kw: tmp_path/'restored')
    monkeypatch.setattr(daemon, 'restore', lambda *args, **kw: events.append(('restore', kw)) or {'restored': True})
    def lost_ownership(request):
        events.append(('fence', None))
        raise RuntimeFailure('operation no longer owns this subscription')
    controller.fence = lost_ownership
    monkeypatch.setattr(daemon.lifecycle, 'set_state', lambda *args: pytest.fail('stale operation resumed tenant'))
    request = {'release': {'version': '0.1.0'}}
    sub = {'subscriptionId': 'tenant-123', 'customerId': 'customer-123'}
    with pytest.raises(RuntimeFailure, match='no longer owns'):
        controller.recover(request, sub, {'productVersion': '0.1.0'}, cold=False)
    assert events == [('restore', {'cold': False, 'resume': False}), ('fence', None)]
    with pytest.raises(RuntimeFailure, match='matching released'):
        controller.recover(request, sub, {'productVersion': '0.2.0'}, cold=False)


def test_backup_retry_persists_expiry_before_success_and_uses_capture_date(monkeypatch, tmp_path):
    controller = Controller.__new__(Controller)
    controller.host = SimpleNamespace(root=tmp_path)
    controller.s3 = object()
    controller.env = dict(BACKUP_BUCKET='backups', DATA_KEY_ARN='kms-key', AWS_ACCOUNT_ID='123456789012', AWS_REGION='us-east-2')
    request = {'subscription': {'id': 'tenant-123'}, 'operation': {'id': 'operation-123', 'type': 'BACKUP'}}
    receipt = {'capturedAt': '2026-09-01T00:00:00Z', 'backupId': 'backup-test'}
    monkeypatch.setattr(daemon, 'capture', lambda *args, **kw: tmp_path/'backups'/'snapshot')
    monkeypatch.setattr(cloud_backup, 'upload', lambda *args: receipt)
    monkeypatch.setattr(daemon, 'confined_remove', lambda *args: None)
    writes = []
    def interrupted_write(path, data):
        writes.append(path.parent.name)
        if path.parent.name == 'cloud-receipts':
            raise OSError('synthetic crash after expiry journal persisted')
        write_json(path, data)
    monkeypatch.setattr(daemon, 'write_json', interrupted_write)
    with pytest.raises(OSError): controller.backup(request)
    assert writes == ['backup-expiry', 'cloud-receipts']
    journal = json.loads(next((tmp_path/'backup-expiry').glob('*.json')).read_text())
    assert journal['expiresAt'] == '2026-10-01T00:00:00Z'
    monkeypatch.setattr(daemon, 'write_json', write_json)
    assert controller.backup(request) == receipt
    assert len(list((tmp_path/'backup-expiry').glob('*.json'))) == 1
