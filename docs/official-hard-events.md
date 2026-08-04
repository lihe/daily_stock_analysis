# A 股正式交易所硬事件

## 目标

停复牌、监管措施、纪律处分、业绩报告/预告和减持属于硬事件。个股分析不得再根据搜索摘要或大模型记忆确认这些事实，必须先查询对应交易所元数据。

PDF 正文不是默认依赖。交易所元数据用于确认事件标题、证券代码、发布日期、事件类型和官方链接；公告正文中的金额、比例和具体日期未核验时保持未知。

## 数据源

| 市场 | 公告 | 监管与纪律处分 |
| --- | --- | --- |
| 深交所 | `POST /api/disc/announcement/annList` | `GET /api/report/ShowReport/data`，监管目录 `1800_jgxxgk`，纪律处分目录 `1800_jgxxgk_cf` |
| 上交所 | `GET https://query.sse.com.cn/security/stock/queryCompanyBulletin.do` | `GET https://query.sse.com.cn/commonSoaQuery.do`，覆盖监管警示、监管工作函、通报批评、公开谴责和公开认定 |

实现入口：

- `src/services/exchange_disclosure_service.py`：交易所 HTTP 适配和字段标准化。
- `src/services/official_hard_event_service.py`：事件分类、覆盖状态、证据落盘和输出校验。
- `src/schemas/hard_event.py`：证据字段契约。

## 状态口径

| 状态 | 含义 | 决策约束 |
| --- | --- | --- |
| `VERIFIED` | 所有必需查询成功，窗口内存在硬事件 | 报告展示交易所事件，不代表事件一定利空 |
| `CLEAN` | 所有必需查询成功，窗口内未发现已配置类型 | 仅表示该查询窗口未命中，不代表股票整体安全 |
| `PARTIAL` | 至少一个必需查询失败 | 风险未知，空仓者不新增仓位 |
| `BLOCKED` | 所有必需查询失败或服务异常 | 风险未知，空仓者不新增仓位 |
| `NOT_APPLICABLE` | 当前证券不属于沪深 A 股适配范围 | 不执行本规则 |

通用新闻仍可提供行业风险、经营舆情、合同和政策线索，但其中出现的停牌、监管、业绩报告/预告或减持不得进入硬事件结论。

## GitHub Actions

每日分析工作流不需要新增密钥，也不下载 PDF。个股分析会直接访问交易所公开接口，并把证据写入：

```text
reports/evidence/<query_id>-<stock_code>.json
```

现有工作流上传整个 `reports/` 目录，因此证据 JSON 会随 `analysis-reports-*` artifact 一同保留。报告正文中的“正式交易所硬事件”章节由程序直接渲染，不经过大模型改写。Action 运行摘要同时列出每只股票的核验状态、硬事件数量和查询缺口。

单个数据源失败不会终止整次 Action，但对应股票必须显示 `PARTIAL` 或 `BLOCKED`。不能把接口失败解释为未发现风险。
