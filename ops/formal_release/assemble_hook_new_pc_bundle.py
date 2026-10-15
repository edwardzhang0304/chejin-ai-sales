"""Assemble a private new-PC Hook bundle around an already accepted Worker ZIP.

The WeChat installer and Hook DLL are supplied through a controlled channel.
Neither asset belongs in the public source repository or public CI artifacts.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import zipfile
from pathlib import Path


SHA256_RE = re.compile(r"[0-9a-fA-F]{64}\Z")
VERSION_RE = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+\Z")


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verified_file(path: Path, expected: str, label: str) -> tuple[Path, str]:
    if not SHA256_RE.fullmatch(expected):
        raise ValueError(f"{label}: expected SHA-256 must be 64 hex characters")
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"{label}: regular input file required")
    actual = sha256(path)
    if actual != expected.lower():
        raise ValueError(f"{label}: SHA-256 mismatch")
    return path, actual


def worker_identity(path: Path) -> tuple[str, str]:
    if not path.is_file() or path.is_symlink():
        raise ValueError("Worker ZIP: regular input file required")
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        manifests = [name for name in names if name == "CheJinWorkerClient/update-package-manifest.json"]
        if len(manifests) != 1:
            raise ValueError("Worker ZIP: missing single official update manifest")
        required = {
            "CheJinWorkerClient/CheJinWorkerClient.exe",
            "CheJinWorkerClient/CheJinUpdater.exe",
            "CheJinWorkerClient/start-hook.ps1",
            "CheJinWorkerClient/Setup-CheJin-Hook.cmd",
        }
        if not required.issubset(names):
            raise ValueError("Worker ZIP: missing Hook launcher or signed executables")
        manifest = json.loads(archive.read(manifests[0]))
        version = manifest.get("version")
        if not isinstance(version, str) or not VERSION_RE.fullmatch(version):
            raise ValueError("Worker ZIP: invalid product version")
        if manifest.get("transport") != "hook":
            raise ValueError("Worker ZIP: transport is not Hook")
        if any(name.lower().endswith(("/wechat.exe", "/version.dll")) for name in names):
            raise ValueError("Worker ZIP: unexpected WeChat or Hook binary already embedded")
        return version, str(manifest.get("git_commit") or "")


def assemble(args: argparse.Namespace) -> Path:
    worker = Path(args.worker_zip)
    installer, installer_hash = verified_file(
        Path(args.wechat_installer), args.wechat_installer_sha256, "WeChat installer"
    )
    dll, dll_hash = verified_file(
        Path(args.hook_dll), args.hook_dll_sha256, "Hook DLL"
    )
    if installer.suffix.lower() not in {".exe", ".msi"}:
        raise ValueError("WeChat installer: expected .exe or .msi")
    if dll.name.lower() != "version.dll":
        raise ValueError("Hook DLL must be named version.dll")
    if not re.fullmatch(r"[0-9]+(?:\.[0-9]+){3}", args.wechat_file_version):
        raise ValueError("Expected installed WeChat file version must have four parts")
    version, commit = worker_identity(worker)
    worker_hash = sha256(worker)
    output_dir = Path(args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    output = output_dir / f"CheJinHookFullSetup-v{version}-windows-x64.zip"
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing bundle: {output}")
    inputs = [worker.resolve(), installer.resolve(), dll.resolve()]
    if len(set(inputs)) != len(inputs):
        raise ValueError("Bundle inputs must be different files")
    manifest = {
        "schema_version": 1,
        "product_version": version,
        "worker_git_commit": commit,
        "transport": "hook",
        "distribution": "private_new_pc_installation",
        "expected_installed_wechat_file_version": args.wechat_file_version,
        "expected_wechat_architecture": "x86",
        "files": {
            f"worker/{worker.name}": worker_hash,
            f"wechat/{installer.name}": installer_hash,
            "hook/version.dll": dll_hash,
        },
        "windows_live_acceptance": "pending",
    }
    instructions = (
        f"车金 Hook {version} 新电脑完整包\n\n"
        "1. 安装包内的微信，并在实际安装后的 WeChat.exe 核对文件版本 "
        f"{args.wechat_file_version} 与 x86/32 位；不符合即停止。\n"
        "2. 关闭该微信进程。核对 hook/version.dll 的 SHA-256 与 bundle-manifest.json 一致，"
        "然后放在该 WeChat.exe 同目录。\n"
        "3. 解压 worker 子目录内的原始 Worker ZIP。为本机单独准备不含 Token/API Key 的 profile.json；"
        "api_base_url 使用正式 HTTPS，端口取本机部署配置，不照抄其他电脑。\n"
        "4. 运维将本机专用 profile.json 放在 Worker 解压目录。确认微信语音自动转文字已关闭，"
        "再双击 Setup-CheJin-Hook.cmd；销售无需填写命令或端口。\n"
        "5. 在 Windows 实际点击生成的工作微信和 Worker 两个快捷方式；核对 StartPort=<配置端口>、"
        "ConnectType=http、RecvType=0，DLL 已加载、端口归属正确、Worker 环境检查通过。"
        "未完成这些实机验收不得标记交付通过。\n"
        "本完整包只供受控测试机分发；不要上传第三方微信与 Hook 原件到公开仓库。\n"
    )
    temporary = output.with_suffix(output.suffix + ".tmp")
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            archive.write(worker, f"worker/{worker.name}")
            archive.write(installer, f"wechat/{installer.name}")
            archive.write(dll, "hook/version.dll")
            archive.writestr("bundle-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
            archive.writestr("INSTALL-README.txt", instructions.encode("utf-8-sig"))
        os.replace(temporary, output)
    finally:
        temporary.unlink(missing_ok=True)
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--worker-zip", required=True)
    parser.add_argument("--wechat-installer", required=True)
    parser.add_argument("--wechat-installer-sha256", required=True)
    parser.add_argument("--hook-dll", required=True)
    parser.add_argument("--hook-dll-sha256", required=True)
    parser.add_argument("--wechat-file-version", required=True)
    parser.add_argument("--output-dir", required=True)
    args = parser.parse_args()
    output = assemble(args)
    print(json.dumps({"bundle": str(output), "sha256": sha256(output)}, ensure_ascii=False))


if __name__ == "__main__":
    main()
