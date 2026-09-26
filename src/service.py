from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (EQUIPMENT_KINDS, ConflictError, ensure_role,
                     normalize_severity, require_id_list, require_number,
                     require_text, require_time_window)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREATE_ROLES, DISPATCH_ARRIVE_ROLES,
                    DISPATCH_CANCEL_ROLES, DISPATCH_COMPLETE_ROLES,
                    DISPATCH_CREATE_ROLES, DISPATCH_ITEM_STATES,
                    DISPATCH_SEVERITIES, ENTITY, RECORD_ROLES, RESOURCE_ROLES,
                    TITLE, VIEW_ROLES, completion_blockers, escalation_required,
                    priority_score, response_deadline_hours, role_for_transition,
                    validate_dispatch_transition, validate_transition)


class Service:
    def __init__(self, repository: Repository):
        self.repository = repository

    def _view(self, role: str) -> None:
        ensure_role(role, VIEW_ROLES)

    def create_item(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        title = require_text(payload.get("title"), "title", 200)
        description = require_text(payload.get("description"), "description")
        severity = normalize_severity(payload.get("severity"))
        quantity = require_number(payload.get("quantity", 0), "quantity")
        threshold = require_number(payload.get("threshold", 1), "threshold", 0.000001)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        item = self.repository.create_item(title, description, severity, quantity,
                                           threshold, external_ref, actor)
        self.repository.append_audit("create", ENTITY, item["id"], actor, {
            "title": title, "severity": severity, "quantity": quantity,
            "priority": priority_score(severity, quantity, threshold),
        })
        return self.enrich(item)

    def add_record(self, item_id: int, payload: Dict[str, Any], actor: str,
                   role: str) -> Dict[str, Any]:
        ensure_role(role, RECORD_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 100)
        detail = require_text(payload.get("detail"), "detail")
        status = payload.get("status", "open")
        if status not in ("open", "closed"):
            raise ValueError("status必须是open或closed")
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        record = self.repository.add_record(item_id, kind, detail, status,
                                            external_ref, actor)
        self.repository.append_audit("record", ENTITY, item_id, actor, {
            "record_id": record["id"], "kind": kind, "status": status,
        })
        return record

    def transition(self, item_id: int, target: str, expected_version: int,
                   actor: str, role: str) -> Dict[str, Any]:
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        validate_transition(item["status"], target)
        ensure_role(role, role_for_transition(target))
        if not isinstance(expected_version, int) or expected_version < 1:
            raise ValueError("expected_version必须是正整数")
        blockers = completion_blockers(
            target, self.repository.open_record_count(item_id), item["severity"],
            self.repository.completed_dispatch_count(item_id))
        if blockers:
            raise ConflictError("；".join(blockers))
        updated = self.repository.transition_item(item_id, target, expected_version, actor)
        self.repository.append_audit("transition", ENTITY, item_id, actor, {
            "from": item["status"], "to": target,
            "escalation_required": escalation_required(
                item["severity"], item["quantity"], item["threshold"]),
        })
        return self.enrich(updated)

    def get_item(self, item_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.enrich(self.repository.get_item(item_id))

    def list_items(self, role: str, status: Optional[str] = None) -> list:
        self._view(role)
        return [self.enrich(item) for item in self.repository.list_items(status)]

    def list_records(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_records(item_id)

    def audit(self, role: str, item_id: Optional[int] = None) -> list:
        ensure_role(role, AUDIT_ROLES)
        return self.repository.list_audit(
            item_id, ENTITY if item_id is not None else None)

    def create_crew(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 200)
        skills = payload.get("skills")
        if not isinstance(skills, list) or not skills:
            raise ValueError("skills必须是非空数组")
        skills = [require_text(skill, "skills", 100) for skill in skills]
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        crew = self.repository.create_crew(name, skills, external_ref, actor)
        self.repository.append_audit("crew_create", "抢修班组", crew["id"], actor, {
            "name": name, "skills": skills,
        })
        return crew

    def list_crews(self, role: str) -> list:
        self._view(role)
        return self.repository.list_crews()

    def create_equipment(self, payload: Dict[str, Any], actor: str,
                         role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ROLES)
        actor = require_text(actor, "actor", 100)
        kind = require_text(payload.get("kind"), "kind", 20)
        if kind not in EQUIPMENT_KINDS:
            raise ValueError("kind必须是pump或vehicle")
        name = require_text(payload.get("name"), "name", 200)
        external_ref = payload.get("external_ref")
        if external_ref is not None:
            external_ref = require_text(external_ref, "external_ref", 100)
        equipment = self.repository.create_equipment(kind, name, external_ref, actor)
        self.repository.append_audit("equipment_create", "应急设备", equipment["id"],
                                     actor, {"kind": kind, "name": name})
        return equipment

    def list_equipment(self, role: str, kind: Optional[str] = None) -> list:
        self._view(role)
        if kind is not None and kind not in EQUIPMENT_KINDS:
            raise ValueError("kind必须是pump或vehicle")
        return self.repository.list_equipment(kind)

    def create_dispatch(self, item_id: int, payload: Dict[str, Any], actor: str,
                        role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_CREATE_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["severity"] not in DISPATCH_SEVERITIES:
            raise ValueError("仅重大或应急缺陷可创建处置调度")
        if item["status"] not in DISPATCH_ITEM_STATES:
            raise ConflictError("缺陷未确认或已办结，不能创建处置调度")
        skill = require_text(payload.get("skill"), "skill", 100)
        start_at, end_at = require_time_window(payload.get("start_at"),
                                               payload.get("end_at"))
        pump_ids = require_id_list(payload.get("pump_ids"), "pump_ids")
        vehicle_ids = require_id_list(payload.get("vehicle_ids"), "vehicle_ids")
        for pump_id in pump_ids:
            if self.repository.get_equipment(pump_id)["kind"] != "pump":
                raise ValueError(f"设备{pump_id}不是排水泵")
        for vehicle_id in vehicle_ids:
            if self.repository.get_equipment(vehicle_id)["kind"] != "vehicle":
                raise ValueError(f"设备{vehicle_id}不是车辆")
        crew_id = payload.get("crew_id")
        if crew_id is not None:
            if isinstance(crew_id, bool) or not isinstance(crew_id, int) or crew_id < 1:
                raise ValueError("crew_id必须是正整数")
        dispatch = self.repository.create_dispatch(
            item_id, skill, start_at, end_at, pump_ids + vehicle_ids, actor, crew_id)
        self.repository.append_audit("dispatch_create", ENTITY, item_id, actor, {
            "dispatch_id": dispatch["id"], "crew_id": dispatch["crew_id"],
            "skill": skill, "start_at": start_at, "end_at": end_at,
            "equipment_ids": pump_ids + vehicle_ids,
        })
        return dispatch

    def list_dispatches(self, item_id: int, role: str) -> list:
        self._view(role)
        return self.repository.list_dispatches(item_id)

    def get_dispatch(self, dispatch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_dispatch(dispatch_id)

    def arrive_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ARRIVE_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch = self.repository.get_dispatch(dispatch_id)
        validate_dispatch_transition(dispatch["status"], "arrived")
        updated = self.repository.transition_dispatch(
            dispatch_id, "arrived", ["scheduled"], actor)
        self.repository.append_audit("dispatch_arrive", ENTITY, updated["item_id"],
                                     actor, {"dispatch_id": dispatch_id})
        return updated

    def cancel_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_CANCEL_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch = self.repository.get_dispatch(dispatch_id)
        validate_dispatch_transition(dispatch["status"], "cancelled")
        updated = self.repository.transition_dispatch(
            dispatch_id, "cancelled", ["scheduled"], actor)
        self.repository.append_audit("dispatch_cancel", ENTITY, updated["item_id"],
                                     actor, {"dispatch_id": dispatch_id,
                                             "released_equipment_ids": [
                                                 r["equipment_id"]
                                                 for r in updated["resources"]]})
        return updated

    def complete_dispatch(self, dispatch_id: int, actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_COMPLETE_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch = self.repository.get_dispatch(dispatch_id)
        validate_dispatch_transition(dispatch["status"], "completed")
        updated = self.repository.complete_dispatch(dispatch_id, actor)
        self.repository.append_audit("dispatch_complete", ENTITY, updated["item_id"],
                                     actor, {"dispatch_id": dispatch_id,
                                             "returned_equipment_ids": [
                                                 r["equipment_id"]
                                                 for r in updated["resources"]]})
        return updated

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        return result
