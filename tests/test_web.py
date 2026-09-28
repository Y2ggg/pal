"""Local Web control-plane tests.

Traceability: requirement 017, decision 054; WEB-001 through WEB-006;
PRD-TECH-001, ACC-012.
"""

from __future__ import annotations

import errno
import json
import threading
import time
from http.client import HTTPConnection
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

import pal.web as web_module
from pal.cli import main
from pal.config_mount import mount_config
from pal.creation import begin_creation, commit_creation
from pal.io import atomic_replace_json, formatted_json_bytes
from pal.library import initialize_library
from pal.web import create_server


@pytest.fixture(autouse=True)
def isolated_installation_checks(monkeypatch):
    monkeypatch.setattr(
        web_module,
        "inspect_installations",
        lambda *args, **kwargs: {
            "checked_at": "2026-09-24T00:00:00Z",
            "targets": [],
            "production_version_id": None,
            "mounted_version_id": None,
        },
    )


@pytest.mark.parametrize("open_browser", [True, False])
def test_web_repeated_start_reuses_matching_console(tmp_path, monkeypatch, capsys, open_browser):
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    opened = []
    monkeypatch.setattr(web_module.webbrowser, "open", opened.append)
    # Local probes must not depend on the user's proxy settings.
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:1")
    try:
        result = web_module.run_web(
            library, config_root=config, port=server.server_port, open_browser=open_browser
        )
        assert result == 0
        assert opened == ([base + "/"] if open_browser else [])
        output = capsys.readouterr().out
        assert "已在运行" in output and base in output
        assert thread.is_alive()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_web_rejects_reusing_an_outdated_console(tmp_path, monkeypatch, capsys):
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    monkeypatch.setattr(
        web_module,
        "_existing_console",
        lambda *_: (str(library.resolve()), str(config.resolve()), "old-build"),
    )
    try:
        result = main(
            [
                "web",
                "--library",
                str(library),
                "--config-root",
                str(config),
                "--port",
                str(server.server_port),
            ]
        )
        assert result == 20
        output = capsys.readouterr().err
        assert base in output and "旧版" in output and "pal web stop" in output
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_web_stop_closes_only_a_registered_server(tmp_path, capsys):
    library, config = _setup_library(tmp_path)
    thread = threading.Thread(
        target=web_module.run_web,
        args=(library,),
        kwargs={"config_root": config, "port": 0, "open_browser": False},
        daemon=True,
    )
    thread.start()
    deadline = time.monotonic() + 5
    record_files = []
    while time.monotonic() < deadline:
        record_files = list((config / "web").glob("port-*.json"))
        if record_files:
            break
        time.sleep(0.02)
    assert len(record_files) == 1
    record = json.loads(record_files[0].read_text())
    port = record["port"]
    try:
        with pytest.raises(HTTPError) as error:
            urlopen(Request(f"http://127.0.0.1:{port}/api/stop", data=b""), timeout=3)
        assert error.value.code == 403
        atomic_replace_json(record_files[0], {**record, "instance_id": "0" * 32})
        assert main(["web", "stop", "--config-root", str(config), "--port", str(port)]) == 20
        assert thread.is_alive()
        atomic_replace_json(record_files[0], record)
        assert main(["web", "stop", "--config-root", str(config), "--port", str(port)]) == 0
        thread.join(timeout=3)
        assert not thread.is_alive()
        assert not record_files[0].exists()
        assert "正在停止" in capsys.readouterr().out
    finally:
        if thread.is_alive():
            web_module.stop_web(config_root=config, port=port)
            thread.join(timeout=3)


def test_web_stop_does_not_touch_unregistered_listener(tmp_path, capsys):
    _, config = _setup_library(tmp_path)
    server = ThreadingHTTPServer(("127.0.0.1", 0), BaseHTTPRequestHandler)
    try:
        assert (
            main(["web", "stop", "--config-root", str(config), "--port", str(server.server_port)])
            == 20
        )
        assert "没有可关闭" in capsys.readouterr().err
    finally:
        server.server_close()


