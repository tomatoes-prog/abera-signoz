"""Version-pinned S3 recovery manifests compatible with driver protocol v4."""
import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path

from .backup import digest, verify_files
from .host import RuntimeFailure
from .model import read_json, write_json


def existing(s3, bucket, key, sha256):
    from botocore.exceptions import ClientError
    try:
        head = s3.head_object(Bucket=bucket, Key=key)
    except ClientError as exc:
        if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NotFound"}:
            return None
        raise
    if head.get("Metadata", {}).get("sha256") != sha256:
        raise RuntimeFailure("immutable backup key already contains different data")
    return head


def upload(s3, bucket, kms_key, directory, request, account, region):
    from boto3.s3.transfer import TransferConfig
    local = verify_files(directory)
    prefix = f"recovery/{request['subscription']['id']}/{local['backupId']}/"
    context = request["context"]
    artifacts = {}
    for kind, name in [("database", "database.zip"), ("files", "files.tar.gz"), ("applicationSecrets", "application-secrets.json")]:
        path = Path(directory) / name
        key = prefix + name
        checksum = local["files"][name]["sha256"]
        head = existing(s3, bucket, key, checksum)
        if head is None:
            s3.upload_file(str(path), bucket, key, ExtraArgs={"ServerSideEncryption": "aws:kms", "SSEKMSKeyId": kms_key, "Metadata": {"sha256": checksum}},
                           Config=TransferConfig(multipart_threshold=64*1024**2, multipart_chunksize=64*1024**2, max_concurrency=1))
            head = s3.head_object(Bucket=bucket, Key=key)
        version = head.get("VersionId")
        if not version or version == "null":
            raise RuntimeFailure("versioning is required for recovery artifacts")
        result = s3.get_object(Bucket=bucket, Key=key, VersionId=version)
        h = hashlib.sha256()
        with result["Body"] as stream:
            for block in iter(lambda: stream.read(1024**2), b""):
                h.update(block)
        if head["ContentLength"] != path.stat().st_size or h.hexdigest() != local["files"][name]["sha256"]:
            raise RuntimeFailure("remote artifact verification failed")
        artifacts[kind] = {"key": key, "versionId": version, "sha256": h.hexdigest(), "bytes": head["ContentLength"]}
    receipt = {"schemaVersion": 3, "backupId": local["backupId"], "subscriptionId": request["subscription"]["id"],
               "customerId": request["subscription"]["customerId"], "productId": "abera-signoz", "environment": request["environment"],
               "accountId": account, "region": region, "dataGeneration": context.get("dataGeneration", 1),
               "archiveCycleId": context.get("archiveCycleId"), "sequence": context.get("backupSequence", 0),
               "productVersion": local["productVersion"], "compatibilityGeneration": local["compatibilityGeneration"],
               "capturedAt": datetime.fromtimestamp(local["capturedAt"], timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
               "verified": True, "bucket": bucket, "manifestKey": prefix + "manifest.json", "artifacts": artifacts,
               "databaseBytes": artifacts["database"]["bytes"], "fileBytes": artifacts["files"]["bytes"], "runtimeManifest": local}
    body = json.dumps(receipt, sort_keys=True).encode()
    checksum = hashlib.sha256(body).hexdigest()
    response = existing(s3, bucket, receipt["manifestKey"], checksum)
    if response is None:
        response = s3.put_object(Bucket=bucket, Key=receipt["manifestKey"], Body=body,
                                 ContentType="application/json", ServerSideEncryption="aws:kms", SSEKMSKeyId=kms_key,
                                 Metadata={"sha256": checksum})
    receipt["manifestVersionId"] = response.get("VersionId")
    if not receipt["manifestVersionId"] or receipt["manifestVersionId"] == "null":
        raise RuntimeFailure("manifest was not versioned")
    return receipt


def download(s3, receipt, root, *, subscription_id, customer_id, environment, bucket, account, region):
    expected = {"subscriptionId": subscription_id, "customerId": customer_id, "environment": environment,
                "productId": "abera-signoz", "bucket": bucket, "accountId": account, "region": region,
                "compatibilityGeneration": "signoz-0.1"}
    if any(receipt.get(k) != v for k, v in expected.items()) or not receipt.get("verified"):
        raise RuntimeFailure("foreign or incompatible recovery receipt")
    backup_id = receipt.get("backupId", "")
    if not re.fullmatch(r"[a-z0-9-]{3,128}", backup_id):
        raise RuntimeFailure("invalid backup identity")
    prefix = f"recovery/{subscription_id}/{backup_id}/"
    if receipt.get("manifestKey") != prefix + "manifest.json" or not receipt.get("manifestVersionId"):
        raise RuntimeFailure("unversioned recovery manifest")
    response = s3.get_object(Bucket=bucket, Key=receipt["manifestKey"], VersionId=receipt["manifestVersionId"])
    with response["Body"] as stream:
        manifest = json.loads(stream.read(256*1024))
    if any(manifest.get(k) != receipt.get(k) for k in (*expected, "backupId", "artifacts", "dataGeneration", "archiveCycleId", "productVersion", "capturedAt", "sequence")):
        raise RuntimeFailure("recovery receipt differs from its versioned manifest")
    target = Path(root) / "backups" / subscription_id / backup_id
    target.mkdir(parents=True, exist_ok=True)
    for kind, name in [("database", "database.zip"), ("files", "files.tar.gz"), ("applicationSecrets", "application-secrets.json")]:
        item = manifest["artifacts"][kind]
        if item["key"] != prefix + name or not item.get("versionId"):
            raise RuntimeFailure("artifact is not scoped and version-pinned")
        s3.download_file(bucket, item["key"], str(target/name), ExtraArgs={"VersionId": item["versionId"]})
        if digest(target/name) != item["sha256"]:
            raise RuntimeFailure("downloaded artifact checksum mismatch")
        (target/name).chmod(0o600)
    write_json(target / "manifest.json", manifest["runtimeManifest"])
    verify_files(target)
    return target


def purge(s3, receipt, *, subscription_id, bucket):
    prefix = f"recovery/{subscription_id}/{receipt['backupId']}/"
    if receipt["bucket"] != bucket or receipt["subscriptionId"] != subscription_id or receipt["manifestKey"] != prefix + "manifest.json":
        raise RuntimeFailure("purge identity mismatch")
    objects = list(receipt["artifacts"].values()) + [{"key": receipt["manifestKey"], "versionId": receipt["manifestVersionId"]}]
    for item in objects:
        if not item["key"].startswith(prefix) or not item.get("versionId"):
            raise RuntimeFailure("purge requires exact owned object versions")
    for item in objects:
        s3.delete_object(Bucket=bucket, Key=item["key"], VersionId=item["versionId"])
