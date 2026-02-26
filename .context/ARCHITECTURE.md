# Ops Dashboard 架构说明

## 定位

统一运维告警面板，收敛所有 Docker 项目中需要人工介入的事件。

## 架构：Push 优先 + Pull 兜底

### Push 层（各项目主动上报）

各项目在关键错误处理点调用 `ops_reporter.report_event()` 上报事件。
SDK 文件：`ops_reporter.py`（零依赖、异步非阻塞、失败静默）。

已接入项目和上报点：
- **infohunter**: YouTube OAuth token 刷新失败 → `auth_expired`
- **github-sentinel**: GraphQL 重连失败 → `connection_failed`，BigQuery 配额超限 → `quota_exceeded`
- **TrendRadar**: 企微推送失败 → `push_failed`

### Pull 层（定时探测）

每 5 分钟轮询：
- 所有容器的 Docker 状态（running/unhealthy/stopped）
- 各项目的 `/api/health` 端点

覆盖所有 7 个项目，包括未做代码改造的项目（digital-twin / trump-monitor / rsshub / mcp-gateway）。

## 技术栈

- 后端：Python FastAPI + SQLite
- 前端：单文件 HTML + Tailwind CSS + Alpine.js
- 部署：Docker，挂载 docker.sock 读取容器状态

## API 端点

| 端点 | 方法 | 用途 |
|------|------|------|
| `POST /api/events` | POST | 接收事件上报 |
| `GET /api/events` | GET | 查询事件列表 |
| `PATCH /api/events/{id}` | PATCH | 更新事件状态 |
| `GET /api/projects` | GET | 项目汇总（事件+容器状态） |
| `GET /api/stats` | GET | 统计概览 |
| `GET /` | GET | 前端面板 |

## 事件分类

| category | 含义 | 典型场景 |
|----------|------|---------|
| `auth_expired` | 认证凭据过期 | YouTube OAuth token |
| `quota_exceeded` | 配额耗尽 | BigQuery 免费额度 |
| `push_failed` | 推送失败 | 企微/飞书 webhook |
| `connection_failed` | 连接失败 | GraphQL 断连 |
| `container_unhealthy` | 容器不健康 | healthcheck 失败 |
| `container_stopped` | 容器停止 | 非 running 状态 |

## 去重机制

- `dedup_key` 唯一约束，同一问题只产生一条记录
- 重复上报时更新 detail 和 updated_at，不创建新记录
- 事件 resolved 后再次出现会重新插入

## 告警推送

- critical/warning 事件首次创建时推送飞书/企微
- 同一事件 24 小时内不重复推送
- 每日 09:00 UTC+8 推送汇总

## 部署

```bash
cd /data/workspace/ops-dashboard
docker compose up -d
```

访问：http://localhost:9090
