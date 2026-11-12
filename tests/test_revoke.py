"""撤销补采 API 的端到端行为测试。"""
from __future__ import annotations

import threading

import pytest

from app import harvester


def _harvest(client, payload):
    resp = client.post("/api/v1/harvest", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()


def _revoke(client, run_id, expected=200):
    resp = client.post(f"/api/v1/runs/{run_id}/revoke")
    assert resp.status_code == expected, resp.text
    return resp.json()


def test_revoke_second_batch_restores_catalog_and_progress(client):
    api, _db, _oai = client
    first = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    second = _harvest(api, {"until": "2026-10-20"})
    assert second["inserted"] == 5  # rec-016..rec-020 本批新增

    result = _revoke(api, second["run_id"])
    # 10-15 的 rec-015 被本批改写过，需恢复；5 条新增移除
    assert result["status"] == "revoked"
    assert result["restored"] == 1
    assert result["deleted"] == 5
    assert result["prev_last_until"] == "2026-10-15"

    # 目录恢复为第一批的 15 条（含 1 条 withdrawn）
    catalog = api.get("/api/v1/catalog?status=all").json()
    assert len(catalog) == 15
    assert {r["identifier"] for r in catalog} == {
        f"oai:sample.lib:rec-{i:03d}" for i in range(1, 16)
    }
    # 进度退回批前
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-15"
    # 运行历史标记 revoked
    run = next(r for r in api.get("/api/v1/runs").json()
               if r["id"] == second["run_id"])
    assert run["status"] == "revoked"
    assert run["revoked_at"]


def test_revoke_first_batch_shows_never_harvested(client):
    api, _db, _oai = client
    first = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    result = _revoke(api, first["run_id"])

    assert result["prev_last_until"] is None
    progress = api.get("/api/v1/progress").json()
    assert progress["last_until"] is None
    assert progress["last_harvest_at"] is None
    assert api.get("/api/v1/catalog?status=all").json() == []


def test_revoke_restores_updated_content_and_status(client):
    api, _db, _oai = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-10"})
    before = api.get("/api/v1/catalog/oai:sample.lib:rec-005").json()

    # 同区间重采：全部为 updated；撤销后内容应与批前逐字段一致
    again = _harvest(api, {"from": "2026-10-01", "until": "2026-10-10"})
    assert again["updated"] == 10 and again["inserted"] == 0
    _revoke(api, again["run_id"])

    after = api.get("/api/v1/catalog/oai:sample.lib:rec-005").json()
    assert after == before
    assert len(api.get("/api/v1/catalog?status=all").json()) == 10


def test_revoke_restores_withdrawn_record(client):
    api, _db, _oai = client
    # 第一批：rec-013 以删除标记首次到达，落 withdrawn 墓碑
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    assert (
        api.get("/api/v1/catalog/oai:sample.lib:rec-013").json()["status"]
        == "withdrawn"
    )
    # 第二批：同区间重采，14 条 active 被更新，rec-013 墓碑再次撤下
    second = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    assert second["updated"] == 14 and second["withdrawn"] == 1
    _revoke(api, second["run_id"])
    # 批前是 withdrawn 就恢复为 withdrawn，状态不丢失
    restored = api.get("/api/v1/catalog/oai:sample.lib:rec-013").json()
    assert restored["status"] == "withdrawn"
    assert len(api.get("/api/v1/catalog?status=all").json()) == 15


def test_revoke_deletes_tombstone_added_in_batch(client):
    api, _db, _oai = client
    # rec-013 不在第一批区间内（库中不存在）
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-12"})
    assert len(api.get("/api/v1/catalog?status=all").json()) == 12
    # 第二批中 rec-013 以删除标记首次到达 -> 本批新增墓碑
    second = _harvest(api, {"until": "2026-10-15"})
    assert (
        api.get("/api/v1/catalog/oai:sample.lib:rec-013").json()["status"]
        == "withdrawn"
    )
    result = _revoke(api, second["run_id"])
    assert result["restored"] == 1  # rec-012 批前已存在，恢复批前整行
    assert result["deleted"] == 3   # rec-013 墓碑 + rec-014/015 两条新增
    # 本批新增的墓碑随撤销移除，查询返回 404
    assert api.get(
        "/api/v1/catalog/oai:sample.lib:rec-013"
    ).status_code == 404
    assert len(api.get("/api/v1/catalog?status=all").json()) == 12


def test_revoke_no_match_batch_rolls_date_back(client):
    api, _db, _oai = client
    first = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    empty = _harvest(api, {"from": "2027-01-01", "until": "2027-01-31"})
    assert empty["fetched"] == 0

    result = _revoke(api, empty["run_id"])
    assert result["restored"] == 0 and result["deleted"] == 0
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-15"
    # 第一批仍然完整
    assert len(api.get("/api/v1/catalog?status=all").json()) == 15


def test_revoke_no_match_first_batch_back_to_never_harvested(client):
    api, _db, _oai = client
    empty = _harvest(api, {"from": "2027-01-01", "until": "2027-01-31"})
    _revoke(api, empty["run_id"])
    progress = api.get("/api/v1/progress").json()
    assert progress["last_until"] is None
    assert progress["last_harvest_at"] is None


def test_failed_batch_does_not_block_revoke_of_last_success(client, monkeypatch):
    api, db_mod, oai_mod = client
    first = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    second = _harvest(api, {"until": "2026-10-20"})

    # 制造一次失败批次：resumptionToken 持续失效，整批回滚。
    # 区间 10-20~10-27 有 7 条，PAGE_SIZE=10 单页，因此用更宽区间确保翻页，
    # 并令令牌在重抓时仍失效（不可恢复 -> 502）。
    real_consume = oai_mod.TOKENS.consume

    def always_invalid(token):
        real_consume(token)
        return None

    monkeypatch.setattr(oai_mod.TOKENS, "consume", always_invalid)
    resp = api.post(
        "/api/v1/harvest", json={"from": "2026-10-01", "until": "2026-10-27"}
    )
    assert resp.status_code == 502

    # 失败批次之后，最近一次成功批次仍可撤销
    result = _revoke(api, second["run_id"])
    assert result["run_id"] == second["run_id"]
    assert len(api.get("/api/v1/catalog?status=all").json()) == 15
    assert api.get("/api/v1/progress").json()["last_until"] == "2026-10-15"


def test_cannot_revoke_older_batch_after_newer_success(client):
    api, _db, _oai = client
    first = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    _harvest(api, {"until": "2026-10-20"})

    body = _revoke(api, first["run_id"], expected=409)
    assert "最近一次成功" in body["detail"]
    # 库未受影响
    assert len(api.get("/api/v1/catalog?status=all").json()) == 20


def test_cannot_revoke_twice(client):
    api, _db, _oai = client
    first = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    _revoke(api, first["run_id"])
    body = _revoke(api, first["run_id"], expected=409)
    assert "已撤销" in body["detail"]


def test_revoke_unknown_run_returns_404(client):
    api, _db, _oai = client
    body = _revoke(api, 9999, expected=404)
    assert "不存在" in body["detail"]


def test_harvest_after_revoke_allows_revoke_again(client):
    api, _db, _oai = client
    first = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    _revoke(api, first["run_id"])
    # 源馆修正后重新补采，新批次成为最近成功批次，可再次撤销
    again = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    result = _revoke(api, again["run_id"])
    assert result["run_id"] == again["run_id"]
    assert api.get("/api/v1/catalog?status=all").json() == []


def test_revoke_conflicts_with_running_harvest(client, monkeypatch):
    api, db_mod, oai_mod = client
    _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})
    run_id = api.get("/api/v1/runs").json()[0]["id"]

    entered = threading.Event()
    release = threading.Event()
    real_get_run = harvester.db.get_run

    def blocking_get_run(conn, rid):
        entered.set()
        release.wait(timeout=5)
        return real_get_run(conn, rid)

    monkeypatch.setattr(harvester.db, "get_run", blocking_get_run)
    errors = []

    def do_revoke():
        try:
            api.post(f"/api/v1/runs/{run_id}/revoke")
        except Exception as exc:  # pragma: no cover - 失败诊断
            errors.append(exc)

    t = threading.Thread(target=do_revoke)
    t.start()
    assert entered.wait(timeout=5)
    try:
        # 撤销进行中：采集互斥，后发请求被 409 拒绝
        resp = api.post(
            "/api/v1/harvest", json={"until": "2026-10-20"}
        )
        assert resp.status_code == 409
        assert "撤销" in resp.json()["detail"]
    finally:
        release.set()
        t.join(timeout=5)
    assert not errors


