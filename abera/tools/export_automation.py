"""Stage exact, private-data-free Docker build contexts for Automations publishing."""
import argparse
import hashlib
import json
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def export(automation):
    product = Path(automation).resolve() / "products" / "abera-signoz"
    if not (product / "product.yaml").is_file():
        raise ValueError("target is not the Abera SigNoz product directory")
    names = subprocess.check_output(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"], cwd=ROOT).decode().split("\0")
    excluded = {".git", ".aws", ".codex", ".agents", ".runtime", "results", "__pycache__", "node_modules", "ee", "build", "dist", "target"}
    files = []
    for name in names:
        path = Path(name)
        if not name or set(path.parts) & excluded or name.startswith("cmd/enterprise/") or path.name.startswith(".env"):
            continue
        source = ROOT / path
        if source.is_file() and not source.is_symlink():
            files.append(path)
    contexts = product / "image-build"
    for kind in ("app", "collector", "clickhouse", "admin"):
        directory = contexts / kind
        resolved = directory.resolve()
        if resolved.parent != contexts.resolve() or not resolved.is_relative_to(product):
            raise ValueError("context path escaped product directory")
        # Only generated files inside this explicitly designated context are replaced.
        if directory.exists():
            shutil.rmtree(directory)
        directory.mkdir(parents=True)
        selected = files if kind == "app" else [p for p in files if str(p).replace('\\','/').startswith("abera/")]
        if kind in {"collector", "clickhouse"}:
            selected += [Path("pkg/abera/namespace/namespace.go"), Path("scripts/clickhouse/histogramquantile/main.go"), Path("LICENSE")]
        for path in selected:
            destination = directory / path
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(ROOT / path, destination)
        dockerfile = ROOT / "abera/docker" / ("Dockerfile.community" if kind in {"app", "collector"} else "Dockerfile." + ("controller" if kind == "admin" else "clickhouse"))
        content = dockerfile.read_text(encoding="utf-8")
        if kind == "collector":
            # Keep collector-source/build/runtime; the next Go stage is backend.
            content = dockerfile.read_text().split(" AS backend", 1)[0].rsplit("FROM --platform=$BUILDPLATFORM", 1)[0]
        (directory / "Dockerfile").write_text(content, encoding="utf-8")
        shutil.copy2(ROOT / "abera/docker/Dockerfile.community.dockerignore", directory / ".dockerignore")
    source_hash = hashlib.sha256()
    for path in sorted(files):
        source_hash.update(path.as_posix().encode() + b"\0" + (ROOT/path).read_bytes())
    (contexts / "source.json").write_text(json.dumps({"sourceSha256": source_hash.hexdigest(), "files": len(files)}, indent=2))
    print(json.dumps({"staged": str(contexts), "sourceSha256": source_hash.hexdigest(), "files": len(files)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--automations", type=Path, required=True)
    export(parser.parse_args().automations)
