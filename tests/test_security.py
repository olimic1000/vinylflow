"""Same-origin guard: Host allow-list, Origin check, and no CORS headers."""

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend import api
from backend.security import host_allowed, origin_allowed

EVIL = {"Origin": "https://evil.example"}
client = TestClient(api.app)


@pytest.fixture
def no_extra_hosts(monkeypatch):
    # tests/conftest.py allows "testserver" for the other TestClient suites.
    monkeypatch.delenv("VINYLFLOW_ALLOWED_HOSTS", raising=False)


@pytest.mark.parametrize("host", [
    "localhost", "localhost:8000", "127.0.0.1:8000", "[::1]:8000", "[::1]",
    "192.168.1.20:8000", "app.localhost:8000", "LOCALHOST",
])
def test_host_allowed(host):
    assert host_allowed(host)


@pytest.mark.parametrize("host", [None, "", "evil.example", "evil.example:8000", "localhost.evil.example", "testserver"])
def test_host_rejected(host):
    assert not host_allowed(host)


def test_host_extra_names_case_insensitive():
    assert host_allowed("NAS.local:8000", {"nas.local"})
    assert not host_allowed("other.local", {"nas.local"})


def test_env_hosts_parsed(monkeypatch):
    from backend.security import allowed_hosts_from_env
    monkeypatch.setenv("VINYLFLOW_ALLOWED_HOSTS", " nas.local , Box.lan ,")
    assert allowed_hosts_from_env() == {"nas.local", "box.lan"}


def test_testserver_rejected_without_env(no_extra_hosts):
    assert client.get("/api/status").status_code == 400


def test_origin_helper():
    assert origin_allowed(None, "http", "localhost:8000")
    assert origin_allowed("http://localhost:8000", "http", "localhost:8000")
    assert origin_allowed("HTTP://LocalHost:8000", "http", "localhost:8000")
    assert origin_allowed("http://localhost", "http", "localhost:80")
    assert origin_allowed("https://nas.local", "https", "nas.local:443")
    assert origin_allowed("http://[::1]:8000", "ws", "[::1]:8000")
    assert not origin_allowed("http://localhost:9000", "http", "localhost:8000")
    assert not origin_allowed("https://localhost:8000", "http", "localhost:8000")
    assert not origin_allowed("https://evil.example", "http", "localhost:8000")
    assert not origin_allowed("null", "http", "localhost:8000")
    assert not origin_allowed("http://localhost:8000", "http", None)


def test_cross_origin_put_config_blocked(tmp_path):
    before = client.get("/api/config").json()
    r = client.put("/api/config", json={"output_dir": str(tmp_path / "x")}, headers=EVIL)
    assert r.status_code == 403
    assert r.text == "Cross-origin request blocked"
    assert client.get("/api/config").json() == before


def test_same_origin_not_blocked():
    r = client.delete("/api/queue/no-such-file", headers={"Origin": "http://testserver"})
    assert r.status_code != 403


def test_cross_origin_upload_blocked(tmp_path, monkeypatch):
    monkeypatch.setattr(api, "UPLOAD_DIR", tmp_path)
    r = client.post("/api/upload", files={"file": ("a.wav", b"RIFF", "audio/wav")}, headers=EVIL)
    assert r.status_code == 403
    assert list(tmp_path.iterdir()) == []


def test_get_has_no_cors_headers():
    r = client.get("/api/status", headers=EVIL)
    assert r.status_code == 200
    assert "access-control-allow-origin" not in r.headers


def test_preflight_gets_no_allow_origin():
    r = client.options("/api/config", headers={**EVIL, "Access-Control-Request-Method": "PUT"})
    assert "access-control-allow-origin" not in r.headers


def test_bad_host_rejected():
    r = client.get("/api/status", headers={"Host": "evil.example"})
    assert r.status_code == 400
    assert r.text == "Host not allowed"


def test_websocket_evil_origin_refused():
    with pytest.raises(WebSocketDisconnect) as exc:
        with client.websocket_connect("/ws", headers=EVIL):
            pass
    assert exc.value.code == 1008


def test_websocket_no_origin_connects():
    with client.websocket_connect("/ws"):
        pass
