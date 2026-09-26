from __future__ import annotations

from typing import Any, Dict, Optional

from .domain import (ConflictError, ValidationError, ensure_role,
                     normalize_severity, require_number, require_positive_int,
                     require_text)
from .repository import Repository
from .rules import (AUDIT_ROLES, CREW_ENTITY, CREATE_ROLES, DISPATCH_ENTITY,
                    DISPATCH_ROLES, RECORD_ROLES,
                    RESOURCE_ADMIN_ROLES, RESOURCE_ENTITY, RESOURCE_TYPES,
                    TITLE, ENTITY, VIEW_ROLES, completion_blockers,
                    dispatch_required, dispatch_transition,
                    escalation_required, priority_score,
                    response_deadline_hours, role_for_transition,
                    validate_dispatch_action, validate_dispatch_window,
                    validate_id_list, validate_transition)


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
        blockers = completion_blockers(target, self.repository.open_record_count(item_id))
        if target == "closed" and dispatch_required(item["severity"]) \
                and not self.repository.has_completed_dispatch(item_id):
            blockers.append("处置调度尚未办结，缺陷不允许关闭")
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
        return self.repository.list_audit(item_id)

    # ---- 抢修班组与调度资源 ----
    def register_crew(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ADMIN_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        skills = payload.get("skills")
        if not isinstance(skills, list) or not skills:
            raise ValidationError("skills必须是非空数组")
        skills = [require_text(skill, "skill", 100) for skill in skills]
        crew = self.repository.create_crew(name, skills, actor)
        self.repository.append_audit("register", CREW_ENTITY, crew["id"], actor, {
            "name": name, "skills": skills,
        })
        return crew

    def list_crews(self, role: str) -> list:
        self._view(role)
        return self.repository.list_crews()

    def register_resource(self, payload: Dict[str, Any], actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, RESOURCE_ADMIN_ROLES)
        actor = require_text(actor, "actor", 100)
        name = require_text(payload.get("name"), "name", 100)
        rtype = payload.get("type")
        if rtype not in RESOURCE_TYPES:
            raise ValidationError("type必须是pump或vehicle")
        resource = self.repository.create_resource(name, rtype, actor)
        self.repository.append_audit("register", RESOURCE_ENTITY, resource["id"], actor, {
            "name": name, "type": rtype,
        })
        return resource

    def list_resources(self, role: str, rtype: Optional[str] = None) -> list:
        self._view(role)
        if rtype is not None and rtype not in RESOURCE_TYPES:
            raise ValidationError("type必须是pump或vehicle")
        return self.repository.list_resources(rtype)

    # ---- 处置调度 ----
    def create_dispatch(self, item_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        item = self.repository.get_item(item_id)
        if item["status"] != "defect_confirmed":
            raise ConflictError("只有已确认的缺陷可以安排处置调度")
        if not dispatch_required(item["severity"]):
            raise ConflictError("仅重大(major)或应急(emergency)缺陷需要处置调度")
        if self.repository.active_dispatch_for_item(item_id) is not None:
            raise ConflictError("该缺陷已有进行中的调度")
        required_skill = require_text(payload.get("required_skill"), "required_skill", 100)
        pump_ids = validate_id_list(payload.get("pump_ids"), "pump_ids")
        vehicle_ids = validate_id_list(payload.get("vehicle_ids"), "vehicle_ids")
        window_start = validate_dispatch_window(payload.get("window_start"), "window_start")
        window_end = validate_dispatch_window(payload.get("window_end"), "window_end")
        if window_start >= window_end:
            raise ValidationError("window_end必须晚于window_start")
        # 资源存在性与类型由仓储在事务内核验；冲突时整笔回滚，不保存任何占用。
        dispatch = self.repository.reserve_dispatch(
            item_id, required_skill, pump_ids, vehicle_ids,
            window_start, window_end, actor)
        self.repository.append_audit("reserve", DISPATCH_ENTITY, dispatch["id"], actor, {
            "item_id": item_id, "crew_id": dispatch["crew_id"],
            "pump_ids": pump_ids, "vehicle_ids": vehicle_ids,
            "window_start": window_start, "window_end": window_end,
        })
        return dispatch

    def dispatch_action(self, dispatch_id: int, payload: Dict[str, Any],
                        actor: str, role: str) -> Dict[str, Any]:
        ensure_role(role, DISPATCH_ROLES)
        actor = require_text(actor, "actor", 100)
        dispatch = self.repository.get_dispatch(dispatch_id)
        action = validate_dispatch_action(payload.get("action"))
        expected_version = require_positive_int(
            payload.get("expected_version"), "expected_version")
        target = dispatch_transition(dispatch["status"], action)
        updated = self.repository.update_dispatch_status(
            dispatch_id, target, expected_version, actor)
        self.repository.append_audit(action, DISPATCH_ENTITY, dispatch_id, actor, {
            "item_id": updated["item_id"], "from": dispatch["status"], "to": target,
            "returned": len(updated["resources"]) if action == "complete" else 0,
        })
        return updated

    def get_dispatch(self, dispatch_id: int, role: str) -> Dict[str, Any]:
        self._view(role)
        return self.repository.get_dispatch(dispatch_id)

    def list_dispatches(self, role: str, item_id: Optional[int] = None,
                        status: Optional[str] = None) -> list:
        self._view(role)
        return self.repository.list_dispatches(item_id, status)

    @staticmethod
    def enrich(item: Dict[str, Any]) -> Dict[str, Any]:
        result = dict(item)
        result["priority"] = priority_score(
            item["severity"], item["quantity"], item["threshold"])
        result["deadline_hours"] = response_deadline_hours(
            item["severity"], item["quantity"], item["threshold"])
        result["escalation_required"] = escalation_required(
            item["severity"], item["quantity"], item["threshold"])
        result["dispatch_required"] = dispatch_required(item["severity"])
        return result
