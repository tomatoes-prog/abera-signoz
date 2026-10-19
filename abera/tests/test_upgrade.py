from copy import deepcopy
from contextlib import closing
import sqlite3
from types import SimpleNamespace

import pytest

from abera.runtime import upgrade as u
from abera.runtime.host import RuntimeFailure


def image(name, digest='a'):
    return 'repo/' + name + '@sha256:' + digest * 64


class Host:
    def __init__(self, root):
        self.root, self.events = root, []
        self.fail_migration = False
        self.data = {sid: {'migrations': ['132'], 'pricing': 'source-pricing', 'usage': 99} for sid in ['a', 'b']}
        self.value = {'local': False, 'clickhouseImage': image('clickhouse'), 'keeperImage': image('keeper'), 'tenants': [
            {'subscriptionId': sid, 'customerId': 'customer-' + sid, 'productVersion': '0.2.4', 'state': 'ACTIVE', 'ready': True,
             'slot': slot, 'appImage': image('app'), 'collectorImage': image('collector'), 'revision': 1,
             'terms': [{'cycleId': 'paid-cycle', 'startsAt': 1, 'endsAt': 2, 'plan': 'lite'}]}
            for slot, sid in enumerate(['a', 'b'], 1)]}

    @property
    def state(self):
        return deepcopy(self.value)

    def save(self, state):
        self.value = deepcopy(state)

    def up(self, sid, *, ingress):
        assert not ingress
        self.events.append(('private-up', sid))
        tenant = next(t for t in self.value['tenants'] if t['subscriptionId'] == sid)
        if tenant['productVersion'] == '0.3.0':
            self.data[sid]['pricing'] = None  # irreversible migration 135
            if self.fail_migration:
                raise RuntimeFailure('synthetic failed migration')
            self.data[sid]['migrations'] = ['132', '133', '134', '135']

    def compose(self, *args):
        self.events.append(args)
        if args[0] == 'cp':
            slot = int(args[1].split(':')[0].split('-')[1])
            sid = self.value['tenants'][slot - 1]['subscriptionId']
            with closing(sqlite3.connect(str(args[2]) + '/signoz.db')) as db:
                db.execute('CREATE TABLE migration (name TEXT)')
                db.executemany('INSERT INTO migration VALUES (?)', [(m,) for m in self.data[sid]['migrations']])
                db.commit()

    def isolated_probe(self):
        self.events.append(('isolation',))


@pytest.fixture
def setup(tmp_path, monkeypatch):
    host = Host(tmp_path)
    core = {'operationId': 'op', 'unit': 'host:i-host'}
    controller = SimpleNamespace(host=host, s3=object(),
        env={'HOST_INSTANCE_ID': 'i-host', 'ENVIRONMENT': 'dev', 'BACKUP_BUCKET': 'bucket',
             'DATA_KEY_ARN': 'kms', 'AWS_ACCOUNT_ID': '123456789012', 'AWS_REGION': 'us-east-2'},
        core=SimpleNamespace(get_item=lambda **kw: {'Item': deepcopy(core)}))
    source_terms = deepcopy(host.value['tenants'][0]['terms'])
    def subscription(sid):
        return {'subscriptionId': sid, 'customerId': 'customer-' + sid, 'currentVersion': '0.2.4',
                'activeOperationId': 'op' if sid == 'a' else None, 'billingRevision': 1,
                'entitlementContext': {'terms': source_terms}, 'desiredEntitlement': 'ACTIVE'}
    controller.subscription = subscription
    controller.fence = lambda req: subscription(req['subscription']['id'])
    images = {name: image(name, 'b' if name in {'app', 'collector', 'admin'} else 'a')
              for name in ('app', 'collector', 'admin', 'clickhouse', 'keeper')}
    request = {'subscription': {'id': 'a'}, 'operation': {'id': 'op', 'type': 'UPDATE', 'fromVersion': '0.2.4'},
        'release': {'version': '0.3.0', 'imageUris': images}, 'context': {'maintenanceReview': {
            'unit': 'host:i-host', 'pilot': 'a', 'procedure': {'adapter': 'signoz-host-v2', 'unitKind': 'host',
                'storageCompatibility': 'verified-restore', 'components': ['controller'],
                'compatibleApplicationVersions': ['0.2.4', '0.3.0'], 'compatibilityEvidence': 'reviewed-tests'},
            'members': [{'subscriptionId': sid, 'customerId': 'customer-' + sid, 'currentVersion': '0.2.4'} for sid in ['a', 'b']]}}}
    snapshots, backups = {}, []
    def backup(child, *, final):
        assert final and child['release']['version'] == '0.2.4'
        sid = child['subscription']['id']
        snapshots[sid] = deepcopy(host.data[sid])
        backups.append(sid)
        return {'verified': True, 'manifestVersionId': 'version-' + sid, 'productVersion': '0.2.4', 'backupId': 'backup-' + sid,
                'runtimeManifest': {'sealed': True}, 'manifestKey': 'recovery/' + sid, 'capturedAt': '2026-10-16T00:00:00Z'}
    controller.backup = backup
    monkeypatch.setattr(u.maintenance, 'command', lambda *a, **kw: images['admin'])
    monkeypatch.setattr(u.cloud_backup, 'download', lambda *a, **kw: tmp_path / 'backups' / kw['subscription_id'])
    def restore(host, sid, directory, **kwargs):
        assert kwargs == {'resume': False, 'preserve_current': False}
        host.data[sid] = deepcopy(snapshots[sid])
        host.events.append(('restore', sid))
        return {'restored': True}
    monkeypatch.setattr(u, 'restore', restore)
    monkeypatch.setattr(u, 'confined_remove', lambda *a: None)
    def reconcile(host, sid, revision, terms):
        assert terms == source_terms and revision == 1  # no invented paid periods
        host.events.append(('reconcile', sid))
    monkeypatch.setattr(u.lifecycle, 'reconcile', reconcile)
    monkeypatch.setattr(u.lifecycle, 'set_state', lambda host, sid, state: host.events.append(('ingress', sid, state)))
    return controller, request, backups, core


