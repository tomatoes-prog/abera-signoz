from copy import deepcopy
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from abera.runtime.host import RuntimeFailure
from abera.runtime.runtime_power import RuntimePower


def fixture(tmp_path):
    group = 'a' * 20
    image = 'image@sha256:' + 'a' * 64
    project = 'abera-signoz-' + group
    host = SimpleNamespace(root=tmp_path, state={'hostGroupId': group, 'project': project, 'local': False,
        'tenants': [{'subscriptionId': 'tenant-one', 'state': 'ACTIVE'},
                    {'subscriptionId': 'tenant-two', 'state': 'SUSPENDED'}]})
    services = {s: {'image': image} for s in ['clickhouse', 'keeper', 'app-one', 'app-two']}
    (tmp_path / 'compose.json').write_text(json.dumps({'services': services}))
    containers = [{'id': digit * 64, 'kind': 'service', 'service': service, 'image': image,
                   'project': project, 'running': service != 'app-two', 'privateEnvironment': 'must-not-persist'}
                  for digit, service in zip('1234', services)]
    containers.append({'id': '5' * 64, 'kind': 'controller', 'name': 'abera-controller',
                       'groupId': group, 'image': image, 'running': True})
    docker = Mock()
    docker.inventory.side_effect = lambda: deepcopy(containers)
    actions = []
    def change(ids, running, **kwargs):
        actions.append(('start' if running else 'stop', ids))
        for c in containers:
            if c['id'] in ids:
                c['running'] = running
        return True
    docker.stop_wait.side_effect = lambda ids, **kw: change(ids, False, **kw)
    docker.start_wait.side_effect = lambda ids, **kw: change(ids, True, **kw)
    core = Mock()
    core.get_item.return_value = {'Item': {'operationId': 'runtime-one', 'unit': 'runtime:abera-signoz'}}
    jobs = Mock()
    jobs.get_item.return_value = {'Item': {}}
    clock = [100]
    env = {'ENVIRONMENT': 'dev', 'HOST_GROUP_ID': group, 'HOST_INSTANCE_ID': 'i-owned', 'ADMIN_IMAGE': image}
    power = RuntimePower(host, docker, core, jobs, env, lambda: clock[0])
    members = ['tenant-one', 'tenant-two']
    return SimpleNamespace(**locals())


def test_hour_limit_cancels_then_resume_restores_only_original_running_set(tmp_path):
    f = fixture(tmp_path)
    before = deepcopy(f.host.state)
    assert f.power.stop('runtime-one', f.members) is False
    assert f.actions == []
    f.clock[0] = 3700
    assert f.power.stop('runtime-one', f.members) is True
    assert [a[1] for a in f.actions] == [['5' * 64], ['3' * 64], ['1' * 64], ['2' * 64]]
    receipt = (tmp_path / 'runtime-power.json').read_text()
    assert 'must-not-persist' not in receipt
    assert f.power.resume('runtime-one', f.members) is True
    assert [a[1] for a in f.actions[4:]] == [['2' * 64], ['1' * 64], ['3' * 64], ['5' * 64]]
    assert f.containers[3]['running'] is False
    assert f.host.state == before
    assert f.power.resume('runtime-one', f.members) is True


def test_pause_ack_from_previous_controller_does_not_bypass_wait(tmp_path):
    f = fixture(tmp_path)
    f.jobs.get_item.return_value = {'Item': {'runtimePauseOwner': 'runtime-one', 'owner': 'new-controller',
        'runtimePauseControllerOwner': 'old-controller', 'runtimePauseConfirmedAt': 101,
        'runtimePauseMembersDigest': hashlib.sha256(json.dumps(f.members, separators=(',', ':')).encode()).hexdigest(),
        'instanceId': 'i-owned', 'expiresAt': 1000}}
    assert f.power.stop('runtime-one', f.members) is False
    assert f.actions == []


@pytest.mark.parametrize('drift', ['prod', 'owner', 'member', 'container', 'image'])
def test_unknown_scope_blocks_all_stop_operations(tmp_path, drift):
    f = fixture(tmp_path)
    f.clock[0] = 3700
    # Anchor the requested hour before the deadline rather than renewing it.
    f.clock[0] = 100
    f.power.request_pause('runtime-one', f.members)
    f.clock[0] = 3700
    if drift == 'prod':
        f.env['ENVIRONMENT'] = 'prod'
    elif drift == 'owner':
        f.core.get_item.return_value['Item']['operationId'] = 'foreign'
    elif drift == 'member':
        f.members = ['tenant-one']
    elif drift == 'container':
        f.containers[0]['project'] = 'foreign-project'
    else:
        f.containers[0]['image'] = 'foreign-image'
    with pytest.raises(RuntimeFailure):
        f.power.stop('runtime-one', f.members)
    assert f.actions == []