def test_harvest_conflicts_with_running_revoke_other_direction(client, monkeypatch):
    api, db_mod, oai_mod = client

    entered = threading.Event()
    release = threading.Event()
    real_start_run = harvester.db.start_run

    def blocking_start_run(conn, from_date, until_date):
        entered.set()
        release.wait(timeout=5)
        return real_start_run(conn, from_date, until_date)

    monkeypatch.setattr(harvester.db, "start_run", blocking_start_run)
    errors = []

    def do_harvest():
        try:
            api.post(
                "/api/v1/harvest",
                json={"from": "2026-10-01", "until": "2026-10-15"},
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    t = threading.Thread(target=do_harvest)
    t.start()
    assert entered.wait(timeout=5)
    try:
        # 采集进行中（锁已持有、尚未结束）：撤销互斥被拒
        resp = api.post("/api/v1/runs/1/revoke")
        assert resp.status_code == 409
    finally:
        release.set()
        t.join(timeout=5)
    assert not errors


def test_revoke_atomic_when_restore_fails(client, monkeypatch):
    api, db_mod, _oai = client
    batch = _harvest(api, {"from": "2026-10-01", "until": "2026-10-15"})

    def boom(conn, run_id):  # 让事务中途失败
        raise RuntimeError("模拟恢复失败")

    monkeypatch.setattr(db_mod, "restore_bib_snapshot", boom)
    resp = api.post(f"/api/v1/runs/{batch['run_id']}/revoke")
    assert resp.status_code == 500

    conn = db_mod.get_connection()
    try:
        # 全部不改：目录、进度、运行状态保持撤销前
        assert conn.execute(
            "SELECT COUNT(*) FROM bib_records"
        ).fetchone()[0] == 15
        assert db_mod.get_last_until(conn) == "2026-10-15"
        run = conn.execute(
            "SELECT status FROM harvest_runs WHERE id = ?", (batch["run_id"],)
        ).fetchone()
        assert run["status"] == "success"
        # 快照仍在，故障排除后还能撤销
        assert conn.execute(
            "SELECT COUNT(*) FROM bib_snapshots WHERE run_id = ?",
            (batch["run_id"],),
        ).fetchone()[0] == 15
    finally:
        conn.close()


def test_legacy_db_without_snapshot_cannot_revoke(tmp_path, monkeypatch):
    """升级场景：旧库（新列全为 NULL）的目录与进度保留，旧批次不可撤销。"""
    from app import db as db_mod
    from app import main as main_mod
    from fastapi.testclient import TestClient

    legacy = tmp_path / "legacy.db"
    conn = db_mod.get_connection(str(legacy))
    conn.executescript(
        """
        CREATE TABLE bib_records (
            identifier TEXT PRIMARY KEY, datestamp TEXT NOT NULL,
            status TEXT NOT NULL, title TEXT, creator TEXT, publisher TEXT,
            subject TEXT, description TEXT, date TEXT, type TEXT, format TEXT,
            identifier_dc TEXT, source TEXT, language TEXT, relation TEXT,
            coverage TEXT, rights TEXT, raw_dc TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        INSERT INTO bib_records VALUES (
            'oai:legacy:rec-001', '2026-09-01', 'active', '旧书目', NULL, NULL,
            NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL,
            '{}', '2026-09-01T00:00:00Z'
        );
        CREATE TABLE harvest_progress (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            last_until TEXT NOT NULL, last_harvest_at TEXT NOT NULL
        );
        INSERT INTO harvest_progress VALUES (1, '2026-09-01', '2026-09-01T00:00:00Z');
        CREATE TABLE harvest_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT, started_at TEXT NOT NULL,
            finished_at TEXT, from_date TEXT NOT NULL, until_date TEXT NOT NULL,
            status TEXT NOT NULL, pages INTEGER NOT NULL DEFAULT 0,
            fetched INTEGER NOT NULL DEFAULT 0, inserted INTEGER NOT NULL DEFAULT 0,
            updated INTEGER NOT NULL DEFAULT 0, withdrawn INTEGER NOT NULL DEFAULT 0,
            retries INTEGER NOT NULL DEFAULT 0, error TEXT
        );
        INSERT INTO harvest_runs (started_at, from_date, until_date, status)
            VALUES ('2026-09-01T00:00:00Z', '2026-09-01', '2026-09-01', 'success');
        """
    )
    conn.commit()
    conn.close()

    monkeypatch.setattr(db_mod, "DB_PATH", str(legacy))
    db_mod.init_db()  # 触发增量迁移

    conn = db_mod.get_connection(str(legacy))
    try:
        # 既有目录和进度原样保留
        assert conn.execute(
            "SELECT title FROM bib_records WHERE identifier = 'oai:legacy:rec-001'"
        ).fetchone()[0] == "旧书目"
        assert db_mod.get_last_until(conn) == "2026-09-01"
        legacy_run = conn.execute(
            "SELECT snapshot_version FROM harvest_runs WHERE id = 1"
        ).fetchone()
        assert legacy_run["snapshot_version"] is None
    finally:
        conn.close()

    with TestClient(main_mod.app) as api:
        resp = api.post("/api/v1/runs/1/revoke")
        assert resp.status_code == 409
        assert "升级前" in resp.json()["detail"]
        # 旧数据仍在
        assert len(api.get("/api/v1/catalog?status=all").json()) == 1
        assert api.get("/api/v1/progress").json()["last_until"] == "2026-09-01"
