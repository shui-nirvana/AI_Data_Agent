"""构建固定作业镜像并可选导出离线开发包，不含云 SDK 或运行许可自动接受。"""

import argparse
import json
from pathlib import Path
import zipfile

from backend.jobs.contracts import Stage


def bundle_sources(root: Path, target: Path, manifest: dict) -> None:
    """白名单打包源码，明确排除密钥、任务数据、缓存、Git 和用户 CSV。"""
    roots = ("backend", "workers", "images", "web", "deploy")
    files = [root / name for name in ("pyproject.toml", "README.md", ".env.example")]
    for name in roots:
        files.extend(path for path in (root / name).rglob("*")
                     if path.is_file() and "__pycache__" not in path.parts
                     and (path.suffix in (".py", ".md", ".html", ".css", ".js")
                          or path.name == "Dockerfile.job"))
    files.extend(path for path in (root / "deploy").glob("*")
                 if path.suffix in (".lock", ".in"))
    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(set(files)):
            relative = path.relative_to(root)
            # 不跟随源码目录中的链接/Windows junction，避免带出工作区外文件。
            ancestors = [root / Path(*relative.parts[:index])
                         for index in range(1, len(relative.parts) + 1)]
            if not any(item.is_symlink() or (
                hasattr(item, "is_junction") and item.is_junction()
            ) for item in ancestors):
                archive.write(path, path.relative_to(root))
        archive.writestr("deployment.json", json.dumps(manifest, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="构建通用 Linux 作业镜像与开发部署包")
    parser.add_argument("--target", choices=("local", "vultr", "aws"), default="local")
    parser.add_argument("--platform", choices=("linux/amd64", "linux/arm64"),
                        default="linux/amd64", help="必须与目标 VM 的 CPU 架构一致")
    parser.add_argument("--output", type=Path, default=Path(".runtime/docker"))
    parser.add_argument("--export", action="store_true", help="导出镜像 tar 和白名单源码 zip")
    parser.add_argument("--base", default="python@sha256:aa943ce411423c7582288f18707ff66eddcbaeb510502d747a72aeb34f30d159",
                        help="Trusted Python base image pinned by digest")
    args = parser.parse_args()
    import docker
    client = docker.from_env(timeout=120)
    if client.info().get("OSType") != "linux":
        raise SystemExit("需要 Linux Docker 引擎")
    root = Path(__file__).resolve().parents[2]
    # 构建时网络允许；执行时只用本地 ID，不自动拉取镜像/依赖。
    import re
    if not re.fullmatch(r"[a-zA-Z0-9./_-]+@sha256:[0-9a-f]{64}", args.base):
        raise SystemExit("基础镜像必须使用不可变摘要")
    base = client.images.pull(args.base, platform=args.platform)
    digests = base.attrs.get("RepoDigests", [])
    if not digests:
        raise SystemExit("未获取基础镜像摘要")
    image, _ = client.images.build(
        path=str(root), dockerfile="deploy/Dockerfile.job",
        buildargs={"PYTHON_BASE": digests[0]}, tag="ai-data-agent-job:dev",
        rm=True, pull=False, platform=args.platform,
    )
    if image.attrs.get("Architecture") != args.platform.split("/")[1]:
        raise SystemExit("构建镜像 CPU 架构与目标不匹配")
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
    print(f"镜像/部署配置已写入 {args.output}；目标平台尚未验收。")


if __name__ == "__main__":
    main()
