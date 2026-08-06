# Requirements Baseline

## Goal

让 A 股数据源按用途分流：

- 历史日线、筹码和结构化财务数据优先使用已配置的 Tushare 兼容源。
- 实时行情默认按腾讯、新浪、东方财富的顺序获取；后续来源既可在前序失败时降级，也可补充缺失字段。
- Tushare 财务或资金流接口为空、无权限、超时或失败时，在同一总预算内自动降级到现有东方财富/AkShare 适配器。

## Non-goals

- 不新增用户必须配置的开关。
- 不改变用户显式配置的 `REALTIME_SOURCE_PRIORITY`。
- 不调整港股、美股、ETF 的专用路由。
- 不修改 Token、API URL 或代理作用域。
- 不把盘后 Tushare `moneyflow` 当作盘中实时数据。
- 首期不引入 `top10_floatholders`，不虚构股东变动字段。
- 首期龙虎榜继续使用现有 AkShare 路径；Tushare `top_list` 按交易日查询，若实现 20 日统计会额外产生约 20 次请求，不符合本轮提速目标。

## User-visible Behavior

- 配置 `TUSHARE_TOKEN` 不再自动把 `tushare` 插到实时行情首位。
- 未显式配置实时源时，固定使用 `tencent,akshare_sina,efinance,akshare_em`。
- 显式非空实时源继续逐字保留；空字符串回退默认；纯空白值保持既有显式配置语义。
- A 股非 ETF 的 `growth`、`earnings`、`institution`、`capital_flow` 优先查询 Tushare；仅缺失部分由东方财富/AkShare 补齐。
- `dragon_tiger`、`valuation` 分别继续使用现有 AkShare 路径和同轮实时行情，不改变已有口径。
- 仅凭 `TUSHARE_TOKEN` 将 Tushare 置于实时首位的旧用户，需要显式设置 `REALTIME_SOURCE_PRIORITY=tushare,...` 才能恢复旧行为。

## Data Contract

### 财务与股东

- 仅使用已披露且 `f_ann_date`（缺失时用 `ann_date`）不晚于上海当前时间的 `report_type=1` 合并报表。
- 同一 `end_date` 的修订记录选择最新可见披露版本；`income`、`cashflow`、`fina_indicator` 必须按同一报告期匹配。
- 增长率优先累计口径：营收使用 `tr_yoy`、再用 `or_yoy`，缺失时才用 `q_gr_yoy`；归母净利使用 `netprofit_yoy`，缺失时才用 `q_profit_yoy`。结果标记 `cumulative` 或 `single_quarter`。
- 财务报告保留 `report_date`、`announcement_date`、`revenue`、`net_profit_parent`、`operating_cash_flow`、`roe`、`currency=CNY`、`amount_unit=yuan`、`report_type=1`；缺失字段保持 `None`。
- 分红只累计实施且事件已完成的 `cash_div_tax`（税前每股现金分红），计算最近 365 天 TTM；不得与 `cash_div`（税后）互换。
- `top10_holders` 只输出可直接观察的快照：报告期、公告日、股东数、持股比例合计和逐行明细。`institution_holding_change`、`top10_holder_change` 等变动字段留空，允许 AkShare 补齐。

### 资金流

- 以 19:00 为当日盘后可用边界，通过交易日历确定最近 10 个已完成交易日。
- `moneyflow.net_mf_amount` 保留真实字段名；最近 5/10 个非空交易日值分别写入 `net_mf_amount_5d`/`net_mf_amount_10d`。
- Tushare 不把全口径 `net_mf_amount` 伪装成现有 `main_net_inflow`/`inflow_5d`/`inflow_10d`；这些既有“主力”口径字段保持缺失并由 AkShare 降级结果补齐。
- 个股资金流明确记录 `net_flow_kind=net_mf_amount`、`amount_unit=万元`。
- 行业榜 `moneyflow_ind_ths.net_buy_amount` 保持 `amount_unit=亿元`。Tushare 非空榜单整体优先，空榜单才使用 AkShare；禁止跨单位逐项合并或相加。

### 缺失值与来源

