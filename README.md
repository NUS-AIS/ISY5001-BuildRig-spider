# 新加坡电脑选购 Agent：数据采集

> The repository now follows the course submission template: all system code lives in `SystemCode/`, and every command below must be run from `SystemCode/`. This README will be rewritten in full once the system is complete.

使用 Python 爬虫采集新加坡店铺公开商品目录，保存原始响应、结构化报价、TXT 文档与采集报告。现阶段提供 FastAPI 数据查询和 LangChain Document 加载，不包含最终推荐 Agent、向量数据库或硬件兼容性引擎。

## 安装与运行

```powershell
python -m pip install -r requirements.txt
python -m crawler.collect --max-pages 30 --review-products 22 --review-import data/imports/review_samples.jsonl
```

只运行自动爬虫、不导入既有评价摘录：

```powershell
python -m crawler.collect --max-pages 30 --review-products 22
```

`--max-pages` 为每个分类的分页上限；`--review-products` 为商品评论检查数量，采用分类轮询抽样；`--delay` 默认每站至少间隔 1.5 秒。完整遍历公开目录不代表覆盖商家的全部线下库存。网络错误、禁止访问、空分类、分页未完成和评论缺失都记录在报告中。失败时不会覆盖上一次有效数据指针；部分成功运行仍可能更新指针，使用前检查报告。

## 数据目录

```text
data/
  latest.json                 最新有报价的运行目录指针
  imports/review_samples.jsonl 公开网页检索所得评价摘录，非直接爬虫结果
  runs/<UTC采集时间>/
    prices.txt                可读报价文档
    prices.jsonl              每行一个商品配置的报价
    reviews.txt               可读用户评价/摘录
    reviews.jsonl             评价与来源元数据
    report.txt                可读采集报告
    report.json               数量、覆盖范围、失败原因、请求来源清单
    raw/                      实际请求响应与 robots.txt
```

以 TXT 为主要可读格式，同时提供 JSONL，避免 PDF 表格解析导致价格或配置错位。每次运行单独归档，可比较历史价格。时间字段为 UTC；新加坡时间为 UTC+8。

## 数据来源与边界

- Dynacore Singapore：CPU、GPU、主板、RAM、SSD、HDD、PSU、机箱、散热、机箱风扇及笔记本分类。
- Vii PC Singapore：CPU、GPU、主板、RAM、SSD、PSU、机箱和散热分类。
- 默认优先请求公开 Shopify 分类商品 JSON，按变体保存价格与 SKU；不会把分期月供当作售价，也不会自动把多个店铺同名商品合并。
- 价格币种 SGD 来自已确认的新加坡店铺配置，不对所有带 `$` 的页面一概推断币种。税费、运费是否包含保留 `null`，不等于免税或免运费。
- `bundle` 是根据名称识别的套餐，`accessory` 排除部分显卡支架，`external_storage` 与 `laptop_ram` 分离外置存储及笔记本内存；`source_category` 保留店铺原始分类。分类属于启发式结果，不能代替人工检查。店铺可能把风扇混在散热分类中。
- 报价为采集时页面标价，不保证未来成交价；`available=false` 的条目不能作为现货推荐。`source_updated_at` 是商品更新日期，不一定是价格更新日期。
- 自动评论解析只接收 Product JSON-LD 中的评论正文，不把店铺服务评价、聚合星级或广告文案当作产品评价。
- 当前店铺评论正文可能为空，Reddit 直连受 robots.txt 限制。`data/imports/review_samples.jsonl` 是本次通过公开网页检索补充的真实用户原文摘录，使用 `web_page_excerpt` / `web_search_excerpt` 标记，保留原采集时间；重新导入不代表已重新抓取原网页。
- 这些评价为小样本，地区与购买身份未核实，可能属于较早版本或不同配置；不是每个 SKU 都有评价。`match_level` 为型号系列或品牌级时不得自动关联到具体 SKU。原文中的技术错误保留，不作为规格事实；用户报告不能推算故障率。早期体验样机相关披露保存在 `disclosure`。
- 爬虫尊重 robots.txt、限速、有限重试，不绕过验证码、登录、付费墙。公开评价页面 URL 可加到配置；不能访问的来源记录失败。