@pytest.mark.parametrize("different", ["library", "config"])
def test_web_occupied_console_must_match_both_roots(tmp_path, monkeypatch, capsys, different):
    library, config = _setup_library(tmp_path)
    if different == "library":
        (tmp_path / "other").mkdir()
        selected, selected_config = _setup_library(tmp_path / "other")
    else:
        selected, selected_config = library, tmp_path / "other-config"
        selected_config.mkdir()
    opened = []
    monkeypatch.setattr(web_module.webbrowser, "open", opened.append)
    server, thread, base = _running_server(library, config)
    try:
        result = main(
            [
                "web",
                "--library",
                str(selected),
                "--config-root",
                str(selected_config),
                "--port",
                str(server.server_port),
            ]
        )
        assert result == 20
        error = capsys.readouterr().err
        assert "其他库或配置" in error and base in error and "--port" in error
        assert "PAL_WEB_REJECTED" not in error
        assert not opened
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize(
    "kind", ["html", "invalid-json", "array", "redirect", "unhealthy", "oversize"]
)
def test_web_unknown_listener_shows_address_without_opening(tmp_path, monkeypatch, capsys, kind):
    library, config = _setup_library(tmp_path)
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_args):
            pass

        def do_GET(self):  # noqa: N802
            requests.append(self.path)
            status = {"redirect": 302, "unhealthy": 409}.get(kind, 200)
            body = {
                "html": b"<html>Other service</html>",
                "invalid-json": b"not json",
                "array": b"[]",
                "oversize": b" " * (1024 * 1024 + 1),
            }.get(kind, b"{}")
            self.send_response(status)
            self.send_header("Content-Type", "text/html" if kind == "html" else "application/json")
            self.send_header("Location", "/redirect-target")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    opened = []
    monkeypatch.setattr(web_module.webbrowser, "open", opened.append)
    try:
        result = main(
            [
                "web",
                "--library",
                str(library),
                "--config-root",
                str(config),
                "--port",
                str(server.server_port),
            ]
        )
        assert result == 20
        error = capsys.readouterr().err
        assert f"http://127.0.0.1:{server.server_port}/" in error
        assert "未能确认" in error and "--port 0" in error
        expected_requests = (
            ["/api/status", "/api/context"] if kind == "unhealthy" else ["/api/status"]
        )
        assert not opened and requests == expected_requests
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_web_probe_timeout_closes_connection(monkeypatch):
    events = []

    class Connection:
        def __init__(self, host, port, timeout):
            assert (host, port, timeout) == ("127.0.0.1", 8787, 2)

        def request(self, method, path):
            raise TimeoutError()

        def close(self):
            events.append("closed")

    monkeypatch.setattr(web_module, "HTTPConnection", Connection)
    assert web_module._existing_console("127.0.0.1", 8787) is None
    assert events == ["closed"]


def test_web_port_zero_prints_actual_bound_port(tmp_path, monkeypatch, capsys):
    library, config = _setup_library(tmp_path)
    visited = []

    def stop(server):
        visited.append(server.server_port)
        raise KeyboardInterrupt

    monkeypatch.setattr(web_module._WebServer, "serve_forever", stop)
    assert web_module.run_web(library, config_root=config, port=0, open_browser=False) == 0
    assert visited[0] > 0
    assert f"http://127.0.0.1:{visited[0]}/" in capsys.readouterr().out


def test_web_other_bind_errors_do_not_probe_an_existing_service(tmp_path, monkeypatch):
    def reject(*args, **kwargs):
        raise OSError(errno.EACCES, "Permission denied")

    monkeypatch.setattr(web_module, "create_server", reject)
    with pytest.raises(OSError) as error:
        web_module.run_web(tmp_path)
    assert error.value.errno == errno.EACCES


def _setup_library(tmp_path: Path) -> tuple[Path, Path]:
    library = tmp_path / "library"
    config = tmp_path / "config"
    initialize_library(library, "web-library")
    mount_config(library, "claude-code", config_root=config)
    mount_config(library, "codex", config_root=config)
    return library, config


