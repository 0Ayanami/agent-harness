# Docker Cron 调度服务

Cron 容器按 `CRON_TIMEZONE`（默认 `Asia/Shanghai`）计算到期时间，数据库用 UTC Unix 秒保存 `next_run_at`。计划和待交付记录存放在 Compose 命名卷 `agent-harness-cron_cron-state` 的 `cron.sqlite3`。容器可以在网页后端关闭时继续检测。后端按当前工作区识别自己的计划。

## 启动

在项目根目录的 PowerShell 中执行：

```powershell
docker compose -f deployment/cron/compose.yaml up -d --build --wait
```

已有本机镜像时，改用 `docker compose -f deployment/cron/compose.yaml up -d --no-build --wait`。若 Docker Hub 基础镜像认证超时，可在构建前执行 `$env:CRON_PYTHON_IMAGE = 'mirror.gcr.io/library/python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90'`。服务只映射到本机 `127.0.0.1:8081`。

在项目 `.env` 配置 `CRON_SERVICE_URL=http://127.0.0.1:8081`，然后重启 `project.ps1 run`。首次启动会幂等导入会话目录里的旧持久计划，归档会话保持暂停。旧 JSON 用作迁移前备份，迁移后新变更记录在容器卷里。CLI 和未配置 `CRON_SERVICE_URL` 的旧部署继续使用进程内检测。迁移后如需回退，应先处理两套计划的状态差异，避免旧 JSON 重新触发已完成的计划。

## 检测与执行

1. 容器每 0.5 秒检查到期索引。它在同一笔 SQLite 事务中登记 `(session_id, job_id, scheduled_at)`，并推进下次运行时间。容器自身停机错过多次周期时只补发最早未登记的一次，之后从当前时间继续。后端关闭而容器仍在时，到期记录照常入队。
2. 后端的共享派发线程轮询到期会话。只有会话存在、未归档、空闲并持有会话锁时才领取。Docker 模式下不会为各个网页会话启动检测线程。
3. 领取附带 5 分钟租约。Agent 成功完成并持久化回答后确认结果。执行失败可以将记录返还队列，进程异常终止时租约到期后可以重新领取。到期记录去重和 Agent 的外部副作用分属不同边界。如果 Agent 已执行外部操作但在确认前崩溃，恢复可能重复执行。

用 `docker compose -f deployment/cron/compose.yaml ps` 查看健康状态。停止或重启容器会保留计划。删除对应数据卷会删除容器内的计划与待交付记录。
