from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import tempfile
from pathlib import Path

ABERA = Path(__file__).resolve().parents[1]
DATABASE_SUFFIXES = ("logs", "traces", "metrics", "metadata", "analytics", "meter", "audit")
IDENTITY = re.compile(r"^[a-z0-9][a-z0-9-]{1,46}[a-z0-9]$")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    os.close(descriptor)
    temporary = Path(name)
    try:
        with temporary.open("w", encoding="utf-8", newline="\n") as stream:
            os.chmod(temporary, 0o600)
            json.dump(value, stream, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def namespace(subscription_id: str, generation: str) -> str:
    if not IDENTITY.fullmatch(subscription_id) or not re.fullmatch(r"[a-f0-9]{32}", generation):
        raise ValueError("invalid subscription identity or generation")
    return "abera_" + hashlib.sha256(f"{subscription_id}:{generation}".encode()).hexdigest()[:20]


def new_tenant(subscription_id: str, email: str, plan: str, slot: int, terms: list[dict], revision: int) -> dict:
    plans = read_json(ABERA / "plans.json")["plans"]
    if plan not in plans or not 1 <= slot <= 4 or revision < 1:
        raise ValueError("invalid plan, slot or billing revision")
    if not re.fullmatch(r"[^\s@<>]{1,64}@[^\s@<>]+\.[^\s@<>]+", email) or len(email) > 254:
        raise ValueError("invalid admin email")
    generation = secrets.token_hex(16)
    ns = namespace(subscription_id, generation)
    return {
        "subscriptionId": subscription_id, "generation": generation, "namespace": ns,
        "slot": slot, "adminEmail": email, "plan": plan, "revision": revision,
        "terms": terms, "state": "ACTIVE", "ready": False,
        "readerPassword": secrets.token_hex(32), "writerPassword": secrets.token_hex(32),
        "dataPassword": secrets.token_hex(32), "rootPassword": secrets.token_urlsafe(32) + "Aa1!",
        "adminPassword": secrets.token_urlsafe(32) + "Aa1!", "otlpToken": secrets.token_urlsafe(32),
    }
