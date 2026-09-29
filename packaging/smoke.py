"""Exercise the archived executable and installer using disposable user paths.

Traceability: ACC-001/003/007/011/012. No vendor CLI or model is substituted.
"""

import json
import os
import subprocess
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main():
    (archive,) = (ROOT / "dist/bundles").glob("pal-*")
    with tempfile.TemporaryDirectory(prefix="pal package 中文 ") as temporary:
        root = Path(temporary)
        if archive.suffix == ".zip":
            with zipfile.ZipFile(archive) as packed:
                packed.extractall(root)
        else:
            with tarfile.open(archive) as packed:
                packed.extractall(root, filter="data")
        bundle = root / archive.name.removesuffix(".tar.gz").removesuffix(".zip")
        program = root / "installed"
        environment = os.environ.copy()
        environment.update(
            PAL_INSTALL_DIR=str(program),
            PAL_BIN_DIR=str(root / "bin"),
            PAL_CONFIG_ROOT=str(root / "config"),
        )
        if os.name == "nt":
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(bundle / "install.ps1"),
                    "-InstallDir",
                    str(program),
                ],
                env=environment,
                check=True,
            )
        else:
            subprocess.run(["sh", str(bundle / "install.sh")], env=environment, check=True)
        executable = program / "runtime" / ("pal.exe" if os.name == "nt" else "pal")

        def run(*arguments):
            result = subprocess.run(
                [str(executable), *arguments],
                env=environment,
                check=True,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=60,
            )
            return result.stdout

        identity = json.loads((bundle / "BUILD.json").read_text(encoding="utf-8"))
        assert run("--version").strip() == identity["build"]
        library = root / "library"
        run("init", "--library", str(library), "--library-id", "package-smoke")
        environment["PAL_LIBRARY_ROOT"] = str(library)
        for cli in ("claude-code", "codex"):
            run("mount", "config", "--library", str(library), "--cli", cli)
        request = root / "request.json"
        request.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "unit_id": "package-skill",
                    "summary": "安装包验证",
                    "profile_by_cli": {
                        "claude-code": "skill-md-v1-basic",
                        "codex": "skill-md-v1-basic",
                    },
                }
            ),
            encoding="utf-8",
        )
        opened = json.loads(
            run("create", "begin", "--cli", "codex", "--request-file", str(request))
        )
        for candidate in opened["candidates"]:
            Path(candidate["skill_path"]).write_bytes(
                b"---\nname: package-skill\ndescription: Package verification.\n"
                b"---\n\nRead assets/data.txt.\n"
            )
            asset = Path(candidate["skill_root"]) / "assets/data.txt"
            asset.parent.mkdir()
            asset.write_bytes("中文内容\n".encode())
        run("create", "commit", "--creation", opened["creation_id"])
        run("publish", "--unit", "package-skill")
        run("doctor", "--library", str(library))
        server = subprocess.Popen(
            [str(executable), "web", "--port", "0", "--no-browser"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            url = None
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if server.poll() is not None:
                    raise RuntimeError(server.stderr.read().decode("utf-8"))
                for record in (root / "config").glob("web/*.json"):
                    data = json.loads(record.read_bytes())
                    url = f"http://{data['host']}:{data['port']}/"
                if url:
                    break
                time.sleep(0.1)
            assert url, "Web service record missing"
            with urllib.request.urlopen(url + "api/status", timeout=10) as response:
                assert response.status == 200
            run("web", "stop", "--port", str(data["port"]))
            assert server.wait(timeout=15) == 0
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
            server.stderr.close()
        if os.name == "nt":
            subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(program / "uninstall.ps1"),
                ],
                env=environment,
                check=True,
            )
        else:
            subprocess.run(["sh", str(program / "uninstall.sh")], env=environment, check=True)
        assert not program.exists()
        assert (library / "library.json").is_file()
        assert (root / "config").is_dir()
        print("PASS: archived install/version/multifile-create/publish/doctor/web/stop/uninstall")


if __name__ == "__main__":
    main()
