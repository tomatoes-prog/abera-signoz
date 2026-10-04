import copy
import json
import sqlite3
import time
from contextlib import closing
from pathlib import Path

import pytest
from abera.runtime.backup import merge_ledger
from abera.runtime.host import RuntimeFailure
from abera.runtime.lifecycle import reconcile, validate_terms
from abera.runtime.model import new_tenant, read_json, write_json
from abera.runtime.render import render


def terms(plan="lite"):
    now = int(time.time())
    return [{"cycleId": "paid-1", "startsAt": now-10, "endsAt": now+10000, "plan": plan}]


def test_render_four_separate_namespaces_secrets_networks_and_data_clusters(tmp_path):
    state = {"masterPassword": "operator-secret", "disabledDefaultPassword": "disabled", "tenants": [
        new_tenant(f"tenant-{slot}", f"admin-{slot}@example.test", "lite", slot, terms(), 1) for slot in range(1,5)]}
    output = read_json(render(tmp_path, state))
    assert len({t["namespace"] for t in state["tenants"]}) == 4
    assert all(output["networks"][f"tenant-{s}"]["internal"] for s in range(1,5))
    for tenant in state["tenants"]:
        slot = tenant["slot"]
        app = output["services"][f"app-{slot}"]
        assert app["environment"]["SIGNOZ_TELEMETRYSTORE_CLICKHOUSE_CLUSTER"] == tenant["namespace"]+"_data"
        assert "operator-secret" not in json.dumps(app)
        assert output["services"][f"gateway-{slot}"]["ports"] == [f"127.0.0.1:{24800+slot}:8081"]
    state["tenants"].append(copy.deepcopy(state["tenants"][0]))
    with pytest.raises(ValueError): render(tmp_path, state)


def test_delayed_billing_cannot_rollback_terms_or_reuse_cycle_boundaries():
    initial = terms()
    tenant = new_tenant("tenant-1", "a@example.test", "lite", 1, initial, 2)
    class Host:
        state = {"tenants": [tenant]}
        def save(self, state): self.state = copy.deepcopy(state)
        def observe(self): pass
    host = Host()
    assert reconcile(host, "tenant-1", 1, terms("essential")) is False
    with pytest.raises(RuntimeFailure): reconcile(host, "tenant-1", 2, terms("essential"))
    changed = copy.deepcopy(initial); changed[0]["startsAt"] += 1
    with pytest.raises(RuntimeFailure): reconcile(host, "tenant-1", 3, changed)
    with pytest.raises(ValueError): validate_terms(initial+initial)


def ledger(path, amount):
    with closing(sqlite3.connect(path)) as db:
        db.executescript("CREATE TABLE identity(id INTEGER,subscription TEXT,namespace TEXT,revision INTEGER,digest TEXT); CREATE TABLE cycles(id TEXT PRIMARY KEY,starts INTEGER,ends INTEGER,bytes INTEGER,samples INTEGER); CREATE TABLE receipts(id TEXT PRIMARY KEY,created INTEGER); CREATE TABLE queue(id TEXT PRIMARY KEY,signal TEXT,payload BLOB,created INTEGER,attention INTEGER);")
        db.executescript("CREATE TABLE series(id TEXT PRIMARY KEY,seen INTEGER); CREATE TABLE rate(id INTEGER PRIMARY KEY,tokens REAL,updated REAL);")
        db.execute("INSERT INTO identity VALUES(1,'tenant-1','abera_aaaaaaaaaaaaaaaaaaaa',2,'hash')")
        db.execute("INSERT INTO cycles VALUES('paid-1',1,100,?,?)", (amount, amount))
        db.execute("INSERT INTO receipts VALUES(?,1)", (str(amount),))
        db.execute("INSERT INTO queue VALUES(?,'logs',?,1,0)", (str(amount), b"payload"))
        db.execute("INSERT INTO series VALUES(?,?)", (str(amount), amount))
        db.execute("INSERT INTO rate VALUES(1,?,?)", (amount, amount))
        db.commit()


def test_warm_restore_keeps_larger_counters_and_every_accepted_batch(tmp_path):
    before, recent = tmp_path/'backup.db', tmp_path/'current.db'
    ledger(before, 10); ledger(recent, 20)
    tenant = {"subscriptionId": "tenant-1", "namespace": "abera_aaaaaaaaaaaaaaaaaaaa", "terms": []}
    merge_ledger(before, recent, tenant)
    with closing(sqlite3.connect(before)) as db:
        assert db.execute("SELECT bytes,samples FROM cycles").fetchone() == (20,20)
        assert db.execute("SELECT count(*) FROM queue").fetchone() == (2,)
        assert db.execute("SELECT count(*) FROM series").fetchone() == (2,)
        assert db.execute("SELECT tokens FROM rate").fetchone() == (20,)


def test_unsealed_cold_restore_holds_even_a_cycle_created_after_snapshot(tmp_path):
    path = tmp_path/'old.db'
    ledger(path, 10)
    active = terms()[0] | {"cycleId": "new-cycle"}
    merge_ledger(path, None, {"subscriptionId": "tenant-1", "namespace": "abera_aaaaaaaaaaaaaaaaaaaa", "terms": [active]})
    with closing(sqlite3.connect(path)) as db:
        assert db.execute("SELECT bytes,samples FROM cycles WHERE id='new-cycle'").fetchone() == (10_000_000_000,25_000_000)


def test_foreign_usage_ledger_is_never_merged(tmp_path):
    before, recent = tmp_path/'backup.db', tmp_path/'current.db'
    ledger(before, 10); ledger(recent, 20)
    with pytest.raises(RuntimeFailure):
        merge_ledger(before, recent, {"subscriptionId": "another", "namespace": "abera_aaaaaaaaaaaaaaaaaaaa"})
