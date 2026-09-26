from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import ID_PREFIX, STATES


class Repository:
    def __init__(self, db_path: str):
        self.db_path = str(db_path)
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(self.db_path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA foreign_keys = ON")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        statuses = ",".join("'" + s.replace("'", "''") + "'" for s in STATES)
        with self.conn:
            self.conn.executescript(f"""
                CREATE TABLE IF NOT EXISTS items (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    title TEXT NOT NULL,
                    description TEXT NOT NULL,
                    severity TEXT NOT NULL,
                    quantity REAL NOT NULL DEFAULT 0,
                    threshold REAL NOT NULL DEFAULT 1,
                    status TEXT NOT NULL CHECK(status IN ({statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_items_external_ref
                    ON items(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    kind TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'open'
                        CHECK(status IN ('open','closed')),
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(item_id, external_ref)
                );
                CREATE TABLE IF NOT EXISTS audit_events (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    action TEXT NOT NULL,
                    entity_type TEXT NOT NULL,
                    entity_id INTEGER NOT NULL,
                    actor TEXT NOT NULL,
                    detail TEXT NOT NULL,
                    previous_hash TEXT NOT NULL,
                    entry_hash TEXT NOT NULL UNIQUE,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS crews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL UNIQUE,
                    skills TEXT NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS resources (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    type TEXT NOT NULL CHECK(type IN ('pump','vehicle')),
                    active INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crews(id),
                    required_skill TEXT NOT NULL,
                    window_start TEXT NOT NULL,
                    window_end TEXT NOT NULL,
                    status TEXT NOT NULL
                        CHECK(status IN ('reserved','arrived','cancelled','completed')),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_dispatches_item ON dispatches(item_id);
                CREATE TABLE IF NOT EXISTS dispatch_resources (
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                    resource_id INTEGER NOT NULL REFERENCES resources(id),
                    resource_type TEXT NOT NULL,
                    returned_at TEXT,
                    PRIMARY KEY(dispatch_id, resource_id)
                );
                CREATE INDEX IF NOT EXISTS ix_dispatch_resources_resource
                    ON dispatch_resources(resource_id);
            """)

    @staticmethod
    def _item(row: sqlite3.Row) -> Dict[str, Any]:
        return dict(row)

    def create_item(self, title: str, description: str, severity: str,
                    quantity: float, threshold: float, external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO items(title, description, severity, quantity, threshold,
                       status, version, external_ref, created_by, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                    (title, description, severity, quantity, threshold, STATES[0], 1,
                     external_ref, actor, now, now),
                )
                item_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("external_ref已存在") from exc
        return self.get_item(item_id)

    def get_item(self, item_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM items WHERE id=?", (item_id,)).fetchone()
        if row is None:
            raise NotFoundError("项目不存在")
        return self._item(row)

    def list_items(self, status: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM items"
        params: tuple = ()
        if status:
            sql += " WHERE status=?"
            params = (status,)
        sql += " ORDER BY id DESC"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [self._item(row) for row in rows]

    def transition_item(self, item_id: int, target: str, expected_version: int,
                        actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE items SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, item_id, expected_version),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute("SELECT 1 FROM items WHERE id=?", (item_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("项目不存在")
                raise ConflictError("版本冲突，请刷新后重试")
        return self.get_item(item_id)

    def add_record(self, item_id: int, kind: str, detail: str, status: str,
                   external_ref: Optional[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        self.get_item(item_id)
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO records(item_id, kind, detail, status, external_ref,
                       created_by, created_at) VALUES(?,?,?,?,?,?,?)""",
                    (item_id, kind, detail, status, external_ref, actor, now),
                )
                record_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("记录唯一标识已存在") from exc
        with self._lock:
            row = self.conn.execute("SELECT * FROM records WHERE id=?", (record_id,)).fetchone()
        return dict(row)

    def list_records(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM records WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [dict(row) for row in rows]

    def open_record_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM records WHERE item_id=? AND status='open'",
                (item_id,),
            ).fetchone()
        return int(row["n"])

    def append_audit(self, action: str, entity_type: str, entity_id: int,
                     actor: str, detail: dict) -> Dict[str, Any]:
        with self._lock, self.conn:
            row = self.conn.execute(
                "SELECT entry_hash FROM audit_events ORDER BY id DESC LIMIT 1"
            ).fetchone()
            previous = row["entry_hash"] if row else "GENESIS"
            event = make_entry(action, entity_type, entity_id, actor, detail, previous)
            cur = self.conn.execute(
                """INSERT INTO audit_events(action, entity_type, entity_id, actor, detail,
                   previous_hash, entry_hash, created_at) VALUES(?,?,?,?,?,?,?,?)""",
                (event["action"], event["entity_type"], event["entity_id"], event["actor"],
                 json.dumps(event["detail"], ensure_ascii=False, sort_keys=True),
                 event["previous_hash"], event["entry_hash"], event["created_at"]),
            )
            event_id = int(cur.lastrowid)
        event["id"] = event_id
        return event

    def list_audit(self, entity_id: Optional[int] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        params: tuple = ()
        if entity_id is not None:
            sql += " WHERE entity_id=?"
            params = (entity_id,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = json.loads(item["detail"])
            result.append(item)
        return result

    def verify_audit_chain(self) -> bool:
        from .audit import calculate_hash
        with self._lock:
            rows = self.conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
        previous = "GENESIS"
        for row in rows:
            if row["previous_hash"] != previous:
                return False
            payload = {
                "action": row["action"], "entity_type": row["entity_type"],
                "entity_id": row["entity_id"], "actor": row["actor"],
                "detail": json.loads(row["detail"]), "created_at": row["created_at"],
            }
            if calculate_hash(previous, payload) != row["entry_hash"]:
                return False
            previous = row["entry_hash"]
        return True

    # ---- 抢修班组、调度资源与处置调度 ----
    def create_crew(self, name, skills, actor):
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    "INSERT INTO crews(name, skills, active, created_by, created_at) VALUES(?,?,1,?,?)",
                    (name, json.dumps(skills, ensure_ascii=False), actor, now))
                crew_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("班组名称已存在") from exc
        return self.get_crew(crew_id)

    def get_crew(self, crew_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM crews WHERE id=?", (crew_id,)).fetchone()
        if row is None:
            raise NotFoundError("班组不存在")
        crew = dict(row)
        crew["skills"] = json.loads(crew["skills"])
        crew["active"] = bool(crew["active"])
        return crew

    def list_crews(self, active_only=True):
        sql = "SELECT * FROM crews"
        if active_only:
            sql += " WHERE active=1"
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql).fetchall()
        result = []
        for row in rows:
            crew = dict(row)
            crew["skills"] = json.loads(crew["skills"])
            crew["active"] = bool(crew["active"])
            result.append(crew)
        return result

    def create_resource(self, name, rtype, actor):
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                "INSERT INTO resources(name, type, active, created_by, created_at) VALUES(?,?,1,?,?)",
                (name, rtype, actor, now))
            resource_id = int(cur.lastrowid)
        return self.get_resource(resource_id)

    def get_resource(self, resource_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
        if row is None:
            raise NotFoundError("资源不存在")
        resource = dict(row)
        resource["active"] = bool(resource["active"])
        return resource

    def list_resources(self, rtype=None, active_only=True):
        sql = "SELECT * FROM resources"
        clauses, params = [], []
        if active_only:
            clauses.append("active=1")
        if rtype:
            clauses.append("type=?")
            params.append(rtype)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        result = []
        for row in rows:
            resource = dict(row)
            resource["active"] = bool(resource["active"])
            result.append(resource)
        return result

    def reserve_dispatch(self, item_id, required_skill, pump_ids, vehicle_ids,
                         window_start, window_end, actor):
        """单事务内匹配班组并占住泵机、车辆；任一占用冲突则整笔不保存。"""
        now = utc_now()
        resource_ids = pump_ids + vehicle_ids
        expected_type = {rid: "pump" for rid in pump_ids}
        expected_type.update({rid: "vehicle" for rid in vehicle_ids})
        with self._lock, self.conn:
            conflicts = []
            placeholders = ",".join("?" for _ in resource_ids)
            rows = self.conn.execute(
                f"""SELECT r.id, r.name, r.type, r.active,
                           d.id AS dispatch_id, d.item_id AS item_id
                    FROM resources r
                    LEFT JOIN dispatch_resources dr
                        ON dr.resource_id=r.id AND dr.returned_at IS NULL
                    LEFT JOIN dispatches d
                        ON d.id=dr.dispatch_id
                           AND d.status IN ('reserved','arrived')
                           AND d.window_start < ? AND ? < d.window_end
                    WHERE r.id IN ({placeholders})
                    ORDER BY r.id""",
                (window_end, window_start, *resource_ids)).fetchall()
            found = {int(row["id"]): row for row in rows}
            for resource_id in resource_ids:
                row = found.get(resource_id)
                if row is None:
                    raise NotFoundError(f"资源{resource_id}不存在")
                if not row["active"]:
                    raise ValidationError(f"资源{row['name']}已停用")
                if row["type"] != expected_type[resource_id]:
                    raise ValidationError(f"资源{row['name']}类型不匹配")
                if row["dispatch_id"] is not None:
                    conflicts.append({
                        "kind": "resource", "resource_id": resource_id,
                        "type": row["type"], "name": row["name"],
                        "dispatch_id": int(row["dispatch_id"]),
                        "item_id": int(row["item_id"]),
                    })
            busy_rows = self.conn.execute(
                """SELECT crew_id FROM dispatches
                   WHERE status IN ('reserved','arrived')
                     AND window_start < ? AND ? < window_end
                   GROUP BY crew_id""",
                (window_end, window_start)).fetchall()
            busy = {int(row["crew_id"]) for row in busy_rows}
            candidates = [crew for crew in self.list_crews(active_only=True)
                          if crew["id"] not in busy and required_skill in crew["skills"]]
            if not candidates:
                conflicts.append({"kind": "crew", "required_skill": required_skill})
            if conflicts:
                raise ConflictError("调度时段存在占用，整笔调度未保存", conflicts)
            crew = candidates[0]
            cur = self.conn.execute(
                """INSERT INTO dispatches(item_id, crew_id, required_skill, window_start,
                   window_end, status, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,'reserved',1,?,?,?)""",
                (item_id, crew["id"], required_skill, window_start, window_end,
                 actor, now, now))
            dispatch_id = int(cur.lastrowid)
            self.conn.executemany(
                """INSERT INTO dispatch_resources(dispatch_id, resource_id, resource_type,
                   returned_at) VALUES(?,?,?,NULL)""",
                [(dispatch_id, rid, "pump") for rid in pump_ids]
                + [(dispatch_id, rid, "vehicle") for rid in vehicle_ids])
        return self.get_dispatch(dispatch_id)

    def get_dispatch(self, dispatch_id):
        with self._lock:
            row = self.conn.execute("SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
        if row is None:
            raise NotFoundError("调度不存在")
        dispatch = dict(row)
        dispatch["crew"] = self.get_crew(dispatch["crew_id"])
        with self._lock:
            rows = self.conn.execute(
                """SELECT dr.resource_id, dr.resource_type, dr.returned_at, r.name
                   FROM dispatch_resources dr JOIN resources r ON r.id=dr.resource_id
                   WHERE dr.dispatch_id=? ORDER BY dr.resource_id""",
                (dispatch_id,)).fetchall()
        dispatch["resources"] = [
            {"resource_id": int(r["resource_id"]), "type": r["resource_type"],
             "name": r["name"], "returned_at": r["returned_at"]}
            for r in rows]
        return dispatch

    def update_dispatch_status(self, dispatch_id, target, expected_version, actor):
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE dispatches SET status=?, version=version+1, updated_at=?
                   WHERE id=? AND version=?""",
                (target, now, dispatch_id, expected_version))
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("调度不存在")
                raise ConflictError("调度版本冲突，请刷新后重试")
            if target == "completed":
                self.conn.execute(
                    """UPDATE dispatch_resources SET returned_at=?
                       WHERE dispatch_id=? AND returned_at IS NULL""",
                    (now, dispatch_id))
        return self.get_dispatch(dispatch_id)

    def active_dispatch_for_item(self, item_id):
        with self._lock:
            row = self.conn.execute(
                """SELECT id FROM dispatches WHERE item_id=?
                   AND status IN ('reserved','arrived') ORDER BY id LIMIT 1""",
                (item_id,)).fetchone()
        return int(row["id"]) if row else None

    def has_completed_dispatch(self, item_id):
        with self._lock:
            row = self.conn.execute(
                "SELECT 1 FROM dispatches WHERE item_id=? AND status='completed' LIMIT 1",
                (item_id,)).fetchone()
        return row is not None

    def list_dispatches(self, item_id=None, status=None):
        sql = "SELECT id FROM dispatches"
        clauses, params = [], []
        if item_id is not None:
            clauses.append("item_id=?")
            params.append(item_id)
        if status:
            clauses.append("status=?")
            params.append(status)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
        return [self.get_dispatch(int(row["id"])) for row in rows]

    def close(self) -> None:
        with self._lock:
            self.conn.close()