def test_migration_keeps_ingress_closed_until_finalize_and_preserves_neighbour(setup):
    controller, request, backups, _ = setup
    neighbour = deepcopy(controller.host.data['b'])
    u.prepare(controller, request)
    u.prepare(controller, request)
    assert backups == ['a', 'b']
    first = u.verify(controller, request)
    assert u.verify(controller, request) == first
    assert first['sqliteMigrations'] == ['133', '134', '135']
    assert controller.host.data['b'] == neighbour
    assert not any(e[0] == 'ingress' for e in controller.host.events)
    u.finalize(controller, request)
    assert [e for e in controller.host.events if e[0] == 'ingress'] == [('ingress', 'b', 'ACTIVE'), ('ingress', 'a', 'ACTIVE')]


def test_failed_migration_restores_source_data_and_images_and_resumes_idempotently(setup):
    controller, request, _, _ = setup
    original = deepcopy(controller.host.data)
    u.prepare(controller, request)
    controller.host.fail_migration = True
    with pytest.raises(RuntimeFailure, match='failed migration'):
        u.verify(controller, request)
    assert controller.host.data['a']['pricing'] is None
    result = u.compensate(controller, request)
    assert result['restored'] and result['sourceVersion'] == '0.2.4'
    assert controller.host.data == original
    tenant = controller.host.state['tenants'][0]
    assert tenant['appImage'] == image('app') and tenant['productVersion'] == '0.2.4'
    assert u.compensate(controller, request) == result
    assert controller.host.events.count(('restore', 'a')) == 1
    with pytest.raises(RuntimeFailure, match='requires recovery'):
        u.verify(controller, request)


def test_interrupted_recovery_remains_stopped_and_resumes_same_source(setup, monkeypatch):
    controller, request, _, _ = setup
    u.prepare(controller, request)
    controller.host.fail_migration = True
    with pytest.raises(RuntimeFailure):
        u.verify(controller, request)
    restore = u.restore
    monkeypatch.setattr(u, 'restore', lambda *a, **kw: (_ for _ in ()).throw(RuntimeFailure('interrupted restore')))
    with pytest.raises(RuntimeFailure, match='interrupted restore'):
        u.compensate(controller, request)
    assert not any(e[0] == 'ingress' for e in controller.host.events)
    assert u.checkpoint(controller, request)[1]['stage'] == 'restoring'
    monkeypatch.setattr(u, 'restore', restore)
    assert u.compensate(controller, request)['restored']


@pytest.mark.parametrize('change', ['gate', 'storage', 'source', 'receipt'])
def test_unsafe_migration_is_rejected_before_target_data_changes(setup, change):
    controller, request, _, core = setup
    original = deepcopy(controller.host.data)
    if change == 'gate':
        core['operationId'] = 'another'
    elif change == 'storage':
        request['release']['imageUris']['clickhouse'] = image('clickhouse', 'c')
    elif change == 'source':
        controller.host.value['tenants'][0]['productVersion'] = '0.2.3'
    else:
        controller.backup = lambda *a, **kw: {'verified': True, 'manifestVersionId': 'v', 'productVersion': '0.3.0'}
    with pytest.raises(RuntimeFailure):
        u.prepare(controller, request)
    assert controller.host.data == original
    assert not any(e[0] in {'private-up', 'ingress'} for e in controller.host.events)


