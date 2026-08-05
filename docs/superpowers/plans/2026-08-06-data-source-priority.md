# Data Source Priority Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Use Tushare first for structured A-share financial and capital-flow data while keeping Tencent/Sina/Eastmoney as the default realtime chain and AkShare/Eastmoney as a bounded fail-open fallback.

**Architecture:** Add a focused `TushareFundamentalAdapter` that converts standard Tushare Pro DataFrames into the repository's existing fundamental contracts. `TushareFetcher` owns the adapter and a shared four-slot API semaphore. `DataFetcherManager` splits each capability budget between Tushare and AkShare, then recursively fills only explicitly missing values. Valuation, ETF/offshore routing, and dragon-tiger behavior stay unchanged.

**Tech Stack:** Python 3.11, pandas, standard-library bounded threads/semaphores, pytest, GitHub Actions YAML

## Global Constraints

- Default realtime priority is exactly `tencent,akshare_sina,efinance,akshare_em`; Token presence never changes it.
- Explicit truthy `REALTIME_SOURCE_PRIORITY`, including whitespace-only text, preserves existing verbatim semantics; empty string falls back to default.
- Tushare is preferred only for CN non-ETF fundamental bundle and capital-flow capabilities. Dragon tiger remains AkShare-only in this phase.
- `valuation` continues to reuse the current realtime quote.
- Missing means only absent key, `None`, `NaN`, blank string or empty container. `0`, `False` and valid dates are authoritative.
- Dictionaries merge recursively; a preferred nonempty list wins as a whole, otherwise the fallback list wins as a whole.
- All Tushare calls pass through `_call_api_with_rate_limit()`; its counter is locked and all calls share a four-slot semaphore.
- Each capability establishes an absolute monotonic deadline. The preferred call is bounded to `min(1.8s, 60% of capability budget)`; fallback receives `max(0, deadline - monotonic_now)`. Forty percent is a target reserve, never a reason to exceed the deadline.
- ETF, offshore, credentials, global fetcher priority, cache policy and public context schema remain unchanged.

---

### Task 1: Separate realtime defaults from Tushare historical priority

**Files:**
- Modify: `tests/test_config_env_compat.py`
- Modify: `tests/test_daily_analysis_workflow_tushare_env.py`
- Modify: `src/config.py`
- Modify: `.env.example`
- Modify: `.github/workflows/00-daily-analysis.yml`

**Interface:** `Config._resolve_realtime_source_priority() -> str`

- [x] **Step 1: Write failing tests**

Add real-`Config` tests for Token without override, explicit custom value, empty string, and whitespace-only string. Add a YAML assertion for the exact Action default expression. The Token-only test must be the observed RED; the existing workflow literal may already pass at baseline.

- [x] **Step 2: Verify RED**

Run `.venv/bin/pytest -q tests/test_config_env_compat.py tests/test_daily_analysis_workflow_tushare_env.py` and record the failing Token-only assertion.

- [x] **Step 3: Implement the minimal resolver change**

Remove automatic Tushare injection. Keep the truthy explicit branch and default literal. Update `.env.example` and workflow comments to state that Tushare realtime is opt-in while structured data remains preferred.

- [x] **Step 4: Verify GREEN and regressions**

Run `.venv/bin/pytest -q tests/test_config_env_compat.py tests/test_daily_analysis_workflow_tushare_env.py tests/test_realtime_quote_fallback_logging.py`.

- [x] **Step 5: Commit**

Commit only Task 1 files with subject `fix: keep Tushare out of realtime defaults`.

---

### Task 2: Add the bounded Tushare fundamental adapter

**Files:**
- Create: `data_provider/tushare_fundamental_adapter.py`
- Create: `tests/test_tushare_fundamental_adapter.py`
- Modify: `data_provider/tushare_fetcher.py`
- Modify: `tests/test_tushare_fetcher_followups.py`

**Interfaces:**
- `TushareFundamentalAdapter.get_fundamental_bundle(stock_code, timeout_seconds) -> dict`
- `TushareFundamentalAdapter.get_capital_flow(stock_code, timeout_seconds, top_n=5) -> dict`
- `TushareFetcher.get_fundamental_bundle(stock_code, timeout_seconds) -> dict`
- `TushareFetcher.get_capital_flow(stock_code, timeout_seconds, top_n=5) -> dict`

