"""Tenant-scoped native ClickHouse backups and cold application snapshots.

Backups stop only the affected tenant. Verification restores the native archive
under inaccessible temporary database names before publishing a receipt.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tarfile
import time
import zipfile
from contextlib import closing
from pathlib import Path

from .host import RuntimeFailure, api
from .model import ABERA, DATABASE_SUFFIXES, read_json, write_json

BACKUP_ID = re.compile(r"^[a-z0-9-]{3,128}$")


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def confined_remove(path, parent):
    path, parent = Path(path).resolve(), Path(parent).resolve()
    if path == parent or not path.is_relative_to(parent):
        raise RuntimeFailure("cleanup escaped its private staging directory")
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def verify_sqlite(path):
    with closing(sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)) as db:
        if db.execute("PRAGMA integrity_check").fetchall() != [("ok",)]:
            raise RuntimeFailure("SQLite snapshot failed its integrity check")


def tenant_for(host, subscription_id):
    matches = [t for t in host.state["tenants"] if t["subscriptionId"] == subscription_id]
    if len(matches) != 1:
        raise RuntimeFailure("subscription is not allocated to this host")
    return matches[0]


def databases(tenant):
    ns = tenant["namespace"]
    if not re.fullmatch(r"abera_[a-f0-9]{20}", ns):
        raise RuntimeFailure("invalid database namespace")
    return [ns + "_" + suffix for suffix in DATABASE_SUFFIXES]


def stop(host, tenant):
    slot = tenant["slot"]
    # The gateway shuts down accepting requests before exporters/app are stopped.
    host.compose("stop", "-t", "45", f"gateway-{slot}")
    host.compose("stop", "-t", "45", f"collector-{slot}", f"app-{slot}")


def start(host, tenant):
    slot = tenant["slot"]
    # Cold recovery deliberately has no migration container: starting the
    # restored app must not traverse and rerun its migration dependency.
    host.compose("up", "-d", "--no-deps", "--no-recreate", f"app-{slot}", f"collector-{slot}", f"gateway-{slot}")
    deadline = time.monotonic() + 120
    while True:
        try:
            api(f"http://127.0.0.1:{25800+slot}", "GET", "/api/v1/health")
            break
        except (RuntimeFailure, OSError):
            if time.monotonic() >= deadline:
                raise RuntimeFailure("restored application did not become healthy")
            time.sleep(2)
    host.observe()


def verify_files(directory, tenant=None):
    directory = Path(directory)
    receipt = read_json(directory / "manifest.json")
    if receipt.get("schemaVersion") != 1 or receipt.get("verified") is not True:
        raise RuntimeFailure("backup is not verified")
    if tenant and any(receipt.get(k) != tenant.get(k) for k in ("subscriptionId", "namespace", "generation")):
        raise RuntimeFailure("backup belongs to another tenant generation")
    if set(receipt.get("files", {})) != {"database.zip", "files.tar.gz", "application-secrets.json"}:
        raise RuntimeFailure("backup is incomplete")
    for name, expected in receipt["files"].items():
        path = directory / name
        if not path.is_file() or path.stat().st_size != expected["bytes"] or digest(path) != expected["sha256"]:
            raise RuntimeFailure("backup checksum or length mismatch")
    with zipfile.ZipFile(directory / "database.zip") as z:
        if z.testzip() is not None:
            raise RuntimeFailure("native backup archive is corrupt")
    return receipt


def verify_native(host, tenant, native_path, originals):
    targets = [db + "_verify" for db in originals]
    # The host operation lock prevents overlapping verification runs.
    mappings = ", ".join(f"DATABASE {src} AS {dst}" for src, dst in zip(originals, targets))
    try:
        host.sql(f"RESTORE {mappings} FROM File('{native_path}')", timeout=900)
        names = ",".join("'" + db + "'" for db in targets)
        tables = json.loads(host.sql(f"SELECT database,name FROM system.tables WHERE database IN ({names}) AND endsWith(engine,'MergeTree') FORMAT JSON"))["data"]
        if not tables:
            raise RuntimeFailure("restored backup contains no telemetry tables")
        for table in tables:
            name = table["name"]
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
                raise RuntimeFailure("unexpected backup table identifier")
        checks = ";".join(f"CHECK TABLE {t['database']}.{t['name']} SETTINGS check_query_single_value_result=1" for t in tables)
        output = host.sql(checks, timeout=900)
        if not output or any(line != "1" for line in output.splitlines()):
            raise RuntimeFailure("restored ClickHouse table failed integrity checking")
        # Compare primary data while the live tenant is quiesced. Distributed
        # tables are never queried during verification (their engines are copied).
        for suffix, table in [("logs", "logs_v2"), ("traces", "signoz_index_v3"), ("metrics", "samples_v4"), ("metrics", "exp_hist")]:
            db = tenant["namespace"] + "_" + suffix
            counts = host.sql(f"SELECT (SELECT count() FROM {db}.{table}) = (SELECT count() FROM {db}_verify.{table})")
            if counts != "1":
                raise RuntimeFailure("restored primary row count differs from source")
        return len(tables)
    finally:
        host.sql(";".join(f"DROP DATABASE IF EXISTS {db} SYNC" for db in targets), timeout=300)


def capture(host, subscription_id, backup_id, *, resume=True):
    if not BACKUP_ID.fullmatch(backup_id):
        raise ValueError("invalid backup id")
    tenant = tenant_for(host, subscription_id)
    target = host.root / "backups" / subscription_id / backup_id
    if (target / "manifest.json").exists():
        verify_files(target, tenant)
        if not resume:
            stop(host, tenant)
        return target
    target.mkdir(parents=True, exist_ok=True)
    work = target / "staging"
    if work.exists():
        confined_remove(work, target)
    work.mkdir(mode=0o700)
    native_path = f"/var/lib/clickhouse/abera-backups/{tenant['namespace']}-{backup_id}.zip"
    started = time.time()
    stop(host, tenant)
    try:
        # Remove only this operation's incomplete artifact on an idempotent retry.
        host.compose("exec", "-T", "clickhouse", "rm", "-f", native_path)
        existing = json.loads(host.sql(f"SELECT name FROM system.databases WHERE startsWith(name,'{tenant['namespace']}_') FORMAT JSON"))["data"]
        present = sorted(set(databases(tenant)) & {d["name"] for d in existing})
        if not {tenant["namespace"] + "_" + s for s in ("logs", "metrics", "traces")} <= set(present):
            raise RuntimeFailure("primary telemetry databases are missing")
        clause = ", ".join("DATABASE " + db for db in present)
        host.sql(f"BACKUP {clause} TO File('{native_path}') SETTINGS compression_method='deflate',compression_level=1", timeout=900)
        restored_tables = verify_native(host, tenant, native_path, present)
        host.compose("cp", f"clickhouse:{native_path}", str(target / "database.zip"))
        for service, destination, mount, filename in [("app", "app", "/var/lib/signoz/.", "signoz.db"),
                                                    ("gateway", "gateway", "/var/lib/abera/.", "usage.db")]:
            (work / destination).mkdir()
            host.compose("cp", f"{service}-{tenant['slot']}:{mount}", str(work / destination))
            verify_sqlite(work / destination / filename)
        with tarfile.open(target / "files.tar.gz", "w:gz", compresslevel=1) as archive:
            archive.add(work / "app", arcname="app")
            archive.add(work / "gateway", arcname="gateway")
        write_json(target / "application-secrets.json", tenant)
        files = {name: {"sha256": digest(target / name), "bytes": (target / name).stat().st_size}
                 for name in ("database.zip", "files.tar.gz", "application-secrets.json")}
        receipt = {"schemaVersion": 1, "backupId": backup_id, "subscriptionId": subscription_id,
                   "namespace": tenant["namespace"], "generation": tenant["generation"], "capturedAt": int(started),
                   "productVersion": tenant.get("productVersion", read_json(ABERA / "release.json")["version"]), "compatibilityGeneration": "signoz-0.1",
                   "verified": True, "sealed": not resume, "databases": present, "restoredTables": restored_tables, "seconds": int(time.time()-started), "files": files}
        write_json(target / "manifest.json", receipt)
        verify_files(target, tenant)
        return target
    finally:
        host.compose("exec", "-T", "clickhouse", "rm", "-f", native_path, check=False)
        confined_remove(work, target)
        if resume and tenant["state"] == "ACTIVE":
            start(host, tenant)


def merge_ledger(restored, current, tenant, *, sealed=False):
    """Warm restore cannot roll usage back or lose a more recent accepted batch."""
    verify_sqlite(restored)
    with closing(sqlite3.connect(restored)) as db:
        if db.execute("SELECT subscription,namespace FROM identity").fetchone() != (tenant["subscriptionId"], tenant["namespace"]):
            raise RuntimeFailure("restored usage ledger belongs to another tenant")
        if current and Path(current).exists():
            verify_sqlite(current)
            db.execute("ATTACH DATABASE ? AS recent", (str(current),))
            identity = db.execute("SELECT subscription,namespace FROM recent.identity").fetchone()
            if identity != (tenant["subscriptionId"], tenant["namespace"]):
                raise RuntimeFailure("current usage ledger belongs to another tenant")
            for row in db.execute("SELECT id,starts,ends,bytes,samples FROM recent.cycles").fetchall():
                prior = db.execute("SELECT starts,ends FROM cycles WHERE id=?", (row[0],)).fetchone()
                if prior and prior != tuple(row[1:3]):
                    raise RuntimeFailure("usage snapshot cycle boundaries differ")
                db.execute("INSERT INTO cycles VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET bytes=max(bytes,excluded.bytes),samples=max(samples,excluded.samples)", row)
            for table in ("receipts", "queue"):
                db.execute(f"INSERT OR IGNORE INTO {table} SELECT * FROM recent.{table}")
            db.execute("INSERT INTO series SELECT * FROM recent.series WHERE 1 ON CONFLICT(id) DO UPDATE SET seen=max(seen,excluded.seen)")
            db.execute("DELETE FROM rate")
            db.execute("INSERT INTO rate SELECT * FROM recent.rate")
            db.execute("DELETE FROM identity")
            db.execute("INSERT INTO identity SELECT * FROM recent.identity")
        elif not sealed:
            # A destroyed host may have accepted data since the latest backup.
            # Fail closed for its active term; never silently grant that quota again.
            plans = read_json(ABERA / "plans.json")["plans"]
            now = int(time.time())
            for term in tenant["terms"]:
                if term["startsAt"] <= now < term["endsAt"]:
                    plan = plans[term["plan"]]
                    db.execute("INSERT INTO cycles VALUES(?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET bytes=max(bytes,excluded.bytes),samples=max(samples,excluded.samples)",
                               (term["cycleId"], term["startsAt"], term["endsAt"], plan["logTraceBytes"], plan["metricSamples"]))
        db.commit()
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")


def restore(host, subscription_id, directory, *, cold=False, resume=True):
    tenant = tenant_for(host, subscription_id)
    directory = Path(directory).resolve()
    receipt = verify_files(directory, tenant)
    if not receipt.get("databases") or not set(receipt["databases"]) <= set(databases(tenant)):
        raise RuntimeFailure("backup contains foreign databases")
    # Preserve a verifiable rollback point before touching any existing data.
    if not cold:
        capture(host, subscription_id, "before-restore-" + receipt["backupId"], resume=False)
    else:
        stop(host, tenant)
    work = host.root / "restore-staging" / subscription_id
    if work.exists():
        confined_remove(work, work.parent)
    work.mkdir(parents=True, exist_ok=True)
    native_path = f"/var/lib/clickhouse/abera-backups/restore-{tenant['namespace']}.zip"
    try:
        if not cold:
            (work / "recent").mkdir(exist_ok=True)
            host.compose("cp", f"gateway-{tenant['slot']}:/var/lib/abera/.", str(work / "recent"))
        with tarfile.open(directory / "files.tar.gz") as archive:
            for member in archive.getmembers():
                path = (work / member.name).resolve()
                if not path.is_relative_to(work.resolve()) or member.issym() or member.islnk() or not (member.isdir() or member.isfile()):
                    raise RuntimeFailure("unsafe application backup member")
            # Explicit extraction also supports the development Python 3.11.
            for member in archive.getmembers():
                path = work / member.name
                if member.isdir():
                    path.mkdir(parents=True, exist_ok=True)
                else:
                    path.parent.mkdir(parents=True, exist_ok=True)
                    with archive.extractfile(member) as source, path.open("wb") as destination:
                        shutil.copyfileobj(source, destination)
                    path.chmod(0o600)
        verify_sqlite(work / "app" / "signoz.db")
        merge_ledger(work / "gateway" / "usage.db", None if cold else work / "recent" / "usage.db", tenant, sealed=receipt.get("sealed", False))
        host.compose("cp", str(directory / "database.zip"), f"clickhouse:{native_path}")
        host.sql(";".join(f"DROP DATABASE IF EXISTS {db} SYNC" for db in databases(tenant)), timeout=300)
        host.sql("RESTORE " + ", ".join("DATABASE " + db for db in receipt["databases"]) + f" FROM File('{native_path}')", timeout=900)
        host.apply_retention(tenant)
        # Force TTL expiry before exposing restored data. Plan retention does not
        # restart from the backup's recovery date.
        tables = json.loads(host.sql(f"SELECT database,name FROM system.tables WHERE startsWith(database,'{tenant['namespace']}_') AND endsWith(engine,'MergeTree') AND position(create_table_query,' TTL ')>0 FORMAT JSON"))["data"]
        host.sql(";".join(f"ALTER TABLE {t['database']}.{t['name']} MATERIALIZE TTL SETTINGS mutations_sync=2" for t in tables), timeout=900)
        for service, mount in [("app", "/var/lib/signoz"), ("gateway", "/var/lib/abera")]:
            # Fixed container paths, same-tenant volume only. No shell-derived path.
            host.compose("run", "--rm", "--no-deps", "--user", "65532", "--entrypoint", "sh", f"{service}-{tenant['slot']}", "-ec", f"find {mount} -mindepth 1 -maxdepth 1 -exec rm -rf {{}} +")
            host.compose("cp", str(work / service) + "/.", f"{service}-{tenant['slot']}:{mount}/")
            host.compose("run", "--rm", "--no-deps", "--user", "0", "--cap-add", "CHOWN", "--cap-add", "DAC_OVERRIDE", "--entrypoint", "chown", f"{service}-{tenant['slot']}", "-R", "65532:65532", mount)
        if resume and tenant["state"] == "ACTIVE":
            start(host, tenant)
        return {"restored": True, "backupId": receipt["backupId"], "coldQuotaHeld": cold and not receipt.get("sealed", False)}
    finally:
        host.compose("exec", "-T", "clickhouse", "rm", "-f", native_path, check=False)
        confined_remove(work, work.parent)
