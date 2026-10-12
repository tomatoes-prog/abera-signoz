import json
from types import SimpleNamespace

import pytest

from abera.runtime.docker_power import DockerPower
from abera.runtime.host import RuntimeFailure


def test_inventory_projects_native_identity_without_environment_secrets():
    cid = 'a' * 64
    native = {'Id': cid, 'Name': '/abera-controller', 'Config': {'Image': 'digest',
              'Env': ['HOST_GROUP_ID=group', 'PASSWORD=private'], 'Labels': {}}, 'State': {'Running': True}}
    calls = []
    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=cid if args[1] == 'ps' else json.dumps([native]))
    result = DockerPower(run=run).inventory()
    assert result[0]['id'] == cid and result[0]['groupId'] == 'group'
    assert 'private' not in str(result)
    assert all('shell' not in kwargs for _, kwargs in calls)


@pytest.mark.parametrize('running,result', [(True, False), (False, True)])
def test_stop_acceptance_is_not_native_exit_proof(running, result):
    cid = 'a' * 64
    calls = []
    def run(args, **kwargs):
        calls.append(args)
        data = [{'Id': cid, 'State': {'Running': running}}]
        return SimpleNamespace(returncode=0, stdout=json.dumps(data) if args[1] == 'inspect' else '')
    assert DockerPower(run=run)._wait([cid], start=False, timeout=120) is result
    assert calls[0][:3] == ['docker', 'stop', '--time']


def test_command_rejects_non_native_ids_without_shell_execution():
    def run(*args, **kwargs):
        raise AssertionError('Untrusted ID reached Docker')
    with pytest.raises(RuntimeFailure):
        DockerPower(run=run).stop_wait(['--remove-volumes'], timeout=120)