- [x] **Step 1: Write failing adapter tests**

Use literal DataFrames and a fake callback to cover:

- Callback routing for every endpoint: `fina_indicator`, `income`, `cashflow`, `forecast`, `express`, `dividend`, `top10_holders`, `moneyflow`, `moneyflow_ind_ths`.
- Visible consolidated-report selection: `report_type=1`, `f_ann_date` then `ann_date`, latest visible revision, and same-`end_date` matching.
- Cumulative growth priority (`tr_yoy`/`or_yoy`, `netprofit_yoy`) with single-quarter fallback and basis metadata.
- Financial report values and metadata: CNY, yuan, announcement/report dates; missing industry fields remain `None`.
- `forecast`, `express.perf_summary`, implemented 365-day `cash_div_tax` TTM, including a regression proving `cash_div` and `cash_div_tax` are not swapped.
- `top10_holders` snapshot fields/list without manufactured change fields.
- Last ten completed trade dates, latest/5d/10d `net_mf_amount` under true field names in `万元`, and whole-list sector rankings in `亿元`. Tushare leaves the existing “main-force” aliases absent for AkShare to fill.
- Per-endpoint sanitized errors and partial success.
- Adapter timeout returns completed partial results, cancels pending work and does not wait for running futures.

- [x] **Step 2: Verify RED**

Run `.venv/bin/pytest -q tests/test_tushare_fundamental_adapter.py`; expected import/attribute failures.

- [x] **Step 3: Implement normalization and bounded calls**

Create the focused adapter. Financial calls use at most four local workers, `wait(..., timeout=...)`, pending-future cancellation and `shutdown(wait=False, cancel_futures=True)`. Never synthesize absent values as zero. `source_chain` identifies only attempted Tushare endpoints and errors contain only `api_name:ExceptionType`.

In `TushareFetcher`:

- instantiate one shared `BoundedSemaphore(4)` and a rate-limit `Lock`;
- make every `_call_api_with_rate_limit()` acquire a slot with a bounded wait and release it in `finally`;
- lock counter reset/check/increment;
- delegate the two capability methods to the adapter with the caller timeout;
- keep daily/chip/realtime signatures and behavior unchanged.

- [x] **Step 4: Verify GREEN, concurrency and fetcher contracts**

Run `.venv/bin/pytest -q tests/test_tushare_fundamental_adapter.py tests/test_tushare_fetcher_followups.py tests/test_tushare_fetcher_http_client.py tests/test_fundamental_adapter.py`.

Tests must observe peak shared API concurrency `<=4`, counter increments without loss, and every adapter endpoint passing through the fetcher callback.

- [x] **Step 5: Commit**

Commit only Task 2 files with subject `feat: add bounded Tushare fundamental adapter`.

---

### Task 3: Orchestrate Tushare-first bundle and capital-flow fallback

**Files:**
- Modify: `data_provider/base.py`
- Modify: `tests/test_fundamental_context.py`
- Modify: `tests/test_data_tools_get_capital_flow.py`

**Interfaces:**
- Preferred provider lookup: configured/available `TushareFetcher` only.
- Unchanged public methods: `get_fundamental_context()` and `get_capital_flow_context()`.

- [x] **Step 1: Write failing manager tests**

Cover:

- No Token/no Tushare fetcher: AkShare bundle and capital flow are still invoked; dragon tiger remains unchanged.
- Complete Tushare bundle suppresses AkShare; empty/error triggers AkShare.
- Partial bundle recursively fills missing scalar/dict values without overwriting `0`, `False`, dates or nonempty lists; empty lists may be replaced.
- Tushare top-ten snapshot survives while missing change scalars can be filled by AkShare.
- Capital-flow stock and sector substructures decide fallback independently; a Tushare nonempty sector list wins wholesale and is never merged with a different unit.
- With a three-second capability budget, preferred timeout is at most 1.8 seconds and targets a 1.2-second fallback reserve. A controlled monotonic clock proves fallback receives only the positive deadline remainder, scheduling overhead cannot extend the deadline, early preferred completion yields more remainder, and zero remainder skips calls and fails open.
- ETF/offshore routes skip Tushare; supplied realtime quote remains the valuation input.

