# 移民案件期限与材料管理

纯Python标准库实现的移民案件期限与材料管理原型，使用SQLite持久化，HTTP接口由`http.server`提供。

## 模块结构

- `app.py`：命令行参数、依赖组装和服务启动。
- `src/domain.py`：领域数据类型、错误和基础校验。
- `src/rules.py`：状态转换、法定天数、补件期限和材料完整性和冲突检查。
- `src/repository.py`：SQLite建表、事务和查询。
- `src/service.py`：用例编排、权限检查、乐观并发和审计。
- `src/http_api.py`：HTTP路由与统一错误响应。
- `src/audit.py`：事件时间线。
- `static/index.html`：最小演示页面。
- `tests/`：完整流程、规则计算和失败场景测试。

## 启动

```bash
python3 app.py --db ./data.db --port 8329
```

默认端口为`8329`，默认数据库位于项目目录。服务启动时自动建表。

## 主要接口

- `GET /health`：健康检查。
- `GET /`：演示页面。
- `GET /api/records`：记录列表，可带`state`和`limit`参数。
- `GET /api/records/{id}`：记录详情，`payload.deadline_basis`为当前期限计算依据。
- `GET /api/records/{id}/audit`：审计时间线，每个事件的`details.basis`为该次变更的计算依据。
- `GET /api/stats`：状态统计。
- `POST /api/records`：创建记录，请求体为`{"reference":"...","data":{...}}`。
- `POST /api/records/{id}/actions/{action}`：执行业务动作，请求体为`{"expected_version":1,"data":{...}}`。

除`/health`和`/`外，请求需提供`X-User-Id`、`X-Role`，可选`X-Org`。

## 期限计算与停表

- 案件基础期限：`deadline_day = received_day + deadline_days`；有效期限 = 基础期限 + 已结束的停表天数。
- `request_evidence`（case_officer）：补件发出后剩余天数暂停计算，停表区间记入`payload.tolling_intervals`，`evidence_due_day`先按发出日+允许天数给出临时期限。
- `confirm_delivery`（case_officer/intake_officer）：送达日期与送达方式确认后重算，`evidence_due_day = delivery_day + allowed_days + 送达额外天数`；额外天数：`electronic` 0 天、`courier` 1 天、`mail` 3 天。
- `respond`（legal_rep）：回应后停表结束；晚于期限的材料仍可提交，记录`late_response`并使案件进入逾期，已发生的停表区间保留在记录中。
- `withdraw_evidence`（supervisor）/ `return_evidence`（case_officer/intake_officer）：撤回补件或通知退回后旧期限立即失效、恢复原期限，停表区间标记`annulled`但保留。
- `update_delivery`（case_officer/supervisor）：变更送达方式，旧期限立即失效并按新方式重算，旧期限与旧方式写入计算依据。
- 服务启动时自动升级旧数据：按当前案件状态补齐停表记录（审计动作`migrated`），已回应的按回应日关闭区间，进行中的保持停表。
- 所有写操作使用`expected_version`乐观并发控制：两名经办同时提交同一份送达确认时，先到者成功，后到者收到`409`版本冲突；期限与审计时间线在同一事务写入，不会各写各的。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖完整流程、规则计算、重复引用、权限拒绝和版本冲突。
