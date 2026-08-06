# Implementation Log

## 实施结果

功能实现已于 `codex/tushare-compatible-endpoint` 完成，Action 执行的 Code/Run HEAD 为
`aa6a36c2a5cff9566df4c0dfb3d99fa570523657`。本轮保持了实时行情、结构化数据和
降级路径的边界：

| 能力 | 最终行为 | 兼容性边界 |
|---|---|---|
| 实时行情 | 默认 `tencent,akshare_sina,efinance,akshare_em` | Token 不再自动改变实时顺序；显式配置保真 |
| 日线与筹码 | 保持现有 Tushare 路由 | ETF、港美股路由不变 |
| 基本面 bundle | A 股非 ETF 先 Tushare，再在总 deadline 余量内用 AkShare 补空 | `0`/`False`/日期不被覆盖，列表不跨来源混合 |
| 资金流 | 个股和行业子结构独立决定 fallback | 万元/亿元口径不混合 |
| 来源证据 | 每只 A 股输出一行 `[DataSourceEvidence]` | 只保留股票代码、状态、provider/endpoint 名和异常类型 |
| 手动 Action | 新增可选 `stock_codes` 输入 | 非空手动值仅覆盖当次运行，schedule 回退不变 |

## 提交链

| SHA | 提交 |
|---|---|
| `3a63f35` | `fix: keep Tushare out of realtime defaults` |
| `e222a51` | `feat: add bounded Tushare fundamental adapter` |
| `8124098` | `fix: enforce Tushare request boundaries` |
| `4f40bdd` | `fix: rate limit legacy Tushare realtime` |
| `93ed982` | `fix: harden Tushare report and rate limits` |
| `a5ac53c` | `feat: prefer Tushare fundamentals with fallback` |
| `274da3e` | `fix: preserve Tushare fallback boundaries` |
| `5ae1341` | `fix: track provider attempts explicitly` |
| `f5d8f94` | `fix: enforce ordered capability deadlines` |
| `7fc599d` | `fix: align Tushare standard field contracts` |
| `aed7ea0` | `fix: bound Tushare calendar resolution` |
| `2af0624` | `feat: accept stock codes in manual workflow` |
| `9aae289` | `feat: log fundamental source evidence` |
| `3aba306` | `fix: harden fundamental evidence logging` |
| `aa6a36c` | `fix: honor deadlines while waiting for fetcher locks` |

## TDD 与离线验证

### RED 证据

- 实时默认顺序：`1 failed, 43 passed`，Token-only 用例观察到旧的 Tushare 自动插入。
- 基本面 adapter：首次为 `ModuleNotFoundError`，后续分别观察到报告期、分红、股东、资金流与 timeout 合同缺失。
- manager 编排：`5 failed, 32 passed`，证明旧路径仍直接调用 AkShare，且未满足字段级补空与绝对 deadline。
- 官方字段/日历：首轮 `10 failed, 18 passed`，长假日和 19:00 边界各有独立 RED。
- Action 精确股票输入：`2 failed, 1 passed`。
- 脱敏来源证据：`2 failed`，旧实现没有 INFO 级证据行。
- 锁竞争 deadline：`2 failed`，旧实现在主调用返回后仍可能发生 late provider call。

### 最终联合 GREEN

```bash
.venv/bin/pytest -q \
  tests/test_config_env_compat.py \
  tests/test_daily_analysis_workflow_tushare_env.py \
  tests/test_realtime_quote_fallback_logging.py \
  tests/test_tushare_fundamental_adapter.py \
  tests/test_tushare_fetcher_followups.py \
  tests/test_tushare_fetcher_http_client.py \
  tests/test_fundamental_adapter.py \
  tests/test_fundamental_context.py \
  tests/test_data_tools_get_capital_flow.py \
  tests/test_fetcher_source_optimization.py
```

结果：`173 passed, 6 warnings in 23.58s`。6 条 warning 均来自既有第三方依赖（Starlette/httpx、
`pkg_resources`/`lark_oapi`）的弃用提示。同时完成：

- `py_compile` 通过；
- fatal Flake8（`E9,F63,F7,F82`）通过；
- `git diff --check` 通过；
- `e42c9e7..aa6a36c` 最终广度 diff review 未发现 blocker/important finding。

广度评审包位于
`.superpowers/sdd/2026-08-06-data-source-priority/review-e42c9e7..aa6a36c.diff`。

## 在线 Action 读回

- Run：[31049628869](https://github.com/lihe/daily_stock_analysis/actions/runs/31049628869)
- 触发：`workflow_dispatch`，`mode=stocks-only`，`force_run=true`，`stock_codes=600797,002315`
- Action Code/Run HEAD：`aa6a36c2a5cff9566df4c0dfb3d99fa570523657`
- 随机延迟 step：`skipped`
- Workflow / `analyze` job：`success / success`，用时约 5 分 42 秒
- `TUSHARE_TOKEN`：仅核验为已配置，未读取或记录值。

最终 PR/remote HEAD 在 Code/Run HEAD 之上包含本轮验收文档；不将 `aa6a36c` 称为文档提交后的当前 PR HEAD。

运行时来源证据和 artifact 读回见
[`04-acceptance-report.md`](./04-acceptance-report.md)。