def test_stop_is_not_complete_until_native_exit_is_observed(tmp_path):
    f = fixture(tmp_path)
    f.power.request_pause('runtime-one', f.members)
    f.clock[0] = 3700
    f.docker.stop_wait.side_effect = None
    f.docker.stop_wait.return_value = True
    assert f.power.stop('runtime-one', f.members) is False
    f.clock[0] = 4300
    with pytest.raises(RuntimeFailure, match='deadline'):
        f.power.stop('runtime-one', f.members)


def test_changed_saved_container_cannot_be_adopted_during_resume(tmp_path):
    f = fixture(tmp_path)
    f.power.request_pause('runtime-one', f.members)
    f.clock[0] = 3700
    assert f.power.stop('runtime-one', f.members) is True
    f.containers[2]['id'] = '6' * 64
    with pytest.raises(RuntimeFailure):
        f.power.resume('runtime-one', f.members)
    f.docker.start_wait.assert_not_called()


def completed_cycle(f):
    f.power.request_pause('runtime-one', f.members)
    f.clock[0] = 3700
    assert f.power.stop('runtime-one', f.members) is True
    assert f.power.resume('runtime-one', f.members) is True
    f.core.get_item.return_value['Item']['operationId'] = 'runtime-two'
    f.clock[0] = 4000


def test_second_cycle_has_new_deadline_and_preserves_suspended_consumer(tmp_path):
    f = fixture(tmp_path)
    completed_cycle(f)
    assert f.power.stop('runtime-two', f.members) is False
    marker = json.loads((tmp_path / 'runtime-pause.json').read_text())
    assert marker['requestedAt'] == 4000 and marker['deadlineAt'] == 7600
    assert json.loads((tmp_path / 'runtime-power-completed.json').read_text())['owner'] == 'runtime-one'
    f.clock[0] = 7600
    assert f.power.stop('runtime-two', f.members) is True
    assert f.power.resume('runtime-two', f.members) is True
    assert f.containers[3]['running'] is False
    assert f.host.state['tenants'][1]['state'] == 'SUSPENDED'


@pytest.mark.parametrize('drift', ['stopped', 'image', 'extra-running'])
def test_new_cycle_cannot_discard_unrestored_previous_receipt(tmp_path, drift):
    f = fixture(tmp_path)
    completed_cycle(f)
    if drift == 'stopped':
        f.containers[2]['running'] = False
    elif drift == 'image':
        f.containers[2]['image'] = 'foreign'
    else:
        f.containers[3]['running'] = True
    previous = (tmp_path / 'runtime-power.json').read_text()
    action_count = len(f.actions)
    with pytest.raises(RuntimeFailure):
        f.power.request_pause('runtime-two', f.members)
    assert (tmp_path / 'runtime-power.json').read_text() == previous
    assert not (tmp_path / 'runtime-pause.json').exists()
    assert len(f.actions) == action_count


def test_completed_owner_cannot_replay_stop_and_renew_hour(tmp_path):
    f = fixture(tmp_path)
    completed_cycle(f)
    f.core.get_item.return_value['Item']['operationId'] = 'runtime-one'
    with pytest.raises(RuntimeFailure, match='replayed'):
        f.power.stop('runtime-one', f.members)
    assert not (tmp_path / 'runtime-pause.json').exists()


def test_partial_cooperative_stop_continues_after_controller_heartbeat_expires(tmp_path):
    f = fixture(tmp_path)
    f.power.request_pause('runtime-one', f.members)
    f.jobs.get_item.return_value = {'Item': {'runtimePauseOwner': 'runtime-one', 'owner': 'controller',
        'runtimePauseControllerOwner': 'controller', 'runtimePauseConfirmedAt': 100,
        'runtimePauseMembersDigest': hashlib.sha256(json.dumps(f.members, separators=(',', ':')).encode()).hexdigest(),
        'instanceId': 'i-owned', 'expiresAt': 190}}
    change = f.docker.stop_wait.side_effect
    f.docker.stop_wait.side_effect = lambda ids, **kw: False if '3' * 64 in ids else change(ids, **kw)
    assert f.power.stop('runtime-one', f.members) is False
    assert f.containers[-1]['running'] is False
    f.clock[0] = 200
    f.jobs.get_item.return_value = {'Item': {}}
    f.docker.stop_wait.side_effect = change
    assert f.power.stop('runtime-one', f.members) is True
    assert f.clock[0] < 3700
