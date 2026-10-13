"""A controller can operate only its assigned host and verify retirement safely."""
from copy import deepcopy
from types import SimpleNamespace

import pytest

from abera.runtime import daemon
from abera.runtime.daemon import Controller
from abera.runtime.host import RuntimeFailure
from abera.runtime.model import write_json

GROUP = "a" * 20
RETIREMENT = "b" * 64


class Table:
    def __init__(self, items): self.items, self.reads = items, []
    def get_item(self, Key, ConsistentRead):
        assert ConsistentRead
        self.reads.append(Key['pk'])
        return {"Item": deepcopy(self.items[Key['pk']])} if Key['pk'] in self.items else {}


def controller(tmp_path):
    value = Controller.__new__(Controller)
    value.group_id = GROUP
    value.owner = "i-owned-controller"
    value.env = {"HOST_INSTANCE_ID": "i-owned"}
    value.host = SimpleNamespace(root=tmp_path, state={"project": "abera-signoz-" + GROUP, "tenants": []},
        sql=lambda query: "default\nsystem\ninformation_schema\nINFORMATION_SCHEMA" if query.startswith("SHOW") else "0")
    value.jobs = Table({"GROUP#" + GROUP: {"status": "RETIRING", "retirementId": RETIREMENT, "slots": {}, "assignedCount": 0}})
    value.heartbeat = lambda: setattr(value, "lease", True)
    return value


def test_subscription_requires_same_customer_group_and_slot(tmp_path):
    value = controller(tmp_path)
    value.core = Table({"SUB#tenant-123": {"productId": "abera-signoz", "customerId": "customer-123"}})
    value.jobs.items["GROUP#" + GROUP].update(status="READY", slots={"2": "tenant-123"}, assignedCount=1)
    value.jobs.items["SUB#tenant-123"] = {"groupId": GROUP, "slot": 2, "customerId": "customer-123", "allocationOperationId": "create-123"}
    assert value.subscription("tenant-123")["capacitySlot"] == 2
    for field, other in [("customerId", "foreign-customer"), ("groupId", "c" * 20), ("slot", 3)]:
        assigned = value.jobs.items["SUB#tenant-123"]
        previous = assigned[field]; assigned[field] = other
        with pytest.raises(RuntimeFailure, match="another host"):
            value.subscription("tenant-123")
        assigned[field] = previous


def test_empty_proof_requires_matching_retirement_no_databases_and_no_application_volumes(tmp_path, monkeypatch):
    value = controller(tmp_path)
    request = {"retirementId": RETIREMENT}
    project = value.host.state["project"]
    volumes = SimpleNamespace(returncode=0, stdout=project + "_clickhouse\n" + project + "_keeper\n")
    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kw: volumes)
    proof = value.assert_empty_host(request)["metadata"]["emptyHostProof"]
    assert proof["groupId"] == GROUP and proof["instanceId"] == "i-owned" and proof["verified"]
    with pytest.raises(RuntimeFailure, match="does not own"):
        value.assert_empty_host({"retirementId": "another-retirement"})
    original = value.host.sql
    value.host.sql = lambda query: "default\nsystem\nabera_unknown_logs" if query.startswith("SHOW") else "0"
    with pytest.raises(RuntimeFailure, match="unknown databases"):
        value.assert_empty_host(request)
    value.host.sql = original
    volumes.stdout += project + "_app-1\n"
    with pytest.raises(RuntimeFailure, match="unknown Docker volumes"):
        value.assert_empty_host(request)


def test_local_backup_must_have_external_expiry_journal_before_host_retirement(tmp_path, monkeypatch):
    value = controller(tmp_path)
    project = value.host.state["project"]
    monkeypatch.setattr(daemon.subprocess, "run", lambda *args, **kw: SimpleNamespace(returncode=0, stdout=project + "_clickhouse\n" + project + "_keeper\n"))
    receipt = {"backupId": "backup-example", "capturedAt": "2026-01-01T00:00:00Z"}
    write_json(tmp_path / "backup-expiry/backup-example.json", {"receipt": receipt, "expiresAt": "2026-01-31T00:00:00Z"})
    with pytest.raises(RuntimeFailure, match="not persisted"):
        value.assert_empty_host({"retirementId": RETIREMENT})
    value.jobs.items["BACKUP#backup-example"] = {"receipt": receipt}
    assert value.assert_empty_host({"retirementId": RETIREMENT})["status"] == "DONE"
    (tmp_path / "backups/partial-restore").mkdir(parents=True)
    with pytest.raises(RuntimeFailure, match="uncommitted"):
        value.assert_empty_host({"retirementId": RETIREMENT})


def test_controller_queries_only_its_queue_and_rejects_a_foreign_row_before_writing(tmp_path):
    value = controller(tmp_path)
    value.lease = True
    writes = []
    def query(**kw):
        assert kw["ExpressionAttributeValues"][":ready"] == "READY#" + GROUP
        return {"Items": [{"pk": "JOB#foreign", "groupId": "c" * 20, "request": {}}]}
    value.jobs.query = query
    value.jobs.update_item = lambda **kw: writes.append(kw)
    value.execute = lambda request: pytest.fail("foreign job was executed")
    with pytest.raises(RuntimeFailure, match="another host"):
        value.work_once()
    assert not writes
