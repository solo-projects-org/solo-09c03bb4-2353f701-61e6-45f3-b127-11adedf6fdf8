"""测试夹具：临时 SQLite + 独立端口的样例 OAI 源 + FastAPI TestClient。"""
from __future__ import annotations

import os
import socket
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "sample-oai"))


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@pytest.fixture()
def oai_server(monkeypatch, tmp_path):
    """每个测试一个全新的样例 OAI 源（PAGE_SIZE=10）。"""
    import importlib

    port = _free_port()
    server_mod = importlib.import_module("server")
    monkeypatch.setenv("BASE_URL", f"http://127.0.0.1:{port}/oai")
    monkeypatch.setattr(server_mod, "PAGE_SIZE", 10)
    monkeypatch.setattr(server_mod, "TOKEN_TTL", 3600)
    monkeypatch.setattr(server_mod, "TOKENS", server_mod.TokenStore())
    httpd = ThreadingHTTPServer(("127.0.0.1", port), server_mod.Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield server_mod, f"http://127.0.0.1:{port}/oai"
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join(timeout=5)


@pytest.fixture()
def client(monkeypatch, tmp_path, oai_server):
    _, endpoint = oai_server
    db_path = tmp_path / "test.db"

    # 模块在导入时读取环境变量；这里直接 patch 模块属性，避免 reload
    # 造成 harvester/main 持有不同 db 模块对象
    from app import db as db_mod
    from app import main as main_mod

    monkeypatch.setattr(db_mod, "DB_PATH", str(db_path))
    monkeypatch.setattr(main_mod, "OAI_ENDPOINT", endpoint)
    db_mod.init_db()
    with TestClient(main_mod.app) as test_client:
        yield test_client, db_mod, oai_server[0]
