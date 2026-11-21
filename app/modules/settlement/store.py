"""闭馆日结存储：只追加、不改写。

- 同一日结键（规则版次 + 各班归卷摘要 + 持续门槛）重复提交返回旧版，不新增行。
- 同一天的不同输入生成新版本行，旧版保留；取最新版作为当前日结。
- 写入在单事务内完成，事务失败（含 sqlite 错误）不会改动任何既有版本。
"""
import json
import sqlite3
from contextlib import closing, contextmanager

from app.manifest import canonical


class SettlementStore:
    def __init__(self, path):
        self.path = path

    @contextmanager
    def connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection, connection:
            yield connection

    def setup(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("""
                CREATE TABLE IF NOT EXISTS daily_settlements (
                    id TEXT NOT NULL,
                    day TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    current INTEGER NOT NULL,
                    report TEXT NOT NULL,
                    created TEXT NOT NULL,
                    PRIMARY KEY (id)
                )
            """)
            connection.execute(
                "CREATE INDEX IF NOT EXISTS idx_settlement_day ON daily_settlements(day, version DESC)")

    def insert(self, report, settlement_id, created):
        """返回 (报告, 是否新建, 版本号)。整方法在一个事务内，异常时连接回滚。"""
        day = report["day"]
        with self.connect() as connection:
            existing = connection.execute(
                "SELECT report, version FROM daily_settlements WHERE id = ?",
                (settlement_id,)).fetchone()
            if existing is not None:
                return json.loads(existing[0]), False, existing[1]
            latest = connection.execute(
                "SELECT MAX(version) FROM daily_settlements WHERE day = ?", (day,)).fetchone()[0]
            version = (latest or 0) + 1
            connection.execute("UPDATE daily_settlements SET current = 0 WHERE day = ?", (day,))
            connection.execute(
                "INSERT INTO daily_settlements VALUES (?, ?, ?, 1, ?, ?)",
                (settlement_id, day, version, canonical(report), created))
            record = connection.execute(
                "SELECT report FROM daily_settlements WHERE id = ?", (settlement_id,)).fetchone()
        return json.loads(record[0]), True, version

    def fetch(self, settlement_id):
        with self.connect() as connection:
            record = connection.execute(
                "SELECT report FROM daily_settlements WHERE id = ?", (settlement_id,)).fetchone()
        if record is None:
            from app.errors import FieldError
            raise FieldError("UNKNOWN_SETTLEMENT", "该日结未登记", 404)
        return json.loads(record[0])

    def current_for_day(self, day):
        with self.connect() as connection:
            record = connection.execute(
                "SELECT report FROM daily_settlements WHERE day = ? AND current = 1", (day,)).fetchone()
        return json.loads(record[0]) if record else None

    def history(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT day, version, current, id, created, report FROM daily_settlements "
                "ORDER BY day DESC, version DESC").fetchall()
        return [{
            "day": day, "version": version, "current": bool(current), "id": settlement_id,
            "created": created, "window": json.loads(report)["window"],
            "shifts": [item["shift"] for item in json.loads(report)["shifts"]],
            "tally": json.loads(report)["tally"],
        } for day, version, current, settlement_id, created, report in rows]

    def days(self):
        with self.connect() as connection:
            rows = connection.execute(
                "SELECT DISTINCT day FROM daily_settlements ORDER BY day DESC").fetchall()
        return [row[0] for row in rows]
