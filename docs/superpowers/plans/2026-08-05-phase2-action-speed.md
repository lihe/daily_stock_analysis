# GitHub Action 第二阶段提速 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 手动 Action 立即开始执行，并在 A 股单股分析内复用已成功获取的实时行情，消除基本面估值阶段的重复腾讯请求。

**Architecture:** Workflow 通过 GitHub 事件名控制随机延迟；Python 主流程通过关键字参数把请求级行情对象传给基本面聚合。复用仅发生在同一股票的一次分析中，首次行情失败仍走现有重试和 fallback。

**Tech Stack:** GitHub Actions YAML、Python 3.11、pytest、PyYAML、GitHub CLI。

## Global Constraints

- `schedule` 继续执行 0～59 秒随机延迟，`workflow_dispatch` 必须跳过该步骤。
- 只复用 A 股主流程已经成功取得的 `realtime_quote`；传入 `None` 时必须重新调用现有实时行情入口。
- 不新增全局缓存、TTL、配置项或跨股票、跨运行共享状态。
- 不改变数据源优先级、超时、重试、fallback、报告字段和非 A 股路径。
- 关键逻辑使用中文注释解释复用边界与失败重试原因。
- 每个行为变化都先运行失败测试，再写最小实现。

---

### Task 1: 手动触发跳过随机延迟

**Files:**
- Modify: `tests/test_daily_analysis_workflow_llm_env.py`
- Modify: `.github/workflows/00-daily-analysis.yml:43-46`

**Interfaces:**
- Consumes: GitHub 上下文表达式 `github.event_name`。
- Produces: 随机延迟步骤的 `if: github.event_name == 'schedule'` 运行契约。

- [ ] **Step 1: 写入失败的 Workflow 契约测试**

```python
def test_daily_analysis_random_delay_only_runs_for_schedule() -> None:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["analyze"]["steps"]
    delay_step = next(
        step for step in steps if step.get("name") == "随机延迟（避免固定时间访问）"
    )

    assert delay_step.get("if") == "github.event_name == 'schedule'"
```

- [ ] **Step 2: 运行测试并确认按预期失败**

```bash
python -m pytest tests/test_daily_analysis_workflow_llm_env.py::test_daily_analysis_random_delay_only_runs_for_schedule -q
```

Expected: FAIL；当前 `delay_step.get("if")` 为 `None`。

- [ ] **Step 3: 添加最小 Workflow 条件**

```yaml
- name: 随机延迟（避免固定时间访问）
  if: github.event_name == 'schedule'
  run: sleep $((RANDOM % 60))  # 随机延迟0-59秒启动
```

- [ ] **Step 4: 运行 Workflow 契约测试**

```bash
python -m pytest tests/test_daily_analysis_workflow_llm_env.py -q
```

Expected: PASS，零失败。

- [ ] **Step 5: 提交 Workflow 行为**

```bash
git add tests/test_daily_analysis_workflow_llm_env.py .github/workflows/00-daily-analysis.yml
git commit -m "ci: skip random delay for manual analysis"
```

---

### Task 2: 基本面估值复用传入行情

**Files:**
- Modify: `tests/test_fundamental_context.py:221-255`
- Modify: `data_provider/base.py:2951-3034`

**Interfaces:**
- Consumes: `realtime_quote: Optional[Any]`，字段包含 `pe_ratio`、`pb_ratio`、`total_mv`、`circ_mv`。
- Produces: `DataFetcherManager.get_fundamental_context(stock_code, budget_seconds=None, *, realtime_quote=None) -> Dict[str, Any]`。

- [ ] **Step 1: 写入成功行情复用的失败测试**

