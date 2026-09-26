# 大坝巡检、缺陷与应急管理

安排巡检，记录渗流、位移、裂缝等缺陷并跟踪修复、复检和应急预案。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、调度、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8316
```

默认端口为`8316`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`，支持`status`过滤
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/audit`

班组与物资（应急管理员`emergency_manager`登记，所有角色可查）：

- `POST /api/crews`，`{"name","skills":[...]}`登记抢修班组及其技能
- `GET /api/crews`
- `POST /api/resources`，`{"name","type":"pump|vehicle"}`登记排水泵/车辆
- `GET /api/resources`，支持`type=pump|vehicle`过滤

处置调度（`dam_engineer`或`emergency_manager`操作）：

- `POST /api/items/{id}/dispatches`，确认重大(major)/应急(emergency)缺陷后安排调度：
  `{"required_skill","pump_ids":[...],"vehicle_ids":[...],"window_start","window_end"}`，
  时间为带时区ISO8601。系统按`required_skill`匹配时段内空闲班组（同条件取编号最小者），
  并在同一事务内占住指定泵机与车辆；班组或任一物资时段冲突时返回409，
  `details`列出全部占用对象（含占用方调度/缺陷），整笔调度不保存。
- `POST /api/dispatches/{id}/action`，`action`为`arrive`（到场）、`cancel`（到场前撤单）、
  `complete`（办结），必须提交`expected_version`。到场前撤单释放班组和全部物资；
  到场后只能办结，办结时统一归还全部泵机、车辆（`returned_at`记时）。
- `GET /api/dispatches`，支持`item_id`、`status`过滤；`GET /api/dispatches/{id}`；
  `GET /api/items/{id}/dispatches`。

允许角色：inspector, dam_engineer, emergency_manager, viewer。异常值比控制阈值越高，缺陷优先级越高；应急处置缺陷必须完成复检并记录证据后才能关闭。重大/应急缺陷还必须有一笔已办结(`completed`)的处置调度，否则不允许关闭。

## 测试

```bash
python3 -m unittest discover -s tests -v
```
