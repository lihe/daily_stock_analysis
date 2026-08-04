# HHXG Data API 共享缓存

## 目标与边界

HHXG Data API 为 A 股个股分析补充市场、题材、资金、技术和软风险上下文。它不替代：

| 信息 | 权威来源 |
| --- | --- |
| 公告、监管、停复牌、业绩、减持 | 上交所、深交所、北交所等正式交易所数据 |
| 分钟分时、VWAP、五档盘口 | 实时行情源和交易软件截图 |
| 成交与持仓 | 券商成交回报和持仓截图 |

`stale`、`unknown_date`、`unavailable` 和 `no_data` 都是数据缺口，不能解释为“没有风险”。

## Action 配置

在仓库 `Settings -> Secrets and variables -> Actions` 中新增 Repository secret：

```text
HHXG_DATA_API_TOKEN=<网页签发的 Data API token>
```

Data API token 与 HHXG MCP token 独立，不可混用。Action 不打印 token，也不会把 token 写入报告或 Artifact。
Data API token 有有效期；重新签发后旧 token 会立即失效，需要同步更新 Repository secret。

可选 Repository variables：

| 变量 | 默认值 | 用途 |
| --- | --- | --- |
| `HHXG_DATA_API_BASE_URL` | `https://hhxg.top/api/data` | API 根地址 |
| `HHXG_DATA_API_TIMEOUT_SECONDS` | `15` | 单个 scope 超时 |
| `HHXG_DATA_API_PROMPT_MAX_CHARS` | `8000` | 每只股票的压缩上下文上限 |

## 单次运行流程

1. 股票分析开始前并发请求固定的 23 个 scope，每个 scope 正常情况下只请求一次。
2. 每个响应记录 `status`、`data_date`、`generated_at`、记录数和错误原因。
3. 原始响应保存到 `reports/evidence/hhxg_data/<scope>.json`，总状态保存到 `manifest.json`。
4. 每只 A 股仅从共享缓存生成个股命中和压缩市场上下文，不再请求 HHXG。
5. 报告和 GitHub Step Summary 展示同一个 `cache_id`，Artifact 保存原始证据供复核。

网络、鉴权、限流或单个 scope 失败时走 fail-open：主分析继续，但报告必须保留相应数据缺口。正式交易所硬事件的 fail-closed 规则不受影响。

## 固定 Scope

```text
snapshot,breadth,funds,sentiment,news,hotmoney,margin,northbound,
themes-ranking,theme-concepts,concept-chain,strategy,strategy-full,
stock-indicators,indicator-resonance,chipwork,risk-alerts,resonance,
community-sentiment,dongmi,etf,moneyflow,blocktrade
```

## 验证

Action 完成后检查：

1. Step Summary 中出现“HHXG Data API 共享缓存”，scope 数量为 23。
2. 日志中只出现一次“HHXG Data API 统一预取完成”。
3. 多只股票报告的 `cache_id` 相同。
4. Artifact 的 `reports/evidence/hhxg_data/manifest.json` 与 23 个 scope 状态一致。
