# Phase 11.6 Usage KPI views／dashboard

11.6 提供一個可由合成事件重跑的 KPI contract，以及只讀的 BigQuery view plan。實作使用
`usage_kpis.py` 將 11.5 的 canonical deduplicated events 聚合成
`usage_kpi_dashboard`；`scripts/plan_usage_kpis.py` 只產生 SQL 與資源 manifest，不會登入
或修改 GCP。真實 view、dashboard reader IAM、ledger table 與 refresh 尚未在本分支套用。

## 資料來源與邊界

- 只讀 `analytics_request_completed` canonical events，去重鍵是
  `(schema_version, interaction_id, event_name)`。摘要附件、完整 request、SQL、query
  result、tenant 名稱與 synthetic routing probe 不進 KPI。
- `analytics_requests` 以 `request_kind=analytics` 的 canonical event 計算，包含成功、失敗、
  denied、unsupported 與 needs-clarification；一個多 metric request 仍只算一次。
- `tool_calls` 只計可辨識的 MCP known tool canonical events，REST 呼叫另列於 analytics
  request，不補造 MCP tried。
- `inferred_sessions` 只對 verified pseudonymous user 計算；同一 user 的 canonical
  events 相鄰不超過 30 分鐘視為同一活動 session。它不是 host conversation，也不推估未送到
  服務的對話。
- user、tenant 與 metrics 只使用已驗證的 wire 欄位。缺失身分不會合併成匿名 user，缺失的
  eligible／authorized 名單不會從事件反推分母。

## KPI contract

`telemetry/kpi.schema.v1.json` 固定最外層 `schema_version=1.0` 與
`report_type=usage_kpi_dashboard`；`telemetry/weekly-summary.schema.v1.json` 固定每週摘要的
`report_type=usage_weekly_summary`。期間以 `Asia/Taipei`、週一開週；輸出同時保留資料延遲、
identity coverage、unknown intent／host、status／resolution 分布、latency p50／p95、
需求分類與 history coverage。

`usage` 區分三種計數單位：`tool_calls`、`analytics_requests` 與 `inferred_sessions`。
DAU／WAU／MAU 以成功 analytics 的 verified users 計算，並保留各日／週／月的 series；active
days per user、requests per user 與 repeat usage rate 以不同 local calendar days 計算，同日
重試不算回訪。

`demand` 依 request kind 分開保存 goal／subject／intent source／period／comparison 分布。metrics 與
dimensions 每個 interaction 至多計一次，只接受 catalog／traffic report 已驗證的 ID。明確
日期即 `explicit_range`；不能從上週巧合、固定 previous report 或其他 tool call 猜相對意圖。
unsupported reason 另分 capability preflight 與 analytics，unknown 與 null coverage 都保留，
不用 label、SQL alias 或摘要文字代替。Latency／error categories 也可按 tool／transport
拆開檢視。

離線輸入 mapping 會重新套用 `UsageEvent` 的 strict wire contract；不合法的 enum、ID、
tenant 型別、時間或 array 會被丟棄並留在 quality coverage，不會進入任一 KPI。`query_ga4`
只接受 published semantic catalog IDs，`traffic_summary` 只接受 report contract IDs。

## Activation ledger 與 history coverage

`ActivationLedger` 只保存三個欄位：`user_id`、最早的 `first_success_at` 與
`measurement_version`。背景 pipeline 從去重、identity verified、`request_kind=analytics`、
`status=success` 的事件冪等更新；重送不增加 user，晚到的更早成功事件可以把首次時間往前修正。
附件與 capability preflight 永遠不會建立 activation。更新失敗不得回傳到 GA4 analytics request。

ledger 需明確提供 measurement start、保存政策核准、identity key 連續性、pipeline watermark
與事件歷史範圍。缺少任一項時，view 仍可回傳可觀測期間的絕對數，但
`activation.status=degraded`，不發布不受資料支持的累積 activation、首次 cohort 或 W4
retention。Ledger 刪除、到期、pipeline gap 或 key rotation 造成的斷裂都列入
`data_freshness.history.reasons`；不能把重新出現的 user 當成新人，也不能把缺失 follow-up
填成零。Identity continuity 必須由 pipeline 對 measurement version 明確提供正向證據；
欄位缺失、null 或尚未驗證都視為未知並 fail closed，不因建立新 ledger 自動推定連續。
History attestation 的 `measurement_version` 必須與 ledger 相同；缺失或版本不符時不得沿用
其他量測版本的 continuity／coverage 證據。

W4 retention 以首次 activation 所在的週一至週日為 W0，W4 是 W0 後第 28–34 天。只有已完整
觀察至 W4 結束、ledger 與 event history 均連續的 cohort 才進分母；未成熟 cohort 回傳
`not_mature`，歷史不足回傳 `insufficient_history`。Activation 只要求事件歷史覆蓋至報告期末；
W4 則逐 cohort 以已證明的 `known_event_end` 判斷 `w0 + 34` 是否完整，不能用 W4 的額外
34 天需求連帶隱藏仍可可靠發布的 activation。

背景工作應透過 `update_activation_ledger` 呼叫 ledger。來源故障或批次格式錯誤只回傳固定
`ledger_update_failed` 並標示 pipeline gap，不把 exception 傳回原本的 GA4 request。Canonical
batch 只要含無法驗證的 row 就不做部分更新；synthetic probe 與 duplicate 仍屬可預期排除，
不會被誤判成來源缺口。

