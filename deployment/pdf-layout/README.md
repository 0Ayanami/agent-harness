# PDF 版面解析

容器使用 `pdfplumber` 提取有文本层的 PDF。应用只向本机回环地址发送 PDF，无需挂载资料库目录。 [pdfplumber 官方文档](https://github.com/jsvine/pdfplumber)

在项目根目录的 PowerShell 中执行：

```powershell
docker compose -f deployment/pdf-layout/compose.yaml up -d --build --wait
```

在项目 `.env` 中配置 `PDF_LAYOUT_URL=http://127.0.0.1:8079`，然后重启应用。新上传或导入的 PDF 会使用新解析器。已有 PDF 可在资料库发起重建索引，或调用 `POST /api/agent/library/files/{id}/reindex`。重建会按新分块更新关键词和向量索引。启用后服务不可用时，引擎会显示索引失败，可启动容器后重试。省略 `PDF_LAYOUT_URL` 时应用使用原有的 `pypdf` 文本解析。

重复边栏文字按顶部和底部各 10% 的坐标范围筛选，并在至少两页且达到文档约半数页面时移除。常见的可变页码会先归一成一致的规则。同一页上的主体文本保留。表格识别以线条组成的网格为主，按页保存表格坐标、单元格矩阵和行范围，避免同一表格文字又进入普通段落。表格块按整行分组，长度允许时重复第一行作为列名，超长单行按长度上限拆分。

表格与正文共用 BM25、Embedding 和 Milvus，原 PDF 可按页预览。可调用结构接口查看页下的 `table` 节点、`metadata.rows` 和关联块。当前扫描件仍需要 OCR。无边框、跨页或复杂合并单元格的表格提取效果取决于 PDF 的版面和文字编码。

## Docker 镜像拉取失败时

也可在单独的 PowerShell 终端运行同一服务。依赖安装到 `.artifacts`，服务只监听 `127.0.0.1:8079`。`PDF_LAYOUT_URL` 的配置相同。

```powershell
uv pip install --python .venv/Scripts/python.exe --target .artifacts/pdf-layout-deps pdfplumber==0.11.10
$env:PYTHONPATH = (Resolve-Path '.artifacts/pdf-layout-deps').Path
.\.venv\Scripts\python.exe deployment/pdf-layout/server.py
```
