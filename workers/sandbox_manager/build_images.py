"""Build a fixed job image and optionally export an offline development package without cloud SDKs or automatic acceptance of runtime licenses."""

import argparse
import json
from pathlib import Path
import zipfile

from backend.jobs.contracts import Stage


def bundle_sources(root: Path, target: Path, manifest: dict) -> None:
    """Package allowlisted source while excluding credentials, task data, caches, Git, and user CSVs."""
    roots = ("backend", "workers", "images", "web")
    files = [root / name for name in (
        "pyproject.toml", "README.md", "LICENSE", ".env.example", ".dockerignore", "start.py",
        "start_linux.py"
    ) if (root / name).is_file()]
    for name in roots:
        files.extend(path for path in (root / name).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and path.suffix in (".py", ".html", ".css", ".js"))
    files.extend(root / "docs" / name for name in (
        "architecture.md", "security.md", "operations.md", "testing.md"
    ) if (root / "docs" / name).is_file())
    files.extend(root / "deploy" / name for name in (
        "Dockerfile.job", "nginx.conf.example"
    ) if (root / "deploy" / name).is_file())
    files.extend(path for path in (root / "deploy").glob("*")
                 if path.suffix in (".lock", ".in"))
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            relative = path.relative_to(root)
            # Do not follow source-tree links or Windows junctions outside the workspace.
            ancestors = [root / Path(*relative.parts[:index])
                         for index in range(1, len(relative.parts) + 1)]
            if not any(item.is_symlink() or (
                hasattr(item, "is_junction") and item.is_junction()
            ) for item in ancestors):
                archive.write(path, path.relative_to(root))
        archive.writestr("deployment.json", json.dumps(manifest, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Linux job image and development deployment bundle")
    parser.add_argument("--target", choices=("local", "vultr", "aws"), default="local")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                        default="linux/amd64", help="Must match the target VM CPU architecture")
    parser.add_argument("--output", type=Path, default=Path(".runtime/docker"))
    parser.add_argument("--export", action="store_true", help="Export the image tar and allowlisted source zip")
    parser.add_argument("--base", default="python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159",
                        help="Trusted Python base image pinned by digest")
    args = parser.parse_args()
    import docker
    client = docker.from_env(timeout=120)
    if client.info().get("OSType") != "linux":
        raise SystemExit("A Linux Docker engine is required")
    root = Path(__file__).resolve().parents[2]
    # Networking is allowed for builds; execution uses local image IDs and never pulls images or dependencies.
    import re
    if not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", args.base):
        raise SystemExit("The base image must use an immutable digest")
    base = client.images.pull(args.base, platform=args.platform)
    digests = base.attrs.get("RepoDigests", [])
    if not digests:
        raise SystemExit("Could not obtain the base image digest")
    image, _ = client.images.build(
        path=str(root), dockerfile="deploy/Dockerfile.job",
        buildargs={"PYTHON_BASE": digests[0]}, tag="ai-data-agent-job:dev",
        rm=True, pull=False, platform=args.platform,
    )
    if image.attrs.get("Architecture") != args.platform.split("/")[1]:
        raise SystemExit("The built image CPU architecture does not match the target")
    manifest = {
        "schema_version": "1.0", "deployment_platform": args.target,
        "execution_backend": "docker", "base_digest": digests[0],
        "architecture": image.attrs.get("Architecture"), "platform": args.platform,
        "images": {stage.value: image.id for stage in Stage},
        "tested_on_target": False,
        "execution_exposed_by_api": False,
    }
    args.output.mkdir(parents=True, exist_ok=True)
    (args.output / "images.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    if args.export:
        with (args.output / "job-image.tar").open("wb") as output:
            for chunk in image.save(named=False):
                output.write(chunk)
        bundle_sources(root, args.output / "application.zip", manifest)
    print(f"Image/deployment configuration written to {args.output}; the target platform has not been accepted.")


if __name__ == "__main__":
    main()