週報 SQL 的輸入範圍上限為 146 個含首尾日期；加上 W4 follow-up 後仍落在 canonical 180
天來源函式的 bounded range。metadata 空表、measurement version 不一致、保存政策未核准、
identity／pipeline 不連續或 event bounds 不足時，SQL 仍回傳可觀測期間計數並把 activation、
cumulative 與 W4 欄位降級為 null／degraded。

## KPI 發布契約與共同驗收

`HistoryCoverage` 是證據驗證邊界；mapping 與直接物件經相同初始化驗證，
`with_ledger` 是唯一合併入口。`ActivationLedger.history_coverage` 與 legacy
`history_complete` 參數也經過此邊界。正向證明只接受 literal `True`，不接受 `1`、
`"true"` 或 `"false"`；deletion 必須是 boolean。無效型別的 reason 在合併後仍保留，
已知 identity break、pipeline gap、deletion 不能被正向證據覆蓋。Identity 未知時，
同 measurement version 的另一個有效來源可以明確提供證據；無效輸入則不能被修復為可信。
Measurement version 必須相符，已提供的 measurement 起訖也不能與 ledger 矛盾。

| KPI／情境 | 必要證據與時間邊界 | 不足時 |
| --- | --- | --- |
| New／cumulative activation | Ledger policy、同版本 continuity 與 pipeline 證據；歷史從 measurement 起點覆蓋至報告期末 | null／degraded；不額外等待 W4 |
| W4 retention | 上述基礎證據；逐 cohort 覆蓋完整 W4，且仍在 measurement 保存窗內 | 只納入完整 cohort；未成熟或缺漏不當作零留存 |
| W4 日界 | 台北時間半開區間 `[W0+28 天 00:00, W0+35 天 00:00)` | W4 週日任何時刻都未成熟；下一週一零時才可能成熟 |
| Canonical batch 拒收 | 整批驗證成功才能更新 ledger | 不部分更新、標記 gap；不將錯誤傳回原 analytics request |
| Duplicate／synthetic probe | 正常排除，不代表 canonical 資料遺失 | 不增加人數，也不製造 gap |

`event_history_end`／SQL `known_event_end` 是**已完整觀察的一天（含該日）**，
不能填入只有部分資料的當日。Python 另受 `as_of` 限制；SQL 以查詢當下的
`CURRENT_DATE('Asia/Taipei')` 防止提前成熟，不更改既有 table-function 參數。

共同案例在 `tests/test_usage_kpi_publication.py`：activation 不等待 follow-up、
W4 週日開始／中午／最後一微秒／週一零時、watermark 落後、部分及全部 cohort 完整，
以及每個 attestation 欄位的 null、truthy／falsey 非布林輸入、跨版本、負面證據與混合批次。

```bash
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python -m unittest tests.test_usage_kpis tests.test_usage_kpi_publication -v
.venv/bin/python -m unittest discover -s tests -v
```

SQL 結果測試從 `render_sql()` 取得完整 weekly SELECT，透過 SQLGlot 轉為 DuckDB，
僅替換合成來源、輸入日期、時鐘與 Monday-week 方言表示；沒有另寫一份 KPI 計算。
Python 與 SQL 使用相同時間決策表、型別合法的負面／缺失證據，核對 activation、
成熟分母與留存率。鬆散型別在 Python 邊界測試，BigQuery metadata 使用 BOOL 欄位；
SQL 依 measurement version 過濾 ledger，Python 另驗證附加 ledger 的版本一致性。
測試依賴缺失會失敗，不靜默略過。這些套件僅供測試，不加入 runtime requirements。

本機方言轉換與結果測試不能取代 BigQuery 原生 dry-run、實際合成資料查詢、IAM、
partition filter、bytes billed 與 pipeline metadata 產生流程的部署前驗收。

## Funnel 與資料充分性

`tried` 是 verified user 至少一次有效 MCP known tool schema call；unsupported／clarification
仍算 tried，auth denial 與 invalid schema 不算。`activated` 是 verified user 至少一次成功
analytics（MCP 或 REST）。`eligible` 需要外部員工資格名單，`authorized` 需要持久 connection
lifecycle 台帳；兩者缺失時 stage 為 `available=false`，conversion rate 為 null，不以 OAuth
allowlist、consent page 或 refresh 推造分母。REST user 可以 activated 而沒有 MCP tried，報表
會標示這不是嚴格階梯 funnel；若 activated 與 tried 不是可比較的階梯集合，或任何前後階段
不是階梯集合，對應 conversion rate 保留為 `null`，並在 `transport_note` 記錄不可比較的原因；
不發布超過 100% 的比例。

發布前需要完成以下檢查：

1. owner 核准 activation ledger 的保存、刪除、user deletion、IAM 與 identity key rotation
   政策；未核准不可發布長期 cumulative activation。
2. dashboard reader 只讀 aggregate KPI views；不得因 GA4 tenant 權限取得 canonical event
   或摘要原始資料。
3. 對合成 fixture 驗證 duplicate／reorder、跨日與週界、W4 未成熟 cohort、30 分鐘 session、
   兩種 transport、unknown identity／intent／host、null tenant ID 與保存期限降級。
4. 以 bounded date range 及 `maximum_bytes_billed` dry-run SQL；不要將 30 天摘要附件 join
   後物化到 180 天 view。

可用的離線計畫：

```bash
.venv/bin/python scripts/plan_usage_kpis.py --output-dir /tmp/ga4-usage-kpi-plan
```

此命令只輸出 `usage-kpi-plan.json` 與三個 table-function SQL 檔案，不建立 dataset、table、
view、sink、IAM 或 dashboard。回復時先停 dashboard refresh，再停 usage sinks；不撤銷原有
OAuth、tenant routing 或 GA4 read 權限。