def _commit_unit(tmp_path: Path, library: Path, config: Path, unit_id: str) -> None:
    request_path = tmp_path / f"{unit_id}.json"
    request_path.write_bytes(
        formatted_json_bytes(
            {
                "schema_version": 1,
                "unit_id": unit_id,
                "summary": "Web console test unit",
                "profile_by_cli": {
                    "claude-code": "skill-md-v1-basic",
                    "codex": "skill-md-v1-basic",
                },
            }
        )
    )
    opened = begin_creation(library, "codex", request_path, config_root=config)
    content = (
        "---\n"
        f"name: {unit_id}\n"
        "description: Web console test skill.\n"
        "---\n\n"
        "# Instructions\n\n"
        "Return the web console test result.\n"
    )
    for candidate in opened["candidates"]:
        Path(candidate["skill_path"]).write_text(content, encoding="utf-8")
    commit_creation(library, opened["creation_id"], config_root=config)


def _running_server(library: Path, config: Path):
    server = create_server(library, config_root=config, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    host, port = server.server_address[:2]
    base = f"http://[{host}]:{port}" if ":" in host else f"http://{host}:{port}"
    return server, thread, base


def _request(base: str, path: str, *, method: str = "GET", body: dict[str, object] | None = None):
    data = None if body is None else json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json"} if data is not None else {}
    if method == "POST":
        headers["X-PAL-Action"] = "confirm"
    request = Request(f"{base}{path}", data=data, headers=headers, method=method)
    with urlopen(request, timeout=3) as response:
        return response.status, json.loads(response.read().decode("utf-8"))


@pytest.mark.parametrize(
    "headers",
    [
        [("Host", "attacker.example")],
        [("Host", "localhost.attacker.example:{port}")],
        [("Host", "127.0.0.1:1")],
        [("Host", "127.0.0.1:{port}"), ("Host", "attacker.example")],
        [],
        [("Host", "127.0.0.1:{port}"), ("Origin", "http://attacker.example")],
        [("Host", "127.0.0.1:{port}"), ("Origin", "null")],
        [("Host", "127.0.0.1:{port}"), ("Origin", "http://localhost:{port}")],
        [("Host", "127.0.0.1:{port}"), ("Sec-Fetch-Site", "cross-site")],
        [("Host", "127.0.0.1:{port}"), ("Sec-Fetch-Site", "same-site")],
        [
            ("Host", "127.0.0.1:{port}"),
            ("Origin", "http://127.0.0.1:{port}"),
            ("Origin", "http://attacker.example"),
        ],
    ],
)
def test_web_rejects_untrusted_request_before_read_or_write(tmp_path, monkeypatch, headers):
    library, config = _setup_library(tmp_path)
    server, thread, _ = _running_server(library, config)

    def unexpected(*args, **kwargs):
        pytest.fail("Untrusted request reached a library operation")

    monkeypatch.setattr(web_module, "_status", unexpected)
    monkeypatch.setattr(web_module, "execute_skill_action", unexpected)
    try:
        for method, path in [
            ("GET", "/api/context"),
            ("GET", "/api/status"),
            ("POST", "/api/skill-actions/execute"),
            ("POST", "/api/stop"),
        ]:
            connection = HTTPConnection("127.0.0.1", server.server_port, timeout=3)
            try:
                connection.putrequest(method, path, skip_host=True)
                for name, value in headers:
                    connection.putheader(name, value.format(port=server.server_port))
                connection.putheader("X-PAL-Action", "confirm")
                connection.putheader("Content-Length", "2")
                connection.endheaders(b"{}")
                response = connection.getresponse()
                assert response.status == 403
                payload = json.loads(response.read())
                assert "同源" in payload["error"]
                assert str(library) not in json.dumps(payload)
            finally:
                connection.close()
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize("hostname", ["localhost", "127.0.0.1"])
def test_web_allows_local_same_origin_browser_and_blocks_framing(tmp_path, hostname):
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    authority = f"{hostname}:{server.server_port}"
    try:
        request = Request(
            base + "/",
            headers={
                "Host": authority,
                "Origin": f"http://{authority}",
                "Sec-Fetch-Site": "same-origin",
            },
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
            assert response.headers["X-Frame-Options"] == "DENY"
            assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
            assert response.headers["Referrer-Policy"] == "no-referrer"
        # A link may open the console as a top-level page; cross-site API reads
        # and embedding remain forbidden.
        request = Request(
            base + "/",
            headers={
                "Sec-Fetch-Site": "cross-site",
                "Sec-Fetch-Mode": "navigate",
                "Sec-Fetch-Dest": "document",
            },
        )
        with urlopen(request, timeout=3) as response:
            assert response.status == 200
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_web_status_marks_committed_development_unit_unpublished(tmp_path: Path) -> None:
    library, config = _setup_library(tmp_path)
    _commit_unit(tmp_path, library, config, "web-pending")
    server, thread, base = _running_server(library, config)
    try:
        status, payload = _request(base, "/api/status")
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()

    assert status == 200
    assert payload["production"]["active_version_id"] is None
    assert payload["units"] == [
        {
            "active_release_id": None,
            "active_revision_id": None,
            "component_type": "skill",
            "current_revision_id": payload["units"][0]["current_revision_id"],
            "development_revision_id": payload["units"][0]["current_revision_id"],
            "mounted_revision_id": None,
            "production_revision_id": None,
            "state": "unpublished",
            "mount_enabled": True,
            "pending_deletion": False,
            "description": "Web console test skill.",
            "description_source": "development",
            "unit_id": "web-pending",
            "updated_at": payload["units"][0]["updated_at"],
        }
    ]


def test_web_home_explains_entry_flow_and_operation_boundaries(tmp_path: Path) -> None:
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    try:
        with urlopen(f"{base}/", timeout=3) as response:
            body = response.read().decode("utf-8")
            csp = response.headers["Content-Security-Policy"]
        with urlopen(f"{base}/favicon.ico", timeout=3) as response:
            favicon_status = response.status
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()

    assert "使用说明" in body
    assert "查看详情" in body
    assert "加入批量发布" not in body
    assert "draft-count" not in body
    assert "已发布内容" in body
    assert "我的 Skill" in body
    assert "管理此 Skill" in body
    assert "发布到生产" in body
    assert "从 CLI 卸载" in body
    assert "完成 CLI 卸载" in body
    assert "放弃未发布修改" in body
    assert '<dialog id="action-dialog"' in body
    assert "window.confirm" not in body
    assert "window.alert" not in body
    assert "CLI 同步" in body
    assert "库与维护" in body
    assert "prefers-reduced-motion" in body
    assert "发布历史" not in body
    assert "未发布修改不会带入" in body
    assert "已卸载的 Skill 保留停用" in body
    assert "当前版本不支持在 Web 中删除 Skill" not in body
    assert "default-src 'self'" in csp
    assert favicon_status == 204


def test_web_state_changes_require_explicit_confirmation(tmp_path: Path) -> None:
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    try:
        request = Request(
            f"{base}/api/recover",
            data=b"{}",
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(HTTPError) as error:
            urlopen(request, timeout=3)
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()

    assert error.value.code == 403
    assert "确认" in error.value.read().decode("utf-8")


def test_web_recover_delegates_production_and_usage_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library, config = _setup_library(tmp_path)
    production_calls: list[tuple[Path, Path]] = []
    usage_calls: list[Path] = []

    def recover_production(root: Path, *, config_root: Path) -> dict[str, object]:
        production_calls.append((root, config_root))
        return {"production_status": "clean"}

    def recover_usage(root: Path) -> dict[str, object]:
        usage_calls.append(root)
        return {"recovered_usage_ids": [], "aborted_usage_ids": [], "pending_usage_ids": []}

    monkeypatch.setattr(web_module, "recover_production", recover_production)
    monkeypatch.setattr(web_module, "recover_usage_transactions", recover_usage)
    server, thread, base = _running_server(library, config)
    try:
        status, payload = _request(base, "/api/recover", method="POST", body={})
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()

    assert status == 200
    assert payload["proof"] == "PAL_PRODUCTION_RECOVERED"
    assert payload["usage_recovery"]["recovered_usage_ids"] == []
    assert production_calls == [(library.resolve(), config.resolve())]
    assert usage_calls == [library.resolve()]


@pytest.mark.parametrize("path", ["/api/publish", "/api/deletion/preview", "/api/deletion/execute"])
def test_duplicate_management_routes_are_retired(tmp_path, path):
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    before = {str(p): p.read_bytes() for p in library.rglob("*") if p.is_file()}
    try:
        with pytest.raises(HTTPError) as error:
            _request(base, path, method="POST", body={"unit_id": "web-pending"})
        assert error.value.code == 404
        assert {str(p): p.read_bytes() for p in library.rglob("*") if p.is_file()} == before
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_web_batch_publication_is_retired(tmp_path: Path) -> None:
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    before = {
        path.relative_to(library): path.read_bytes()
        for path in library.rglob("*")
        if path.is_file()
    }
    try:
        with pytest.raises(HTTPError) as error:
            _request(
                base,
                "/api/production-set",
                method="POST",
                body={
                    "base_version_id": None,
                    "members": [{"unit_id": "web-pending", "revision_id": "revision-test"}],
                    "removed_unit_ids": [],
                },
            )
        assert error.value.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()

    after = {
        path.relative_to(library): path.read_bytes()
        for path in library.rglob("*")
        if path.is_file()
    }
    assert after == before


def test_web_rejects_non_loopback_host(tmp_path: Path) -> None:
    library, config = _setup_library(tmp_path)
    with pytest.raises(Exception, match="loopback"):
        create_server(library, config_root=config, host="0.0.0.0", port=0)


def test_skill_details_read_validated_text_and_reject_drift(tmp_path: Path) -> None:
    library, config = _setup_library(tmp_path)
    _commit_unit(tmp_path, library, config, "details-skill")
    server, thread, base = _running_server(library, config)
    try:
        _, payload = _request(base, "/api/skills/details-skill")
        assert payload["production"] is None
        artifact = payload["development"]["artifacts"][0]
        assert artifact["description"] == "Web console test skill."
        assert artifact["covered_clis"] == ["claude-code", "codex"]
        assert "Return the web console test result." in artifact["instructions"]
        skill = next((library / "development/units/details-skill").rglob("SKILL.md"))
        skill.write_text("tampered", encoding="utf-8")
        with pytest.raises(HTTPError) as error:
            _request(base, "/api/skills/details-skill")
        assert error.value.code == 409
        with pytest.raises(HTTPError) as error:
            _request(base, "/api/skills/../bad")
        assert error.value.code == 409
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_web_history_actions_are_retired(tmp_path: Path) -> None:
    library, config = _setup_library(tmp_path)
    server, thread, base = _running_server(library, config)
    try:
        for action in ("delete", "restore"):
            with pytest.raises(HTTPError) as error:
                _request(
                    base,
                    f"/api/history/{action}",
                    method="POST",
                    body={"version_id": "production-old"},
                )
            assert error.value.code == 404
        with pytest.raises(HTTPError) as error:
            _request(
                base,
                "/api/history/delete",
                method="POST",
                body={"version_id": "../bad"},
            )
        assert error.value.code == 404
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


def test_installation_monitor_and_repair_http_contract(tmp_path, monkeypatch):
    library, config = _setup_library(tmp_path)
    report = {
        "checked_at": "2026-09-24T00:00:00Z",
        "targets": [{"cli_id": "codex", "creation": {"state": "outdated"}}],
    }
    monkeypatch.setattr(web_module, "inspect_installations", lambda *args, **kwargs: report)
    repairs = []

    def repair(root, **kwargs):
        repairs.append((root, kwargs))
        return {"monitor": report, "message": "核验完成"}

    monkeypatch.setattr(web_module, "repair_installation", repair)
    server, thread, base = _running_server(library, config)
    try:
        assert _request(base, "/api/monitor")[1]["targets"][0]["creation"]["state"] == "outdated"
        _, result = _request(
            base,
            "/api/installations/repair",
            method="POST",
            body={"kind": "creation", "cli_id": "codex", "expected_version": "0.2.0+native.7"},
        )
        assert result["monitor"] == report
        assert repairs[0][1]["expected_version"] == "0.2.0+native.7"
        with pytest.raises(HTTPError) as rejected:
            _request(
                base,
                "/api/installations/repair",
                method="POST",
                body={"kind": "creation", "cli_id": "codex"},
            )
        assert rejected.value.code == 409
        assert len(repairs) == 1
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()
