# PDF 版面解析

容器使用 `pdfplumber` 提取有文本层的 PDF。应用只向本机回环地址发送 PDF，无需挂载资料库目录。 [pdfplumber 官方文档](https://github.com/jsvine/pdfplumber)

在项目根目录的 PowerShell 中执行：

```powershell
docker compose -f deployment/pdf-layout/compose.yaml up -d --build --wait
```

已有本项目镜像时，可直接启动并查看状态：

```powershell
docker compose -f deployment/pdf-layout/compose.yaml up -d --no-build --wait
docker compose -f deployment/pdf-layout/compose.yaml ps
```

如果 Docker Hub 认证超时，可以只为本项目的构建指定已验证的 Python 基础镜像缓存，不需要修改 Docker Desktop 的全局设置。 [Docker Hub 公共镜像缓存](https://docs.cloud.google.com/artifact-registry/docs/pull-cached-dockerhub-images)

```powershell
$env:PDF_LAYOUT_PYTHON_IMAGE = 'mirror.gcr.io/library/python:3.13-slim@sha256:e48f59cdba69a3cbd9ed2cf0f348d90d45a8d350d98134cf1070bc106ee9ed90'
docker pull $env:PDF_LAYOUT_PYTHON_IMAGE
docker compose -f deployment/pdf-layout/compose.yaml up -d --build --wait
```

在项目 `.env` 中配置 `PDF_LAYOUT_URL=http://127.0.0.1:8079`，然后重启应用。新上传或导入的 PDF 会使用新解析器。已有 PDF 可在资料库发起重建索引，或调用 `POST /api/agent/library/files/{id}/reindex`。重建会按新分块更新关键词和向量索引。启用后服务不可用时，引擎会显示索引失败，可启动容器后重试。省略 `PDF_LAYOUT_URL` 时应用使用原有的 `pypdf` 文本解析。

重复边栏文字按顶部和底部各 10% 的坐标范围筛选，并在至少两页且达到文档约半数页面时移除。常见的可变页码会先归一成一致的规则。同一页上的主体文本保留。表格识别以线条组成的网格为主，按页保存表格坐标、单元格矩阵和行范围，避免同一表格文字又进入普通段落。表格块按整行分组，长度允许时重复第一行作为列名，超长单行按长度上限拆分。

表格与正文共用 BM25、Embedding 和 Milvus，原 PDF 可按页预览。可调用结构接口查看页下的 `table` 节点、`metadata.rows` 和关联块。当前扫描件仍需要 OCR。无边框、跨页或复杂合并单元格的表格提取效果取决于 PDF 的版面和文字编码。

## Docker 镜像拉取失败时

也可在单独的 PowerShell 终端运行同一服务。解释器使用项目 `.venv`，`pdfplumber` 及其依赖安装在 `.artifacts/pdf-layout-deps` 并通过 `PYTHONPATH` 加载。两种运行方式使用同一个 `127.0.0.1:8079`，启动其中一种即可。`PDF_LAYOUT_URL` 的配置相同。

```powershell
uv pip install --python .venv/Scripts/python.exe --target .artifacts/pdf-layout-deps pdfplumber==0.11.10
$env:PYTHONPATH = (Resolve-Path '.artifacts/pdf-layout-deps').Path
.\.venv\Scripts\python.exe deployment/pdf-layout/server.py
```
