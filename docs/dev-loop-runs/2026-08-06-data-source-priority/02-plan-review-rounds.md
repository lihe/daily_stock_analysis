# Plan Review Rounds

## Round 1

### Architecture review

- `IMPORTANT`: 收敛修改范围，不改 `data_provider` 全局优先级与路由代码。
- `IMPORTANT`: 明确核对并自动断言 Action 默认值。
- `QUESTION`: 明确空白显式配置沿用当前回退语义。

### Test strategy review

- `BLOCKER`: 为 Action 默认值增加 YAML 加载断言。
- `IMPORTANT`: 覆盖 Token 存在时的显式 override 保真。
- `IMPORTANT`: 用现有离线测试验证 Tushare Priority `-1` 与自定义 URL 合同。

### Product and risk review

- `IMPORTANT`: 说明实时后续源还可能用于字段补充。
- `IMPORTANT`: 区分兼容客户端初始化合同与第三方端点运行时可用性。
- `QUESTION`: 告知 Token-only 用户如何显式恢复 Tushare 实时优先。

### Resolution

- 计划删除 `data_provider/__init__.py` 与 `data_provider/base.py` 修改项，明确不动全局排序。
- 新增默认值、显式覆盖和 workflow 三类断言。
- 验收命令纳入 `test_tushare_fetcher_http_client.py` 与现有实时回退测试。
- 需求说明补充字段补全语义、空值兼容语义和升级迁移方式。

## Round 2

复审发现当前基本面调用链并不经过全局 fetcher priority；仅修改实时配置无法满足“完整方案”。用户已确认扩大到 Tushare 财务适配器与 Tushare→AkShare 字段级降级。

## Round 3

- 架构调查建议在 `TushareFetcher` 暴露与 AkShare adapter 同形状的三项能力，Manager 只负责编排。
- 测试调查将配置、adapter 标准化、manager fallback/merge、ETF/海外边界分层覆盖。
- 数据契约调查确认首期接口：`fina_indicator`、`income`、`cashflow`、`forecast`、`express`、`dividend`、`top10_holders`、`moneyflow`、`moneyflow_ind_ths`、`top_list`；`top10_floatholders` 暂不纳入。
- 修订计划采用独立 `TushareFundamentalAdapter`，避免继续扩大已较长的 fetcher 文件，并使用有界并发适配现有 3 秒单能力预算。

## Round 4 resolution

上一轮全量评审的阻断项已写入需求和计划：

- 为默认 3 秒能力预算明确保留 1.2 秒 AkShare 降级窗口；Tushare 上限 1.8 秒，提前结束则按实际剩余时间降级。
- 并发限制上移到单个 `TushareFetcher` 的共享四槽信号量；adapter 超时只返回已完成结果，不能取消的运行线程受全局槽位约束。
- 缺失值合同明确：`0`、`False` 和日期有效；字典递归补齐，列表整体选择，禁止行业榜跨单位混合。
- 股东数据仅输出 `top10_holders` 可观察快照，不制造变动指标。
- `moneyflow` 明确最近 5/10 个已完成交易日和万元口径；行业榜保持亿元口径。
- 增长率优先累计口径；财务报表按可见披露、合并报表、同报告期和最新修订匹配。
- 官方字段合同中 `cash_div_tax` 是税前每股现金分红，`cash_div` 是税后；因此保留 `cash_div_tax` 并增加防互换回归测试。
- 首期移除 Tushare 龙虎榜接入，继续使用 AkShare；避免为兼容现有 20 日统计额外执行约 20 次 `top_list` 请求。
- 增加无 Token、共享并发峰值、回调路径、预算预留和 Action 三闸门测试/验收。

待修订版全量复审。

## Round 5 resolution

修订版架构与测试计划已通过；数据风险复审新增两项重要意见并完成处理：

- `moneyflow.net_mf_amount` 不等同既有“主力净流入”，因此 Tushare 只输出真实命名的 latest/5d/10d 字段，既有主力字段留给 AkShare 补齐。
- 1.2 秒是默认 3 秒预算下的目标预留，不是突破 deadline 的硬保底。每项能力使用绝对单调时钟 deadline，所有 timeout 只取正的剩余时间。

## Final verdict

- 架构复审：`APPROVED`
- 测试策略复审：`APPROVED`
- 数据契约与风险复审：`APPROVED`

计划无阻断或重要遗留项，可以进入测试先行实施。
