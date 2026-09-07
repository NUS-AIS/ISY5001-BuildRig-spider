# 新加坡电脑选购 Agent：数据采集

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
