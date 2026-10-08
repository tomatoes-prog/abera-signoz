from copy import deepcopy
from pathlib import Path
import pytest
from abera.runtime import controller_update as c
from abera.runtime.host import RuntimeFailure


@pytest.mark.parametrize('failure', ['stop', 'rename', 'create', 'start', None])
def test_replacement_resumes_without_losing_controller_or_source(tmp_path, monkeypatch, failure):
    image = 'ecr/admin@sha256:' + 'b'*64
    token = 'a'*32
    old = {'Id': 'old-id', 'Config': {'Image': 'old-image'}, 'HostConfig': {
        'Binds': ['/var/run/docker.sock:/var/run/docker.sock', '/opt/abera:/opt/abera'],
        'Memory': 123, 'LogConfig': {'Type': 'awslogs', 'Config': {'awslogs-group': 'existing'}}}}
    containers = {'abera-controller': old}
    commands = []
    failed = False
    (tmp_path/'release').mkdir()
    (tmp_path/'release'/'original').write_text('old source')
    def command(args):
        nonlocal failed
        commands.append(args)
        verb = args[1]
        name = args[args.index('--name')+1] if '--name' in args else args[-1]
        # Fail the real replacement once; source extraction is independent.
        if failure == verb and not failed and (args[-2] if verb == 'rename' else name) == 'abera-controller':
            failed = True
            raise RuntimeFailure('interrupted')
        if verb == 'create': containers[name] = {**deepcopy(old), 'Id': 'target-id', 'Config': {'Image': args[-1]}}
        elif verb == 'rm': containers.pop(name)
        elif verb == 'rename': containers[args[-1]] = containers.pop(args[-2])
        elif verb == 'cp': (Path(args[-1])/'new').write_text('reviewed target source')
        return ''
    monkeypatch.setattr(c, 'inspect', lambda name: deepcopy(containers.get(name)))
    monkeypatch.setattr(c, 'command', command)
    monkeypatch.setattr(c.time, 'sleep', lambda _:None)
    if failure:
        with pytest.raises(RuntimeFailure): c.replace(image, token, root=tmp_path)
    c.replace(image, token, root=tmp_path)
    c.replace(image, token, root=tmp_path)
    assert containers['abera-controller']['Config']['Image'] == image
    assert containers['abera-controller-before-'+token[:12]]['Id'] == 'old-id'
    assert (tmp_path/('release-before-'+token)/'original').read_text() == 'old source'
    assert (tmp_path/'release'/'new').read_text() == 'reviewed target source'
    replacements = [args for args in commands if args[1]=='create' and args[args.index('--name')+1]=='abera-controller']
    assert '--env-file' in replacements[-1] and '--log-opt' in replacements[-1]


def test_missing_controller_without_checkpoint_blocks(tmp_path, monkeypatch):
    monkeypatch.setattr(c, 'inspect', lambda _:None)
    monkeypatch.setattr(c, 'command', lambda _:pytest.fail('no mutation permitted'))
    with pytest.raises(RuntimeFailure, match='missing'):
        c.replace('ecr/admin@sha256:'+'b'*64, 'a'*32, root=tmp_path)
