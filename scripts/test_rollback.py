"""故障注入：resumptionToken 永远失效，用于验证整批回滚。

单独放在 scripts/，不进入应用镜像。
"""
import sys
import types

sys.path.insert(0, "/workspace")

from app.oai_client import OAIClient, OAIError, OAIPage  # noqa: E402


class FlakyClient(OAIClient):
    def __init__(self):  # 不建立真实 HTTP 连接
        pass

    def list_records_first(self, *, from_date, until_date):
        return OAIPage(
            records=[{"fake": True}],  # 占位，模拟首页已取到 10 条
            resumption_token="token-a",
            complete_list_size=27,
            cursor=0,
        )

    def list_records_resume(self, token):
        raise OAIError("badResumptionToken", "模拟令牌持续失效")


from app import db, harvester  # noqa: E402

db.init_db("/tmp/e2e/data/failtest.db")
before = db.get_last_until(db.get_connection("/tmp/e2e/data/failtest.db"))
print("采集前 last_until:", before)

try:
    harvester.harvest(
        FlakyClient(), until_date="2026-10-27", from_date="2026-10-01"
    )
except OAIError as e:
    print("预期失败抛出:", e)

conn = db.get_connection("/tmp/e2e/data/failtest.db")
print("采集后 last_until（应仍为 None）:", db.get_last_until(conn))
print("书目条数（应为 0，半批未入库）:",
      conn.execute("SELECT COUNT(*) FROM bib_records").fetchone()[0])
run = conn.execute(
    "SELECT status, error FROM harvest_runs ORDER BY id DESC LIMIT 1"
).fetchone()
print("最近运行:", dict(run))
conn.close()
