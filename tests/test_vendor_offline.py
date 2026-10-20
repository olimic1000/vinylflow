"""Offline-UI and LAN-binding hardening checks (plan 03)."""

import hashlib
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.api import app

ROOT = Path(__file__).resolve().parent.parent
STATIC = ROOT / "backend" / "static"
VENDOR = STATIC / "vendor"
client = TestClient(app)


def _recorded_hashes():
    rows = {}
    for line in (VENDOR / "README.md").read_text().splitlines():
        m = re.match(r"\|\s*`([^`]+\.js)`\s*\|.*\|\s*`([0-9a-f]{64})`\s*\|", line)
        if m:
            rows[m.group(1)] = m.group(2)
    return rows


def test_index_has_no_external_assets():
    html = (STATIC / "index.html").read_text()
    assert not re.search(r"<script[^>]+src=[\"']?(https?:)?//", html, re.I)
    for tag in re.findall(r"<link\b[^>]*>", html, re.I):
        assert not re.search(r"href=[\"']?(https?:)?//", tag, re.I), tag
    assert "@import" not in html or "http" not in html.split("@import", 1)[1][:80]
    assert "/static/vendor/" in html


def test_vendor_readme_covers_four_files():
    assert len(_recorded_hashes()) == 4


@pytest.mark.parametrize("name,digest", sorted(_recorded_hashes().items()))
def test_vendored_file_hash_and_served(name, digest):
    path = VENDOR / name
    assert path.is_file()
    assert hashlib.sha256(path.read_bytes()).hexdigest() == digest
    resp = client.get(f"/static/vendor/{name}")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]


def test_index_references_only_existing_vendor_files():
    html = (STATIC / "index.html").read_text()
    refs = re.findall(r"/static/vendor/([\w.\-]+\.js)", html)
    assert len(refs) == 4
    for r in refs:
        assert (VENDOR / r).is_file()


@pytest.mark.skipif(shutil.which("node") is None, reason="node not available")
@pytest.mark.parametrize("name", sorted(_recorded_hashes()))
def test_vendored_file_parses(name):
    script = (
        "const s=require('fs').readFileSync(process.argv[1],'utf8');new Function(s);"
    )
    r = subprocess.run(["node", "-e", script, str(VENDOR / name)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


# --- Slice 2: LAN binding ---------------------------------------------------

def test_compose_binds_localhost_by_default():
    text = (ROOT / "docker-compose.yml").read_text()
    try:
        import yaml
        ports = yaml.safe_load(text)["services"]["vinylflow"]["ports"]
    except ImportError:
        ports = re.findall(r'-\s*"([^"]*:8000)"', text)
    assert ports == ["${BIND_ADDRESS:-127.0.0.1}:${PORT:-8000}:8000"]


def test_env_example_documents_bind_address():
    text = (ROOT / ".env.example").read_text()
    assert re.search(r"^BIND_ADDRESS=127\.0\.0\.1$", text, re.M)


def test_api_main_reads_host_env_default_localhost():
    src = (ROOT / "backend" / "api.py").read_text()
    main = src.split('if __name__ == "__main__":', 1)[1]
    assert 'os.environ.get("HOST", "127.0.0.1")' in main
    assert "0.0.0.0" not in main


def test_dockerfile_keeps_container_bind_all():
    assert "--host 0.0.0.0" in (ROOT / "Dockerfile").read_text()


def test_desktop_launcher_defaults_to_localhost():
    assert 'setdefault("HOST", "127.0.0.1")' in (ROOT / "desktop_launcher.py").read_text()


@pytest.mark.skipif(shutil.which("docker") is None, reason="docker not available")
def test_compose_config_renders_ports(tmp_path):
    env = tmp_path / "e.env"
    for value, expect in ((None, "127.0.0.1"), ("0.0.0.0", "0.0.0.0")):
        env.write_text("PORT=8123\n" + (f"BIND_ADDRESS={value}\n" if value else ""))
        r = subprocess.run(
            ["docker", "compose", "-p", "vinylflow-configcheck", "--env-file", str(env),
             "-f", str(ROOT / "docker-compose.yml"), "config", "--format", "json"],
            capture_output=True, text=True,
        )
        if r.returncode != 0:
            pytest.skip("docker compose config unavailable")
        port = json.loads(r.stdout)["services"]["vinylflow"]["ports"][0]
        assert port["host_ip"] == expect and str(port["published"]) == "8123"
