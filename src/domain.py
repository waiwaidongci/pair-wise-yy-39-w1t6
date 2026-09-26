from __future__ import annotations
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, Optional
class ErrorKind:
    VALIDATION="validation"; NOT_FOUND="not_found"; FORBIDDEN="forbidden"; CONFLICT="conflict"
class DomainError(Exception):
    kind=ErrorKind.VALIDATION
    def __init__(self,message,details=None): super().__init__(message); self.message=message; self.details=details
class ValidationError(DomainError): kind=ErrorKind.VALIDATION
class NotFoundError(DomainError): kind=ErrorKind.NOT_FOUND
class PermissionDenied(DomainError): kind=ErrorKind.FORBIDDEN
class ConflictError(DomainError): kind=ErrorKind.CONFLICT
SEVERITIES=['observation', 'minor', 'major', 'emergency']; STATES=['planned', 'inspected', 'defect_confirmed', 'repair', 'verified', 'closed']; ROLES=['inspector', 'dam_engineer', 'emergency_manager', 'viewer']
DISPATCH_STATES=['scheduled', 'arrived', 'completed', 'cancelled']; EQUIPMENT_KINDS=['pump', 'vehicle']
@dataclass(frozen=True)
class Item:
    id:int; title:str; description:str; severity:str; quantity:float; threshold:float; status:str; version:int; external_ref:Optional[str]; created_by:str; created_at:str; updated_at:str
@dataclass(frozen=True)
class Record:
    id:int; item_id:int; kind:str; detail:str; status:str; external_ref:Optional[str]; created_by:str; created_at:str
@dataclass(frozen=True)
class AuditEntry:
    id:int; action:str; entity_type:str; entity_id:int; actor:str; detail:Dict[str,Any]; previous_hash:str; entry_hash:str; created_at:str
def require_text(value,field,max_length=2000):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    value=value.strip()
    if len(value)>max_length: raise ValidationError(f"{field}不能超过{max_length}个字符")
    return value
def normalize_severity(value):
    if value not in SEVERITIES: raise ValidationError("severity不在允许范围内")
    return value
def require_number(value,field,minimum=0.0):
    if isinstance(value,bool): raise ValidationError(f"{field}必须是数字")
    try: number=float(value)
    except (TypeError,ValueError): raise ValidationError(f"{field}必须是数字")
    if number<minimum: raise ValidationError(f"{field}不能小于{minimum}")
    return number
def ensure_role(role,allowed):
    if role not in allowed: raise PermissionDenied("当前角色无权执行该操作")
def _parse_iso(value,field):
    if not isinstance(value,str) or not value.strip(): raise ValidationError(f"{field}不能为空")
    try: moment=datetime.fromisoformat(value.strip().replace("Z","+00:00"))
    except ValueError: raise ValidationError(f"{field}必须是ISO 8601时间")
    if moment.tzinfo is None: raise ValidationError(f"{field}必须包含时区")
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat()
def require_time_window(start,end):
    start=_parse_iso(start,"start_at"); end=_parse_iso(end,"end_at")
    if start>=end: raise ValidationError("start_at必须早于end_at")
    return start,end
def require_id_list(value,field,max_items=50):
    if value is None: return []
    if not isinstance(value,list): raise ValidationError(f"{field}必须是数组")
    if len(value)>max_items: raise ValidationError(f"{field}不能超过{max_items}项")
    result=[]
    for item in value:
        if isinstance(item,bool) or not isinstance(item,int) or item<1: raise ValidationError(f"{field}必须是正整数数组")
        if item in result: raise ValidationError(f"{field}存在重复项")
        result.append(item)
    return result
