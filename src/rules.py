from __future__ import annotations
from datetime import datetime, timezone
from .domain import ConflictError, ValidationError
TITLE='大坝巡检、缺陷与应急管理'; ENTITY='大坝缺陷'; ID_PREFIX='DS'
SEVERITIES=['observation', 'minor', 'major', 'emergency']; STATES=['planned', 'inspected', 'defect_confirmed', 'repair', 'verified', 'closed']; TRANSITIONS={'planned': ['inspected'], 'inspected': ['defect_confirmed'], 'defect_confirmed': ['repair'], 'repair': ['verified'], 'verified': ['closed'], 'closed': []}; TRANSITION_ROLES={'inspected': ['inspector'], 'defect_confirmed': ['dam_engineer'], 'repair': ['dam_engineer'], 'verified': ['inspector'], 'closed': ['emergency_manager']}
CREATE_ROLES=set(['inspector']); RECORD_ROLES=set(['inspector', 'dam_engineer']); AUDIT_ROLES=set(['emergency_manager', 'viewer']); VIEW_ROLES=set(['inspector', 'dam_engineer', 'emergency_manager', 'viewer'])
SEVERITY_WEIGHT={'observation': 1.0, 'minor': 3.0, 'major': 6.0, 'emergency': 9.0}; DEADLINE_HOURS={'observation': 72, 'minor': 24, 'major': 8, 'emergency': 4}; TERMINAL_STATES=set(['closed'])
# 处置调度
DISPATCH_ENTITY='处置调度'; RESOURCE_ENTITY='调度资源'; CREW_ENTITY='抢修班组'
RESOURCE_TYPES=['pump', 'vehicle']; DISPATCH_STATES=['reserved', 'arrived', 'cancelled', 'completed']; DISPATCH_ACTIVE_STATES=set(['reserved', 'arrived'])
DISPATCH_SEVERITIES=set(['major', 'emergency'])
DISPATCH_ROLES=set(['dam_engineer', 'emergency_manager']); RESOURCE_ADMIN_ROLES=set(['emergency_manager'])
DISPATCH_ACTIONS={'arrive': ('reserved', 'arrived'), 'cancel': ('reserved', 'cancelled'), 'complete': ('arrived', 'completed')}
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
def completion_blockers(target,open_records): return ["仍有未关闭事项"] if target in TERMINAL_STATES and open_records>0 else []
def role_for_transition(target): return set(TRANSITION_ROLES.get(target,[]))
def validate_dispatch_window(value,field):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    text=value.strip()
    try: parsed=datetime.fromisoformat(text.replace("Z","+00:00"))
    except ValueError: raise ValidationError(f"{field}必须是ISO8601时间")
    if parsed.tzinfo is None: raise ValidationError(f"{field}必须带时区")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat()
def validate_id_list(values,field,allow_empty=False):
    if not isinstance(values,list): raise ValidationError(f"{field}必须是数组")
    if not values and not allow_empty: raise ValidationError(f"{field}不能为空")
    result=[]
    for value in values:
        if isinstance(value,bool) or not isinstance(value,int) or value<1: raise ValidationError(f"{field}必须是正整数数组")
        result.append(value)
    if len(set(result))!=len(result): raise ValidationError(f"{field}不能重复")
    return result
def validate_dispatch_action(action):
    if action not in DISPATCH_ACTIONS: raise ValidationError("action必须是arrive、cancel或complete")
    return action
def dispatch_transition(current,action):
    required,target=DISPATCH_ACTIONS[action]
    if current!=required:
        if action=='cancel' and current!='reserved': raise ConflictError("班组到场后不能撤单，只能办完并归还全部物资")
        if action=='arrive': raise ConflictError("只有已预留的调度可以登记到场")
        raise ConflictError("只有到场后的调度可以办结")
    return target
def dispatch_required(severity): return severity in DISPATCH_SEVERITIES
