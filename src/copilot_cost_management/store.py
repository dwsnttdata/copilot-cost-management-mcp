from __future__ import annotations
import json, sqlite3, uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from .config import settings

class ApprovalStore:
    def __init__(self) -> None:
        self.path = settings.db_path
        self._init()

    def _connect(self):
        con = sqlite3.connect(self.path)
        con.row_factory = sqlite3.Row
        return con

    def _init(self):
        with self._connect() as con:
            con.executescript("""
            CREATE TABLE IF NOT EXISTS operations(
              id TEXT PRIMARY KEY, action TEXT NOT NULL, payload TEXT NOT NULL,
              reason TEXT, requested_by TEXT, approved_by TEXT,
              status TEXT NOT NULL, created_at TEXT NOT NULL,
              expires_at TEXT NOT NULL, approved_at TEXT, executed_at TEXT,
              result TEXT
            );
            CREATE TABLE IF NOT EXISTS audit(
              id INTEGER PRIMARY KEY AUTOINCREMENT, operation_id TEXT,
              event TEXT NOT NULL, actor TEXT, timestamp TEXT NOT NULL, details TEXT
            );
            """)

    def propose(self, action: str, payload: dict[str, Any], reason: str, requested_by: str) -> dict[str, Any]:
        now = datetime.now(timezone.utc)
        op_id = str(uuid.uuid4())
        expires = now + timedelta(minutes=settings.approval_ttl_minutes)
        with self._connect() as con:
            con.execute("INSERT INTO operations VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", (
                op_id, action, json.dumps(payload), reason, requested_by, None, "pending",
                now.isoformat(), expires.isoformat(), None, None, None))
            self._audit(con, op_id, "proposed", requested_by, {"action": action, "payload": payload, "reason": reason})
        return self.get(op_id)

    def get(self, op_id: str) -> dict[str, Any]:
        with self._connect() as con:
            row = con.execute("SELECT * FROM operations WHERE id=?", (op_id,)).fetchone()
        if not row:
            raise KeyError(f"Approval operation {op_id} not found")
        data = dict(row)
        data["payload"] = json.loads(data["payload"])
        if data.get("result"):
            data["result"] = json.loads(data["result"])
        return data

    def approve(self, op_id: str, approver: str) -> dict[str, Any]:
        op = self.get(op_id)
        if op["status"] != "pending": raise ValueError(f"Operation is {op['status']}, not pending")
        if datetime.now(timezone.utc) > datetime.fromisoformat(op["expires_at"]):
            self.set_status(op_id, "expired", approver, {"message": "Approval expired"})
            raise ValueError("Approval expired")
        if not approver.strip(): raise ValueError("approver is required")
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute("UPDATE operations SET status='approved', approved_by=?, approved_at=? WHERE id=?", (approver, now, op_id))
            self._audit(con, op_id, "approved", approver, {})
        return self.get(op_id)

    def reject(self, op_id: str, actor: str, reason: str) -> dict[str, Any]:
        op = self.get(op_id)
        if op["status"] not in ("pending", "approved"): raise ValueError(f"Operation is {op['status']}")
        self.set_status(op_id, "rejected", actor, {"reason": reason})
        return self.get(op_id)

    def set_status(self, op_id: str, status: str, actor: str, result: dict[str, Any]):
        now = datetime.now(timezone.utc).isoformat()
        with self._connect() as con:
            con.execute("UPDATE operations SET status=?, executed_at=?, result=? WHERE id=?", (status, now, json.dumps(result), op_id))
            self._audit(con, op_id, status, actor, result)

    def history(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._connect() as con:
            rows = con.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (min(max(limit, 1), 500),)).fetchall()
        return [dict(r) | {"details": json.loads(r["details"] or "{}") } for r in rows]

    @staticmethod
    def _audit(con, op_id: str, event: str, actor: str, details: dict[str, Any]):
        con.execute("INSERT INTO audit(operation_id,event,actor,timestamp,details) VALUES(?,?,?,?,?)",
                    (op_id, event, actor, datetime.now(timezone.utc).isoformat(), json.dumps(details)))

store = ApprovalStore()
