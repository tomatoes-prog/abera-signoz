"""Idempotent tenant actions. Call under the host's exclusive operation lock."""
from __future__ import annotations

import hashlib
import json
import re
import subprocess
import time

from .backup import capture, databases, start, stop, tenant_for
from .host import RuntimeFailure
from .model import ABERA, new_tenant, read_json, write_json


def validate_terms(terms):
    plans = read_json(ABERA / "plans.json")["plans"]
    if not isinstance(terms, list) or len(terms) > 24:
        raise ValueError("invalid funded terms")
    end, seen = 0, set()
    for term in terms:
        if (set(term) != {"cycleId", "startsAt", "endsAt", "plan"} or term["plan"] not in plans
            or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", term["cycleId"])
            or term["cycleId"] in seen or type(term["startsAt"]) is not int or type(term["endsAt"]) is not int
            or term["startsAt"] < end or not 0 < term["endsAt"]-term["startsAt"] <= 32*86400):
            raise ValueError("invalid funded window")
        seen.add(term["cycleId"])
        end = term["endsAt"]


def create(host, *, subscription_id, customer_id, admin_email, plan, slot, terms, revision, service_url, operation_id, images=None):
    validate_terms(terms)
    state = host.state
    matches = [t for t in state["tenants"] if t["subscriptionId"] == subscription_id]
    if matches:
        tenant = matches[0]
        if tenant.get("customerId") != customer_id or tenant["adminEmail"] != admin_email or tenant["slot"] != slot:
            raise RuntimeFailure("allocation identity conflict")
    else:
        if len(state["tenants"]) >= 4 or any(t["slot"] == slot for t in state["tenants"]):
            raise RuntimeFailure("host has no free assigned slot")
        tenant = new_tenant(subscription_id, admin_email, plan, slot, terms, revision)
        tenant.update(customerId=customer_id, serviceUrl=service_url, allocationOperation=operation_id)
        tenant.update(images or {})
        state["tenants"].append(tenant)
        host.save(state)
    host.up(subscription_id)
    return tenant_for(host, subscription_id)


def release_images(host, release):
    """Tenant apps can roll independently; shared storage requires maintenance."""
    import re
    images = release.get("imageUris", {})
    if host.state.get("local") and not images:
        return {}
    if set(images) != {"app", "collector", "clickhouse", "keeper", "admin"}:
        raise RuntimeFailure("release must pin all five images")
    if any(not re.fullmatch(r"[A-Za-z0-9._:/-]+@sha256:[a-f0-9]{64}", image) for image in images.values()):
        raise RuntimeFailure("release images must use immutable digests")
    for name in ("clickhouse", "keeper"):
        if images[name] != host.state.get(name + "Image"):
            raise RuntimeFailure("shared storage version requires a reviewed host maintenance operation")
    return {"appImage": images["app"], "collectorImage": images["collector"], "productVersion": release["version"]}


def update(host, subscription_id, images):
    state = host.state
    tenant = next(t for t in state["tenants"] if t["subscriptionId"] == subscription_id)
    stop(host, tenant)
    tenant.update(images)
    host.save(state)
    host.up(subscription_id)


def reconcile(host, subscription_id, revision, terms):
    validate_terms(terms)
    state = host.state
    tenant = next(t for t in state["tenants"] if t["subscriptionId"] == subscription_id)
    if revision < tenant["revision"]:
        return False
    if revision == tenant["revision"] and tenant["terms"] != terms:
        raise RuntimeFailure("same billing revision changed its funded terms")
    previous = {t["cycleId"]: t for t in tenant["terms"]}
    for term in terms:
        old = previous.get(term["cycleId"])
        if old and any(old[k] != term[k] for k in ("startsAt", "endsAt")):
            raise RuntimeFailure("funded cycle boundaries cannot change")
    before = tenant["plan"]
    for term in terms:
        if term["startsAt"] <= int(time.time()) < term["endsAt"]:
            tenant["plan"] = term["plan"]
    if revision == tenant["revision"] and terms == tenant["terms"] and before == tenant["plan"]:
        return True
    tenant.update(revision=revision, terms=terms)
    host.save(state)
    # Render is invoked only for topology/plan changes. Routine observations
    # write just gateway.json to avoid resetting all tenants' health timestamps.
    if before != tenant["plan"]:
        host.render()
        host.apply_retention(tenant)
        host.compose("up", "-d", "--no-deps", f"collector-{tenant['slot']}")
        host.compose("restart", f"collector-{tenant['slot']}")
    host.observe()
    return True


def set_state(host, subscription_id, target):
    if target not in {"ACTIVE", "SUSPENDED"}:
        raise ValueError("invalid transition")
    state = host.state
    tenant = next(t for t in state["tenants"] if t["subscriptionId"] == subscription_id)
    tenant["state"] = target
    host.save(state)
    host.observe()
    if target == "ACTIVE":
        start(host, tenant)
    else:
        stop(host, tenant)
    return {"state": target}


def remove(host, subscription_id, *, backup_id=None):
    """Free a slot only after its containers, volumes and databases are gone."""
    state = host.state
    tenant = next((t for t in state["tenants"] if t["subscriptionId"] == subscription_id), None)
    if tenant is None:
        return {"removed": True}
    if backup_id:
        capture(host, subscription_id, backup_id, resume=False)
    stop(host, tenant)
    services = [f"{kind}-{tenant['slot']}" for kind in ("app", "collector", "gateway", "migrate")]
    host.compose("rm", "-f", "-s", *services)
    host.sql(";".join(f"DROP DATABASE IF EXISTS {db} SYNC" for db in databases(tenant)), timeout=300)
    project = state.get("project", "abera-signoz-dev")
    if not re.fullmatch(r"abera-signoz-[a-z0-9-]+", project):
        raise RuntimeFailure("unexpected Docker project")
    for kind in ("app", "gateway"):
        name = f"{project}_{kind}-{tenant['slot']}"
        result = subprocess.run(["docker", "volume", "rm", name], capture_output=True, text=True)
        if result.returncode and "no such volume" not in result.stderr.lower():
            raise RuntimeFailure("tenant volume cleanup failed; slot remains occupied")
    state["tenants"] = [t for t in state["tenants"] if t["subscriptionId"] != subscription_id]
    state.setdefault("removed", {})[subscription_id] = {"at": int(time.time()), "generation": tenant["generation"]}
    host.save(state)
    host.render()
    host.sql("SYSTEM RELOAD CONFIG")
    for path in (host.root / "credentials" / (subscription_id + ".json"),):
        path.unlink(missing_ok=True)
    return {"removed": True}
