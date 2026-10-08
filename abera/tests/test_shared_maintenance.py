from copy import deepcopy
from types import SimpleNamespace
from pathlib import Path
import pytest
from abera.runtime import maintenance as m
from abera.runtime.host import RuntimeFailure

class Host:
    def __init__(self, root):
        self.root = root
        self.value = {'clickhouseImage': 'old@sha256:'+'a'*64, 'keeperImage': 'old@sha256:'+'a'*64,
            'tenants': [{'subscriptionId': sid, 'customerId': 'customer-'+sid, 'state': 'ACTIVE', 'slot': slot}
                        for slot,sid in enumerate(['sub-a','sub-b'],1)]}
        self.actions = []
    @property
    def state(self): return deepcopy(self.value)
    def save(self, state): self.value = deepcopy(state)
    def render(self): self.actions.append('render')
    def compose(self, *args):
        self.actions.append(args)
        if args[0] == 'cp': (Path(args[2])/'cold-data').write_bytes(b'preserved shared data')
    def isolated_probe(self): self.actions.append('isolation'); return {'checks': 2}

class S3:
    def __init__(self): self.objects = {}
    def upload_file(self,path,bucket,key,ExtraArgs): self.objects[key] = {'VersionId':'exact', 'Metadata':ExtraArgs['Metadata']}
    def head_object(self, Bucket, Key): return self.objects[Key]

@pytest.fixture
def setup(tmp_path, monkeypatch):
    images = {name: 'repo/'+name+'@sha256:'+'b'*64 for name in ('app','collector','clickhouse','keeper','admin')}
    host=Host(tmp_path); backups=[]
    core=SimpleNamespace(get_item=lambda **kw: {'Item': {'operationId':'operation','unit':'host:i-host'}})
    controller=SimpleNamespace(host=host, core=core, s3=S3(), env={'HOST_INSTANCE_ID':'i-host', 'BACKUP_BUCKET':'bucket', 'DATA_KEY_ARN':'kms'},
        subscription=lambda sid: {'customerId':'customer-'+sid, 'currentVersion':'0.1.0', 'activeOperationId':'operation' if sid=='sub-a' else None})
    def backup(request, final):
        assert final
        backups.append(request['subscription']['id'])
        return {'verified':True, 'manifestVersionId':'version-'+request['subscription']['id']}
    controller.backup=backup
    request={'subscription':{'id':'sub-a'}, 'operation':{'id':'operation'}, 'release':{'version':'0.2.0','imageUris':images},
        'context':{'maintenanceReview':{'unit':'host:i-host','pilot':'sub-a', 'procedure':{'unitKind':'host','adapter':'signoz-host-v1',
            'storageCompatibility':'no-schema-change','compatibilityEvidence':'reviewed-suite',
            'components':['controller','clickhouse','keeper'], 'compatibleApplicationVersions':['0.1.0']},
            'members':[{'subscriptionId':sid,'customerId':'customer-'+sid,'currentVersion':'0.1.0'} for sid in ['sub-a','sub-b']]}}}
    monkeypatch.setattr(m, 'stop', lambda host,tenant: host.actions.append(('stop',tenant['subscriptionId'])))
    monkeypatch.setattr(m, 'start', lambda host,tenant: host.actions.append(('start',tenant['subscriptionId'])))
    monkeypatch.setattr(m, 'command', lambda *a,**kw: images['admin'])
    monkeypatch.setattr(m, 'api', lambda *a,**kw: {'status':'ok'})
    return controller, request, backups


def test_shared_backup_then_update_preserves_neighbours_and_retries(setup):
    controller,request,backups=setup
    before=controller.host.state['tenants']
    m.prepare(controller,request); m.prepare(controller,request)
    assert backups == ['sub-a','sub-b']
    assert controller.host.state['clickhouseImage'].startswith('old')
    assert len(controller.s3.objects) == 2
    result=m.verify(controller,request)
    assert result['verified'] and result['members'] == ['sub-a','sub-b']
    assert controller.host.state['tenants'] == before
    assert controller.host.state['clickhouseImage'] == request['release']['imageUris']['clickhouse']
    assert 'isolation' in controller.host.actions

@pytest.mark.parametrize('problem',['inventory','owner','schema','neighbour_version','digest'])
def test_unknown_shared_state_blocks_before_backup_or_image_change(setup, problem):
    controller,request,backups=setup
    if problem=='inventory': request['context']['maintenanceReview']['members'].pop()
    elif problem=='owner': request['operation']['id']='other'
    elif problem=='schema': request['context']['maintenanceReview']['procedure']['storageCompatibility']='incompatible'
    elif problem=='neighbour_version': request['context']['maintenanceReview']['members'][1]['currentVersion']='0.9.0'
    else: request['release']['imageUris']['keeper']='latest'
    before=controller.host.state
    with pytest.raises(RuntimeFailure): m.prepare(controller,request)
    assert not backups and controller.host.state == before and not controller.host.actions


def test_unverified_backup_never_changes_storage(setup):
    controller,request,backups=setup
    controller.backup=lambda *a,**kw:{'verified':False}
    with pytest.raises(RuntimeFailure,match='verified'): m.prepare(controller,request)
    assert controller.host.state['clickhouseImage'].startswith('old')
    assert not controller.s3.objects


def test_storage_update_requires_completed_backups(setup):
    controller,request,_=setup
    with pytest.raises(RuntimeFailure,match='backups'): m.verify(controller,request)
    assert controller.host.state['clickhouseImage'].startswith('old')