- [x] **Step 2: Verify RED**

Run `.venv/bin/pytest -q tests/test_fundamental_context.py tests/test_data_tools_get_capital_flow.py` and record failures showing direct AkShare routing.

- [x] **Step 3: Implement ordered orchestration**

Add small private helpers to:

1. find an available `TushareFetcher` without changing global priority;
2. create an absolute monotonic deadline, allocate preferred budget as `min(1.8, total * 0.6)`, and calculate every fallback timeout as the positive deadline remainder;
3. decide whether core bundle or either capital-flow substructure needs fallback;
4. recursively fill only the defined missing values and concatenate metadata from attempted providers.

Call the Tushare capability through the existing timeout wrapper using its preferred budget, passing that same timeout into the adapter. If fallback is needed, invoke AkShare with actual remaining capability budget. Running timed-out Tushare network calls remain bounded by the fetcher's global semaphore and cannot consume AkShare slots. Do not modify `get_dragon_tiger_context()`.

- [x] **Step 4: Verify GREEN and related regressions**

Run:

```bash
.venv/bin/pytest -q \
  tests/test_fundamental_context.py \
  tests/test_fundamental_adapter.py \
  tests/test_tushare_fundamental_adapter.py \
  tests/test_data_tools_get_capital_flow.py \
  tests/test_realtime_quote_fallback_logging.py \
  tests/test_fetcher_source_optimization.py
```

- [x] **Step 5: Commit**

Commit only Task 3 files with subject `feat: prefer Tushare fundamentals with fallback`.

---

### Task 4: Correct standard Tushare field and calendar contracts

**Files:**
- Modify: `data_provider/tushare_fundamental_adapter.py`
- Modify: `data_provider/tushare_fetcher.py`
- Modify: `tests/test_tushare_fundamental_adapter.py`
- Modify: `tests/test_tushare_fetcher_followups.py`

- [x] **Step 1: Write failing official-shape tests**

Cover standard `fina_indicator` without `report_type`, consolidated income/cashflow anchoring, exact growth priority, official `moneyflow_ind_ths.net_buy_amount` in 亿元 without conversion, 19:00 completed-trade-date selection, `net_flow_kind`, naked/qualified `92xxxx` BJ codes, top-ten ratio total, and cancelled pending futures absent from errors/source metadata.

- [x] **Step 2: Implement the minimal corrections**

- Determine the latest visible consolidated `end_date` from `income`, otherwise `cashflow`; match `fina_indicator` to that period without requiring a nonexistent report type.
- Use only `tr_yoy -> or_yoy -> q_gr_yoy` and `netprofit_yoy -> q_profit_yoy` priorities.
- Inject the fetcher's completed-trade-date resolver. At 19:00 Shanghai time include the current open day; before then use the previous open day. Query the last ten completed open dates and aggregate only returned rows in that set.
- Read industry `net_buy_amount` directly as 亿元. Keep stock `net_mf_amount*` in 万元 and add `net_flow_kind=net_mf_amount`.
- Correct BJ conversion, add `top10_total_hold_ratio`, and record timeout only when pending work could not be cancelled because it had actually started.

- [x] **Step 3: Verify and commit**

Run Task 2's 62-test controller suite plus the new tests, `py_compile`, focused flake8 and `git diff --check`. Commit subject: `fix: align Tushare standard field contracts`.

---

### Task 5: Add deterministic workflow stock input

**Files:**
- Modify: `.github/workflows/00-daily-analysis.yml`
- Modify: `tests/test_daily_analysis_workflow_tushare_env.py`

- [x] **Step 1: Write failing YAML tests**

Assert `workflow_dispatch.inputs.stock_codes` exists as an optional string and the runtime stock-list expression gives a nonempty manual value priority over `vars.STOCK_LIST_CONFIG`, while scheduled events retain the existing variable/default behavior.

- [x] **Step 2: Implement and verify**

Add the input and wire it only into stock-list selection. Preserve the existing `github.event_name == 'schedule'` random-delay condition. Run the workflow/config/realtime tests from Task 1 and `git diff --check`.

- [x] **Step 3: Commit**