```python
def test_fundamental_context_reuses_provided_realtime_quote(self) -> None:
    manager = DataFetcherManager(fetchers=[])
    cfg = SimpleNamespace(
        enable_fundamental_pipeline=True,
        fundamental_cache_ttl_seconds=0,
        fundamental_stage_timeout_seconds=1.5,
        fundamental_fetch_timeout_seconds=0.8,
        fundamental_retry_max=1,
    )
    quote = SimpleNamespace(
        pe_ratio=12.3,
        pb_ratio=2.1,
        total_mv=1.0e11,
        circ_mv=7.0e10,
        source=SimpleNamespace(value="tencent"),
    )
    bundle = {
        "status": "not_supported",
        "growth": {},
        "earnings": {},
        "institution": {},
        "source_chain": [],
        "errors": [],
    }
    with patch("src.config.get_config", return_value=cfg), \
            patch.object(manager, "get_realtime_quote") as fetch_quote, \
            patch(
                "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle",
                return_value=bundle,
            ), \
            patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
            patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
            patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
        ctx = manager.get_fundamental_context(
            "600519",
            budget_seconds=1.5,
            realtime_quote=quote,
        )

    fetch_quote.assert_not_called()
    self.assertEqual(ctx["valuation"]["data"]["pe_ratio"], 12.3)
    self.assertEqual(ctx["valuation"]["data"]["pb_ratio"], 2.1)
    self.assertEqual(ctx["valuation"]["data"]["total_mv"], 1.0e11)
    self.assertEqual(ctx["valuation"]["data"]["circ_mv"], 7.0e10)
```

- [ ] **Step 2: 运行测试并确认按预期失败**

```bash
python -m pytest tests/test_fundamental_context.py::TestFundamentalContext::test_fundamental_context_reuses_provided_realtime_quote -q
```

Expected: FAIL，错误为 `get_fundamental_context()` 不接受 `realtime_quote` 关键字参数。

- [ ] **Step 3: 添加可选参数和复用分支**

```python
def get_fundamental_context(
    self,
    stock_code: str,
    budget_seconds: Optional[float] = None,
    *,
    realtime_quote: Optional[Any] = None,
) -> Dict[str, Any]:
```

```python
valuation_timeout = min(fetch_timeout, remaining_seconds)
if realtime_quote is not None:
    # 同一报告复用同一快照，既减少重复网络请求，也避免估值与技术面价格口径漂移。
    quote_payload, valuation_err, valuation_ms = realtime_quote, None, 0
    logger.info("[基本面] %s 估值复用本轮实时行情", stock_code)
elif valuation_timeout > 0:
    quote_payload, valuation_err, valuation_ms = self._run_with_retry(
        lambda: self.get_realtime_quote(stock_code),
        valuation_timeout,
        "fundamental_valuation",
    )
    _consume_budget(valuation_ms)
else:
    quote_payload, valuation_err, valuation_ms = None, "fundamental stage timeout", 0
```

- [ ] **Step 4: 运行复用和原有重试测试**

```bash
python -m pytest \
  tests/test_fundamental_context.py::TestFundamentalContext::test_fundamental_context_reuses_provided_realtime_quote \
  tests/test_fundamental_context.py::TestFundamentalContext::test_fundamental_context_aggregates_blocks -q
```

Expected: 两项 PASS；第二项继续证明未传行情时会调用原实时入口。

- [ ] **Step 5: 提交 Manager 复用能力**

```bash
git add tests/test_fundamental_context.py data_provider/base.py
git commit -m "perf: reuse realtime quote for valuation"
```

---

### Task 3: Pipeline 传递请求级行情

**Files:**
- Modify: `tests/test_realtime_quote_fallback_logging.py:193-208`
- Modify: `src/core/pipeline.py:521-533`

**Interfaces:**
- Consumes: Task 2 新增的 `realtime_quote` 关键字参数。
- Produces: `StockAnalysisPipeline.analyze_stock()` 在基本面阶段传递 Step 1 已取得的同一对象。

- [ ] **Step 1: 写入 Pipeline 传递的失败测试**

