"""Read-only Skill file boundaries and Markdown safety.

Traceability: PSM-010; PRD-RELEASE-002; ACC-011/012.
"""

from urllib.error import HTTPError
from urllib.parse import urlencode

import pytest

from pal.errors import PALError
from pal.skill_browser import render_markdown, skill_details, skill_file
from tests.test_publishing import commit_unit, setup_library
from tests.test_web import _request, _running_server


@pytest.fixture
def browser_library(tmp_path):
    extras = {
        "references/guide.md": (
            "# 指南\n\n**加粗**与 `代码`。\n\n| 列 | 值 |\n| --- | --- |\n| 一 | 二 |\n"
        ).encode(),
        "scripts/read.py": b'print("<script>not HTML</script>")\n',
        "assets/image.bin": b"\x00\xff\x01",
        "assets/big.txt": b"x" * (1024 * 1024 + 1),
        "references/空 白.md": "包含空格的路径。".encode(),
    }

    library, config = setup_library(tmp_path, "file-browser")
    commit_unit(tmp_path, library, config, "files-skill", directed=True, extra_files=extras)
    details = skill_details(library, "files-skill")
    artifact = details["development"]["artifacts"][0]
    query = {
        "unit_id": "files-skill",
        "source": "development",
        "revision_id": details["development"]["revision_id"],
        "version_id": "",
        "artifact_id": artifact["artifact_id"],
        "path": artifact["canonical_path"],
    }
    return library, config, details, query


def test_file_inventory_and_http_previews_use_validated_payload(browser_library):
    library, config, details, query = browser_library
    artifacts = details["development"]["artifacts"]
    assert sorted(a["covered_clis"] for a in artifacts) == [["claude-code"], ["codex"]]
    files = artifacts[0]["files"]
    assert {f["display_path"] for f in files} == {
        "SKILL.md",
        "references/guide.md",
        "scripts/read.py",
        "assets/image.bin",
        "assets/big.txt",
        "references/空 白.md",
    }
    assert all("context/" not in f["path"] and "artifact.json" not in f["path"] for f in files)
    server, thread, base = _running_server(library, config)
    try:
        _, content = _request(base, "/api/skill-file?" + urlencode(query))
        assert content["content"].splitlines()[:2] == ["---", "name: files-skill"]
        assert content["kind"] == "markdown"
        for filename, kind in [
            ("references/guide.md", "markdown"),
            ("scripts/read.py", "text"),
            ("assets/image.bin", "binary"),
            ("assets/big.txt", "large"),
            ("references/空 白.md", "markdown"),
        ]:
            _, value = _request(
                base,
                "/api/skill-file?" + urlencode({**query, "path": f"skills/files-skill/{filename}"}),
            )
            assert value["kind"] == kind
            if kind in {"binary", "large"}:
                assert "content" not in value
            elif filename.endswith("guide.md"):
                assert "<strong>加粗</strong>" in value["markdown_html"]
                assert "<table>" in value["markdown_html"]
        with pytest.raises(HTTPError) as error:
            _request(base, "/api/skill-file?" + urlencode(query) + "&source=production")
        assert error.value.code == 409
    finally:
        server.shutdown()
        thread.join(timeout=3)
        server.server_close()


@pytest.mark.parametrize(
    "changes",
    [
        {"path": "../../library.json"},
        {"path": "/etc/passwd"},
        {"path": "skills/files-skill/../SKILL.md"},
        {"path": "context/profiles/skill-md-v1-basic.json"},
        {"artifact_id": "other-artifact"},
        {"source": "unknown"},
        {"source": "production"},
        {"revision_id": "revision-stale"},
        {"version_id": "production-wrong"},
    ],
)
def test_file_request_rejects_wrong_scope_or_stale_version(browser_library, changes):
    library, _, _, query = browser_library
    with pytest.raises(PALError):
        skill_file(library, {**query, **changes})


def test_file_read_rejects_drift_symlinks_and_read_time_change(
    browser_library, monkeypatch, tmp_path
):
    library, _, _, query = browser_library
    # Choose the file belonging to the same artifact as the request.
    path = next(
        p
        for p in (library / "development").rglob("scripts/read.py")
        if query["artifact_id"] in p.parts
    )
    query = {**query, "path": "skills/files-skill/scripts/read.py"}
    original = path.read_bytes()
    path.write_bytes(b"tampered")
    with pytest.raises(PALError):
        skill_file(library, query)
    path.unlink()
    outside = tmp_path / "outside.py"
    outside.write_bytes(original)
    path.symlink_to(outside)
    with pytest.raises(PALError, match="symbolic link"):
        skill_file(library, query)
    path.unlink()
    path.write_bytes(original)
    import pal.skill_browser as browser

    source = browser._source

    def change_after_validation(*args):
        value = source(*args)
        path.write_bytes(b"changed after inventory validation")
        return value

    monkeypatch.setattr(browser, "_source", change_after_validation)
    with pytest.raises(PALError, match="读取时发生变化"):
        skill_file(library, query)


def test_markdown_formats_prose_without_executable_html_or_remote_images():
    rendered = render_markdown("""# 标题

<script>alert(1)</script>

[危险](javascript:alert%281%29) [本地](references/guide.md) [网站](https://example.com)

![图](https://example.com/image.png)

```html
<img onerror="alert(1)">
```
""")
    assert "<h1>标题</h1>" in rendered
    assert "<script>" not in rendered
    assert "<img" not in rendered
    assert 'href="javascript:' not in rendered
    assert 'data-doc-link="references/guide.md"' in rendered
    assert 'rel="noopener noreferrer"' in rendered
    assert "&lt;script&gt;" in rendered