def test_changed_request_cannot_reuse_migration_checkpoint(setup):
    controller, request, _, _ = setup
    u.prepare(controller, request)
    request['release']['imageUris']['app'] = image('app', 'c')
    with pytest.raises(RuntimeFailure, match='checkpoint'):
        u.verify(controller, request)


def test_compensation_after_ingress_reopens_preserves_new_data_for_review(setup):
    controller, request, _, _ = setup
    u.prepare(controller, request)
    u.verify(controller, request)
    u.finalize(controller, request)
    controller.host.data['a']['usage'] = 200
    with pytest.raises(RuntimeFailure, match='preserve new data'):
        u.compensate(controller, request)
    assert controller.host.data['a']['usage'] == 200
    assert not any(e[0] == 'restore' for e in controller.host.events)


def test_missing_sqlite_migration_never_reopens_ingress(setup):
    controller, request, _, _ = setup
    u.prepare(controller, request)
    controller.host.up = lambda *a, **kw: None
    with pytest.raises(RuntimeFailure, match='133-135'):
        u.verify(controller, request)
    assert u.checkpoint(controller, request)[1]['stage'] == 'recovery-required'
    assert not any(e[0] == 'ingress' for e in controller.host.events)


def follower_request(controller, request, core):
    u.prepare(controller, request)
    result = u.verify(controller, request)
    u.finalize(controller, request)
    follower = deepcopy(request)
    follower['subscription']['id'] = 'b'
    follower['operation']['id'] = 'follower-op'
    follower['context'] = {'maintenanceAcceptance': {'operationId': 'op', 'subscriptionId': 'a',
        'review': request['context']['maintenanceReview'], 'result': result, 'targetVersion': '0.3.0',
        'releaseRevision': 'revision', 'releaseContentSha256': 'content-sha'}}
    old_subscription = controller.subscription
    controller.subscription = lambda sid: {**old_subscription(sid), 'activeOperationId': 'follower-op' if sid == 'b' else None,
                                           'currentVersion': '0.3.0' if sid == 'a' else '0.2.4'}
    controller.fence = lambda req: controller.subscription(req['subscription']['id'])
    core.clear()
    return follower


def test_follower_uses_accepted_host_but_backs_up_only_its_own_source(setup):
    controller, request, backups, core = setup
    follower = follower_request(controller, request, core)
    pilot = deepcopy(controller.host.data['a'])
    u.prepare(controller, follower)
    assert backups == ['a', 'b', 'b']
    u.verify(controller, follower)
    u.finalize(controller, follower)
    assert controller.host.data['a'] == pilot
    assert controller.host.state['tenants'][1]['productVersion'] == '0.3.0'


@pytest.mark.parametrize('change', ['gate', 'controller', 'members', 'target', 'app'])
def test_follower_requires_current_host_and_accepted_controller(setup, change):
    controller, request, _, core = setup
    follower = follower_request(controller, request, core)
    acceptance = follower['context']['maintenanceAcceptance']
    if change == 'gate':
        core.update(operationId='other', unit='host:i-host')
    elif change == 'controller':
        acceptance['result']['controllerImage'] = image('admin', 'c')
    elif change == 'members':
        controller.host.value['tenants'].pop(0)
    elif change == 'app':
        follower['release']['imageUris']['app'] = image('app', 'c')
    else:
        acceptance['targetVersion'] = '0.4.0'
    data = deepcopy(controller.host.data['b'])
    with pytest.raises(RuntimeFailure):
        u.prepare(controller, follower)
    assert controller.host.data['b'] == data


def test_controller_replacement_uses_the_32_character_operation_token(setup, monkeypatch):
    controller, request, _, _ = setup
    u.prepare(controller, request)
    def replacing(controller, request, path, reviewed):
        assert len(path.stem) == 32
        return False
    monkeypatch.setattr(u.maintenance, 'ensure_controller', replacing)
    assert u.verify(controller, request) is None
    assert u.checkpoint(controller, request)[1]['stage'] == 'backed-up'


def test_suspended_neighbour_stays_suspended_after_pilot_finalization(setup):
    controller, request, _, _ = setup
    controller.host.value['tenants'][1]['state'] = 'SUSPENDED'
    u.prepare(controller, request)
    u.verify(controller, request)
    u.finalize(controller, request)
    assert ('ingress', 'b', 'SUSPENDED') in controller.host.events


def test_managed_backup_without_installed_version_never_uses_controller_version(tmp_path):
    from abera.runtime.backup import capture
    host = Host(tmp_path)
    del host.value['tenants'][0]['productVersion']
    with pytest.raises(RuntimeFailure, match='installed tenant product version'):
        capture(host, 'a', 'backup-source')
    assert not host.events
