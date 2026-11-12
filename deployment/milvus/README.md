# 本机 Milvus Standalone

本配置采用官方 Milvus **v3.0.5 release 附件**中的服务组合。官方版本表列出配套 PyMilvus **3.0.2**，本项目的 `>=3.0.2,<3.1` 约束由锁文件固定为 **3.0.3**。版本对应关系已核对 [官方发布说明](https://github.com/milvus-io/milvus/releases/tag/v3.0.5)。项目改动包括统一数据目录、项目名称和仅绑定本机的端口。

## 启动

在项目根目录的 PowerShell 中逐行运行。Docker Desktop 需要已经启动，并使用 WSL 2 后端及 Linux containers；[官方 Windows 安装说明](https://milvus.io/docs/install_standalone-windows.md) 给出了该运行方式。

```powershell
Set-Location 'D:\research\agent-harness'
docker info --format '{{.OSType}}'
docker compose version
docker compose -f deployment/milvus/compose.yaml config --quiet
docker compose -f deployment/milvus/compose.yaml up -d --wait
docker compose -f deployment/milvus/compose.yaml ps
Invoke-RestMethod 'http://127.0.0.1:9091/healthz'
```

第一条 `docker info` 应返回 `linux`。如果无法连接 Docker daemon，先打开 Docker Desktop 并等待引擎就绪；只安装 Docker 命令行不代表服务已运行。首次启动需要下载镜像，耗时与本机网络、资源有关。

Compose 包含 Milvus、etcd、MinIO 三个常驻服务和一次性数据目录权限初始化服务。完成初始化服务退出0属于正常状态，不要求它保持运行。Milvus连接地址是 `http://127.0.0.1:19530`，健康检查是9091端口；etcd和MinIO只在Compose内部网络通信。

## 应用配置

项目通过可选依赖安装PyMilvus，不需要安装Milvus Lite或Embedding模型包。

```powershell
uv sync --frozen --extra test --extra browser --extra milvus --extra redis
```

在本地 `.env` 添加以下非秘密配置。已有Embedding、Rerank和聊天配置继续沿用。

```dotenv
VECTOR_BACKEND=milvus
MILVUS_URI=http://127.0.0.1:19530
MILVUS_COLLECTION_PREFIX=ayanami_library
```

本机默认实例没有启用Milvus认证，可以不设置 `MILVUS_TOKEN`。连接有认证的实例时，将该变量写在服务端本地配置里。当前SDK使用默认数据库，collection命名还包含工作区路径和Embedding profile的指纹，以免不同项目或模型配置共用同一集合。

之后运行项目。

```powershell
.\scripts\project.ps1 run
```

代码默认后端仍为Qdrant，`.env` 中显式选择Milvus后才切换。应用不会把Milvus不可用解释为可以悄悄改用另一向量库；混合检索的关键词降级提示仍遵循检索服务规则。

## 数据和启停

所有Compose绑定路径相对本文件解析，持久数据位于项目 `.runtime/agent-harness/milvus/`。

| 子目录 | 用途 |
|---|---|
| `standalone/` | Milvus运行状态与本地数据 |
| `etcd/` | Milvus元数据服务数据 |
| `minio/` | Milvus对象存储数据 |

原始资料和SQLite继续位于 `.runtime/agent-harness/library/`，会话库位于 `.runtime/agent-harness/sessions.sqlite3`。这些是不同职责的存储，停止容器不会删除它们。

```powershell
docker compose -f deployment/milvus/compose.yaml stop
docker compose -f deployment/milvus/compose.yaml start
docker compose -f deployment/milvus/compose.yaml logs --tail 80 standalone
```

更换工作区路径会使用不同的collection命名空间。改模型、维度、后端或向量目标后，由应用的新索引profile管理重建和可见性，不应手工把旧profile标成ready。Qdrant原数据会保留，不需要为了切换而删除旧向量目录。

本配置用于本机开发，Milvus API只监听127.0.0.1。后续企业内网部署还需单独配置认证、持久卷、备份和资源治理，见 [分层知识库与迁移说明](../../interview/crud-rag-interview-guide.md#milvus-hierarchy)。

## 来源和版本核对

- [Milvus v3.0.5 release及SDK对应表](https://github.com/milvus-io/milvus/releases/tag/v3.0.5)
- [采用的官方Compose附件](https://github.com/milvus-io/milvus/releases/download/v3.0.5/milvus-standalone-docker-compose.yaml)
- [PyMilvus v3.0.2 release](https://github.com/milvus-io/pymilvus/releases/tag/v3.0.2)
- [Python SDK安装说明](https://milvus.io/docs/install-pymilvus.md)

不要用GitHub tag中的Compose文件替代release附件而不核对镜像版本。本次核对时，v3.0.5源码树里的相应Compose仍含v3.0.4镜像，官方release附件已经是v3.0.5。
