"""Synthetic local integration test. Never runs against paid/customer tenants."""
import json
import secrets
import sys
import time
import urllib.error
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from abera.runtime.host import Host, RuntimeFailure, api
from abera.runtime.model import ABERA, write_json


def run(root):
    host = Host(root)
    state = host.state
    if not state.get("local") or any(not t["subscriptionId"].startswith("development-") for t in state["tenants"]):
        raise RuntimeFailure("the smoke test only accepts synthetic local tenants")
    marker = "smoke_" + secrets.token_hex(8)
    host.observe()
    for tenant in state["tenants"]:
        resource = {"attributes": [{"key": "service.name", "value": {"stringValue": marker}}]}
        nanos = time.time_ns()
        payloads = {
            "logs": {"resourceLogs": [{"resource": resource, "scopeLogs": [{"logRecords": [{"timeUnixNano": str(nanos), "body": {"stringValue": marker}}]}]}]},
            "traces": {"resourceSpans": [{"resource": resource, "scopeSpans": [{"spans": [{"traceId": secrets.token_hex(16), "spanId": secrets.token_hex(8), "name": marker, "kind": 2, "startTimeUnixNano": str(nanos), "endTimeUnixNano": str(nanos + 1000000)}]}]}]},
            "metrics": {"resourceMetrics": [{"resource": resource, "scopeMetrics": [{"metrics": [{"name": marker, "gauge": {"dataPoints": [{"timeUnixNano": str(nanos), "asDouble": 1.0}]}}]}]}]},
        }
        base = f"http://127.0.0.1:{24800+tenant['slot']}"
        for signal, body in payloads.items():
            api(base, "POST", "/v1/" + signal, body, tenant["otlpToken"])
        other = next(t for t in state["tenants"] if t != tenant)
        try:
            api(base, "POST", "/v1/logs", payloads["logs"], other["otlpToken"])
        except RuntimeFailure as exc:
            assert "401" in str(exc)
        else:
            raise RuntimeFailure("another tenant token was accepted")
    # Verify accepted OTLP reaches actual tables, not just the durable queue.
    counts = []
    for tenant in state["tenants"]:
        for signal, table, column in [("logs", "logs_v2", "body"), ("traces", "signoz_index_v3", "name"), ("metrics", "samples_v4", "metric_name")]:
            deadline = time.monotonic() + 90
            while True:
                value = int(host.sql(f"SELECT count() FROM {tenant['namespace']}_{signal}.{table} WHERE {column}='{marker}'"))
                if value == 1:
                    break
                if time.monotonic() > deadline:
                    raise RuntimeFailure(f"{signal} delivery failed for slot {tenant['slot']}: {value}")
                time.sleep(2)
            counts.append({"slot": tenant["slot"], "signal": signal, "rows": value})
    report = {"result": "PASS", "at": int(time.time()), "ingestion": counts, "foreignTokensRejected": len(state["tenants"]), "databaseIsolation": host.isolated_probe()}
    write_json(ABERA / "results" / "smoke.json", report)
    return report


if __name__ == "__main__":
    print(json.dumps(run(ABERA / ".runtime" / "dev")))
