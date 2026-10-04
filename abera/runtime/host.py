from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .model import ABERA, read_json, write_json
from .render import gateway_config, render


class RuntimeFailure(RuntimeError):
    pass


def api(base: str, method: str, path: str, body=None, token: str = ""):
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(base + path, data=None if body is None else json.dumps(body).encode(), headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            raw = response.read(4 * 1024 * 1024)
            try:
                value = json.loads(raw) if raw else {}
            except ValueError:
                raise RuntimeFailure(f"SigNoz API {method} {path.split('?')[0]} did not return JSON") from None
            return value.get("data", value)
    except urllib.error.HTTPError as exc:
        # Do not include request bodies, credentials or server echoes in logs.
        raise RuntimeFailure(f"SigNoz API {method} {path.split('?')[0]} returned {exc.code}") from None


class Host:
    def __init__(self, root: Path):
        self.root = root.resolve()
        self.state_path = self.root / "state.json"

    @property
    def state(self):
        return read_json(self.state_path)

    def save(self, state):
        write_json(self.state_path, state)

    def compose(self, *args, check=True, timeout=900):
        result = subprocess.run(["docker", "compose", "-f", str(self.root / "compose.json"), *args], capture_output=True, text=True, timeout=timeout)
        if check and result.returncode:
            # Generated compose contains passwords: callers receive the action
            # and exit status, never the command environment/configuration.
            raise RuntimeFailure(f"Docker Compose {args[0]} failed ({result.returncode}); inspect the named service logs locally")
        return result

    def sql(self, query: str, *, timeout=120) -> str:
        result = self.compose("exec", "-T", "clickhouse", "clickhouse-client", "--config-file", "/etc/clickhouse-server/abera-client.xml", "--multiquery", "--query", query, timeout=timeout)
        return result.stdout.strip()

    def render(self):
        state = self.state
        return render(self.root, state, local=state.get("local", True), pin_cpu=state.get("pinCPU", False))

    def up(self, subscription_id=None):
        self.render()
        self.compose("up", "-d", "--wait", "--wait-timeout", "180", "keeper", "clickhouse")
        self.sql("SYSTEM RELOAD CONFIG")
        for tenant in self.state["tenants"]:
            if subscription_id and tenant["subscriptionId"] != subscription_id:
                continue
            if tenant["state"] != "ACTIVE":
                continue
            slot = tenant["slot"]
            # DDL runs serially to limit memory and avoid migration storms on a
            # one-vCPU pilot. Existing healthy apps are not recreated needlessly.
            self.compose("up", "-d", f"app-{slot}", f"collector-{slot}")
            base = f"http://127.0.0.1:{25800 + slot}"
            deadline = time.monotonic() + 120
            while True:
                try:
                    api(base, "GET", "/api/v1/health")
                    break
                except (RuntimeFailure, OSError):
                    if time.monotonic() >= deadline:
                        raise RuntimeFailure(f"app-{slot} did not become healthy")
                    time.sleep(2)
            self.apply_retention(tenant)
            self.bootstrap(tenant["subscriptionId"])
            self.compose("up", "-d", f"gateway-{slot}")
            self.observe()

    def bootstrap(self, subscription_id: str):
        state = self.state
        t = next(x for x in state["tenants"] if x["subscriptionId"] == subscription_id)
        if t["ready"]:
            return
        base = f"http://127.0.0.1:{25800 + t['slot']}"
        email = t["namespace"] + "@operations.abera.invalid"
        context = api(base, "GET", "/api/v2/sessions/context?email=" + urllib.parse.quote(email))
        if not context["exists"]:
            api(base, "POST", "/api/v1/register", {"name": "Abera Operations", "email": email, "password": t["rootPassword"], "orgName": t["namespace"], "orgDisplayName": "Abera SigNoz"})
            context = api(base, "GET", "/api/v2/sessions/context?email=" + urllib.parse.quote(email))
        if len(context["orgs"]) != 1:
            raise RuntimeFailure("unexpected organization count during private bootstrap")
        org_id = context["orgs"][0]["id"]
        login = api(base, "POST", "/api/v2/sessions/email_password", {"email": email, "password": t["rootPassword"], "orgId": org_id})
        token = login["accessToken"]
        try:
            users = api(base, "GET", "/api/v2/users", token=token)
            customer = next((u for u in users if u["email"] == t["adminEmail"]), None)
            if customer is None:
                roles = api(base, "GET", "/api/v1/roles", token=token)
                admin = next(r for r in roles if r["name"] == "signoz-admin")
                customer = api(base, "POST", "/api/v2/users", {"email": t["adminEmail"], "displayName": "Administrador", "frontendBaseUrl": "", "userRoles": [{"id": admin["id"]}]}, token)
            if customer.get("isRoot"):
                raise RuntimeFailure("customer identity must not be the operational root")
            reset = api(base, "PUT", f"/api/v2/users/{customer['id']}/reset_password_tokens", {}, token)
            api(base, "POST", "/api/v2/factor_password/reset", {"token": reset["token"], "password": t["adminPassword"]})
            # Verify the actual customer session before publishing credentials.
            customer_session = api(base, "POST", "/api/v2/sessions/email_password", {"email": t["adminEmail"], "password": t["adminPassword"], "orgId": org_id})
            me = api(base, "GET", "/api/v2/users/me", token=customer_session["accessToken"])
            if me["isRoot"] or me["email"] != t["adminEmail"]:
                raise RuntimeFailure("customer bootstrap verification failed")
            api(base, "DELETE", "/api/v2/sessions", token=customer_session["accessToken"])
        finally:
            api(base, "DELETE", "/api/v2/sessions", token=token)
        t["orgId"], t["ready"] = org_id, True
        self.save(state)
        write_json(self.root / f"tenant-{t['slot']}" / "gateway.json", gateway_config(t))
        write_json(self.root / "credentials" / (subscription_id + ".json"), {
            "adminEmail": t["adminEmail"], "adminPassword": t["adminPassword"], "otlpToken": t["otlpToken"],
            "serviceUrl": t.get("serviceUrl", f"http://localhost:{24800 + t['slot']}"), "otlpProtocol": "http/protobuf",
        })

    def apply_retention(self, tenant):
        from .retention import retention_changes
        ns = tenant["namespace"]
        rows = json.loads(self.sql(f"SELECT database,name,engine,create_table_query FROM system.tables WHERE startsWith(database,'{ns}_') FORMAT JSON"))["data"]
        days = read_json(ABERA / "plans.json")["plans"][tenant["plan"]]["retentionDays"]
        queries = retention_changes(rows, ns, days)
        if queries:
            self.sql(";\n".join(queries), timeout=300)

    def observe(self):
        state = self.state
        rows = json.loads(self.sql("SELECT database, sum(bytes_on_disk) AS bytes FROM system.parts WHERE active GROUP BY database FORMAT JSON"))["data"]
        sizes = {row["database"]: int(row["bytes"]) for row in rows}
        # Docker's data disk is checked through ClickHouse, not the host CWD.
        disks = json.loads(self.sql("SELECT free_space,total_space FROM system.disks WHERE name='default' FORMAT JSON"))["data"]
        disk_healthy = bool(disks) and all(int(d["free_space"]) >= 5 * 1024**3 and int(d["free_space"]) >= int(d["total_space"]) * .15 for d in disks)
        for t in state["tenants"]:
            t["storageBytes"] = sum(size for name, size in sizes.items() if name.startswith(t["namespace"] + "_"))
            t["storageObservedAt"], t["diskHealthy"] = int(time.time()), disk_healthy
            target = self.root / f"tenant-{t['slot']}" / "gateway.json"
            write_json(target, gateway_config(t))
            if hasattr(os, "chown"):
                os.chown(target, 65532, 65532)
        # Observation does not overwrite concurrently committed lifecycle state.
        write_json(self.root / "observation.json", {"at": int(time.time()), "diskHealthy": disk_healthy,
                   "tenants": [{"subscriptionId": t["subscriptionId"], "storageBytes": t["storageBytes"]} for t in state["tenants"]]})

    def isolated_probe(self) -> dict:
        tenants = self.state["tenants"]
        passed = 0
        for source in tenants:
            reader = "r_" + source["namespace"][6:]
            # Avoid passwords on command lines: temporary client configs are
            # created in the container via stdin and removed in the same call.
            import xml.etree.ElementTree as ET
            client = ET.Element("config")
            ET.SubElement(client, "user").text = reader
            ET.SubElement(client, "password").text = source["readerPassword"]
            config = ET.tostring(client, encoding="unicode")
            for target in tenants:
                query = f"SELECT count() FROM {target['namespace']}_logs.distributed_logs_v2"
                result = subprocess.run(["docker", "compose", "-f", str(self.root / "compose.json"), "exec", "-T", "clickhouse", "sh", "-ec",
                                         'umask 077; f=$(mktemp); trap \'rm -f "$f"\' EXIT; cat > "$f"; clickhouse-client --config-file "$f" --query "$1"', "probe", query], input=config, text=True, capture_output=True, timeout=30)
                should_pass = source["namespace"] == target["namespace"]
                if (result.returncode == 0) != should_pass or (not should_pass and "ACCESS_DENIED" not in result.stderr):
                    raise RuntimeFailure(f"isolation probe failed for slots {source['slot']} -> {target['slot']}")
                passed += 1
            forbidden = ["SELECT name FROM system.users", "SELECT * FROM system.zookeeper WHERE path='/'",
                         "SELECT * FROM file('forbidden.csv','CSV','value String')",
                         f"CREATE TABLE {source['namespace']}_logs.forbidden(x UInt64) ENGINE=Memory"]
            for query in forbidden:
                result = subprocess.run(["docker", "compose", "-f", str(self.root / "compose.json"), "exec", "-T", "clickhouse", "sh", "-ec",
                    'umask 077; f=$(mktemp); trap \'rm -f "$f"\' EXIT; cat > "$f"; clickhouse-client --config-file "$f" --query "$1"', "probe", query], input=config, text=True, capture_output=True, timeout=30)
                if result.returncode == 0 or not any(code in result.stderr for code in ("ACCESS_DENIED", "QUERY_IS_PROHIBITED")):
                    raise RuntimeFailure(f"privilege isolation probe failed for slot {source['slot']}")
                passed += 1
        return {"checks": passed, "customers": len(tenants), "result": "PASS"}
