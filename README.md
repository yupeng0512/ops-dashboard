# Ops Dashboard

轻量级统一运维告警面板，收敛所有 Docker 项目中需要人工介入的事件。

## 特性

- **Push 优先**：各项目通过 `ops_reporter` SDK 主动上报业务级告警（token 过期、配额耗尽、推送失败等）
- **Pull 兜底**：定时探测所有容器状态和 health 端点，兜底覆盖未改造的项目
- **去重机制**：同一问题只产生一条记录，24 小时内不重复推送
- **即时告警**：critical/warning 事件自动推送飞书/企微
- **单容器部署**：FastAPI + SQLite，无外部依赖

## 快速开始

```bash
# 复制环境变量
cp .env.example .env
# 编辑 .env，填入 Webhook URL（可选）

# 启动
docker compose up -d

# 访问面板
open http://localhost:9090
```

## 各项目接入

将 `ops_reporter.py` 复制到项目的 `src/` 目录下，然后在关键错误处理点调用：

```python
from ops_reporter import report_event

report_event(
    project="your-project",
    level="warning",
    category="auth_expired",
    title="OAuth Token 已过期",
    detail="错误详情...",
    action_hint="修复步骤...",
    dedup_key="your-project:auth_expired",
)
```

环境变量 `OPS_DASHBOARD_URL` 控制上报地址（默认 `http://ops-dashboard:9090`）。

## API

| 端点 | 方法 | 用途 |
|------|------|------|
| `POST /api/events` | POST | 接收事件上报 |
| `GET /api/events` | GET | 查询事件列表 |
| `PATCH /api/events/{id}` | PATCH | 更新事件状态 |
| `GET /api/projects` | GET | 项目汇总状态 |
| `GET /api/stats` | GET | 统计概览 |

## 架构详情

参见 [.context/ARCHITECTURE.md](.context/ARCHITECTURE.md)