## FastAPI

```powershell
python -m uvicorn main:app --reload
```

打开 `http://127.0.0.1:8000/docs`：

- `GET /data/status`：数量、分类和采集问题。
- `GET /data/prices?category=laptop&max_price=2000&in_stock=true`：预算和库存过滤；支持 `q`、`offset`、`limit`。
- `GET /data/reviews?category=cpu`：评价与摘录。

### 推荐后端

后端现已提供可运行的会话、需求版本、DAG 推荐、三路检索契约、校验、SSE 事件和记忆接口。默认 `BUILDRIG_RETRIEVAL_BACKEND=local`，直接使用当前 JSONL 数据验证完整业务流程；此模式会在 `/api/v1/health` 标记为 `degraded`，因为它不是最终 Neo4j + Milvus 部署。

主要接口：

- `POST /api/v1/sessions`：创建会话并选择是否启用长期记忆。
- `POST /api/v1/sessions/{id}/messages`：解析需求、更新版本并返回澄清问题。
- `POST /api/v1/sessions/{id}/runs`：以 `dag` 或 `pi` 模式创建推荐任务。
- `GET /api/v1/runs/{id}`、`/events`、`/result`：读取状态、SSE 事件和结果。
- `GET /api/v1/runs/{id}/execution`：检查耐久状态、Agent 工具账本和使用记录。
- `GET/POST /api/v1/sessions/{id}/memories`：管理确认后的会话记忆。
- `POST /api/v1/internal/retrieval/hybrid`：供 Pi Worker 调用三路检索。
- `POST /api/v1/internal/options/validate`：供 Pi Worker 调用确定性校验。

完整接口结构可在启动后访问 `http://127.0.0.1:8000/docs`。系统架构和两种编排方式见 `docs/system-architecture.md`。

生产检索配置采用“本地模型、云端数据库”：

1. 从 `.env.example` 复制 `.env`，填写 Neo4j Aura 和 Zilliz Cloud（Milvus）的 URI、用户与令牌；不要提交 `.env`。
2. 保持 `qwen3:8b`、`bge-m3` 和 `1024` 维配置，运行 `python -m backend.check_dependencies` 检查 Ollama、两套云数据库和 Pi Worker。
3. 运行 `python -m backend.index_data`，将当前发布快照的报价与评价写入 Neo4j，并将文档块分批写入 Milvus。也可使用 `--neo4j-only` 或 `--milvus-only` 单独执行。
4. 启动 FastAPI、`pi-worker` 和 Vue。`GET /api/v1/health` 应显示 `retrieval_backend=neo4j_milvus`。

Milvus 内部融合 Dense 与 BM25，应用层再与 Neo4j 图证据执行 RRF。`.env.example` 使用本机 Ollama 的 `bge-m3` 生成 1024 维 dense vector；确定性 Hash Embedding 只在测试回退中使用。索引和查询共用同一 embedding 工厂，并会在向量维度与配置不一致时直接失败，防止污染 Milvus collection。Neo4j 同时保存 Product、Offer、Review、Source 节点及来源关系；无法确认具体 SKU 的评论保留 `match_level`，不会伪装成精确型号证据。

Pi Agent 采用独立 Node.js Worker 部署，使用官方 `@earendil-works/pi-agent-core` 和本地 Ollama。当前 Pi 版本要求 Node.js 22.19；项目已在 `pi-worker` 中锁定依赖，并为本机旧 Node 环境提供项目内便携 Node 运行脚本：

```powershell
cd pi-worker
npm install
npm run build:local
npm run start:local
```

设置 `BUILDRIG_PI_RUNTIME_URL` 后，请求中的 `orchestration_mode=pi` 会将固定需求版本与快照交给 Pi；Pi 通过受保护的 FastAPI 内部接口读取候选、执行混合检索和确定性校验。前端可直接选择 DAG workflow 或 Pi runtime。未配置 Pi 时明确返回失败状态，不会静默改用 DAG。