Commit subject: `feat: accept stock codes in manual workflow`.

---

### Task 6: Emit sanitized per-stock source evidence

**Files:**
- Modify: `data_provider/base.py`
- Modify: `tests/test_fundamental_context.py`

- [x] **Step 1: Write failing evidence-log tests**

Use captured logs to require one machine-readable JSON line per CN stock after context assembly. It must include stock code, overall/block status, coverage, actual provider/endpoint names and exception types only. It must not contain a Token, endpoint URL, raw response or complete arbitrary exception message. Cover both fresh assembly and cache-hit return.

- [x] **Step 2: Implement a small evidence serializer**

Build evidence only from the normalized context. Derive provider/endpoint names from `source_chain`, reduce errors to safe exception/status types, and emit a stable single-line `[DataSourceEvidence]` log. Do not change returned context, cache keys, status calculation or provider routing.

- [x] **Step 3: Verify and commit**

Run Task 3's 91-test controller suite plus new logging tests, `py_compile` and `git diff --check`. Commit subject: `feat: log fundamental source evidence`.

---

### Task 7: Make capability lock acquisition deadline-aware

**Files:**
- Modify: `data_provider/base.py`
- Modify: `tests/test_fundamental_context.py`
- Modify: `tests/test_data_tools_get_capital_flow.py`

- [x] **Step 1: Write failing lock-contention tests**

For both bundle and capital flow, hold the preferred fetcher's manager-owned lock beyond a short preferred deadline. Assert the ordered call returns without recording the provider, the real fetcher method is never called even after lock release, and fallback may use only the positive total-deadline remainder. Also cover a lock acquired before deadline: recompute the remaining timeout after acquisition instead of passing the stale pre-lock value.

- [x] **Step 2: Implement deadline-aware capability locking**

Keep `_call_fetcher_method()` unchanged for unrelated call sites. Add a narrowly scoped capability-call helper that acquires the same per-fetcher `RLock` with the current positive deadline remainder, rechecks the deadline after acquisition, sets `attempted` immediately before the real method call, and injects the recomputed remaining timeout only for Tushare capability methods. A lock timeout is a scheduling error, not a provider attempt. Apply the helper to preferred and fallback bundle/capital-flow attempts.

- [x] **Step 3: Verify and commit**

Run Task 6's controller suite plus the new contention tests, `py_compile` and `git diff --check`. Commit subject: `fix: honor deadlines while waiting for fetcher locks`.

---

### Task 8: Acceptance and live Action validation ✅

**Files:**
- Modify: `docs/dev-loop-runs/2026-08-06-data-source-priority/03-implementation-log.md`
- Modify: `docs/dev-loop-runs/2026-08-06-data-source-priority/04-acceptance-report.md`
- Create: `docs/dev-loop-runs/2026-08-06-data-source-priority/05-pr-summary.html`

**Authorization:** The user previously authorized commits, push, PR updates and manual Action validation for `600797,002315` on this branch.

- [x] **Step 1: Run combined offline verification**

Run the union of Task 1-3 suites plus `tests/test_tushare_fetcher_http_client.py`; record test count, warnings and current branch/commit.

- [x] **Step 2: Perform final broad review**

Generate a review package from `e42c9e7..HEAD`. Require no blocker/important findings before push. The first broad review found the Task 4-6 contracts above; re-review only after their task-scoped gates pass.

- [x] **Step 3: Push and dispatch manual Action**

Push the existing PR branch. Dispatch `.github/workflows/00-daily-analysis.yml` with `stock_codes=600797,002315`; `workflow_dispatch` must not execute random delay.

- [x] **Step 4: Read back three independent runtime gates**

1. workflow/job conclusion;
2. per-stock data-source decision logs (Tencent realtime; Tushare daily/chip and structured attempts; explicit fallback where needed);
3. generated report/artifact data completeness.

Endpoint permission/availability may validly produce an explicit AkShare fallback; record it as external runtime risk, not implementation failure. A green workflow alone is not sufficient evidence for source selection or completeness.

- [x] **Step 5: Generate and commit acceptance artifacts**

Write the implementation log, acceptance verdict, residual risks and a self-contained HTML PR summary. Commit these docs only after their stated evidence is read back.
