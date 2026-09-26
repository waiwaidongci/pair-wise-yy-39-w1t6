# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案；重大或应急缺陷确认后可发起处置调度，按技能匹配空闲抢修班组并同时占住排水泵和车辆。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验（含调度时段与ID列表校验）。
- `src/rules.py`：状态机、角色矩阵、优先级、期限、调度状态机和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制、资源冲突检测和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应（冲突时返回占用对象明细）。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则、处置调度和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `POST /api/crews`、`GET /api/crews`：登记、查看抢修班组（含技能列表）
- `POST /api/equipment`、`GET /api/equipment?kind=pump|vehicle`：登记、查看排水泵和车辆
- `POST /api/items/{id}/dispatches`：创建处置调度，提交`skill`、`start_at`、`end_at`、`pump_ids`、`vehicle_ids`，可选`crew_id`（缺省自动匹配空闲班组）
- `GET /api/items/{id}/dispatches`、`GET /api/dispatches/{id}`
- `POST /api/dispatches/{id}/arrive|cancel|complete`：到场、撤单、办完
- `GET /api/audit`

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。

## 处置调度规则

- 仅`major`或`emergency`缺陷在确认后（`defect_confirmed`/`repair`/`verified`）可创建调度，由`emergency_manager`发起。
- 创建时按技能匹配空闲班组，并在同一事务内占住所需泵机和车辆；任一时段冲突则返回占用对象明细（`details.conflicts`），整笔调度不保存。
- 班组到场前（`scheduled`）可撤单，撤单即释放班组与全部设备；到场后（`arrived`）不能撤单，只能办完（`completed`）并归还全部物资。
- 重大或应急缺陷没有已完成的处置调度时不允许关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