- 仅 `None`、`NaN`、空白字符串、缺失键和空容器视为缺失；`0`、`False`、有效日期均不得被覆盖。
- 字典递归补齐；非空列表整体保留优先源，空列表整体采用降级源，不逐元素混合。
- `source_chain` 和 `errors` 仅记录实际尝试的数据源，保持真实顺序；错误仅记录接口名和异常类型。

## Timing and Concurrency Contract

- 每项 manager 能力仍受现有 `fundamental_fetch_timeout_seconds` 总预算约束。
- 每项能力在开始时用单调时钟建立绝对 deadline。默认 3 秒预算下，Tushare 优先阶段最多使用 `min(1.8 秒, 总预算的 60%)`，目标为 AkShare 预留 40%（默认 1.2 秒）；Tushare 提前返回时，降级源可使用实际剩余预算。
- 所有实际 timeout 都取 `max(0, deadline - monotonic_now)`；调度开销可能令降级预算略低于目标 1.2 秒，但不得为补足目标而突破总 deadline。若预算耗尽则 fail-open。
- Tushare 财务 bundle 可本地并发，但 `TushareFetcher` 对所有 `_call_api_with_rate_limit()` 调用设置共享最多 4 个在途请求；计数器并发安全。
- adapter 超时后返回已完成的部分结果并取消尚未开始的 future；已运行线程不可强制取消，由共享信号量限制残留请求数量，且不阻塞 AkShare 降级线程。

## Acceptance Criteria

1. 有 `TUSHARE_TOKEN`、无 `REALTIME_SOURCE_PRIORITY` 时，实时默认顺序固定且 Action 与程序一致。
2. 显式非空实时配置逐字返回；空字符串回退默认；纯空白字符串保持当前语义。
3. Tushare 财务适配器标准化 `fina_indicator`、`income`、`cashflow`、`forecast`、`express`、`dividend`、`top10_holders`，字段和单位满足上述合同。
4. Tushare 资金流标准化 `moneyflow` 与 `moneyflow_ind_ths`；最近 5/10 日按已完成交易日聚合到真实命名的 `net_mf_amount_*`，既有主力字段只由匹配该口径的来源提供。
5. Tushare 有效字段优先；AkShare 只补缺失字段，`0`/`False` 不被覆盖，列表不跨来源或单位逐项混合。
6. 无 Token、端点失败、返回空数据或超时时，bundle 和 capital flow 的 AkShare 路径仍可用；龙虎榜路径行为不变。
7. Tushare 默认 3 秒超时只占最多 1.8 秒，目标预留 1.2 秒；所有调用受同一绝对 deadline 约束，共享在途请求峰值不超过 4。
8. ETF、港股、美股路由和实时估值复用保持不变。
9. 自定义 Tushare URL、代理隔离、统一限流及 Priority `-1` 合同继续通过离线回归。
10. 手动 Action 对 `600797,002315` 不执行随机延迟；分别报告 workflow 成功、数据源决策日志、数据完整性，外部端点降级不等于实现失败。
11. `workflow_dispatch` 提供可选 `stock_codes` 字符串输入；非空手动值优先于仓库变量，定时任务继续使用原仓库变量/默认股票池。
12. Action 日志为每只股票输出一行脱敏结构化来源证据，至少包含 block status、coverage、实际 provider/endpoint 和异常类型；不得记录 Token、URL 原始响应或完整异常消息。

## Constraints

- 最小安全修改，保持外部基本面 context schema 兼容。
- 测试先行，每个行为修改必须观察到对应新增测试先失败。
- 所有 Tushare Pro 调用必须经过 `_call_api_with_rate_limit()`。
- 错误记录不得包含 Token、完整服务端响应或凭据。
- 不泄露或写入 Tushare Token。

## Assumptions

- 第三方端点兼容标准 Tushare Pro 请求/响应结构；运行时权限和可用性属于外部条件，必须 fail-open。
- Tushare 盘后接口的截止时间以应用上海时区时钟判断。
- 用户此前已明确授权在现有分支提交、推送、更新 PR，并触发 `600797,002315` 的手动 Action 验证。

## Open Questions

无阻断问题。

## Source Request

“按‘完整方案’继续”。

## Repo Context

- 分支：`codex/tushare-compatible-endpoint`
- 基线提交：`e42c9e7`
- 当前日线、筹码和板块已有 Tushare 能力；基本面与资金流仍直接使用 AkShare adapter。
