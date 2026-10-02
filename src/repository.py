"""SQLite 表结构与事务访问。"""
import json
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from .domain import Conflict, NotFound


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.db_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 15000")
        return connection

    def _init_schema(self) -> None:
        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    reference TEXT NOT NULL UNIQUE,
                    state TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    payload TEXT NOT NULL,
                    created_by TEXT NOT NULL,
                    updated_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    record_id INTEGER NOT NULL REFERENCES records(id) ON DELETE CASCADE,
                    action TEXT NOT NULL,
                    actor_id TEXT NOT NULL,
                    version INTEGER NOT NULL,
                    details TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_records_state ON records(state);
                CREATE INDEX IF NOT EXISTS idx_audit_record ON audit_events(record_id, id);
                """
            )
            self._migrate_legacy_tolling(connection)

    def _migrate_legacy_tolling(self, connection: sqlite3.Connection) -> None:
        """旧数据升级：按当前案件状态补齐停表区间与期限字段，幂等可重复执行。"""
        rows = connection.execute("SELECT id,state,version,payload FROM records").fetchall()
        now = _now()
        for row in rows:
            payload = json.loads(row["payload"])
            if "tolling_intervals" in payload:
                continue
            intervals = []
            clock_paused = False
            paused_remaining = None
            request_day = payload.get("evidence_request_day")
            if isinstance(request_day, int) and not payload.get("evidence_withdrawn"):
                state = row["state"]
                if state == "evidence_requested":
                    intervals.append({"start_day": request_day, "end_day": None, "reason": "evidence_request", "closed_by": None, "migrated": True})
                    clock_paused = True
                    paused_remaining = int(payload["deadline_day"]) - request_day
                elif state in {"response_received", "decided", "appealed", "closed"}:
                    end_day = max(int(payload.get("response_day", request_day)), request_day)
                    intervals.append({"start_day": request_day, "end_day": end_day, "reason": "evidence_request", "closed_by": "respond", "migrated": True})
                    if state == "response_received":
                        paused = int(payload["deadline_day"]) - request_day
                        payload["deadline_day"] = end_day + paused
                        payload["days_remaining"] = int(payload["deadline_day"]) - end_day
                        payload["overdue"] = payload["days_remaining"] < 0
            payload["tolling_intervals"] = intervals
            payload["clock_paused"] = clock_paused
            payload["paused_days_remaining"] = paused_remaining
            payload.setdefault("delivery_method", None)
            payload.setdefault("delivery_day", None)
            payload.setdefault("delivery_confirmed", False)
            payload.setdefault("evidence_withdrawn", False)
            payload.setdefault("evidence_overdue", False)
            payload.setdefault("late_days", 0)
            if "allowed_days" not in payload and isinstance(payload.get("evidence_due_day"), int) and isinstance(request_day, int):
                payload["allowed_days"] = int(payload["evidence_due_day"]) - request_day
            details = None
            if intervals:
                payload["deadline_basis"] = "旧数据升级：按当前案件状态补齐停表记录"
                details = {"summary": "旧数据升级：按当前案件状态补齐停表记录", "state": row["state"], "tolling_intervals": intervals, "clock_paused": clock_paused, "paused_days_remaining": paused_remaining}
            else:
                payload.setdefault("deadline_basis", "初始期限：受理日第%s日+法定%s天=第%s日" % (payload.get("received_day"), payload.get("deadline_days"), payload.get("deadline_day")))
            connection.execute("UPDATE records SET payload=? WHERE id=?", (json.dumps(payload, ensure_ascii=False, sort_keys=True), row["id"]))
            if details is not None:
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (row["id"], "legacy_tolling_backfill", "system", int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), now),
                )

    @staticmethod
    def _row(row: sqlite3.Row) -> Dict[str, Any]:
        item = dict(row)
        item["payload"] = json.loads(item["payload"])
        return item

    def create(self, reference: str, state: str, payload: Dict[str, Any], actor_id: str) -> Dict[str, Any]:
        now = _now()
        try:
            with self._connect() as connection:
                cursor = connection.execute(
                    "INSERT INTO records(reference,state,version,payload,created_by,updated_by,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                    (reference, state, 1, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, actor_id, now, now),
                )
                record_id = int(cursor.lastrowid)
                connection.execute(
                    "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                    (record_id, "created", actor_id, 1, json.dumps({"state": state}, ensure_ascii=False, sort_keys=True), now),
                )
                row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        except sqlite3.IntegrityError as exc:
            raise Conflict("reference已存在") from exc
        return self._row(row)

    def get(self, record_id: int) -> Dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        if row is None:
            raise NotFound("记录不存在")
        return self._row(row)

    def list_records(self, state: Optional[str] = None, limit: int = 100) -> List[Dict[str, Any]]:
        limit = max(1, min(int(limit), 500))
        with self._connect() as connection:
            if state:
                rows = connection.execute("SELECT * FROM records WHERE state=? ORDER BY id DESC LIMIT ?", (state, limit)).fetchall()
            else:
                rows = connection.execute("SELECT * FROM records ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [self._row(row) for row in rows]

    def mutate(self, record_id: int, expected_version: int, state: str, payload: Dict[str, Any], actor_id: str, action: str, details: Dict[str, Any]) -> Dict[str, Any]:
        now = _now()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                connection.rollback()
                raise NotFound("记录不存在")
            if int(row["version"]) != int(expected_version):
                connection.rollback()
                raise Conflict("版本冲突，请刷新后重试")
            version = int(expected_version) + 1
            connection.execute(
                "UPDATE records SET state=?,version=?,payload=?,updated_by=?,updated_at=? WHERE id=?",
                (state, version, json.dumps(payload, ensure_ascii=False, sort_keys=True), actor_id, now, record_id),
            )
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, version, json.dumps(details, ensure_ascii=False, sort_keys=True), now),
            )
            result = connection.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
            connection.commit()
        return self._row(result)

    def add_audit(self, record_id: int, actor_id: str, action: str, details: Dict[str, Any]) -> None:
        with self._connect() as connection:
            row = connection.execute("SELECT version FROM records WHERE id=?", (record_id,)).fetchone()
            if row is None:
                raise NotFound("记录不存在")
            connection.execute(
                "INSERT INTO audit_events(record_id,action,actor_id,version,details,created_at) VALUES(?,?,?,?,?,?)",
                (record_id, action, actor_id, int(row["version"]), json.dumps(details, ensure_ascii=False, sort_keys=True), _now()),
            )

    def audit_timeline(self, record_id: int) -> List[Dict[str, Any]]:
        self.get(record_id)
        with self._connect() as connection:
            rows = connection.execute("SELECT * FROM audit_events WHERE record_id=? ORDER BY id", (record_id,)).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["details"] = json.loads(item["details"])
            result.append(item)
        return result

    def stats(self) -> Dict[str, int]:
        with self._connect() as connection:
            rows = connection.execute("SELECT state, COUNT(*) AS total FROM records GROUP BY state").fetchall()
        return {str(row["state"]): int(row["total"]) for row in rows}

    def health(self) -> bool:
        try:
            with self._connect() as connection:
                connection.execute("SELECT 1").fetchone()
            return True
        except sqlite3.Error:
            return False