### 本地 Ollama 模型

本项目的运行模型组合为 `qwen3:8b`（Agent Chat Model）和 `bge-m3`（1024 维 Embedding），不依赖 OpenAI API。先安装并启动 Ollama，然后执行：

```powershell
ollama pull qwen3:8b
ollama pull bge-m3
Copy-Item .env.local.example .env  # 仅本地检索演示
```

关键配置为：

```dotenv
BUILDRIG_MODEL=qwen3:8b
BUILDRIG_MODEL_PROVIDER=ollama
OLLAMA_BASE_URL=http://127.0.0.1:11434
BUILDRIG_OLLAMA_REASONING=false
BUILDRIG_OLLAMA_NUM_CTX=8192
BUILDRIG_OLLAMA_NUM_PREDICT=1200
BUILDRIG_EMBEDDING_PROVIDER=ollama
BUILDRIG_EMBEDDING_MODEL=bge-m3
BUILDRIG_EMBEDDING_DIMENSIONS=1024
```

接入云数据库时改用 `.env.example`。修改 embedding 模型或维度后必须重新执行 `python -m backend.index_data`。该命令会重建配置的 Milvus collection；不要让 256 维 Hash、1024 维 BGE-M3 或其他模型的向量混存在同一 collection 中。

### Multi-Agent 与 Harness 边界

DAG 模式由五个独立 Agent 角色组成：Planner、Desktop Planner、Laptop Selector、Evidence Agent 和 Review Agent。它们使用 LangChain Runnable 接口，但不共享任意可变对象；共享产物通过 Run Harness 检查点传递。Agent 不能直接访问文件、Neo4j 或 Milvus，只能调用 Tool Harness 注册的只读业务工具。

Harness 为每次工具调用保存 `intent -> effect_pending -> completed/failed` 状态、调用参数、结果、负责 Agent 和 replay policy。读取与检索工具声明为 `safe`；未来的记忆写入、采购或外部消息等工具必须声明为 `never`，中断后交由业务对账。每个运行还有完整 operation state、递增检查点、事件流、Agent 步数和工具调用预算、append-only usage ledger。服务启动时会把遗留的 `effect_pending` 调用标记为 `interrupted`。

所有 ID 使用可排序 UUIDv7。用户接口通过 `X-User-ID` 表示经过上游认证后的主体；生产环境必须由认证网关注入并删除客户端自带的同名 Header。Pi Worker 调用 `/api/v1/internal/*` 时使用 `BUILDRIG_INTERNAL_API_TOKEN`。当前 SQLite 适合单机原型和课程演示；多进程生产部署应迁移到支持事务和 worker lease 的共享数据库及任务队列。

采集在命令行独立运行，不占用 HTTP 请求处理时间。现有根路径和 hello 接口保留。

## LangChain

```python
from crawler.documents import load_documents

documents = list(load_documents())
# documents 可交给后续 splitter / embedding / vector store。
```

Document 元数据包含来源、类别、采集时间和唯一 ID。预算约束应使用结构化价格与 Decimal 过滤；评价可用于检索辅助解释。下一阶段另采集主板插槽、内存代际、机箱尺寸、显卡长度和电源需求等规格，不能仅凭这些报价验证装机兼容性。

## 测试

```powershell
python -m unittest discover -s tests -v
```

覆盖价格有效性、配置去重、库存、套餐识别、商店评价隔离与 Unicode 数据读取。

## Vue 前端

前端位于 `frontend/`，包含注册、登录、Agent 对话、推荐方案、已保存方案、组件目录、记忆管理和个人中心页面。

```powershell
cd frontend
npm install
npm run dev
```

开发服务器运行在 `http://127.0.0.1:5173`，并将 `/api` 和 `/data` 代理到 FastAPI 的 `http://127.0.0.1:8000`。先启动后端，再启动前端。

当前登录注册是课程原型界面，账号只保存在浏览器 localStorage。前端会使用该用户 ID 调用后端的隔离接口，但这不等于可信认证。正式部署需要后端密码哈希、令牌、刷新/注销和认证网关注入的可信用户身份。
