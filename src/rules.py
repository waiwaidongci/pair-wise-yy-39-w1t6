from __future__ import annotations
from .domain import ConflictError, ValidationError
TITLE='大坝巡检、缺陷与应急管理'; ENTITY='大坝缺陷'; ID_PREFIX='DS'
SEVERITIES=['observation', 'minor', 'major', 'emergency']; STATES=['planned', 'inspected', 'defect_confirmed', 'repair', 'verified', 'closed']; TRANSITIONS={'planned': ['inspected'], 'inspected': ['defect_confirmed'], 'defect_confirmed': ['repair'], 'repair': ['verified'], 'verified': ['closed'], 'closed': []}; TRANSITION_ROLES={'inspected': ['inspector'], 'defect_confirmed': ['dam_engineer'], 'repair': ['dam_engineer'], 'verified': ['inspector'], 'closed': ['emergency_manager']}
CREATE_ROLES=set(['inspector']); RECORD_ROLES=set(['inspector', 'dam_engineer']); AUDIT_ROLES=set(['emergency_manager', 'viewer']); VIEW_ROLES=set(['inspector', 'dam_engineer', 'emergency_manager', 'viewer'])
DISPATCH_SEVERITIES=set(['major', 'emergency']); DISPATCH_ITEM_STATES=set(['defect_confirmed', 'repair', 'verified']); DISPATCH_STATES=['scheduled', 'arrived', 'completed', 'cancelled']; DISPATCH_TRANSITIONS={'scheduled': ['arrived', 'cancelled'], 'arrived': ['completed'], 'completed': [], 'cancelled': []}; ACTIVE_DISPATCH_STATES=set(['scheduled', 'arrived'])
RESOURCE_ROLES=set(['emergency_manager']); DISPATCH_CREATE_ROLES=set(['emergency_manager']); DISPATCH_CANCEL_ROLES=set(['emergency_manager']); DISPATCH_ARRIVE_ROLES=set(['emergency_manager', 'dam_engineer']); DISPATCH_COMPLETE_ROLES=set(['emergency_manager', 'dam_engineer'])
SEVERITY_WEIGHT={'observation': 1.0, 'minor': 3.0, 'major': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'observation': 72, 'minor': 24, 'major': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
def priority_score(severity,quantity=0.0,threshold=1.0,open_records=0):
    if severity not in SEVERITY_WEIGHT: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(0,min(10,int(round(SEVERITY_WEIGHT[severity]+min(4.0,ratio*4.0)+min(3.0,float(open_records))))))
def response_deadline_hours(severity,quantity=0.0,threshold=1.0):
    if severity not in DEADLINE_HOURS: raise ValidationError("unknown severity")
    ratio=quantity/threshold if threshold>0 else 1.0
    return max(1,int(DEADLINE_HOURS[severity]/max(1.0,ratio)))
def escalation_required(severity,quantity=0.0,threshold=1.0):
    return severity==SEVERITIES[-1] or (threshold>0 and quantity>=threshold)
def can_transition(current,target): return target in TRANSITIONS.get(current,[])
def validate_transition(current,target):
    if current not in STATES or target not in STATES: raise ValidationError("未知状态")
    if not can_transition(current,target): raise ConflictError(f"不能从{current}转换到{target}")
def completion_blockers(target,open_records,severity=None,completed_dispatches=None):
    blockers=["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
    if target in TERMINAL_STATES and severity in DISPATCH_SEVERITIES and completed_dispatches is not None and completed_dispatches<1: blockers.append("重大或应急缺陷未完成处置调度，不能关闭")
    return blockers
def validate_dispatch_transition(current,target):
    if current not in DISPATCH_STATES or target not in DISPATCH_STATES: raise ValidationError("未知调度状态")
    if target not in DISPATCH_TRANSITIONS.get(current,[]): raise ConflictError(f"调度不能从{current}转换到{target}")
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
