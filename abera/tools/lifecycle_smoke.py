"""Exercise archive/removal/cold recovery on synthetic tenant 4, using local S3 fixtures."""
import io
import json
import secrets
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from abera.runtime.daemon import Controller
from abera.runtime.host import Host, api
from abera.runtime.model import ABERA, write_json


class LocalS3:
    def __init__(self): self.objects, self.metadata = {}, {}
    def put_object(self, Bucket, Key, Body, **kw):
        version = secrets.token_hex(8)
        self.objects[(Key, version)] = bytes(Body)
        self.metadata[(Key, version)] = kw.get("Metadata", {})
        return {"VersionId": version}
    def upload_file(self, Filename, Bucket, Key, **kw):
        self.put_object(Bucket=Bucket, Key=Key, Body=Path(Filename).read_bytes(), **kw.get("ExtraArgs", {}))
    def head_object(self, Bucket, Key):
        versions = [v for (k,v) in self.objects if k == Key]
        if not versions:
            from botocore.exceptions import ClientError
            raise ClientError({"Error": {"Code": "404"}}, "HeadObject")
        version = versions[-1]
        return {"VersionId": version, "ContentLength": len(self.objects[Key,version]), "Metadata": self.metadata[Key,version]}
    def get_object(self, Bucket, Key, VersionId, **kw): return {"Body": io.BytesIO(self.objects[Key,VersionId])}
    def download_file(self, Bucket, Key, Filename, ExtraArgs): Path(Filename).write_bytes(self.objects[Key,ExtraArgs["VersionId"]])
    def delete_object(self, Bucket, Key, VersionId): self.objects.pop((Key, VersionId), None)


def run():
    host = Host(ABERA / ".runtime/dev")
    state = host.state
    assert state["local"] and len(state["tenants"]) == 4
    tenant = next(t for t in state["tenants"] if t["subscriptionId"] == "development-4")
    tenant["customerId"] = "customer-development-4"
    host.save(state)
    sid = tenant["subscriptionId"]
    base = f"http://127.0.0.1:{24800+tenant['slot']}"
    host.observe()
    before = api(base, "GET", "/abera/usage", token=tenant["otlpToken"])["usage"]
    controller = Controller.__new__(Controller)
    controller.host, controller.s3 = host, LocalS3()
    controller.env = {"ENVIRONMENT": "dev", "AWS_ACCOUNT_ID": "000000000000", "AWS_REGION": "us-east-2", "BACKUP_BUCKET": "local-fixture", "DATA_KEY_ARN": "local-fixture", "HOST_INSTANCE_ID": "i-local-fixture"}
    sub = {"subscriptionId": sid, "productId": "abera-signoz", "customerId": tenant["customerId"], "capacitySlot": 4,
           "billingRevision": tenant["revision"], "entitlementContext": {"terms": tenant["terms"]}}
    controller.fence = lambda request: sub
    request = {"environment": "dev", "subscription": {"id": sid, "customerId": tenant["customerId"], "configuration": {"Plan": "lite", "AdminEmail": tenant["adminEmail"]}},
               "operation": {"type": "ARCHIVE", "id": "local-archive-"+secrets.token_hex(8)}, "hook": "prepare",
               "release": {"version": "0.1.0"}, "context": {"dataGeneration": 1, "archiveCycleId": "local-cycle", "backupSequence": 1}}
    receipt = controller.execute(request)["metadata"]["archiveBackup"]
    assert receipt["verified"] and receipt["runtimeManifest"]["sealed"]
    sub["archiveDeletionCommittedAt"] = "2026-10-03T00:00:00Z"
    controller.execute({**request, "hook": "finalize"})
    assert len(host.state["tenants"]) == 3
    request.update(operation={"type": "RECOVER", "id": "local-recover-"+secrets.token_hex(8)}, hook="prepare",
                   context={"dataGeneration": 2, "backup": receipt})
    controller.execute(request)
    controller.execute({**request, "hook": "verify"})
    after = api(base, "GET", "/abera/usage", token=tenant["otlpToken"])["usage"]
    assert len(host.state["tenants"]) == 4
    assert before["cycleId"] == after["cycleId"]
    assert before["logTraceBytes"] == after["logTraceBytes"] and before["metricSamples"] == after["metricSamples"]
    report = {"result": "PASS", "archivedTables": receipt["runtimeManifest"]["restoredTables"], "runtimeRemovedBeforeRecovery": True,
              "usagePreserved": True, "customersAfterRecovery": 4, "s3": "local fixture; AWS integration still required"}
    write_json(ABERA / "results/lifecycle.json", report)
    return report


if __name__ == "__main__": print(json.dumps(run()))
