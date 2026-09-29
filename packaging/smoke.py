"""Exercise the archived executable and installer using disposable user paths.

Traceability: ACC-001/003/007/011/012. No vendor CLI or model is substituted.
"""

import json
import os
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run_installer(bundle: Path, program: Path, environment: dict[str, str], *, check: bool):
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(bundle / "install.ps1"),
            "-InstallDir",
            str(program),
        ]
    else:
        command = ["sh", str(bundle / "install.sh")]
    return subprocess.run(command, env=environment, check=check, capture_output=not check)


def run_uninstaller(script: Path, environment: dict[str, str], *, check: bool):
    if os.name == "nt":
        command = [
            "powershell",
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(script),
        ]
    else:
        command = ["sh", str(script)]
    return subprocess.run(command, env=environment, check=check, capture_output=not check)


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
        native = "--native-clis" in sys.argv
        environment.update(
            CODEX_HOME=str(root / "codex-home"),
            CLAUDE_CONFIG_DIR=str(root / "claude-home"),
            DISABLE_TELEMETRY="1",
        )
        for key in ("CODEX_HOME", "CLAUDE_CONFIG_DIR"):
            Path(environment[key]).mkdir()

        fake = root / "not-a-pal-install"
        fake.mkdir()
        sentinel = fake / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")
        (fake / ".pal-install").write_text("PAL-INSTALL-V1\nwrong\nwrong\n", encoding="utf-8")
        fake_environment = {**environment, "PAL_INSTALL_DIR": str(fake)}
        rejected = run_installer(bundle, fake, fake_environment, check=False)
        assert rejected.returncode != 0
        assert sentinel.read_text(encoding="utf-8") == "keep"
        uninstaller_name = "uninstall.ps1" if os.name == "nt" else "uninstall.sh"
        fake_uninstaller = fake / uninstaller_name
        fake_uninstaller.write_bytes((bundle / uninstaller_name).read_bytes())
        rejected = run_uninstaller(fake_uninstaller, fake_environment, check=False)
        assert rejected.returncode != 0
        assert sentinel.read_text(encoding="utf-8") == "keep"

        damaged = root / "damaged-pal-install"
        damaged.mkdir()
        damaged_bin = damaged / "runtime"
        damaged_sentinel = damaged / "keep.txt"
        damaged_sentinel.write_text("keep", encoding="utf-8")
        damaged_uninstaller = damaged / uninstaller_name
        damaged_uninstaller.write_bytes((bundle / uninstaller_name).read_bytes())
        (damaged / "BUILD.json").write_text("{}\n", encoding="utf-8")
        (damaged / ".pal-install").write_text(
            f"PAL-INSTALL-V1\n{damaged.resolve()}\n{damaged_bin.resolve()}\n", encoding="utf-8"
        )
        damaged_environment = {
            **environment,
            "PAL_INSTALL_DIR": str(damaged),
            "PAL_BIN_DIR": str(damaged_bin),
        }
        rejected = run_installer(bundle, damaged, damaged_environment, check=False)
        assert rejected.returncode != 0
        rejected = run_uninstaller(damaged_uninstaller, damaged_environment, check=False)
        assert rejected.returncode != 0
        assert damaged_sentinel.read_text(encoding="utf-8") == "keep"

        run_installer(bundle, program, environment, check=True)
        if os.name != "nt":
            original_link = Path(environment["PAL_BIN_DIR"]) / "pal"
            changed_environment = {**environment, "PAL_BIN_DIR": str(root / "other-bin")}
            rejected = run_installer(bundle, program, changed_environment, check=False)
            assert rejected.returncode != 0
            assert original_link.is_symlink()
            assert original_link.resolve() == (program / "runtime/pal").resolve()
        Path(environment["PAL_CONFIG_ROOT"]).mkdir()
        run_installer(bundle, program, environment, check=True)
        executable = program / "runtime" / ("pal.exe" if os.name == "nt" else "pal")

        def run(*arguments):
            result = subprocess.run(
                [str(executable), *arguments],
                env=environment,
                check=False,
                text=True,
                encoding="utf-8",
                capture_output=True,
                timeout=240,
            )
            if result.returncode:
                raise RuntimeError(f"{arguments}: {result.stderr or result.stdout}")
            return result.stdout

        identity = json.loads((bundle / "BUILD.json").read_text(encoding="utf-8"))
        assert run("--version").strip() == identity["build"]
        library = root / "library"
        run("init", "--library", str(library), "--library-id", "package-smoke")
        environment["PAL_LIBRARY_ROOT"] = str(library)
        if native:
            answers = root / "answers.json"
            answers.write_text(
                json.dumps(
                    {
                        "schema_version": 2,
                        "library_id": "package-smoke",
                        "library_path": str(library),
                        "config_root": str(root / "config"),
                    }
                ),
                encoding="utf-8",
            )
            run("quickstart", "--answers", str(answers))
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
        if native:
            run("sync")
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
            if native:
                with urllib.request.urlopen(url + "api/monitor", timeout=60) as response:
                    monitoring = json.load(response)
                for target in monitoring["targets"]:
                    assert target["creation"]["state"] == "healthy", monitoring
                    assert target["mount"]["state"] == "healthy", monitoring
                print("PASS: official Claude/Codex system and production plugin installation")
            reused = run("web", "--port", str(data["port"]), "--no-browser")
            assert "已在运行" in reused and url in reused
            assert server.poll() is None
            run("web", "stop", "--port", str(data["port"]))
            assert server.wait(timeout=15) == 0
        finally:
            if server.poll() is None:
                server.terminate()
                server.wait(timeout=10)
            server.stderr.close()
        restarted = subprocess.Popen(
            [str(executable), "web", "--port", str(data["port"]), "--no-browser"],
            env=environment,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        try:
            record_path = root / "config/web" / f"port-{data['port']}.json"
            deadline = time.monotonic() + 30
            while not record_path.exists() and time.monotonic() < deadline:
                if restarted.poll() is not None:
                    raise RuntimeError(restarted.stderr.read().decode("utf-8"))
                time.sleep(0.1)
            assert record_path.exists(), "Restarted Web service record missing"
            assert json.loads(record_path.read_bytes())["instance_id"] != data["instance_id"]
            with urllib.request.urlopen(url + "api/context", timeout=10) as response:
                assert json.load(response)["proof"] == "PAL_WEB_CONTEXT"
            run("web", "stop", "--port", str(data["port"]))
            assert restarted.wait(timeout=15) == 0
        finally:
            if restarted.poll() is None:
                restarted.terminate()
                restarted.wait(timeout=10)
            restarted.stderr.close()
        run_uninstaller(program / uninstaller_name, environment, check=True)
        assert not program.exists()
        assert (library / "library.json").is_file()
        assert (root / "config").is_dir()
        print("PASS: archived install/version/multifile-create/publish/doctor/web/stop/uninstall")


if __name__ == "__main__":
    main()
