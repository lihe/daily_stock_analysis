# Implementation Plan

完整执行计划见 `docs/superpowers/plans/2026-08-06-data-source-priority.md`。

本次最终拆分为 8 个任务，与完整执行计划一致：

| Task | 范围 |
|---|---|
| 1 | 实时默认顺序与 Tushare Token 解耦 |
| 2 | 有界 Tushare 基本面/资金流 adapter |
| 3 | Tushare-first 字段级 fallback 编排 |
| 4 | 官方字段、单位和交易日历合同修正 |
| 5 | 手动 Action 精确股票输入 |
| 6 | 每股脱敏数据源证据日志 |
| 7 | ordered capability 锁等待纳入 deadline |
| 8 | 离线回归、两股 Action 与三门禁验收 |
