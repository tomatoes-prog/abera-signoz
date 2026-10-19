"""Controller-only releases stage their code without copying unused app contexts."""
import json

import pytest

from abera.tools import export_automation as exporter


def test_controller_bridge_excludes_unused_frontend_and_private_runtime(tmp_path, monkeypatch):
    source = tmp_path / 'source'
    paths = {'abera/runtime/daemon.py': '# controller', 'frontend/app.ts': '// frontend',
             'abera/.runtime/credentials.json': 'private', '.env': 'private',
             'abera/docker/Dockerfile.controller': 'FROM python:3.13',
             'abera/docker/Dockerfile.community.dockerignore': '**/.runtime'}
    for name, content in paths.items():
        path = source / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
    product = tmp_path / 'automation/products/abera-signoz'
    product.mkdir(parents=True)
    (product / 'product.yaml').write_text('productId: abera-signoz')
    monkeypatch.setattr(exporter, 'ROOT', source)
    monkeypatch.setattr(exporter.subprocess, 'check_output', lambda *a, **kw: '\0'.join(paths).encode())
    exporter.export(product.parents[1], ['admin'])
    contexts = product / 'image-build'
    assert (contexts / 'admin/abera/runtime/daemon.py').is_file()
    assert not (contexts / 'app').exists() and not (contexts / 'admin/frontend').exists()
    assert not (contexts / 'admin/.env').exists() and not (contexts / 'admin/abera/.runtime').exists()
    assert json.loads((contexts / 'source.json').read_text())['files'] == 4
    with pytest.raises(ValueError, match='unsupported'):
        exporter.export(product.parents[1], ['../outside'])
