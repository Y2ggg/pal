"""WEB-001; ACC-012: backend-only upgrades must not reuse an old running service."""

import json
import shutil
import subprocess
import sys

from pal import build_identity


def test_backend_change_is_detected_without_html_or_install_path_changes(tmp_path):
    package = tmp_path / "installed"
    package.mkdir()
    (package / "web.py").write_text('INDEX_HTML = "unchanged"\n')
    (package / "publication.py").write_text('BEHAVIOR = "old"\n')
    first = build_identity.package_build_id(package)
    copied = tmp_path / "other-install-location"
    shutil.copytree(package, copied)
    assert build_identity.package_build_id(copied) == first
    (package / "__pycache__").mkdir()
    (package / "__pycache__/web.pyc").write_bytes(b"runtime")
    assert build_identity.package_build_id(package) == first
    (package / "publication.py").write_text('BEHAVIOR = "new"\n')
    assert build_identity.package_build_id(package) != first


def test_running_process_keeps_its_identity_after_files_are_replaced(tmp_path):
    shutil.copy(build_identity.__file__, tmp_path / "build_identity.py")
    backend = tmp_path / "backend.py"
    backend.write_text('VERSION = "old"\n')
    script = """import json
from pathlib import Path
import build_identity
before = build_identity.BUILD_ID
Path('backend.py').write_text('VERSION = "new"\\n')
print(json.dumps([before, build_identity.BUILD_ID, build_identity.package_build_id(Path('.'))]))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    before, running, on_disk = json.loads(result.stdout)
    assert before == running and running != on_disk
