"""Build an OS-native, self-contained user installation archive.

Traceability: PRD-TECH-001; ACC-001/011/012.
Run on each target OS/architecture with the locked bundle extra.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import subprocess
import sys
import tarfile
import tempfile
import tomllib
from importlib.metadata import distribution
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
RUNTIME_DEPS = (
    "pal",
    "jsonschema",
    "markdown-it-py",
    "attrs",
    "jsonschema-specifications",
    "mdurl",
    "referencing",
    "rpds-py",
    "typing-extensions",
)


def main() -> None:
    version = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))["project"][
        "version"
    ]
    system = {"Darwin": "macos", "Windows": "windows", "Linux": "linux"}[platform.system()]
    architecture = {"aarch64": "arm64", "arm64": "arm64", "AMD64": "x86_64", "x86_64": "x86_64"}[
        platform.machine()
    ]
    label = f"pal-{version}-{system}-{architecture}"
    output = ROOT / "dist" / "bundles"
    output.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="pal-bundle-") as temporary:
        workspace = Path(temporary)
        source = workspace / "source" / "pal"
        for path in (ROOT / "src/pal").rglob("*.py"):
            target = source / path.relative_to(ROOT / "src/pal")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(path.read_bytes())
        args = [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--clean",
            "--onedir",
            "--name",
            "pal",
            "--distpath",
            str(workspace / "dist"),
            "--workpath",
            str(workspace / "build"),
            "--specpath",
            str(workspace),
            "--paths",
            str(ROOT / "src"),
            "--collect-submodules",
            "pal",
            "--collect-all",
            "jsonschema_specifications",
            "--add-data",
            f"{source}:pal",
        ]
        for name in RUNTIME_DEPS:
            args.extend(["--copy-metadata", name])
        subprocess.run([*args, str(ROOT / "packaging/entry.py")], check=True)
        bundle = workspace / label
        bundle.mkdir()
        shutil.move(str(workspace / "dist/pal"), bundle / "runtime")
        for metadata in (bundle / "runtime").rglob("direct_url.json"):
            metadata.unlink()
        for name in ("LICENSE", "THIRD-PARTY-NOTICES.md"):
            shutil.copy2(ROOT / name, bundle / name)
        shutil.copy2(ROOT / "packaging/INSTALL.md", bundle / "INSTALL.md")
        scripts = (
            ("install.ps1", "uninstall.ps1")
            if system == "windows"
            else ("install.sh", "uninstall.sh")
        )
        for name in scripts:
            target = bundle / name
            shutil.copy2(ROOT / "packaging" / name, target)
            if name.endswith(".ps1"):
                target.write_bytes(b"\xef\xbb\xbf" + target.read_bytes())
            target.chmod(0o755)
        licenses = bundle / "licenses"
        licenses.mkdir()
        shutil.copytree(ROOT / "packaging/licenses", licenses / "python-components")
        for name in (*RUNTIME_DEPS, "pyinstaller"):
            package = distribution(name)
            for file in package.files or []:
                if any(word in str(file).lower() for word in ("license", "copying")):
                    original = Path(package.locate_file(file))
                    if original.is_file():
                        target = licenses / name / str(file)
                        target.parent.mkdir(parents=True, exist_ok=True)
                        target.write_bytes(original.read_bytes())
        python_license = Path(sys.base_prefix) / "LICENSE.txt"
        if not python_license.is_file():
            python_license = (
                Path(sys.base_prefix)
                / f"lib/python{sys.version_info.major}.{sys.version_info.minor}/LICENSE.txt"
            )
        if not python_license.is_file():
            raise RuntimeError("Python distribution license missing")
        shutil.copy2(python_license, licenses / "PYTHON-LICENSE.txt")
        # Include python-build-standalone notices when supplied by the runtime distribution.
        for notice in Path(sys.base_prefix).glob("*LICENSE*"):
            if notice.is_file():
                shutil.copy2(notice, licenses / f"runtime-{notice.name}")
        executable = bundle / "runtime" / ("pal.exe" if system == "windows" else "pal")
        actual = subprocess.check_output(
            [str(executable), "--version"], text=True, encoding="utf-8"
        ).strip()
        if not actual.startswith(f"pal {version} "):
            raise RuntimeError(f"bundle version mismatch: {actual}")
        files = {
            p.relative_to(bundle).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(bundle.rglob("*"))
            if p.is_file()
        }
        (bundle / "BUILD.json").write_text(
            json.dumps(
                {
                    "version": version,
                    "platform": system,
                    "architecture": architecture,
                    "python": platform.python_version(),
                    "os_version": platform.platform(),
                    "source_commit": os.environ.get("GITHUB_SHA"),
                    "build": actual,
                    "dependencies": {name: distribution(name).version for name in RUNTIME_DEPS},
                    "files": files,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        if system == "windows":
            archive = Path(shutil.make_archive(str(output / label), "zip", workspace, label))
        else:
            archive = output / f"{label}.tar.gz"
            with tarfile.open(archive, "w:gz") as packed:

                def normalize(info):
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    info.mtime = 0
                    return info

                packed.add(bundle, arcname=label, filter=normalize)
        print(f"Built {archive.name}: {actual}")


if __name__ == "__main__":
    main()
