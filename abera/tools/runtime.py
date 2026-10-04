"""Local development and host operations for Abera SigNoz."""
from __future__ import annotations

import argparse
import json
import secrets
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from abera.runtime.host import Host
from abera.runtime.model import ABERA, new_tenant, write_json
from abera.runtime import backup, lifecycle


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ABERA / ".runtime" / "dev")
    sub = parser.add_subparsers(dest="action", required=True)
    init = sub.add_parser("init-dev")
    init.add_argument("--customers", type=int, choices=range(1, 5), default=4)
    init.add_argument("--pin-cpu", action="store_true")
    sub.add_parser("render")
    sub.add_parser("up")
    observe = sub.add_parser("observe")
    observe.add_argument("--watch", action="store_true")
    sub.add_parser("verify-isolation")
    for action in ("backup", "restore", "suspend", "reactivate"):
        command = sub.add_parser(action, help="synthetic local tenants only")
        command.add_argument("subscription")
        if action == "restore":
            command.add_argument("--snapshot", type=Path, required=True)
    args = parser.parse_args()
    host = Host(args.root)
    if args.action in {"backup", "restore", "suspend", "reactivate"}:
        if not host.state.get("local") or not args.subscription.startswith("development-"):
            raise SystemExit("Use the Automations API for managed subscriptions; this command accepts local synthetic tenants only.")
    if args.action == "init-dev":
        if host.state_path.exists():
            raise SystemExit("Development state already exists; identities and credentials were preserved.")
        now = int(time.time())
        state = {"schemaVersion": 1, "local": True, "pinCPU": args.pin_cpu, "project": "abera-signoz-dev",
                 "masterPassword": secrets.token_hex(32), "disabledDefaultPassword": secrets.token_hex(32), "tenants": []}
        for slot in range(1, args.customers + 1):
            state["tenants"].append(new_tenant(f"development-{slot}", f"customer-{slot}@example.invalid", "lite", slot,
                                   [{"cycleId": "synthetic-cycle-1", "startsAt": now, "endsAt": now + 31 * 86400, "plan": "lite"}], 1))
        host.save(state)
        host.render()
        print("Synthetic development identities created. Secrets remain in the ignored runtime directory.")
    elif args.action == "render":
        print(host.render())
    elif args.action == "up":
        host.up()
        print("Development apps provisioned. Credentials are in the private runtime/credentials directory.")
    elif args.action == "observe":
        while True:
            host.observe()
            if not args.watch:
                break
            time.sleep(30)
    elif args.action == "verify-isolation":
        print(json.dumps(host.isolated_probe()))
    elif args.action == "backup":
        print(backup.capture(host, args.subscription, "manual-"+secrets.token_hex(12)))
    elif args.action == "restore":
        print(json.dumps(backup.restore(host, args.subscription, args.snapshot)))
    elif args.action in {"suspend", "reactivate"}:
        print(json.dumps(lifecycle.set_state(host, args.subscription, "ACTIVE" if args.action == "reactivate" else "SUSPENDED")))


if __name__ == "__main__":
    main()