```python
def test_pipeline_passes_primary_quote_to_fundamental_context():
    quote = _make_quote(source=RealtimeSource.TENCENT)
    pipeline = _make_pipeline(enable_realtime_quote=True, realtime_quote=quote)

    pipeline.analyze_stock("600519", ReportType.SIMPLE, "q-reuse")

    pipeline.fetcher_manager.get_fundamental_context.assert_called_once_with(
        "600519",
        budget_seconds=1.5,
        realtime_quote=quote,
    )
```

- [ ] **Step 2: 运行测试并确认按预期失败**

```bash
python -m pytest tests/test_realtime_quote_fallback_logging.py::test_pipeline_passes_primary_quote_to_fundamental_context -q
```

Expected: FAIL；实际调用缺少 `realtime_quote=quote`。

- [ ] **Step 3: 添加最小关键字传递**

```python
fundamental_context = self.fetcher_manager.get_fundamental_context(
    code,
    budget_seconds=getattr(
        self.config,
        "fundamental_stage_timeout_seconds",
        FUNDAMENTAL_STAGE_TIMEOUT_SECONDS_DEFAULT,
    ),
    realtime_quote=realtime_quote,
)
```

- [ ] **Step 4: 运行 Pipeline 实时行情回归测试**

```bash
python -m pytest tests/test_realtime_quote_fallback_logging.py -q
```

Expected: PASS，零失败；实时行情成功、失败和禁用日志语义保持不变。

- [ ] **Step 5: 提交 Pipeline 传递行为**

```bash
git add tests/test_realtime_quote_fallback_logging.py src/core/pipeline.py
git commit -m "perf: propagate realtime quote to fundamentals"
```

---

### Task 4: 变更记录与本地验证

**Files:**
- Modify: `docs/CHANGELOG.md:10-20`
- Verify: `.github/workflows/00-daily-analysis.yml`
- Verify: `data_provider/base.py`
- Verify: `src/core/pipeline.py`

**Interfaces:**
- Consumes: Tasks 1-3 的最终行为。
- Produces: 变更说明和完整本地验证证据。

- [ ] **Step 1: 在 Unreleased 扁平列表追加变更说明**

```markdown
- [改进] 每日分析手动触发跳过随机延迟，并在 A 股单次分析内复用实时行情完成基本面估值，减少重复腾讯请求；定时触发和首次失败重试保持不变。
```

- [ ] **Step 2: 运行定向测试**

```bash
python -m pytest \
  tests/test_daily_analysis_workflow_llm_env.py \
  tests/test_fundamental_context.py \
  tests/test_realtime_quote_fallback_logging.py -q
```

Expected: PASS，零失败。

- [ ] **Step 3: 运行语法与全量后端门禁**

```bash
python -m py_compile data_provider/base.py src/core/pipeline.py
./scripts/ci_gate.sh
```

Expected: 两个命令退出码均为 0。若全量门禁存在与本次无关的基线失败，保留完整失败项并单独报告。

- [ ] **Step 4: 检查差异并提交文档**

```bash
git diff --check
git diff --stat main...HEAD
git status --short
git add docs/CHANGELOG.md
git commit -m "docs: record phase two action speedups"
```

- [ ] **Step 5: 推送实现提交**

```bash
git push origin codex/phase2-action-speed
```

Expected: 远端分支 SHA 与本地 `HEAD` 一致。

---

### Task 5: CI 与四股 Action 实测

**Files:**
- Verify remote: GitHub Actions on `codex/phase2-action-speed`
- Modify audit only: `/Users/bytedance/xxx/touziagent/data/action_runs.csv`

**Interfaces:**
- Consumes: 已推送分支、Repository Variable `STOCK_LIST=000777,002077,002270,002793`。
- Produces: CI 结论、四股性能日志、Artifact 和 17 列审计记录。

- [ ] **Step 1: 验证远端股票池**

