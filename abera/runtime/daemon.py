"""Single-host DEV controller: paid-term reconciliation, backups and driver jobs.

Runs as a privileged *operator* container, never in a customer network. AWS
credentials come from IMDSv2 via host networking. No credentials enter jobs.
"""
from __future__ import annotations

import json
import base64
import os
import secrets
import subprocess
import threading
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from pathlib import Path

from . import cloud_backup, lifecycle
from .backup import capture, confined_remove, restore, stop, tenant_for, verify_files
from .host import Host, RuntimeFailure, api
from .model import ABERA, read_json, write_json


def plain(value):
    return json.loads(json.dumps(value, default=lambda x: (int(x) if x == x.to_integral_value() else float(x)) if isinstance(x, Decimal) else str(x)))


def dynamo(value):
    """The DynamoDB resource serializer rejects Python floats."""
    return json.loads(json.dumps(value), parse_float=Decimal)


def iso(seconds=0):
    return (datetime.now(timezone.utc) + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ")


class Controller:
    def __init__(self, host, session, env):
        self.host, self.env = host, env
        if env["ENVIRONMENT"] != "dev":
            raise RuntimeFailure("production is gated for this release")
        self.jobs = session.resource("dynamodb").Table(env["JOB_TABLE"])
        self.core = session.resource("dynamodb").Table(env["CORE_TABLE"])
        self.s3, self.secrets = session.client("s3"), session.client("secretsmanager")
        self.ssm = session.client("ssm")
        self.ecr = session.client("ecr")
        self.registry_refreshed = 0
        self.owner = env["HOST_INSTANCE_ID"] + "-" + secrets.token_hex(8)
        self.lease = False

    def refresh_registry(self):
        if time.time() - self.registry_refreshed < 8*3600:
            return
        for entry in self.ecr.get_authorization_token()["authorizationData"]:
            username, password = base64.b64decode(entry["authorizationToken"]).decode().split(":", 1)
            login = subprocess.run(["docker", "login", "--username", username, "--password-stdin", entry["proxyEndpoint"]],
                                   input=password, text=True, capture_output=True, timeout=30)
            if login.returncode:
                raise RuntimeFailure("container registry authentication failed")
        self.registry_refreshed = time.time()

    def subscription(self, sid):
        item = self.core.get_item(Key={"pk": "SUB#" + sid, "sk": "META"}, ConsistentRead=True).get("Item")
        if not item or item["productId"] != "abera-signoz":
            raise RuntimeFailure("subscription identity is unavailable")
        return plain(item)

    def heartbeat(self):
        now = int(time.time())
        try:
            self.jobs.update_item(Key={"pk": "HOST#pilot"}, UpdateExpression="SET #o=:owner, expiresAt=:expiry",
                ConditionExpression="attribute_not_exists(pk) OR #o=:owner OR expiresAt<:now",
                ExpressionAttributeNames={"#o": "owner"}, ExpressionAttributeValues={":owner": self.owner, ":expiry": now+90, ":now": now})
            self.lease = True
            self.host.observe()
            observation = read_json(self.host.root / "observation.json")
            gate = self.core.get_item(Key={'pk': 'CAPACITY#abera-signoz#MAINTENANCE', 'sk': 'LOCK'}, ConsistentRead=True).get('Item')
            admission = not gate and self.ssm.get_parameter(Name=self.env["ADMISSION_PARAMETER"])["Parameter"]["Value"] == "true"
            self.core.put_item(Item={"pk": "CAPACITY_POOL#abera-signoz", "sk": "META", "healthyUntil": iso(75),
                "admissionEnabled": admission and observation["diskHealthy"],
                "instanceId": self.env["HOST_INSTANCE_ID"], "maxSubscriptions": 4})
        except Exception:
            self.lease = False
            # Expiring gateway health and pool readiness deny new ingestion and
            # checkouts if control or disk observations cannot be renewed.

    def fence(self, request):
        deadline = request.get("context", {}).get("deadlineAt")
        if not deadline or datetime.fromisoformat(deadline.replace("Z", "+00:00")) <= datetime.now(timezone.utc):
            raise RuntimeFailure("operation deadline is missing or elapsed")
        self.heartbeat()
        if not self.lease:
            raise RuntimeFailure("host lease is unavailable")
        sub = self.subscription(request["subscription"]["id"])
        if sub.get("activeOperationId") != request["operation"]["id"]:
            raise RuntimeFailure("operation no longer owns this subscription")
        return sub

    def backup(self, request, *, final=False):
        sid, op = request["subscription"]["id"], request["operation"]["id"]
        bid = "backup-" + __import__("hashlib").sha256((sid + ":" + op).encode()).hexdigest()[:32]
        cache = self.host.root / "cloud-receipts" / (bid + ".json")
        if cache.exists():
            if final:
                stop(self.host, tenant_for(self.host, sid))
            return read_json(cache)
        directory = capture(self.host, sid, bid, resume=not final)
        receipt = cloud_backup.upload(self.s3, self.env["BACKUP_BUCKET"], self.env["DATA_KEY_ARN"], directory,
                                      request, self.env["AWS_ACCOUNT_ID"], self.env["AWS_REGION"])
        # The core owns the final archive retention window. Operational copies
        # expire after 30 days even if the originating tenant has been removed.
        # Persist expiry before success so a crash cannot orphan the inventory.
        if request["operation"].get("type", "BACKUP").upper() != "ARCHIVE":
            expires = datetime.fromisoformat(receipt["capturedAt"].replace("Z", "+00:00")) + timedelta(days=30)
            write_json(self.host.root / "backup-expiry" / (bid + ".json"), {"expiresAt": expires.strftime("%Y-%m-%dT%H:%M:%SZ"), "receipt": receipt})
        write_json(cache, receipt)
        confined_remove(directory, self.host.root / "backups")
        return receipt

    def recover(self, request, sub, receipt, *, cold):
        sid = sub["subscriptionId"]
        if not receipt or receipt.get("productVersion") != request["release"]["version"]:
            raise RuntimeFailure("restore requires a backup from the matching released product version")
        directory = cloud_backup.download(self.s3, receipt, self.host.root, subscription_id=sid,
            customer_id=sub["customerId"], environment=self.env["ENVIRONMENT"], bucket=self.env["BACKUP_BUCKET"],
            account=self.env["AWS_ACCOUNT_ID"], region=self.env["AWS_REGION"])
        if cold:
            state = self.host.state
            existing = next((t for t in state["tenants"] if t["subscriptionId"] == sid), None)
            if existing is None:
                saved = read_json(directory / "application-secrets.json")
                if saved["subscriptionId"] != sid or saved.get("customerId") != sub["customerId"]:
                    raise RuntimeFailure("application secrets identity mismatch")
                slot = sub["capacitySlot"]
                if len(state["tenants"]) >= 4 or any(t["slot"] == slot for t in state["tenants"]):
                    raise RuntimeFailure("recovery slot is occupied")
                saved.update(slot=slot, state="SUSPENDED", ready=True, terms=sub["entitlementContext"]["terms"], revision=sub["billingRevision"])
                state["tenants"].append(saved)
                self.host.save(state)
            self.host.render()
            self.host.sql("SYSTEM RELOAD CONFIG")
            # Only create the empty application containers/volumes. Do not run
            # schema migration or register a new organization before restoring.
            slot = sub["capacitySlot"]
            self.host.compose("up", "--no-start", "--no-deps", f"app-{slot}", f"gateway-{slot}", f"collector-{slot}")
        # A long restore must not reopen ingestion using stale payment terms.
        result = restore(self.host, sid, directory, cold=cold, resume=False)
        sub = self.fence(request)
        lifecycle.reconcile(self.host, sid, sub["billingRevision"], sub["entitlementContext"]["terms"])
        lifecycle.set_state(self.host, sid, "ACTIVE" if sub.get("desiredEntitlement", "ACTIVE") == "ACTIVE" else "SUSPENDED")
        confined_remove(directory, self.host.root / "backups")
        return result

    def execute(self, request):
        sub = self.fence(request)
        if not self.host.state.get("local"):
            self.refresh_registry()
        sid = sub["subscriptionId"]
        operation, hook = request["operation"]["type"].upper(), request["hook"]
        configuration = request["subscription"]["configuration"]
        result = {"status": "DONE"}
        if hook == "prepare":
            if operation == "CREATE":
                images = lifecycle.release_images(self.host, request["release"])
                if not sub.get("entitlementContext") or not sub.get("capacitySlot"):
                    raise RuntimeFailure("paid terms and a claimed capacity slot are required")
                t = lifecycle.create(self.host, subscription_id=sid, customer_id=sub["customerId"],
                    admin_email=configuration["AdminEmail"], plan=configuration["Plan"], slot=sub["capacitySlot"],
                    terms=sub["entitlementContext"]["terms"], revision=sub["billingRevision"],
                    service_url="https://"+request["context"]["serviceHost"], operation_id=request["operation"]["id"], images=images)
                sub = self.fence(request)
                lifecycle.reconcile(self.host, sid, sub["billingRevision"], sub["entitlementContext"]["terms"])
                secret_name = f"abera/dev/subscriptions/{sid}/client/abera-signoz-{t['generation']}"
                value = json.dumps(read_json(self.host.root / "credentials" / (sid + ".json")))
                try:
                    secret = self.secrets.create_secret(Name=secret_name, KmsKeyId=self.env["DATA_KEY_ARN"], SecretString=value,
                        Tags=[{"Key": "abera:product-id", "Value": "abera-signoz"}, {"Key": "abera:environment", "Value": "dev"}, {"Key": "abera:subscription-id", "Value": sid}])
                except self.secrets.exceptions.ResourceExistsException:
                    secret = self.secrets.describe_secret(SecretId=secret_name)
                result["metadata"] = {"credentialSecretArn": secret["ARN"], "serviceUrl": t["serviceUrl"]}
                result["internalParameters"] = {"HostInstanceId": self.env["HOST_INSTANCE_ID"], "HostPort": str(24800+t["slot"])}
            elif operation in {"BACKUP", "ARCHIVE", "DELETE", "UPDATE"}:
                if operation == "UPDATE":
                    if request['context'].get('maintenanceReview'):
                        from .maintenance import prepare
                        prepare(self, request)
                        return result
                    lifecycle.release_images(self.host, request["release"])
                if operation == "DELETE" and not any(t["subscriptionId"] == sid for t in self.host.state["tenants"]):
                    return result  # a compensated failed CREATE has no data left
                receipt = self.backup(request, final=operation in {"ARCHIVE", "DELETE"})
                result["metadata"] = ({"archiveBackup": receipt} if operation == "ARCHIVE" else {
                    "lastBackupKey": receipt["manifestKey"], "lastBackupVersionId": receipt["manifestVersionId"], "lastBackupAt": receipt["capturedAt"]})
            elif operation == "SUSPEND":
                lifecycle.set_state(self.host, sid, "SUSPENDED")
            elif operation == "REACTIVATE":
                lifecycle.reconcile(self.host, sid, sub["billingRevision"], sub["entitlementContext"]["terms"])
                lifecycle.set_state(self.host, sid, "ACTIVE")
            elif operation in {"RESTORE", "RECOVER"}:
                receipt = request["context"].get("backup")
                if operation == "RESTORE":
                    key = request["context"].get("backupKey")
                    metadata = sub.get("metadata", {})
                    if key != metadata.get("lastBackupKey") or not metadata.get("lastBackupVersionId"):
                        raise RuntimeFailure("restore requires the exact last verified backup")
                    response = self.s3.get_object(Bucket=self.env["BACKUP_BUCKET"], Key=key, VersionId=metadata["lastBackupVersionId"])
                    with response["Body"] as stream:
                        receipt = json.loads(stream.read(256*1024))
                    receipt["manifestVersionId"] = metadata["lastBackupVersionId"]
                self.recover(request, sub, receipt, cold=operation == "RECOVER")
                t = tenant_for(self.host, sid)
                result["internalParameters"] = {"HostInstanceId": self.env["HOST_INSTANCE_ID"], "HostPort": str(24800+t["slot"])}
            elif operation == "PURGE":
                for receipt in request["context"]["backups"]:
                    if not receipt.get("expiresAt") or receipt["expiresAt"] > iso():
                        raise RuntimeFailure("backup retention has not expired")
                    cloud_backup.purge(self.s3, receipt, subscription_id=sid, bucket=self.env["BACKUP_BUCKET"])
            else:
                raise RuntimeFailure("unsupported driver operation")
        elif hook == "verify":
            if operation == "UPDATE":
                if request['context'].get('maintenanceReview'):
                    from .maintenance import verify
                    receipt = verify(self, request)
                    if receipt is None:
                        return {'status': 'IN_PROGRESS'}
                    result['metadata']['maintenanceResult'] = receipt
                lifecycle.update(self.host, sid, lifecycle.release_images(self.host, request["release"]))
                sub = self.fence(request)
                lifecycle.reconcile(self.host, sid, sub["billingRevision"], sub["entitlementContext"]["terms"])
            if operation in {"CREATE", "UPDATE", "RESTORE", "RECOVER", "REACTIVATE"}:
                t = tenant_for(self.host, sid)
                api(f"http://127.0.0.1:{25800+t['slot']}", "GET", "/api/v1/health")
        elif hook == "finalize":
            if operation in {"ARCHIVE", "DELETE"}:
                sub = self.fence(request)
                if operation == "ARCHIVE" and not sub.get("archiveDeletionCommittedAt"):
                    raise RuntimeFailure("archive deletion has not been committed")
                lifecycle.remove(self.host, sid)
        elif hook == "compensate":
            if request.get("context", {}).get("abortTasksOnly"):
                if any(t["subscriptionId"] == sid for t in self.host.state["tenants"]):
                    lifecycle.set_state(self.host, sid, "SUSPENDED")
                return result
            if operation == "CREATE":
                tenant = next((t for t in self.host.state["tenants"] if t["subscriptionId"] == sid), None)
                if tenant and tenant.get("allocationOperation") == request["operation"]["id"]:
                    lifecycle.remove(self.host, sid)
            elif operation in {"ARCHIVE", "DELETE"} and not sub.get("archiveDeletionCommittedAt"):
                lifecycle.set_state(self.host, sid, "ACTIVE" if sub.get("desiredEntitlement") == "ACTIVE" else "SUSPENDED")
            elif operation in {"RECOVER", "RESTORE", "UPDATE"}:
                # Keep a failed restore stopped and owned for inspection. Never
                # advertise a successful rollback or silently discard its data.
                if any(t["subscriptionId"] == sid for t in self.host.state["tenants"]):
                    lifecycle.set_state(self.host, sid, "SUSPENDED")
                raise RuntimeFailure("recovery or update requires operator inspection")
        else:
            raise RuntimeFailure("unsupported driver hook")
        return result

    def work_once(self):
        if not self.lease:
            return
        response = self.jobs.query(IndexName="queue", KeyConditionExpression="#q=:ready",
                                  ExpressionAttributeNames={"#q": "queue"}, ExpressionAttributeValues={":ready": "READY"}, Limit=1)
        for item in response.get("Items", []):
            item = plain(item)
            key = {"pk": item["pk"]}
            self.jobs.update_item(Key=key, UpdateExpression="SET #s=:running", ConditionExpression="#s IN (:pending,:running)",
                ExpressionAttributeNames={"#s": "status"}, ExpressionAttributeValues={":running": "RUNNING", ":pending": "PENDING"})
            try:
                result = self.execute(item["request"])
            except Exception as exc:
                print(json.dumps({"job": item["pk"], "errorType": type(exc).__name__}), flush=True)
                result = {"status": "FAILED", "error": {"code": "HOST_ACTION_FAILED", "message": "Host operation failed; inspect the private runtime state.", "retryable": False}}
            if result['status'] == 'IN_PROGRESS':
                continue  # restart/helper work resumes the same durable queue item
            self.jobs.update_item(Key=key, UpdateExpression="SET #s=:status, #r=:response, expiresAt=:expiry REMOVE #q",
                ExpressionAttributeNames={"#s": "status", "#r": "response", "#q": "queue"},
                ExpressionAttributeValues={":status": "SUCCEEDED" if result["status"] == "DONE" else "FAILED", ":response": dynamo(result), ":expiry": int(time.time())+90*86400})

    def reconcile(self):
        for path in (self.host.root / "backup-expiry").glob("*.json"):
            record = read_json(path)
            if record["expiresAt"] <= iso():
                receipt = record["receipt"]
                cloud_backup.purge(self.s3, receipt, subscription_id=receipt["subscriptionId"], bucket=self.env["BACKUP_BUCKET"])
                path.unlink()
        if self.core.get_item(Key={'pk': 'CAPACITY#abera-signoz#MAINTENANCE', 'sk': 'LOCK'}, ConsistentRead=True).get('Item'):
            return
        for tenant in self.host.state["tenants"]:
            sub = self.subscription(tenant["subscriptionId"])
            if sub.get("activeOperationId"):
                continue
            context = sub.get("entitlementContext")
            if context:
                lifecycle.reconcile(self.host, tenant["subscriptionId"], sub["billingRevision"], context["terms"])
            schedule = self.host.root / "backup-schedule" / (tenant["subscriptionId"] + ".json")
            last = read_json(schedule) if schedule.exists() else {"at": 0, "receipts": []}
            interval = read_json(ABERA/"plans.json")["plans"][tenant["plan"]]["backupIntervalHours"]*3600
            if time.time()-last["at"] >= interval and tenant["state"] == "ACTIVE":
                req = {"environment": "dev", "subscription": {"id": sub["subscriptionId"], "customerId": sub["customerId"]},
                       "operation": {"id": "scheduled-"+str(int(time.time()//interval))}, "release": {"version": sub["currentVersion"]},
                       "context": {"dataGeneration": sub.get("dataGeneration", 1)}}
                receipt = self.backup(req)
                receipts = [*last["receipts"], receipt]
                for old in receipts[:-2]:
                    cloud_backup.purge(self.s3, old, subscription_id=sub["subscriptionId"], bucket=self.env["BACKUP_BUCKET"])
                write_json(schedule, {"at": int(time.time()), "receipts": receipts[-2:]})


def main():
    import boto3
    import fcntl
    root = Path(os.environ.get("ABERA_RUNTIME_ROOT", "/opt/abera/state"))
    root.mkdir(parents=True, exist_ok=True)
    with (root/"controller.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        host = Host(root)
        if not host.state_path.exists():
            images = {key: os.environ[value] for key, value in [("appImage", "APP_IMAGE"), ("collectorImage", "COLLECTOR_IMAGE"), ("clickhouseImage", "CLICKHOUSE_IMAGE"), ("keeperImage", "KEEPER_IMAGE")]}
            if any("@sha256:" not in image for image in images.values()):
                raise RuntimeFailure("cloud images must be pinned by digest")
            host.save({"schemaVersion": 1, "local": False, "project": "abera-signoz-dev", "masterPassword": secrets.token_hex(32),
                       "disabledDefaultPassword": secrets.token_hex(32), "tenants": [], **images})
        controller = Controller(host, boto3.Session(region_name=os.environ["AWS_REGION"]), dict(os.environ))
        controller.refresh_registry()
        host.render()
        host.compose("up", "-d", "--wait", "--wait-timeout", "180", "keeper", "clickhouse")
        def heartbeat():
            while True:
                controller.heartbeat()
                time.sleep(25)
        controller.heartbeat()
        threading.Thread(target=heartbeat, daemon=True).start()
        while True:
            try:
                controller.work_once()
                if controller.lease:
                    controller.reconcile()
            except Exception as exc:
                print(json.dumps({"controllerError": type(exc).__name__}), flush=True)
            time.sleep(5)


if __name__ == "__main__":
    main()
