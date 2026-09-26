from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path
from typing import Any, Dict, List, Optional

from .audit import make_entry, utc_now
from .domain import ConflictError, NotFoundError, ValidationError
from .rules import ACTIVE_DISPATCH_STATES, DISPATCH_STATES, ID_PREFIX, STATES


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
        dispatch_statuses = ",".join("'" + s.replace("'", "''") + "'" for s in DISPATCH_STATES)
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
                    name TEXT NOT NULL,
                    skills TEXT NOT NULL,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_crews_external_ref
                    ON crews(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS equipment (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    kind TEXT NOT NULL CHECK(kind IN ('pump','vehicle')),
                    name TEXT NOT NULL,
                    external_ref TEXT,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE UNIQUE INDEX IF NOT EXISTS ux_equipment_external_ref
                    ON equipment(external_ref) WHERE external_ref IS NOT NULL;
                CREATE TABLE IF NOT EXISTS dispatches (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    item_id INTEGER NOT NULL REFERENCES items(id) ON DELETE CASCADE,
                    crew_id INTEGER NOT NULL REFERENCES crews(id),
                    skill TEXT NOT NULL,
                    start_at TEXT NOT NULL,
                    end_at TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'scheduled'
                        CHECK(status IN ({dispatch_statuses})),
                    version INTEGER NOT NULL DEFAULT 1,
                    created_by TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_dispatches_item ON dispatches(item_id);
                CREATE TABLE IF NOT EXISTS dispatch_resources (
                    dispatch_id INTEGER NOT NULL REFERENCES dispatches(id) ON DELETE CASCADE,
                    equipment_id INTEGER NOT NULL REFERENCES equipment(id),
                    returned INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (dispatch_id, equipment_id)
                );
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

    def create_crew(self, name: str, skills: List[str], external_ref: Optional[str],
                    actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO crews(name, skills, external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?)""",
                    (name, json.dumps(skills, ensure_ascii=False), external_ref, actor, now),
                )
                crew_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("班组唯一标识已存在") from exc
        return self.get_crew(crew_id)

    def get_crew(self, crew_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute("SELECT * FROM crews WHERE id=?", (crew_id,)).fetchone()
        if row is None:
            raise NotFoundError("班组不存在")
        crew = dict(row)
        crew["skills"] = json.loads(crew["skills"])
        return crew

    def list_crews(self) -> List[Dict[str, Any]]:
        with self._lock:
            rows = self.conn.execute("SELECT * FROM crews ORDER BY id").fetchall()
        result = []
        for row in rows:
            crew = dict(row)
            crew["skills"] = json.loads(crew["skills"])
            result.append(crew)
        return result

    def create_equipment(self, kind: str, name: str, external_ref: Optional[str],
                         actor: str) -> Dict[str, Any]:
        now = utc_now()
        try:
            with self._lock, self.conn:
                cur = self.conn.execute(
                    """INSERT INTO equipment(kind, name, external_ref, created_by, created_at)
                       VALUES(?,?,?,?,?)""",
                    (kind, name, external_ref, actor, now),
                )
                equipment_id = int(cur.lastrowid)
        except sqlite3.IntegrityError as exc:
            raise ConflictError("设备唯一标识已存在") from exc
        return self.get_equipment(equipment_id)

    def get_equipment(self, equipment_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM equipment WHERE id=?", (equipment_id,)).fetchone()
        if row is None:
            raise NotFoundError("设备不存在")
        return dict(row)

    def list_equipment(self, kind: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM equipment"
        params: tuple = ()
        if kind:
            sql += " WHERE kind=?"
            params = (kind,)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def _crew_conflicts(self, crew_id: int, start_at: str, end_at: str) -> List[Dict[str, Any]]:
        active = ",".join("'" + s + "'" for s in sorted(ACTIVE_DISPATCH_STATES))
        rows = self.conn.execute(
            f"""SELECT id, start_at, end_at FROM dispatches
                WHERE status IN ({active}) AND crew_id=? AND start_at < ? AND end_at > ?
                ORDER BY id""",
            (crew_id, end_at, start_at),
        ).fetchall()
        return [{"resource_type": "crew", "resource_id": crew_id,
                 "dispatch_id": row["id"], "start_at": row["start_at"],
                 "end_at": row["end_at"]} for row in rows]

    def _equipment_conflicts(self, equipment_ids: List[int], start_at: str,
                             end_at: str) -> List[Dict[str, Any]]:
        if not equipment_ids:
            return []
        active = ",".join("'" + s + "'" for s in sorted(ACTIVE_DISPATCH_STATES))
        marks = ",".join("?" for _ in equipment_ids)
        rows = self.conn.execute(
            f"""SELECT d.id, d.start_at, d.end_at, r.equipment_id, e.kind
                FROM dispatches d
                JOIN dispatch_resources r ON r.dispatch_id = d.id
                JOIN equipment e ON e.id = r.equipment_id
                WHERE d.status IN ({active}) AND r.equipment_id IN ({marks})
                AND d.start_at < ? AND d.end_at > ?
                ORDER BY d.id""",
            (*equipment_ids, end_at, start_at),
        ).fetchall()
        return [{"resource_type": row["kind"], "resource_id": row["equipment_id"],
                 "dispatch_id": row["id"], "start_at": row["start_at"],
                 "end_at": row["end_at"]} for row in rows]

    def create_dispatch(self, item_id: int, skill: str, start_at: str, end_at: str,
                        equipment_ids: List[int], actor: str,
                        crew_id: Optional[int] = None) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            for equipment_id in equipment_ids:
                row = self.conn.execute(
                    "SELECT 1 FROM equipment WHERE id=?", (equipment_id,)).fetchone()
                if row is None:
                    raise NotFoundError(f"设备{equipment_id}不存在")
            if crew_id is not None:
                row = self.conn.execute(
                    "SELECT * FROM crews WHERE id=?", (crew_id,)).fetchone()
                if row is None:
                    raise NotFoundError("班组不存在")
                if skill not in json.loads(row["skills"]):
                    raise ValidationError("班组技能不匹配")
                candidates = [dict(row)]
            else:
                rows = self.conn.execute("SELECT * FROM crews ORDER BY id").fetchall()
                candidates = [dict(row) for row in rows
                              if skill in json.loads(row["skills"])]
                if not candidates:
                    raise ValidationError("没有具备该技能的班组")
            chosen = None
            conflicts: List[Dict[str, Any]] = []
            for crew in candidates:
                crew_conflicts = self._crew_conflicts(crew["id"], start_at, end_at)
                if not crew_conflicts:
                    chosen = crew
                    break
                conflicts.extend(crew_conflicts)
                if crew_id is not None:
                    break
            equipment_conflicts = self._equipment_conflicts(equipment_ids, start_at, end_at)
            if chosen is None:
                conflicts.extend(equipment_conflicts)
                raise ConflictError(
                    "时段冲突：没有空闲班组", details={"conflicts": conflicts})
            if equipment_conflicts:
                raise ConflictError(
                    "时段冲突：泵机或车辆已被占用",
                    details={"conflicts": equipment_conflicts})
            cur = self.conn.execute(
                """INSERT INTO dispatches(item_id, crew_id, skill, start_at, end_at,
                   status, version, created_by, created_at, updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (item_id, chosen["id"], skill, start_at, end_at, DISPATCH_STATES[0], 1,
                 actor, now, now),
            )
            dispatch_id = int(cur.lastrowid)
            for equipment_id in equipment_ids:
                self.conn.execute(
                    """INSERT INTO dispatch_resources(dispatch_id, equipment_id, returned)
                       VALUES(?,?,0)""",
                    (dispatch_id, equipment_id),
                )
        return self.get_dispatch(dispatch_id)

    def get_dispatch(self, dispatch_id: int) -> Dict[str, Any]:
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
            if row is None:
                raise NotFoundError("调度单不存在")
            resources = self.conn.execute(
                """SELECT r.equipment_id, r.returned, e.kind, e.name
                   FROM dispatch_resources r JOIN equipment e ON e.id = r.equipment_id
                   WHERE r.dispatch_id=? ORDER BY r.equipment_id""",
                (dispatch_id,),
            ).fetchall()
        dispatch = dict(row)
        dispatch["resources"] = [dict(resource) for resource in resources]
        return dispatch

    def list_dispatches(self, item_id: int) -> List[Dict[str, Any]]:
        self.get_item(item_id)
        with self._lock:
            rows = self.conn.execute(
                "SELECT id FROM dispatches WHERE item_id=? ORDER BY id", (item_id,)
            ).fetchall()
        return [self.get_dispatch(int(row["id"])) for row in rows]

    def transition_dispatch(self, dispatch_id: int, target: str,
                            expected_statuses: List[str], actor: str) -> Dict[str, Any]:
        now = utc_now()
        marks = ",".join("?" for _ in expected_statuses)
        with self._lock, self.conn:
            cur = self.conn.execute(
                f"""UPDATE dispatches SET status=?, version=version+1, updated_at=?
                    WHERE id=? AND status IN ({marks})""",
                (target, now, dispatch_id, *expected_statuses),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("调度单不存在")
                raise ConflictError("调度状态已变化，请刷新后重试")
        return self.get_dispatch(dispatch_id)

    def complete_dispatch(self, dispatch_id: int, actor: str) -> Dict[str, Any]:
        now = utc_now()
        with self._lock, self.conn:
            cur = self.conn.execute(
                """UPDATE dispatches SET status='completed', version=version+1, updated_at=?
                   WHERE id=? AND status='arrived'""",
                (now, dispatch_id),
            )
            if cur.rowcount == 0:
                exists = self.conn.execute(
                    "SELECT 1 FROM dispatches WHERE id=?", (dispatch_id,)).fetchone()
                if exists is None:
                    raise NotFoundError("调度单不存在")
                raise ConflictError("调度状态已变化，请刷新后重试")
            self.conn.execute(
                "UPDATE dispatch_resources SET returned=1 WHERE dispatch_id=?",
                (dispatch_id,),
            )
        return self.get_dispatch(dispatch_id)

    def completed_dispatch_count(self, item_id: int) -> int:
        with self._lock:
            row = self.conn.execute(
                "SELECT COUNT(*) AS n FROM dispatches WHERE item_id=? AND status='completed'",
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

    def list_audit(self, entity_id: Optional[int] = None,
                   entity_type: Optional[str] = None) -> List[Dict[str, Any]]:
        sql = "SELECT * FROM audit_events"
        clauses = []
        params: list = []
        if entity_id is not None:
            clauses.append("entity_id=?")
            params.append(entity_id)
        if entity_type is not None:
            clauses.append("entity_type=?")
            params.append(entity_type)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY id"
        with self._lock:
            rows = self.conn.execute(sql, tuple(params)).fetchall()
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

    def close(self) -> None:
        with self._lock:
            self.conn.close()