```bash
test "$(gh variable get STOCK_LIST -R lihe/daily_stock_analysis)" = "000777,002077,002270,002793"
```

Expected: 退出码为 0。若变量已被其他任务修改，停止并报告，不覆盖外部变更。

- [ ] **Step 2: 推送后等待分支 CI**

```bash
gh run list -R lihe/daily_stock_analysis \
  --branch codex/phase2-action-speed \
  --limit 10 \
  --json databaseId,workflowName,status,conclusion,url
```

Expected: 分支最新阻断型 CI 完成且成功。

- [ ] **Step 3: 手动触发并捕获四股运行**

```bash
before_id="$(gh run list -R lihe/daily_stock_analysis --workflow 00-daily-analysis.yml --limit 1 --json databaseId --jq '.[0].databaseId')"
gh workflow run 00-daily-analysis.yml \
  -R lihe/daily_stock_analysis \
  --ref codex/phase2-action-speed \
  -f mode=stocks-only \
  -f force_run=true
run_id="$(gh run list -R lihe/daily_stock_analysis --workflow 00-daily-analysis.yml --event workflow_dispatch --branch codex/phase2-action-speed --limit 1 --json databaseId --jq '.[0].databaseId')"
test -n "$run_id"
test "$run_id" != "$before_id"
```

Expected: 捕获到目标分支的新 `workflow_dispatch` run。

- [ ] **Step 4: 等待并验证步骤状态**

```bash
gh run watch "$run_id" -R lihe/daily_stock_analysis --exit-status
gh run view "$run_id" -R lihe/daily_stock_analysis --json status,conclusion,jobs,url
```

Expected: run 为 `completed/success`；步骤 `随机延迟（避免固定时间访问）` 的 conclusion 为 `skipped`。

- [ ] **Step 5: 验证完整日志**

```bash
log_path="/private/tmp/dsa-phase2-${run_id}.log"
gh run view "$run_id" -R lihe/daily_stock_analysis --log > "$log_path"
```

逐股检查 `000777`、`002077`、`002270`、`002793`：

- 每只有成功行情的股票各有一次 `[基本面] 股票代码 估值复用本轮实时行情`。
- 同一股票不再出现第二次腾讯财经实时接口调用。
- 汇总为成功 4、失败 0；同时记录核心分析耗时、Workflow 总耗时、429、超时和 fallback。

- [ ] **Step 6: 下载 Artifact**

```bash
artifact_name="$(gh api "repos/lihe/daily_stock_analysis/actions/runs/${run_id}/artifacts" --jq '.artifacts[0].name')"
artifact_dir="/private/tmp/dsa-phase2-${run_id}"
test -n "$artifact_name"
mkdir -p "$artifact_dir"
gh run download "$run_id" -R lihe/daily_stock_analysis -n "$artifact_name" -D "$artifact_dir"
```

Expected: Artifact 中报告包含四只股票代码。

- [ ] **Step 7: 追加盘后审计记录**

使用 `apply_patch` 只向 `/Users/bytedance/xxx/touziagent/data/action_runs.csv` 追加一行实际运行数据，并设置：

```text
mode=stocks-only
requested_stock_count=4
used_for_same_day_decision=no
failed_reason=after_market_observation_only
```

随后用 `csv.DictReader` 验证列数、run id 唯一性、股票池、成功/失败计数和 Artifact URL。

- [ ] **Step 8: 最终一致性验证**

```bash
test -z "$(git status --porcelain)"
local_sha="$(git rev-parse HEAD)"
remote_sha="$(git ls-remote origin refs/heads/codex/phase2-action-speed | awk '{print $1}')"
test "$local_sha" = "$remote_sha"
gh run view "$run_id" -R lihe/daily_stock_analysis --json status,conclusion,url
```

Expected: 实现仓库工作区干净，本地与远端分支 SHA 一致，四股运行为 `success`；否则按实际状态报告，不声明阶段完成。
