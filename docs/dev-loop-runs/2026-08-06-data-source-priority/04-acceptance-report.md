# Acceptance Report

## 验收结论

**PARTIAL — 实现与离线合同通过，目标 Action 成功且 artifact 结构完整，但结构化基本面/资金流在本次外部运行中因 timeout 未形成完整覆盖。**

该结论将三个门禁独立判定，不把绿色 workflow 等同于数据源或报告完整性。

## 三门禁读回

| 门禁 | 状态 | 已确认事实 |
|---|---|---|
| Workflow / job | **PASS** | Run `31049628869` 和 `analyze` job 均为 `success`；event 为 `workflow_dispatch`；head SHA 为 `aa6a36c`；随机延迟 step 为 `skipped` |
| 实时/结构化来源 | **PARTIAL** | 两股实时均由 Tencent 成功；日线与筹码均由 Tushare 成功；两条 `[DataSourceEvidence]` 存在，但基本面/资金流块因 timeout 降级或失败 |
| 报告 / artifact | **PASS（结构）/ PARTIAL（数据覆盖）** | artifact 可下载且未过期；含两股报告章节和两份交易所证据 JSON；`600797` 交易所覆盖为 `PARTIAL` |

Run：[GitHub Actions 31049628869](https://github.com/lihe/daily_stock_analysis/actions/runs/31049628869)

Job：[analyze 92453528658](https://github.com/lihe/daily_stock_analysis/actions/runs/31049628869/job/92453528658)

Artifact：[analysis-reports-93](https://github.com/lihe/daily_stock_analysis/actions/runs/31049628869/artifacts/8948002891)

## 来源决策证据

### 通用路由

| 股票 | 日线 | 实时 | 筹码 |
|---|---|---|---|
| `600797` | Tushare，42 行 | Tencent | Tushare，数据日 `2026-08-05` |
| `002315` | Tushare，42 行 | Tencent | Tushare，数据日 `2026-08-05` |

### `[DataSourceEvidence]`

| 股票 | overall | 成功块 | 结构化失败/降级 |
|---|---|---|---|
| `600797` | `partial` | `valuation=realtime_quote:ok` | `growth/earnings/institution=akshare_fundamental_bundle:timeout`；`capital_flow` 为 timeout 且无 provider attempt；`dragon_tiger/boards` 为 timeout |
| `002315` | `partial` | `valuation=realtime_quote:ok`；`dragon_tiger=stock_lhb_stock_statistic_em:ok` | `growth/earnings/institution=tushare_fundamental_bundle:timeout`；`capital_flow=tushare_capital_flow:timeout -> akshare_capital_flow:timeout`；`boards` 为 timeout |

**Facts**

- 两条 evidence JSON 都是单行、可解析且未含 Token 值、endpoint URL 或 raw response。
- `600797` 的 evidence 没有 Tushare 结构化 provider 记录，因此只能确认其未进入实际 Tushare 结构化调用；不从日志反推未证实的唯一根因。
- `002315` 明确记录了 Tushare 基本面和资金流尝试，其资金流也明确记录了 AkShare fallback 尝试，两者均超时。

**Inference**

- 本次运行证明了日线/筹码与实时来源的用途分流，也证明了结构化调用能在 deadline 内 fail-open；没有证明外部结构化端点在当前预算内能稳定返回完整数据。

## Artifact 完整性

- Artifact ID：`8948002891`，名称 `analysis-reports-93`，大小 `699877` bytes，`expired=false`。
- Action 上传读回：29 个文件，ZIP SHA-256
  `032caf141387ca9d8747f8cf9c18faf7797109c1d5a244bd7692297e55039f94`。
- 下载报告：`reports/report_20260806.md`，`13877` bytes，SHA-256
  `f7b4770dcbd9839da4a06bbb4b2afd33ee79567370643495419879c5283bc436`。
- 报告含且仅含两个目标股票章节：`002315` 和 `600797`；实时来源表均为“腾讯财经”。
- 交易所证据 JSON：`002315=VERIFIED, coverage_complete=true, events=5`；
  `600797=PARTIAL, coverage_complete=false, events=4`，缺口为 `sse_regulatory`。

## 已知风险与建议动作

1. **外部端点时延。** 本次结构化基本面/资金流未在 capability deadline 内完成；若要将验收提升为全 PASS，需在不改代码前先复现并分清权限、空表、网络时延或并发调度等运行原因。
2. **`600797` 结构化首选未尝试。** 日志只能证明 AkShare bundle 超时和 Tushare provider 未记录；需要独立定向运行或更精确的调度诊断证据才能定因。
3. **正式交易所覆盖。** `600797` 的上交所监管查询为 `PARTIAL`，报告已 fail-closed 为不新增仓位；该外部查询缺口不得解读为无风险。

## Definition of Done

- [x] Tasks 1–7 去重联合离线回归通过
- [x] 最终 diff review 无 blocker/important finding
- [x] 代码 HEAD 推送至现有 PR 分支
- [x] 精确两股 Action 触发，随机延迟 skipped
- [x] Workflow、来源决策、artifact 三门禁独立读回
- [x] 脱敏文档与单文件 HTML 形成验收记录
