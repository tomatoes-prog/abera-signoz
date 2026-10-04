"""Render one shared ClickHouse and up to four independent Community runtimes."""
from __future__ import annotations

import hashlib
import copy
import json
import os
import xml.etree.ElementTree as ET
from pathlib import Path

from .model import ABERA, DATABASE_SUFFIXES, namespace, read_json, write_json


def element(parent, name, text=None, **attributes):
    child = ET.SubElement(parent, name, attributes)
    if text is not None:
        child.text = str(text)
    return child


def write_xml(path: Path, root) -> None:
    ET.indent(root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(ET.tostring(root, encoding="unicode"), encoding="utf-8")
    path.chmod(0o600)


def clickhouse_files(root: Path, state: dict) -> None:
    config = ET.fromstring((ABERA / "docker" / "clickhouse.xml").read_text())
    access = element(config, "access_control_improvements")
    for key in ("select_from_system_db_requires_grant", "select_from_information_schema_requires_grant", "on_cluster_queries_require_cluster_grant"):
        element(access, key, "true")
    config.remove(config.find("remote_servers"))
    remotes = element(config, "remote_servers")
    # Administrator cluster is used only for DDL. Distributed engines are
    # patched at source to use <namespace>_data, with per-tenant credentials.
    for name, user, password in [("abera_admin", "operator", state["masterPassword"])] + [
        (t["namespace"] + "_data", "d_" + t["namespace"][6:], t["dataPassword"]) for t in state["tenants"]
    ]:
        replica = element(element(element(remotes, name), "shard"), "replica")
        for key, value in {"host": "clickhouse", "port": 9000, "user": user, "password": password}.items():
            element(replica, key, value)
    element(config, "user_defined_executable_functions_config", "/etc/clickhouse-server/abera-functions.xml")
    element(element(config, "backups"), "allowed_path", "/var/lib/clickhouse/abera-backups")
    write_xml(root / "clickhouse" / "abera.xml", config)
    users_root = ET.Element("clickhouse")
    profiles = element(users_root, "profiles")
    profile = element(profiles, "tenant")
    limits = {"max_threads": 1, "max_insert_threads": 1, "max_memory_usage": 268435456,
              "max_execution_time": 15, "max_result_rows": 100000, "max_result_bytes": 16777216,
              "max_concurrent_queries_for_user": 2, "allow_ddl": 0, "allow_introspection_functions": 0}
    constraints = element(profile, "constraints")
    for key, value in limits.items():
        element(profile, key, value)
        bound = element(constraints, key)
        element(bound, "min", 1 if value > 0 else 0)
        element(bound, "max", value)
    # clickhouse-go adds a timeout allowance to insert contexts, and the
    # exporter writes primary rows and metadata concurrently. Reader limits
    # remain strict; exporter work still has one thread and a bounded budget.
    writer_profile = copy.deepcopy(profile)
    writer_profile.tag = "tenant_writer"
    for key, value in {"max_execution_time": 60, "max_concurrent_queries_for_user": 8}.items():
        writer_profile.find(key).text = str(value)
        writer_profile.find("constraints/" + key + "/max").text = str(value)
    profiles.append(writer_profile)
    users = element(users_root, "users")
    default = element(users, "default")
    # No empty-password network account remains available to containers.
    element(default, "password", state["disabledDefaultPassword"])
    networks = element(default, "networks", replace="replace")
    element(networks, "ip", "127.0.0.1")
    operator = element(users, "operator")
    element(operator, "password_sha256_hex", hashlib.sha256(state["masterPassword"].encode()).hexdigest())
    element(element(operator, "networks"), "ip", "::/0")
    element(operator, "profile", "default")
    element(operator, "access_management", 1)
    element(operator, "quota", "default")
    for tenant in state["tenants"]:
        ns = tenant["namespace"]
        if ns != namespace(tenant["subscriptionId"], tenant["generation"]):
            raise ValueError("namespace does not match the assigned tenant")
        for prefix, password_key in (("r_", "readerPassword"), ("w_", "writerPassword"), ("d_", "dataPassword")):
            account = element(users, prefix + ns[6:])
            element(account, "password_sha256_hex", hashlib.sha256(tenant[password_key].encode()).hexdigest())
            element(element(account, "networks"), "ip", "::/0")
            element(account, "profile", "tenant" if prefix == "r_" else "tenant_writer")
            element(account, "quota", "default")
            grants = element(account, "grants")
            for suffix in DATABASE_SUFFIXES:
                privileges = "SELECT" if prefix == "r_" else "SELECT, INSERT"
                element(grants, "query", f"GRANT {privileges} ON {ns}_{suffix}.*")
            if prefix == "r_":
                element(grants, "query", f"GRANT INSERT ON {ns}_analytics.*")
                element(grants, "query", f"GRANT INSERT ON {ns}_metrics.updated_metadata")
            # Object-listing tables apply ClickHouse's SHOW privilege filters.
            # Other diagnostic tables (queries, users, Keeper, caches) stay denied.
            for table in ("tables", "columns", "databases", "data_skipping_indices"):
                element(grants, "query", f"GRANT SELECT ON system.{table}")
            if prefix == "r_":
                element(grants, "query", "GRANT SELECT(name,type) ON system.disks")
    write_xml(root / "clickhouse" / "users.xml", users_root)
    client = ET.Element("config")
    element(client, "user", "operator")
    element(client, "password", state["masterPassword"])
    write_xml(root / "clickhouse" / "client.xml", client)


def collector_config(tenant: dict) -> dict:
    ns = tenant["namespace"]
    dsn = f"tcp://w_{ns[6:]}:{tenant['writerPassword']}@clickhouse:9000"
    # The durable gateway owns retries. A collector ACK follows synchronous
    # exporter completion; no volatile batch processor can acknowledge early.
    delivery = {"sending_queue": {"enabled": False}, "retry_on_failure": {"enabled": False}, "timeout": "15s"}
    return {
        "receivers": {"otlp": {"protocols": {"http": {"endpoint": "0.0.0.0:4318"}}}},
        "processors": {
            "memory_limiter": {"check_interval": "1s", "limit_mib": 192, "spike_limit_mib": 32},
            "signozspanmetrics/delta": {"metrics_exporter": "signozclickhousemetrics", "metrics_flush_interval": "60s",
                                       "dimensions_cache_size": 1000, "aggregation_temporality": "AGGREGATION_TEMPORALITY_DELTA",
                                       "enable_exp_histogram": True, "latency_histogram_buckets": ["1ms", "10ms", "100ms", "1s", "10s"]},
        },
        "extensions": {"health_check": {"endpoint": "0.0.0.0:13133"}},
        "exporters": {
            "clickhousetraces": {**delivery, "datasource": dsn + "/" + ns + "_traces", "use_new_schema": True},
            "clickhouselogsexporter": {**delivery, "dsn": dsn + "/" + ns + "_logs", "use_new_schema": True,
                                      "max_allowed_data_age_days": read_json(ABERA / "plans.json")["plans"][tenant["plan"]]["retentionDays"]},
            "signozclickhousemetrics": {**delivery, "dsn": dsn, "database": ns + "_metrics", "enable_exp_hist": True},
        },
        "service": {"extensions": ["health_check"], "telemetry": {"logs": {"level": "warn", "encoding": "json"}},
                    "pipelines": {signal: {"receivers": ["otlp"], "processors": ["memory_limiter"] + (["signozspanmetrics/delta"] if signal == "traces" else []), "exporters": [exporter]}
                                  for signal, exporter in (("traces", "clickhousetraces"), ("logs", "clickhouselogsexporter"), ("metrics", "signozclickhousemetrics"))}},
    }


def gateway_config(tenant: dict) -> dict:
    slot = tenant["slot"]
    return {key: tenant[key] for key in ("subscriptionId", "namespace", "revision", "state", "ready", "terms")} | {
        "tokenSHA256": hashlib.sha256(tenant["otlpToken"].encode()).hexdigest(),
        "appURL": f"http://app-{slot}:8080", "collectorURL": f"http://collector-{slot}:4318",
        "storageBytes": tenant.get("storageBytes", 0), "storageObservedAt": tenant.get("storageObservedAt", 0),
        "diskHealthy": tenant.get("diskHealthy", False),
    }


def render(root: Path, state: dict, *, local: bool = True, pin_cpu: bool = False) -> Path:
    if len(state["tenants"]) > 4 or len({t["slot"] for t in state["tenants"]}) != len(state["tenants"]):
        raise ValueError("at most four unique customer slots are allowed")
    root = root.resolve()
    root.mkdir(parents=True, exist_ok=True)
    clickhouse_files(root, state)
    images = read_json(ABERA / "release.json")["images"]
    app_image = state.get("appImage", "abera/signoz-community:dev")
    collector_image = state.get("collectorImage", "abera/signoz-collector:dev")
    common = {"security_opt": ["no-new-privileges:true"], "restart": "unless-stopped",
              "logging": {"driver": "local", "options": {"max-size": "5m", "max-file": "2"}}}
    if pin_cpu:
        common["cpuset"] = "0"
    networks = {"database": {"internal": True}}
    volumes = {"clickhouse": {}, "keeper": {}}
    services = {
        "keeper": {**common, "image": state.get("keeperImage", images["keeper"]), "mem_limit": "256m", "networks": ["database"],
                   "volumes": ["keeper:/var/lib/clickhouse", f"{ABERA / 'docker/keeper.xml'}:/etc/clickhouse-keeper/keeper_config.xml:ro"]},
        "clickhouse": {**common, "image": state.get("clickhouseImage", "abera/signoz-clickhouse:dev"), "mem_limit": "3g", "networks": ["database"],
                       "environment": {"CLICKHOUSE_SKIP_USER_SETUP": "1"},
                       "volumes": ["clickhouse:/var/lib/clickhouse", f"{root / 'clickhouse/abera.xml'}:/etc/clickhouse-server/config.d/abera.xml:ro",
                                   f"{root / 'clickhouse/users.xml'}:/etc/clickhouse-server/users.d/abera.xml:ro",
                                   f"{root / 'clickhouse/client.xml'}:/etc/clickhouse-server/abera-client.xml:ro"],
                       "healthcheck": {"test": ["CMD", "clickhouse-client", "--config-file", "/etc/clickhouse-server/abera-client.xml", "--query", "SELECT 1"], "interval": "5s", "timeout": "3s", "retries": 30},
                       "ulimits": {"nofile": {"soft": 262144, "hard": 262144}}, "depends_on": ["keeper"]},
    }
    for t in state["tenants"]:
        slot, ns = t["slot"], t["namespace"]
        app_image = t.get("appImage", state.get("appImage", "abera/signoz-community:dev"))
        collector_image = t.get("collectorImage", state.get("collectorImage", "abera/signoz-collector:dev"))
        private = root / f"tenant-{slot}"
        write_json(private / "collector.json", collector_config(t))
        write_json(private / "gateway.json", gateway_config(t))
        write_json(private / "plans.json", read_json(ABERA / "plans.json"))
        # Linux non-root containers can read their own config directory only.
        for file in private.iterdir():
            if hasattr(os, "chown"):
                os.chown(file, 65532, 65532)
        network = f"tenant-{slot}"
        networks[network] = {"internal": True}
        networks[f"edge-{slot}"] = {}
        services["clickhouse"]["networks"].append(network)
        env = {"ABERA_REQUIRE_NAMESPACE": "true", "ABERA_TELEMETRY_NAMESPACE": ns, "GOMAXPROCS": "1"}
        migrate = f"migrate-{slot}"
        services[migrate] = {**common, "image": collector_image, "mem_limit": "512m", "networks": ["database"], "restart": "no",
                            "entrypoint": ["/bin/sh", "-ec"],
                            "command": ["/signoz-otel-collector migrate bootstrap && /signoz-otel-collector migrate sync up && /signoz-otel-collector migrate async up"],
                            "environment": {**env, "SIGNOZ_OTEL_COLLECTOR_CLICKHOUSE_DSN": f"tcp://operator:{state['masterPassword']}@clickhouse:9000",
                                            "SIGNOZ_OTEL_COLLECTOR_CLICKHOUSE_CLUSTER": "abera_admin", "SIGNOZ_OTEL_COLLECTOR_CLICKHOUSE_REPLICATION": "false", "SIGNOZ_OTEL_COLLECTOR_TIMEOUT": "10m"},
                            "depends_on": {"clickhouse": {"condition": "service_healthy"}}}
        services[f"app-{slot}"] = {**common, "image": app_image, "mem_limit": "512m", "networks": [network, f"edge-{slot}"], "cap_drop": ["ALL"],
                                   "environment": {**env, "GOMEMLIMIT": "384MiB", "SIGNOZ_TELEMETRYSTORE_CLICKHOUSE_DSN": f"tcp://r_{ns[6:]}:{t['readerPassword']}@clickhouse:9000",
                                                   "SIGNOZ_TELEMETRYSTORE_CLICKHOUSE_CLUSTER": ns + "_data", "SIGNOZ_GLOBAL_EXTERNAL_URL": t.get("serviceUrl", f"http://localhost:{24800+slot}")},
                                   "volumes": [f"app-{slot}:/var/lib/signoz"], "ports": [f"127.0.0.1:{25800+slot}:8080"],
                                   "depends_on": {migrate: {"condition": "service_completed_successfully"}}}
        services[f"collector-{slot}"] = {**common, "image": collector_image, "mem_limit": "256m", "networks": [network], "cap_drop": ["ALL"],
                                         "environment": {**env, "GOMEMLIMIT": "192MiB"}, "command": ["--config=/etc/abera/collector.json"],
                                         "volumes": [f"{private}:/etc/abera:ro"], "depends_on": {migrate: {"condition": "service_completed_successfully"}}}
        services[f"gateway-{slot}"] = {**common, "image": app_image, "mem_limit": "128m", "networks": [network, f"edge-{slot}"], "cap_drop": ["ALL"],
                                       "entrypoint": ["/usr/local/bin/abera-gateway"], "command": [], "environment": {"GOMEMLIMIT": "96MiB", "GOMAXPROCS": "1"},
                                       "volumes": [f"gateway-{slot}:/var/lib/abera", f"{private}:/etc/abera:ro"],
                                       "ports": [f"{'127.0.0.1' if local else '0.0.0.0'}:{24800+slot}:8081"],
                                       "healthcheck": {"test": ["CMD", "wget", "-q", "-O", "/dev/null", "http://127.0.0.1:8081/abera/health"], "interval": "15s", "timeout": "3s", "retries": 3},
                                       "depends_on": {f"app-{slot}": {"condition": "service_healthy"}}}
        volumes[f"app-{slot}"] = {}
        volumes[f"gateway-{slot}"] = {}
    output = root / "compose.json"
    write_json(output, {"name": state.get("project", "abera-signoz-dev"), "services": services, "volumes": volumes, "networks": networks})
    return output
