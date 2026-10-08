import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from backend.ids import uuid7


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


class StateStore:
    def __init__(self, path: Path):
        self.path = path
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._init()

    @contextmanager
    def connect(self):
        with self._lock:
            db = sqlite3.connect(self.path, timeout=30)
            db.row_factory = sqlite3.Row
            db.execute("PRAGMA foreign_keys=ON")
            try:
                yield db
                db.commit()
            finally:
                db.close()

    def _init(self):
        schema = """
        CREATE TABLE IF NOT EXISTS sessions(
          id TEXT PRIMARY KEY, locale TEXT, market TEXT, currency TEXT,
          long_term_memory INTEGER, requirements_version INTEGER DEFAULT 0,
          requirements_json TEXT DEFAULT '{}', status TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS messages(
          id TEXT PRIMARY KEY, session_id TEXT, client_message_id TEXT,
          role TEXT, text TEXT, created_at TEXT,
          UNIQUE(session_id, client_message_id),
          FOREIGN KEY(session_id) REFERENCES sessions(id));
        CREATE TABLE IF NOT EXISTS requirements(
          session_id TEXT, version INTEGER, data_json TEXT, created_at TEXT,
          PRIMARY KEY(session_id, version));
        CREATE TABLE IF NOT EXISTS runs(
          id TEXT PRIMARY KEY, session_id TEXT, requirements_version INTEGER,
          snapshot_id TEXT, orchestration_mode TEXT, status TEXT, stage TEXT,
          result_json TEXT, error_json TEXT, created_at TEXT, updated_at TEXT,
          FOREIGN KEY(session_id) REFERENCES sessions(id));
        CREATE TABLE IF NOT EXISTS events(
          seq INTEGER PRIMARY KEY AUTOINCREMENT, run_id TEXT, event_type TEXT,
          data_json TEXT, created_at TEXT);
        CREATE TABLE IF NOT EXISTS memories(
          id TEXT PRIMARY KEY, session_id TEXT, kind TEXT, value_json TEXT,
          confirmed INTEGER, version INTEGER, source_message_id TEXT,
          deleted INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS operation_state(
          run_id TEXT PRIMARY KEY, state_json TEXT, version INTEGER, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS tool_calls(
          id TEXT PRIMARY KEY, run_id TEXT, agent_role TEXT, tool_name TEXT,
          replay_policy TEXT, status TEXT, args_json TEXT, result_json TEXT,
          error_json TEXT, created_at TEXT, updated_at TEXT);
        CREATE TABLE IF NOT EXISTS usage_ledger(
          id TEXT PRIMARY KEY, run_id TEXT, agent_role TEXT, kind TEXT,
          input_units INTEGER, output_units INTEGER, details_json TEXT, created_at TEXT);
        """
        with self.connect() as db:
            db.executescript(schema)
            columns = {row[1] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
            if "owner_id" not in columns:
                db.execute("ALTER TABLE sessions ADD COLUMN owner_id TEXT NOT NULL DEFAULT 'dev-user'")
            run_columns = {row[1] for row in db.execute("PRAGMA table_info(runs)").fetchall()}
            if "idempotency_key" not in run_columns:
                db.execute("ALTER TABLE runs ADD COLUMN idempotency_key TEXT")
                db.execute("CREATE UNIQUE INDEX IF NOT EXISTS run_idempotency ON runs(session_id,idempotency_key) WHERE idempotency_key IS NOT NULL")

    def create_session(self, data: dict[str, Any], owner_id: str = "dev-user") -> dict[str, Any]:
        sid = uuid7("sess")
        stamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO sessions(id,locale,market,currency,long_term_memory,requirements_version,requirements_json,status,created_at,updated_at,owner_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (
                sid, data["locale"], data["market"], data["currency"],
                int(data["long_term_memory"]), 0, "{}", "collecting_requirements", stamp, stamp, owner_id))
        return self.session(sid)

    def session(self, sid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM sessions WHERE id=?", (sid,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["requirements"] = json.loads(item.pop("requirements_json"))
            item["long_term_memory"] = bool(item["long_term_memory"])
            return item

    def add_message(self, sid: str, client_id: str, text: str) -> str:
        mid = uuid7("msg")
        with self.connect() as db:
            existing = db.execute(
                "SELECT id FROM messages WHERE session_id=? AND client_message_id=?", (sid, client_id)).fetchone()
            if existing:
                return existing["id"]
            db.execute("INSERT INTO messages VALUES(?,?,?,?,?,?)", (mid, sid, client_id, "user", text, now()))
        return mid

    def update_requirements(self, sid: str, expected: int, data: dict[str, Any], status: str) -> int:
        with self.connect() as db:
            row = db.execute("SELECT requirements_version FROM sessions WHERE id=?", (sid,)).fetchone()
            if row is None:
                raise KeyError(sid)
            if row[0] != expected:
                raise ValueError(row[0])
            version = expected + 1
            payload = json.dumps(data, ensure_ascii=False)
            stamp = now()
            db.execute("INSERT INTO requirements VALUES(?,?,?,?)", (sid, version, payload, stamp))
            db.execute("UPDATE sessions SET requirements_version=?, requirements_json=?, status=?, updated_at=? WHERE id=?",
                       (version, payload, status, stamp, sid))
            return version

    def requirements(self, sid: str, version: int) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT data_json FROM requirements WHERE session_id=? AND version=?", (sid, version)).fetchone()
            return json.loads(row[0]) if row else None

    def create_run(self, sid: str, version: int, snapshot: str, mode: str, idempotency_key: str | None = None,
                   base_run_id: str | None = None) -> dict[str, Any]:
        rid = uuid7("run")
        stamp = now()
        with self.connect() as db:
            if idempotency_key:
                existing = db.execute("SELECT id FROM runs WHERE session_id=? AND idempotency_key=?", (sid, idempotency_key)).fetchone()
                if existing:
                    return self.run(existing["id"])
            db.execute("INSERT INTO runs(id,session_id,requirements_version,snapshot_id,orchestration_mode,status,stage,result_json,error_json,created_at,updated_at,idempotency_key) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                       (rid, sid, version, snapshot, mode, "queued", "queued", None, None, stamp, stamp, idempotency_key))
        self.event(rid, "queued", {"stage": "queued"})
        self.checkpoint(rid, {"phase": "queued", "plan_version": 0, "completed_tasks": [], "artifacts": {},
                              "base_run_id": base_run_id})
        return self.run(rid)

    def run(self, rid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (rid,)).fetchone()
            if not row:
                return None
            item = dict(row)
            item["result"] = json.loads(item.pop("result_json")) if item["result_json"] else None
            item["error"] = json.loads(item.pop("error_json")) if item["error_json"] else None
            return item

    def latest_completed_run(self, sid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT id FROM runs WHERE session_id=? AND status='completed' ORDER BY created_at DESC LIMIT 1",
                             (sid,)).fetchone()
        return self.run(row["id"]) if row else None

    def run_owner(self, rid: str) -> str | None:
        with self.connect() as db:
            row = db.execute("SELECT s.owner_id FROM runs r JOIN sessions s ON s.id=r.session_id WHERE r.id=?", (rid,)).fetchone()
            return row[0] if row else None

    def set_run(self, rid: str, status: str, stage: str, result=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?,stage=?,result_json=?,error_json=?,updated_at=? WHERE id=?",
                       (status, stage, json.dumps(result, ensure_ascii=False) if result else None,
                        json.dumps(error, ensure_ascii=False) if error else None, now(), rid))

    def is_cancelled(self, rid: str) -> bool:
        with self.connect() as db:
            row = db.execute("SELECT status FROM runs WHERE id=?", (rid,)).fetchone()
            return bool(row and row[0] == "cancelled")

    def event(self, rid: str, event_type: str, data: dict[str, Any]):
        with self.connect() as db:
            db.execute("INSERT INTO events(run_id,event_type,data_json,created_at) VALUES(?,?,?,?)",
                       (rid, event_type, json.dumps(data, ensure_ascii=False), now()))

    def events(self, rid: str, after: int = 0) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE run_id=? AND seq>? ORDER BY seq", (rid, after)).fetchall()
            return [{**dict(r), "data": json.loads(r["data_json"])} for r in rows]

    def list_memories(self, sid: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM memories WHERE session_id=? AND deleted=0 ORDER BY updated_at DESC", (sid,)).fetchall()
            return [{**dict(r), "value": json.loads(r["value_json"]), "confirmed": bool(r["confirmed"])} for r in rows]

    def add_memory(self, sid: str, kind: str, value: Any, confirmed: bool, source: str | None) -> dict[str, Any]:
        mid, stamp = uuid7("mem"), now()
        with self.connect() as db:
            db.execute("INSERT INTO memories VALUES(?,?,?,?,?,?,?,?,?,?)",
                       (mid, sid, kind, json.dumps(value, ensure_ascii=False), int(confirmed), 1, source, 0, stamp, stamp))
        return self.memory(mid)

    def memory(self, mid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM memories WHERE id=? AND deleted=0", (mid,)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["value"] = json.loads(result["value_json"])
            return result

    def patch_memory(self, mid: str, expected: int, value: Any, confirmed: bool) -> dict[str, Any]:
        with self.connect() as db:
            row = db.execute("SELECT version FROM memories WHERE id=? AND deleted=0", (mid,)).fetchone()
            if not row:
                raise KeyError(mid)
            if row[0] != expected:
                raise ValueError(row[0])
            db.execute("UPDATE memories SET value_json=?,confirmed=?,version=?,updated_at=? WHERE id=?",
                       (json.dumps(value, ensure_ascii=False), int(confirmed), expected + 1, now(), mid))
        return self.memory(mid)

    def delete_memory(self, mid: str) -> bool:
        with self.connect() as db:
            cur = db.execute("UPDATE memories SET deleted=1,updated_at=? WHERE id=? AND deleted=0", (now(), mid))
            return cur.rowcount == 1

    def checkpoint(self, rid: str, state: dict[str, Any]) -> int:
        with self.connect() as db:
            row = db.execute("SELECT version FROM operation_state WHERE run_id=?", (rid,)).fetchone()
            version = (row[0] if row else 0) + 1
            db.execute("INSERT INTO operation_state VALUES(?,?,?,?) ON CONFLICT(run_id) DO UPDATE SET state_json=excluded.state_json,version=excluded.version,updated_at=excluded.updated_at",
                       (rid, json.dumps(state, ensure_ascii=False), version, now()))
            return version

    def operation_state(self, rid: str) -> dict[str, Any] | None:
        with self.connect() as db:
            row = db.execute("SELECT state_json,version FROM operation_state WHERE run_id=?", (rid,)).fetchone()
            return {"state": json.loads(row[0]), "version": row[1]} if row else None

    def tool_intent(self, rid: str, agent: str, tool: str, replay: str, args: dict[str, Any]) -> str:
        call_id = uuid7("tool")
        stamp = now()
        with self.connect() as db:
            db.execute("INSERT INTO tool_calls VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                       (call_id, rid, agent, tool, replay, "intent", json.dumps(args, ensure_ascii=False), None, None, stamp, stamp))
        return call_id

    def tool_pending(self, call_id: str):
        with self.connect() as db:
            db.execute("UPDATE tool_calls SET status='effect_pending',updated_at=? WHERE id=?", (now(), call_id))

    def tool_settle(self, call_id: str, result=None, error=None):
        with self.connect() as db:
            db.execute("UPDATE tool_calls SET status=?,result_json=?,error_json=?,updated_at=? WHERE id=?",
                       ("failed" if error else "completed", json.dumps(result, ensure_ascii=False) if result is not None else None,
                        json.dumps(error, ensure_ascii=False) if error else None, now(), call_id))

    def tool_calls(self, rid: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM tool_calls WHERE run_id=? ORDER BY created_at", (rid,)).fetchall()]

    def reconcile_uncertain_tools(self):
        with self.connect() as db:
            db.execute("UPDATE tool_calls SET status='interrupted',error_json=?,updated_at=? WHERE status='effect_pending'",
                       (json.dumps({"code": "INTERRUPTED_EFFECT", "message": "Process stopped while the tool effect was uncertain."}), now()))

    def usage(self, rid: str, agent: str, kind: str, input_units: int = 0, output_units: int = 0, details=None):
        with self.connect() as db:
            db.execute("INSERT INTO usage_ledger VALUES(?,?,?,?,?,?,?,?)",
                       (uuid7("usage"), rid, agent, kind, input_units, output_units,
                        json.dumps(details or {}, ensure_ascii=False), now()))

    def usage_rows(self, rid: str) -> list[dict[str, Any]]:
        with self.connect() as db:
            return [dict(row) for row in db.execute("SELECT * FROM usage_ledger WHERE run_id=? ORDER BY created_at", (rid,)).fetchall()]
