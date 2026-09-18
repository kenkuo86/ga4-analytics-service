# GA4 Analytics Service roadmap

## Current status

目前 PoC 已具備可供 Claude Custom Connector 使用的 GA4 唯讀查詢流程：

- 透過 Google OIDC 與 email allowlist 完成 OAuth 登入及 consent。
- 由 tenant registry 解析正式客戶名稱、GA4 project 與 ecommerce profile；使用者不需要知道 `tenant_id`、`project_id` 或 `dataset_id`。
- 可直接列出目前能查詢的客戶。
- 透過 versioned semantic catalog 搜尋並執行核准的 GA4 指標，不接受任意 SQL。
- 提供 `customer_lookup`、`list_available_customers`、`get_ga4_capabilities`、`search_ga4_metrics`、`query_ga4` 與相容用的 `traffic_summary`。
- Cloud Run runtime service account 已具備目前 active tenants 的 dataset-level read access，query jobs 集中由 `ga4-reports-dev` 計費。
- 已有 catalog builder、runtime compiler、OAuth、tenant resolution、跨 tenant dry-run 與部署前後驗證。
- 所有 GA4 data query 已套用共用日期與 BigQuery bytes policy，billing project 另有 daily custom query quota。

目前 Phase 4–10 已完成；Phase 10 已由 PR #13 合併至 remote `main`。其餘工作包含持續
監控成本、權限、tenant registry 品質及 connector 行為。

## Completed foundations

### Foundation 1: authentication and tenant routing

Status: Done

Dependencies: None

已完成：

1. 支援 `cloud-run-iam` 與 `oauth` 兩種 authentication mode。
2. OAuth 模式使用 Google OIDC 驗證登入者，並由本服務簽發 audience-bound MCP token；Google token 不會傳給 BigQuery。
3. OAuth PoC 使用 PKCE、email allowlist、一次性 authorization code 與 refresh-token rotation。
4. 所有資料查詢都由服務端透過 tenant registry 解析 routing，不讓對話內容指定 project 或 dataset。
5. `list_available_customers` 只列出名稱非空白、可唯一解析、狀態為 active 且已設定 project 的客戶。

目前 OAuth state 與 refresh token 仍存放在單一 instance 的 process memory，因此 OAuth 模式暫時維持 Cloud Run `max instances = 1`。多 instance、持久 token、完整撤銷與稽核不在目前 PoC 範圍內。

### Foundation 2: cross-project BigQuery access

Status: Done

Dependencies: Foundation 1

2026-09-02 已完成首次授權 rollout：在已確認存在 `ga4_mar` 的 tenant projects 中，授予 Cloud Run runtime service account dataset-level `roles/bigquery.dataViewer`，而不是 project／folder 層級權限。

已完成的驗收與部署項目：

1. 以 runtime service account 查詢 registry 一次，再對 54 個 active tenants dry-run `total_users`；54/54 通過，沒有實際執行 tenant data query。
2. 以 `BIGQUERY_BILLING_PROJECT` 將 PoC query jobs 明確集中到 `ga4-reports-dev`；`roles/bigquery.jobUser` 只授在計費專案。
3. 加入固定部署腳本：部署前檢查登入、worktree 與測試，部署後恢復 `LATEST=100%`、驗證 `/health` 並檢查 ERROR logs。

仍需營運化，但不阻擋下方產品功能：

1. 建立只讀 IAM audit，定期比對 active `project_id`、`ga4_mar` dataset 與 Data Viewer grant，輸出 drift report。
2. 將新增、停用或變更 tenant 的授權同步做成可重複執行的「先 plan、後人工核准 apply」流程。
3. 管理者身分與 runtime service account 持續分離。

### Foundation 3: semantic layer and basic capability boundary

Status: Done

Dependencies: Foundations 1–2

第一版 semantic layer 已完成：

1. 指標來源會編譯成 Git 版控的 `semantic/catalog.v1.json`。
2. `search_ga4_metrics` 先搜尋已發布定義，`query_ga4` 再執行 catalog 中的固定 SQL template。
3. Runtime 依 `tenant_registry.ec` 自動選擇 ecommerce 或 non-ecommerce profile。
4. 不提供任意 SQL tool；只允許單一 `SELECT`／`WITH` query 及核准的 `ga4_mar` models。
5. 未發布指標回傳 `unsupported_metric`，定義衝突回傳 `metric_definition_conflict`。
6. 已限制每次 semantic query 的 metric 數量、結果筆數、日期範圍及 `maximum_bytes_billed`。
7. 已有 catalog build、SQL safety、profile resolution、MCP schema 與 BigQuery dry-run 測試。

尚未完成的 semantic layer 營運項目：

1. 將 Google Sheet 匯出、catalog build、單元測試與 BigQuery dry-run 串成 CI，只有全部通過才發布 catalog。
2. 為來源表增加 metric ID 唯一性、derived metric dependency、時間維度、model grain、owner 與變更說明等 schema validation。
3. 視實際使用情況，將重複 SQL 拆成可組合的 base metric、dimension 與 filter definition。

## Implementation roadmap

以下各階段依成本與資料安全優先，再逐步改善可信度及使用體驗；目前 Phase 4–10 已完成。

### Phase 4: unified query cost controls

Status: Done

Dependencies: None

2026-09-04 已由 PR #3 完成並部署至 Cloud Run：日期上限為 90 天、每個 GA4 tenant data query job 上限為 2 GB、每個 tool request 合計上限為 10 GB，`ga4-reports-dev` project daily custom query quota 為 47,683 MiB（不超過 50 GB）。第一版不提供每位 OAuth 使用者的個別 daily quota。

#### Goal

防止使用者透過過長期間、多指標或重複查詢造成非預期 BigQuery 費用，並讓所有查詢路徑使用相同限制。

#### Scope

1. 建立共用 `QueryPolicy`，讓 `query_ga4`、`traffic_summary` 與 REST endpoint 使用同一套日期及成本驗證。
2. 將單次日期範圍由目前 semantic query 的 366 天調整成可由環境變數設定的較小上限；初始值在實作前依常用報表期間決定，候選為 31 或 90 天。
3. 驗證日期格式、起訖順序、未來日期與可查資料的最早日期。
4. 對 `traffic_summary` 補上 `maximum_bytes_billed`、query cache、timeout 與 query labels。
5. 區分：
   - 每個 BigQuery job 的 bytes 上限。
   - 同一 tool request 中所有 metric jobs 的合計上限。
   - 計費專案每日總額上限。
6. 在 `ga4-reports-dev` 設定 BigQuery project-level daily custom query quota，作為應用程式以外的成本保險。
7. 若需要終端使用者各自的每日配額，將 OAuth `sub` 傳入查詢 context，並使用持久 storage 記錄使用量；BigQuery 看到的是共用 runtime service account，不能直接區分 Claude 使用者。
8. 超限時回傳可辨識的錯誤，例如 `date_range_too_large`、`query_cost_limit_exceeded` 或 `daily_query_quota_exceeded`，不得全部包成 `data_unavailable`。

#### Acceptance criteria

- 任何 GA4 data query 都不能繞過共用日期及 bytes policy。
- 超過期間或預估 bytes 上限時，不執行 tenant data query。
- 一次要求多個 metrics 時，有 request-level 總成本保護，不只是每個 metric 各自受限。
- 單元測試覆蓋 semantic、traffic summary、REST 與 MCP 路徑。

### Phase 5: capability preflight and explicit AI boundaries

Status: Done

Dependencies: None. Recommended after Phase 4 because both are likely to modify `main.py`, MCP error handling, and query tests.

2026-09-08 已由 PR #5 完成並合併至 `main`：加入中央 capability registry、`get_ga4_capabilities`、本機 preflight、公開 tool／server instructions 同步及 versioned connector behavior eval cases。實際 Claude connector 的 tool choice 與回答措辭仍屬部署後驗收項目。

#### Goal

讓 AI 在執行 BigQuery 前先知道服務能否回答需求；廣告、SEO keyword ranking、CRM 或其他非 GA4 資料需求應直接說明不支援。

#### Scope

1. 建立中央 capability registry，定義支援的資料來源、分析類型、公開 tools、限制與不支援項目。
2. 新增 `get_ga4_capabilities`，或擴充 `search_ga4_metrics`，讓 capability lookup 完全由本機 metadata 完成、不連線 BigQuery。
3. 調整 `query_ga4` 驗證順序：先在本機 catalog 確認 metric 至少存在於一個可發布 profile，再建立 BigQuery client、查詢 tenant registry 或 tenant data。
4. 評估讓 `query_ga4` 必須攜帶 catalog search 產生的 selection token，強制每次資料查詢都先通過 capability resolution。
5. 在 server instructions 與 tool descriptions 補上明確正反例，不將一般知識或推論描述成實際客戶資料。
6. 建立 connector behavior eval cases，至少涵蓋：
   - Google Ads 花費：不呼叫 BigQuery，說明不支援。
   - SEO 關鍵字排名：不呼叫 BigQuery，說明不支援。
   - GA4 自然流量 sessions：先解析 metric，再查詢。
   - 不存在的 metric：在 tenant data query 前拒絕。
   - 超出日期或權限範圍的要求：在查詢前拒絕。

PoC 邊界決策：capability intent resolution 採 deterministic metadata、規則與
versioned eval cases，不以窮舉或正確分類所有自然語言排列為目標。未列入規則的外部來源限定詞、
複合句或新措辭可能被判成 `needs_clarification`，或只解析出其中可支援的 GA4 部分；這是目前
owner 接受的呈現／tool-choice 風險。真正的 server-side 安全邊界仍由 catalog publishability、
tenant routing、唯讀 SQL 與 query policy 負責，不能因 intent resolver 的判斷而執行外部資料查詢、
任意 SQL 或未知 metric。

#### Acceptance criteria

- Versioned eval cases 中明確列出的不支援需求不會產生 tenant registry 或 tenant data query。
- Server-side validation 可以阻止模型略過 capability preflight 後直接執行未知 metric。
- Versioned 對話 eval cases 能分辨「不支援」、「需要釐清」及「可查詢」三種結果。

實作備註：repository 內的 deterministic eval fixture 會驗證三種 resolution、next_action
及 BigQuery 呼叫邊界；實際 Claude connector 的 host model tool choice 與回答措辭仍需在部署後
以相同案例進行對話驗收。

### Phase 6: query provenance and auditability

Status: Done

Dependencies: Phase 4

2026-09-09 已由 PR #7 完成並合併至 `main`：`query_ga4`、`traffic_summary` 與 REST traffic summary 路徑支援 opt-in `include_query`，並回傳實際 parameterized SQL、獨立 parameters、job metadata、catalog version 與失敗時可安全保留的 provenance。完整 project／dataset table path 僅在使用者明確要求 technical routing details 或 query provenance 時顯示。

#### Goal

使用者明確要求時，提供當次實際執行的所有指標 SQL 與 BigQuery job metadata，方便自行查核數字與費用。

#### Scope

1. 在 `query_ga4` 與 `traffic_summary` 加入預設為 `false` 的 `include_query` 參數。
2. `include_query=true` 時，逐一回傳實際送出的 parameterized SQL，而不是由 AI 重建 SQL。
3. 日期與其他值以 query parameters 分開回傳，不插值進 SQL 字串。
4. 每個 query record 回傳對應的 metric、job ID、cache hit、bytes processed、bytes billed 與 catalog version。
5. 查詢失敗時仍保留可安全提供的 query provenance 與結構化錯誤。
6. 平常不主動回傳 SQL 或內部 routing；只有使用者要求技術查核時顯示完整資訊。

#### Acceptance criteria

- 多 metric request 會回傳所有實際執行的 query records，且順序及 metric 對應明確。
- 回傳的 SQL、parameters 與 BigQuery job 相符。
- `include_query=false` 時維持精簡回傳，不增加不必要的 routing 資訊。

### Phase 7: managed customer aliases and candidate search

Status: Done

Dependencies: None. Coordinate with Phases 4–6 because tenant resolution and query orchestration share `main.py` and related tests.

2026-09-09 已由 PR #8 完成並合併至 `main`：加入受管理 aliases、統一名稱 normalization、registry collision validation、候選搜尋與各查詢路徑的 tenant context。未登記的部分名稱即使只有一個候選，也只回傳候選並要求使用者以正式名稱確認，不會直接執行 tenant data query。

#### Goal

讓使用者看到客戶清單後，可以用安全且可管理的簡稱查詢，例如以「東方美」查詢正式名稱為「東方美企業」的客戶，同時避免錯查其他 tenant。

#### Scope

1. 在 tenant registry 增加受管理的 alias 資料；PoC 可先使用 aliases 欄位，正式化後可拆成一列一個 alias 的獨立表。
2. 對正式名稱與 alias 使用相同的 trim、Unicode NFKC 與 casefold normalization。
3. 建立 alias 唯一性驗證；同一個正規化 alias 不得指向多個 tenants。
4. 名稱解析依序採用：
   - 正式名稱完全符合：直接查詢。
   - 已登記 alias 完全符合：解析成正式名稱後查詢。
   - 未登記的部分名稱：只進行候選搜尋。
5. 部分名稱只有一個候選時，回傳正式名稱與 `match_type=partial`，並要求使用者以正式名稱確認後才能執行查詢。
6. 多個候選時列出選項，不得猜測或執行 tenant data query。
7. Tool result 保留 `requested_name`、`resolved_name` 與 `match_type`，方便 AI 清楚說明使用了哪個客戶。

#### Acceptance criteria

- 已登記且唯一的「東方美」可以穩定解析為「東方美企業」。
- 重複 alias 會在 registry validation 階段被阻止，不能進入可查詢狀態。
- 多候選及零候選不會觸發 tenant data query。

#### Historical registry note

2026-08-27 的一次匯出共有 77 筆 tenants，其中 54 筆 active、23 筆 provisioning，另有 29 筆缺少 `tenant_name`。這是歷史快照，不應視為目前即時數量；production alias data rollout 前需要重新盤點 registry，名稱空白的 tenants 仍無法供使用者查詢。

### Phase 8: deterministic traffic summary report contract

Status: Done

Dependencies: Phase 4

2026-09-08 已由 PR #6 完成並合併至 `main`：traffic summary 使用單次掃描產出 headline metrics 與本期／前期 daily series，並回傳 versioned `line_chart` small-multiples presentation contract。實際 host 是否穩定依 contract 呈現仍需在部署後持續驗收。

呈現決策：固定拆成四個 small-multiple 折線圖，每個 metric 一張圖，圖內各有本期與前期兩條 series；不使用難以辨識的單圖八條線。

#### Goal

讓 `traffic_summary` 每次都回傳足以產生相同折線圖的資料與呈現規格，避免同一個 tool 有時顯示表格、有時顯示圖表。

#### Scope

1. 調整 traffic summary SQL，在一次掃描中同時產出：
   - 本期與前期 headline metrics。
   - 每日 sessions、users、new users、returning users。
   - 可對齊比較的前期每日序列。
2. 定義固定且 versioned 的 report schema，例如：
   - `report_type=traffic_summary`
   - `report_schema_version`
   - `headline_metrics`
   - `daily_series`
   - `presentation.type=line_chart`
   - 固定的 x 軸、series、單位及排序。
3. Tool description 要求 host 依 `presentation` contract 呈現，不自行改成其他圖表類型。
4. 控制 series 數量、缺失日期補零規則、時區與前期對齊方式。
5. 先以 structured report contract 驗證 Claude 的呈現穩定度；若 host 仍無法穩定遵守，再由服務端產出固定 HTML／SVG／PNG report。

#### Acceptance criteria

- 相同 tool output 在驗收案例中都使用折線圖，而不是由模型任意選擇表格或圖表。
- 圖表資料直接來自 tool result，不由 AI 推算或補造。
- Headline totals 與 daily series 可由測試驗證一致。

### Phase 9: consent page redesign and capability sync

Status: Done

Dependencies: Phases 5–6

2026-09-09 已由 PR #10 完成並合併至 `main`：OAuth consent page 改為簡潔、低彩度且具 responsive／accessibility 基礎的介面，並由中央 capability registry 產生支援能力、公開 tools、限制與不支援項目。原有 approve、deny、PKCE、redirect flow 及安全 response headers 均由測試覆蓋。

#### Goal

將 OAuth consent page 改成接近 Claude 介面的簡潔、低彩度閱讀風格，並準確顯示目前 connector 能做與不能做的事情。

#### Scope

1. 使用暖白背景、清楚的字級層級、窄版 card、低彩度邊框與一致的允許／拒絕按鈕；不直接複製 Claude 商標或品牌資產。
2. 顯示登入帳號、唯讀 scope、可存取的資料類型、公開能力與明確限制。
3. 至少說明以下能力：
   - 列出及辨識可查詢客戶。
   - 查詢 traffic summary。
   - 搜尋並查詢 semantic catalog 中已發布的 GA4 metrics。
   - 在使用者要求時提供 query provenance。
4. 明確說明不提供廣告、SEO keyword ranking、CRM、任意 BigQuery 或資料修改能力。
5. Consent page、server instructions、README 與 tool inventory 共用 Phase 5 的 capability registry，避免新增 tool 後內容再次過期。
6. 保留目前的 CSP、`Cache-Control: no-store`、Referrer Policy 與 frame protection，並補上基本 responsive 及 accessibility checks。

#### Acceptance criteria

- Consent page 中的能力清單與實際公開 tools／scope 一致。
- 新增或移除公開能力時，有測試提醒同步更新或可直接由 metadata 產生內容。
- OAuth approve、deny、PKCE 與 redirect flow 不因視覺改版而回歸。

### Phase 10: intent-level date-range boundary

Status: Done

Dependencies: Phases 4–5

2026-09-16 已由 PR #13 完成並合併至 `main`：新增 versioned `period_phrase_contract` 與
deterministic `PeriodIntent`／`PeriodSafetyAudit`，以 explicit period 聯集計算
`requested_days`，並將 active `QueryPolicy.max_date_range_days` 同步至 capability
metadata、tool descriptions、server instructions 及 capability preflight。超限、無效或
語意不明的期間會在 tenant registry／BigQuery 前拒絕；同步新增 Phase 10 behavior fixture、
專用測試及 README 說明。實際 Claude Custom Connector 的部署後 tool choice、回答措辭與
不拆分／不重試行為仍依本階段驗收矩陣持續驗證。

Phase 4 已完成的 `QueryPolicy` 會驗證每個 `query_ga4`、`traffic_summary` 或 REST request
收到的日期範圍，但目前無法辨識多個合法 tool calls 是否源自同一個超過 active
`QueryPolicy.max_date_range_days` 的使用者需求。此上限由 `GA4_QUERY_MAX_DAYS` 設定，
目前預設為 90 天；host model 仍可能先將半年需求拆成數個各自未超限的區段，分別查詢後再合併結果。

本階段是 PoC 的 connector 行為契約與部署後對話驗收，不新增跨 tool call 的伺服器端狀態、
query token 或每位使用者累積期間限制。單一 tool call 的日期與成本安全邊界仍由 Phase 4
的 `QueryPolicy` 強制執行；intent-level 限制屬於 host tool-choice 的 best-effort 保證，
不得描述成無法繞過的 server-side security boundary。

#### Goal

當使用者要求分析的完整期間超過 active `QueryPolicy.max_date_range_days` 時，connector 應在查詢客戶資料前直接
說明限制並請使用者縮小期間，不得自行以月份、相鄰日期區段、多次 tool calls 或重試拆分查詢。

#### Scope

1. 建立單一、versioned 的 `period_phrase_contract` 作為相對期間 vocabulary、解析規則、capability metadata、server instructions 與 eval fixtures 的共同來源；不得再由 `_period_qualifier_pattern` 或 tool description 個別維護另一份可辨識詞彙。
   - 每個被辨識的 phrase 必須完整對應到 `resolved`、`needs_clarification` 或 `invalid_period` 其中一種結果；不得先將 period qualifier 從 request 移除，卻沒有定義它的日期語意。
   - 數量 `N` 必須正規化為正整數；支援的阿拉伯數字、中英文數字、單複數、`週／周／星期` 與「半年」alias 都必須由 contract 明列。零、負數、無法解析的數量或不自然組合不得猜測。
   - 新增或移除 period phrase 時，contract inventory test 必須同步提醒 parser、instructions 與 fixtures，避免再次出現「已辨識但未定義」的單位或前綴。
2. 定義 intent-level 的標準化結果，不使用含義不明的單一 `requested_period`：
   - `explicit_periods` 是使用者明確要求，或可由 `period_phrase_contract` deterministic 解析出的所有日期區間；每個區間皆包含起訖日。
   - `requested_days` 是所有 `explicit_periods` 聯集中的不重複 calendar days 數；拆分、相鄰區段或重疊區段都以聯集計算，因此不能用多個較小區段規避限制。grouping grain（例如按月 group）不改變 `requested_days`。
   - `implicit_periods` 是 tool contract 自動加入、但使用者沒有另外指定起訖日期的區間，例如 `traffic_summary` 的等長 previous comparison period；它不計入 intent-level `requested_days`。
   - `traffic_summary` 的 previous period 是固定 report contract；使用者只說「與前期比較」或 `compare with the previous period` 時，它仍是 implicit presentation／report modifier，不會升格為 `explicit_periods`。只有使用者另外提供第二個可解析日期區間時，兩段才都屬於 explicit，並以日期聯集計算 `requested_days`。
   - Phase 10 不新增 request-level `effective_scan_periods` 或 `effective_scan_days` schema。實際掃描行為繼續以每個 metric／query 的 `date_scope`、query parameters 與 provenance 表示，並由 Phase 4 的 earliest-date、bytes、timeout 及 daily quota policy 保護。
   - 同一 `query_ga4` request 混合 `requested_period` 與 `all_available_data` metrics 時，intent boundary 仍只計共同的 `explicit_periods`；每個 metric 保留自己的 `date_scope`，不得合併成一個會遺失 bounded metric 資訊的 request-level scan value。`all_available_data` metric 的成本仍由 Phase 4 bytes policy 保護。
3. intent-level boundary 必須使用 data tools 同一個 active `QueryPolicy.max_date_range_days`，不得在 instructions、metadata 或 eval implementation 寫死目前預設的 90。當 `requested_days` 超過 active limit 時，在 tenant registry 或 BigQuery 前拒絕；回覆 deterministic 計算出的 `requested_days`、active limit，並要求使用者重新選擇期間。
4. `period_phrase_contract` 使用 active policy timezone（預設 `Asia/Taipei`）的 today 作為 anchor，並至少完整定義下列 phrase families；表中的中英文同義詞都必須有 fixture：

   | Window kind | 中文 phrase family | English phrase family | Normative semantics |
   | --- | --- | --- | --- |
   | Single day | 今天、昨天 | `today`、`yesterday` | 對應的單一 calendar day。 |
   | Rolling days | 過去／最近／近 N 天 | `past`／`recent`／有明確 N 的 `last N days` | 包含 today；`start=today-(N-1 days)`、`end=today`。 |
   | Previous days | 前 N 天 | `previous N days`、無明確 N 的 `last day` | 不包含 today；`start=today-N days`、`end=yesterday`。 |
   | Rolling weeks | 過去／最近／近 N 週／周／星期 | `past`／`recent`／有明確 N 的 `last N weeks` | 包含 today 的連續 `7*N` 天，不依 calendar week 切齊。 |
   | Completed weeks | 前 N 週／周／星期、上週 | `previous N weeks`、無明確 N 的 `last week` | ISO week（週一至週日），不包含本週。 |
   | Week to date | 本週、這週 | `this week` | 本週一至 today。 |
   | Rolling months | 過去／最近／近 N 個月、過去／最近／近半年 | `past`／`recent`／有明確 N 的 `last N months` | 將 today 往前移 N 個 calendar months，目標日不存在時 clamp 至月底，再加一天為 start；end 為 today。半年等於 6 個月。 |
   | Completed months | 前 N 個月、上個月 | `previous N months`、無明確 N 的 `last month` | 完整 calendar months，不包含本月。 |
   | Month to date | 本月、這個月 | `this month` | 本月第一天至 today。 |
   | Rolling years | 過去／最近／近 N 年 | `past`／`recent`／有明確 N 的 `last N years` | 將 today 往前移 N 年，`02-29` 在非閏年 clamp 至 `02-28`，再加一天為 start；end 為 today。 |
   | Completed years | 前 N 年、去年 | `previous N years`、無明確 N 的 `last year` | 完整 calendar years，不包含今年。 |
   | Year to date | 今年 | `this year` | 當年 `01-01` 至 today。 |
   | Explicit date／range | 單一 `YYYY-MM-DD` 或 `YYYY-MM-DD` 起訖日期 | One `YYYY-MM-DD` date or `YYYY-MM-DD` start／end dates | 單一日期解析為 `start_date=end_date=該日期`、`requested_days=1`；起訖範圍包含兩端。格式、順序、未來日期及 earliest date 沿用 Phase 4 policy。斜線日期等已被舊 qualifier regex 辨識但不符合 ISO contract 的形式回傳 `invalid_period`，不得靜默正規化。 |
   | Fixed previous comparison modifier | 與前期／上一期比較 | `compare with the previous period` | 對 `traffic_summary` 只要求呈現 fixed report contract 已包含的 previous period，不新增 explicit period；若另有明示日期區間，則依 explicit range 規則計入。 |

   其他無法唯一判斷 window kind、anchor 或數量的表述一律回傳 `needs_clarification`，不得自行選擇語意或查詢客戶資料。
5. 更新 server instructions、`get_ga4_capabilities`、`search_ga4_metrics`、`query_ga4` 與 `traffic_summary` 的公開說明：active limit 適用於完整使用者需求；超限時不得拆分、分頁、改用其他 data tool 或自動重試。README 與 consent limitation 必須區分 intent-level best-effort behavior、單次 tool call server enforcement 與 effective scan cost controls。
6. 新增由 `period_phrase_contract` 驅動的 versioned unit／behavior eval matrix，而不是只累加個別自然語言案例：
   - 每個 window kind、中文／英文 phrase family、unit alias、數量格式與 `resolved`／`needs_clarification`／`invalid_period` outcome 至少有代表案例。
   - 所有相對日期 fixtures 注入固定 today 與 policy timezone；覆蓋 today inclusion、ISO week、月底 clamp、跨年、閏年、明確範圍、多區間聯集及語意不明。
   - 固定 `today=2026-09-14`：「近三個月」與「過去三個月」皆為 `2026-06-15` 至 `2026-09-14`、共 92 天；「過去半年、按月 group」為 `2026-03-15` 至 `2026-09-14`、共 184 天；「過去 13 週」為 `2026-06-16` 至 `2026-09-14`、共 91 天。
   - 固定 `today=2026-09-16`：「前兩週」為 `2026-08-31` 至 `2026-09-13`、共 14 天。固定 `today=2024-02-29`：「過去一年」為 `2023-03-01` 至 `2024-02-29`、共 366 天；「去年」為 `2023-01-01` 至 `2023-12-31`、共 365 天。
   - 單獨的 `2026-09-01` 解析為 `start_date=end_date=2026-09-01`、`requested_days=1`；相同日期使用斜線格式 `2026/09/01` 時回傳 `invalid_period`。
   - 一般邊界以 `max_date_range_days` 參數化：剛好 `max_date_range_days` 天可繼續，`max_date_range_days+1` 天拒絕；另驗證 `GA4_QUERY_MAX_DAYS=31` 時 31 天可繼續、32 天拒絕，metadata、instructions 與錯誤訊息均顯示 31。
   - `traffic_summary` 在預設 limit 90、current period 為 `2026-06-17` 至 `2026-09-14` 時，`requested_days=90`，自動 previous period 為 `2026-03-19` 至 `2026-06-16`；無論使用者是否加上「與前期比較」，intent boundary 均應允許，previous period 仍須通過 Phase 4 policy。若使用者另行明示這兩個日期區間，則兩段都是 explicit、`requested_days=180` 並拒絕。
   - 同一 `query_ga4` request 混合一個 `requested_period` metric 與一個 `all_available_data` metric 時，兩者各自保留原有 `date_scope`，不產生 request-level aggregate scan period；intent boundary decision 只依共同的 `requested_days`。
   - 「把過去半年拆成三段查」及先收到 `date_range_too_large` 後縮短、拆分或改用另一 data tool 的情境，預期 tenant registry 與 tenant data query 呼叫數皆為零。
7. 將相同 matrix 的代表案例納入實際 Claude Custom Connector 部署後對話驗收，記錄 normalized period result、tool calls、是否接觸 tenant registry／tenant data，以及最終回答措辭。

#### Out of scope for this PoC phase

1. 不建立跨 conversation 或跨 tool call 的持久 request state。
2. 不要求由 preflight 簽發並由 data query 強制攜帶 selection／query token。
3. 不以 OAuth `sub` 累計相鄰日期區段，也不新增每位終端使用者的 daily query quota。
4. 不宣稱能阻止惡意 client、不同 conversation 或刻意直接呼叫多個合法區段；成本底線仍由每個 job、每個 tool request 與 BigQuery project daily quota 保護。

#### Acceptance criteria

- Versioned behavior eval 對明確超過 active `max_date_range_days` 的完整需求回傳限制說明，且預期 tenant registry 與 tenant data query 呼叫數皆為零。
- 實際 Claude connector 對「過去半年、按月彙總」不拆分查詢，會要求使用者提供不超過 active limit 的新期間。
- 收到結構化 `date_range_too_large` 後，host 不會自動縮短、拆分或改用另一個 data tool 重試。
- `period_phrase_contract`、period parser、已辨識 qualifier、公開 metadata、instructions 與 eval inventory 一致；不存在已辨識但沒有 normative semantics 或明確 fallback outcome 的 phrase。
- 預設及至少一個非預設 `GA4_QUERY_MAX_DAYS` 的邊界行為，與公開 capability metadata、instructions 及錯誤訊息一致。
- 單一／多個 explicit periods、重疊與相鄰期間、implicit comparison modifiers 及混合 `date_scope` 均依標準化 period model 得到一致的 `requested_days` 與 boundary decision，且不新增會遺失 per-metric 資訊的 aggregate scan schema。
- 相對天數、rolling／completed weeks、rolling／completed calendar months、rolling／completed years、to-date、明確日期、語意不明、跨年、閏年及月底 clamp 均有使用固定 today 的明確 fixture 或部署後驗收紀錄。
- `traffic_summary` 的 intent boundary 只計明示 current period；fixed previous comparison 即使由使用者以關係詞提及仍屬 `implicit_periods`，並繼續受 Phase 4 的 earliest-date 與成本 policy 約束。只有另行明示第二段日期時才計入 `explicit_periods`。
- 文件及 consent page 清楚揭露：單次 tool call 限制由服務端強制，完整使用者需求限制在 PoC 階段依賴 connector instructions 與 host behavior。
- Phase 4 既有 semantic、traffic summary、REST 與 MCP 日期／成本測試持續通過。

### Phase 11: GA Analytics 內部使用分析／Usage Telemetry

Status: Planned

Dependencies: Foundation 1 的 per-user identity／tenant authorization 前置補強；已完成的
Phases 4–5、7、10 提供 query outcome、capability、tenant 與 period metadata。
Cloud Logging → BigQuery routing 與使用者母體資料尚待建立及驗證。

#### Goal

在部門開放後，量測有多少同事開始使用、使用頻率及留存，以及常見分析方式、指標、維度、
期間、主題與尚未滿足的需求，供 capability、tool design 與使用體驗改善。主要 activation
定義為「至少成功完成一次 GA Analytics 分析的獨立使用者」。

本功能是新的產品使用分析能力，不等同 Phase 6 的 opt-in query provenance，也不只是在
既有營運項目監控 BigQuery 費用。故置於已完成的 Phase 10 之後，獨立拆分相依工作；
不改變 Phase 4–10 範圍，且不以尚未完成的 catalog CI 或 IAM 自動化為必要前置。

#### Existing architecture and prerequisites

1. `oauth_server.py` 已由 Google OIDC 取得穩定 `sub`，簽發 MCP token，並在驗證後回傳
   `AccessToken.subject`；不是缺少 OAuth 登入功能。但 `mcp_server.py` 的 tool handlers
   尚未傳遞 subject，`main.py` REST dependency 只驗證 token、未將回傳 identity 傳入
   analytics context。`tenant_context.py` 目前只追蹤名稱解析，不含 user identity 或 tenant ID。
   實作前須驗證 MCP SDK context 與 REST identity 的安全傳遞、跨並行 request 隔離及 refresh
   前後識別一致性。正式對部門開放前，所有 analytics 入口必須有可驗證的 per-user subject。
2. `cloud-run-iam` 模式由外層 IAM 保護，`require_rest_oauth` 回傳 `None`；目前不能推定
   app 已取得終端使用者身分。須先定義可信 subject 來源；不得使用員工共用 token、runtime
   service account、email 字串或未驗證 header 冒充獨立使用者。缺失時 `user_id=null`，
   記錄 identity coverage，排除獨立使用者與留存計算，不可將所有未知者算成同一人。
3. Foundation 1 的 email allowlist、全域 `ga4:read` scope 與 active tenant routing 並非
   per-user tenant ACL。須先確認並實作使用者可存取 tenant 的權威政策與查核點；若部門
   成員均可讀同一集合，也要有明確政策依據。Request context 至少能取得 user、host、
   authorized tenant scope 與已解析 `tenant_id`，usage event 只保存必要的 scope reference／
   version 與此次授權判定，不複製完整 tenant 名單。不能為補 telemetry 而額外查 registry，
   或在 authentication／authorization denial 後繼續查資料。
4. 以已驗證 subject 及 identity issuer namespace 推導穩定 pseudonymous `user_id`
   （例如服務端 keyed hash），跨 host／token refresh 保持一致；金鑰與輪替策略需避免
   無聲切斷 cohort。不保存原始 email／subject／token。`host` 優先由受管理 client mapping
   取得；host 自述 metadata 僅作非可信產品屬性，無法辨識用 `other`，不能作授權依據。
5. `eligible` 的員工名單、`authorized` 的 OAuth／connection lifecycle 是額外資料依賴。
   目前 process-memory consent／refresh state 不是持久授權台帳，也不能由 initialize、
   tools/list 或第一筆 query 倒推授權日期。正式 rollout 須驗證重啟、重新連線對統計的影響；
   Foundation 1 的正式化限制維持揭露，不在本功能暗中擴充 authorization server。
6. 現有部署腳本讀取 Cloud Logging ERROR logs，但 repository 未有 versioned usage logger
   或其測試。現有 HTTP access logs、query provenance 與 MCP transport session ID 均不能
   直接當成 usage contract 或完整 conversation ID。

#### Scope and implementation sequence

預設資料流：`ga4-analytics-service` → structured Cloud Logging → filtered Log Sink →
BigQuery usage events → dashboard／weekly usage summary。不導入 Cloud SQL。

| Item | Dependencies／順序 | 主要內容與預期重疊處 | Recommended branch |
| --- | --- | --- | --- |
| 11.1 Event contract 與 privacy policy | 先行；可與 identity 盤點並行 | 固定 schema、計數粒度、保留期限、allowlist 與告知方式；作為後續共同 contract | `feat/phase-11-usage-contract` |
| 11.2 Per-user identity prerequisite | Foundation 1；11.1 context contract | subject、host、tenant access context；`oauth_auth.py`、`oauth_server.py`、`mcp_server.py`、`main.py`、`tenant_context.py` 及 auth tests | `feat/phase-11-usage-identity` |
| 11.3 Request summary 與 intent classification | 11.1；既有 Phases 5、10 metadata | nullable summary、redaction、enum hints、規則與 fixtures；核心 sanitizer／classifier 可獨立開發，tool schema 接線需與 11.4 協調 | `feat/phase-11-usage-classification` |
| 11.4 Usage logging middleware／request hooks | 11.1–11.3；與 11.2 順序整合 | MCP／REST／auth denial boundary、共用 orchestration outcome、failure isolation；和 11.2 高度重疊，不平行修改核心 handlers | `feat/phase-11-usage-logging` |
| 11.5 Cloud Logging → BigQuery routing | 11.1、privacy policy；可與 11.3–11.4 分支開發，接真實資料前須驗證 serializer | 結構化事件／摘要附件分流、filtered sink、dataset／table IAM、partition、TTL、dedup 與成本控制 | `feat/phase-11-usage-routing` |
| 11.6 Usage KPI views／dashboard | 11.2、11.4–11.5；funnel 另依賴員工／授權台帳 | 最小化 activation ledger、明確分母、cohort、品質指標、需求分布與 weekly summary；ledger 保存政策為發布前置，可先用合成 fixture 設計 | `feat/phase-11-usage-kpis` |
| 11.7 Production validation | 11.1–11.6 | 小群組 pilot、雙 host／REST 驗收、權限與成本檢查、回復演練 | `chore/phase-11-usage-validation` |

各可獨立工作使用專用 branch／PR；共同 `ROADMAP.md` 更新仍有文件衝突風險。未知 identity／
tenant authorization 依賴應先回報，不能以放寬權限或共用 user ID 繞過。這裡僅規劃未來工作。

#### Collection boundary and counting grain

1. 只在服務收到 MCP／REST analytics 相關 request 時產生 usage event。可觀察 tool call、
   經 allowlist 選取的 arguments、tenant／capability resolution、query execution、tool
   outcome、error／denial／clarification；不以抓取 host 對話補齊資料。
2. 每次服務接收的獨立呼叫由 server 建立唯一 UUID `interaction_id`；同次呼叫的 middleware、
   handler、query hooks 沿用它。Client retry 是新呼叫、新 ID；同一事件的 log 重送保留原 ID。
   不信任 client 提供的 ID 作為去重或授權依據。
3. 第一版只發一筆 terminal canonical event：`analytics_request_completed`，無論 success、
   failure、denied、unsupported 或 needs-clarification 都使用它；completed 表示本次處理終止，
   不表示查詢成功。以 `(schema_version, interaction_id, event_name)` 去重，不逐 metric job 計數。
   未來如增加 received／capability-resolved／query-completed 等 lifecycle events，必須共用
   interaction ID、增加 event identity，且不得納入 canonical request count。
4. 用 `request_kind` 區分 `analytics`、`capability_preflight`、`discovery`、`unclassified`。
   `query_ga4`、MCP `traffic_summary` 與 REST `/traffic-summary` 是 analytics；
   帶需求的 `get_ga4_capabilities` 是 capability_preflight，無需求的 inventory、metric search、
   customer lookup／list 是 discovery。它們可使用同一 event envelope，但 analytics request
   count **只計 `request_kind=analytics` 的 canonical events**，包含成功及未成功嘗試。
   Preflight 的 unsupported／clarification 必須保留於獨立需求統計，不能因沒有 data query 消失。
5. Tool-call count 計可辨識的 MCP `tools/call` canonical events，含 schema validation failure；
   REST 另計。initialize、tools/list、health、OAuth callback 不算 analytics request 或 tool call。
   Auth 在 MCP body dispatch 前拒絕時，不為 telemetry 讀完整 body；以 `unclassified`、nullable
   tool／tenant／user 記錄拒絕的 transport request，排除 tool-call 與 analytics 分母、另報 denied。
6. Preflight、search、data query 是不同呼叫，現有介面沒有可靠的跨 tool request correlation。
   不把它們加總成「獨立使用者需求」或完整對話，也不以相近時間擅自合併；KPI 分別標示粒度。
   若 host 沒有可靠 conversation ID，可將同一已識別 user 在跨 host／tenant 活動中，相鄰
   間隔不超過 30 分鐘的呼叫分組為 `inferred session`，超過即新 session；這只是活動估計，
   不是 host conversation，未知 user 不推估。MCP transport session 不自動具有此語意。

#### Versioned usage event contract

第一版 `schema_version=1.0`，以下示例為 `traffic_summary` 收到 `start_date=2026-09-07`、
`end_date=2026-09-13` 且未提供 intent hint 的成功呼叫。不新增 `analyze_ga4` tool。
所有 event 都有 schema version、server interaction ID、
UTC ISO-8601 event time；`event_time` 是處理終止時間，latency 從接收時計算。

```json
{
  "schema_version": "1.0",
  "event_name": "analytics_request_completed",
  "event_time": "2026-09-18T01:00:00Z",
  "interaction_id": "8b812382-12f7-4cc6-b5fd-642e14764355",
  "transport": "mcp",
  "request_kind": "analytics",
  "user_id": "pseudonymous-stable-id",
  "identity_status": "verified",
  "host": "claude",
  "tenant_id": 5,
  "authorization_scope_ref": "department-policy-v1",
  "authorization_result": "allowed",
  "tool_name": "traffic_summary",
  "request_summary": null,
  "request_summary_source": "unavailable",
  "analysis_goal": "unknown",
  "analysis_subject": "traffic",
  "intent_source": "server_rule",
  "intent_taxonomy_version": "v1",
  "metrics": ["total_sessions", "total_users", "new_users", "returning_users"],
  "dimensions": ["session_date"],
  "period_type": "explicit_range",
  "comparison_type": "previous_period",
  "resolution": "supported",
  "status": "success",
  "latency_ms": 1350,
  "result_row_count": 7,
  "error_code": null,
  "unsupported_reason": null
}
```

Telemetry mapping 必須以各 tool 的既有 versioned contract 為單一來源，不另維護一份
推測的 ID 清單。`query_ga4` 的 metrics 取已解析 catalog metric IDs；`traffic_summary`
（含 REST）的 metrics 取 `traffic_summary_report.py` 的 `TRAFFIC_METRICS[*].metric_id`，
不用顯示 label 或自行縮寫。維度依 catalog ID 與 report 的明確來源映射；本例 `session_date`
對應 report date basis。未有映射的值保持空陣列／unknown，不用臆測值填滿 schema。

本例的 `period_type=explicit_range` 只表示本次呼叫收到明確日期；即使恰好等於上週，
也不能改寫成 relative window。`comparison_type=previous_period` 來自 report 固定的
`immediately_preceding_equal_length` strategy，只代表實際報表比較方式，不證明使用者
要求比較；因此沒有可靠 goal 證據時 `analysis_goal=unknown`。`analysis_subject=traffic`
可由 tool contract 判定，故依既有混合 unknown 規則保留 `intent_source=server_rule`。
文字摘要不充當 resolved period 或 intent hint 的替代證據。

- `transport` 固定 `mcp | rest`；`host` 固定 `chatgpt | claude | web_app | other`；
  `identity_status` 固定 `verified | unavailable`；authorization result 固定
  `allowed | denied | unknown`。Scope reference 是受管理政策代碼，不是 token 或完整 ACL。
- 字串識別欄位不得有時變成數字／物件；`tenant_id` 固定 integer 或 null，user、tool、scope
  reference 無可信資訊時為 null。Preflight、未解析 tenant 或驗證前拒絕不得猜測 tenant ID。
- `metrics`／`dimensions` 固定為去重的 string arrays，僅記錄已驗證 ID，未知為空陣列；
  preflight 的候選不能描述成實際查詢 metric。需要候選分析時另用明確命名的版本化欄位。
  `period_type`／`comparison_type` 使用受管理 code mapping，無可靠證據用 `unknown`，
  明確沒有比較才用 `none`；明確日期不能自行猜為「上週」。
- `resolution` 固定 `supported | needs_clarification | unsupported | unknown`，與執行
  `status=success | failure | denied | unsupported | needs_clarification` 分開。
  支援需求仍可能執行失敗；preflight supported 的 success 不算 activation。Tenant 候選待確認
  映射 needs_clarification；auth／權限／policy 拒絕映射 denied；backend exception 映射 failure。
  既有 domain code 透過版本化 mapping 保留於 `error_code`，不是由 HTTP 200 或字串訊息猜成功。
- `latency_ms` 為非負 integer；`result_row_count` 為非負 integer 或 null，semantic 為全部
  metric 回傳列數總和，traffic summary 為 daily_series 列數，不把 headline 再算一次；
  未完成查詢為 null，成功零列為 0 且可計為成功。`error_code`／`unsupported_reason` 為
  allowlisted string 或 null，不存 exception 原文；unknown 錯誤映射固定 code。
- 版本內固定欄位型別、nullable 與 enum 語意；新增欄位採向後相容策略，破壞性變更發布新
  schema version／對應 BigQuery view，不能直接改舊表型別。離線修訂分類以 interaction ID
  關聯衍生表，保留原始分類、分類版本與來源，不覆寫原事件或重複計 analytics request。

#### Request summary and privacy policy

`request_summary` 從 v1 納入，但 optional／nullable；舊 client 或不同 host 未提供仍正常
執行。它只描述當前 GA4 分析需求，最長 500 Unicode 字元；寫入前遮罩 email、電話、token、
credentials 與不必要個資，再限制長度。不可含完整 conversation history 或 GA4 無關內容；
無法安全清理時捨棄為 null，不能改成記錄 raw input 作 fallback。

`request_summary_source` 固定 enum：`host_model_generated`、`server_generated`、
`client_generated`、`user_input`、`unavailable`。MCP 預期主要由 host model 提供；未提供時
可由已驗證的結構化 arguments 產生 `server_generated` 摘要，不能稱為使用者原始問句。
無可用摘要為 null／unavailable，來源由取得路徑決定並驗證，不是可信原話保證。

採 allowlist-based event serialization，禁止序列化整個 request、AccessToken／claims、
capability result 或 query result。尤其現有 capability result 含原始 `request`，period
phrase match 含 `phrase`，provenance 含 SQL／parameters；均不得整包寫入 usage log。

不得保存 OAuth access token、Authorization header、cookies、secrets／API keys、完整
HTTP request body、完整 BigQuery query result、不必要的完整 SQL、host 完整 conversation
history 或與 GA Analytics request 無關的文字。所有文字欄位與陣列均有大小上限；logger
自己的錯誤診斷也必須遵守，避免 redaction 失敗反而洩漏 payload。

保留期限初始規劃：結構化 usage events 180 天、summary 30 天，pilot 前由資料負責人確認。
原始事件及其 Cloud Logging、BigQuery、衍生表與匯出副本均適用相同或更短期限；不能僅刪
dashboard 欄位。下述最小化 activation ledger 是獨立核准的保存類別，不延長原始事件期限。

第一版即採分流，不是可選的差異 TTL：canonical event 的 `request_summary` 固定寫 null、
`request_summary_source` 固定寫 unavailable，表示「此紀錄未攜帶摘要」，不表示 host 未提供。
只有摘要附件保存實際來源。完成 redaction／長度限制後，才可向獨立受限 log／table 寫入
以下 `analytics_request_summary` 附件契約；不先將文字寫入 180 天 log 再期待 sink 移除。

```json
{
  "schema_version": "1.0",
  "event_name": "analytics_request_summary",
  "event_time": "2026-09-18T01:00:00Z",
  "interaction_id": "8b812382-12f7-4cc6-b5fd-642e14764355",
  "request_summary": "查詢 2026-09-07 至 2026-09-13 的 GA4 流量摘要",
  "request_summary_source": "server_generated"
}
```

附件只允許上述欄位，沿用 canonical event 的 interaction ID 與處理終止時間；schema、
時間、ID 型別同主契約，summary 為已清理的非空字串（最多 500 字），source 為前述四種
實際來源之一。沒有安全可用摘要就不發附件，不發 null／unavailable 附件。每個 request
至多一筆邏輯附件，以 `(schema_version, interaction_id, event_name)` 去重；重送不得
刷新 event_time 或延長 30 天期限。附件不屬 canonical request，不增加任何 request count。

Logging 與 BigQuery 分別設定專用短期儲存及 routing；確認預設 log bucket、額外 sink、
匯出與備份不會留下長期摘要副本。期限以原事件時間為基準，已過期附件不得重新匯入。
受限 view 可在期限內 left join 附件；不得把含摘要的 join 結果物化／匯出至 180 天儲存。
附件缺失、亂序到達、logging failure 或到期均不影響主事件與 KPI，也不能重試主 analytics
request 來補摘要。Sink 不具備任意欄位轉換的假設不可作為隱私保證，須驗證實際分流及清理。

使用獨立 usage dataset，sink writer、pipeline、dashboard reader 與 summary reviewer
採最小 IAM；一般部門使用者只能看經允許的彙總，不能因 GA4 tenant 權限而讀全員文字紀錄。
定義小樣本呈現限制、刪除及到期驗證，並在內部使用說明／consent 告知收集欄位、目的、
保存期限與可存取角色；不為此更改 GA4 OAuth scopes。

#### Activation ledger and history coverage

為避免原始 events 在 180 天到期後把舊使用者重新算成首次 activated，11.6 規劃獨立的
BigQuery activation ledger：每個 pseudonymous `user_id` 僅保存 `first_success_at`
（UTC timestamp）與 `measurement_version`；不保存 summary、tenant、metric、SQL 或完整
使用歷程。從去重且 identity verified 的成功 analytics canonical events 非同步、冪等更新，
首次時間取最早已觀測成功時間；較晚到達的早期事件可往前修正，並重算受影響 cohort。
附件或 preflight 不得建立 activation，ledger 更新失敗不得影響 analytics response。

Ledger 採獨立且可超過 180 天的保存政策：僅在核准的內部產品量測期間保留，期間結束後
依核准期限清理，不隨每次回訪自動展延。資料負責人須在發布前明定具體保存期間、用途、
最小 IAM、刪除期限、使用者刪除要求及 identity key 輪替／映射處理；未核准不得預設永久
保存，也不得發布依賴長期歷史的累積 activation 指標。內部告知須明列此保存類別及期限。

另以 measurement version 管理不含個人資料的觀測起點、已知缺漏及 pipeline watermark。
Ledger 必須持續維護，不能等事件過期後才由剩餘 180 天資料重建。首次成功指「指定量測
起點以來最早已觀測成功」，不宣稱捕捉遺失的 events。只有 ledger／identity 連續性與歷史
覆蓋足夠時，才提供該量測起點以來的新 activation 及累積值。

Ledger 不可用、歷史不足、已到期／刪除或 key rotation 無法保持連續性時，受影響報表必須
降級為「可觀測期間內首次成功」，標示起點與缺口，停止提供無法支持的全歷史首次／累積
activation 及首次 activation cohort；不能把重新出現的 user 默認為新人。刪除後若無法
可靠識別受影響 user，須對整個受影響 measurement version 降級，不能另留未核准的
user tombstone 繞過刪除。合規刪除造成統計修訂需揭露，不承諾累積值永不下降。

Ledger 只保存首次時間，不保留後續活動：W1–W4 留存仍需完整且未到期的 follow-up events。
超出可用事件窗口的歷史 cohort 標為不可重算，不將缺失值當作零；本階段不藉 ledger
無限期保留個別使用者的活動或留存明細。

#### Intent taxonomy and deterministic classification

需求類型與產品支援狀態分離，固定 `intent_taxonomy_version=v1`：

| Dimension | Allowed values |
| --- | --- |
| `analysis_goal` | `overview`、`comparison`、`trend`、`breakdown`、`ranking`、`diagnosis`、`recommendation`、`data_lookup`、`unknown` |
| `analysis_subject` | `traffic`、`acquisition`、`campaign`、`content`、`landing_page`、`audience`、`conversion`、`engagement`、`cross_source`、`unknown` |
| `intent_source` | `server_rule`、`host_model_hint`、`offline_classifier`、`manual_review`、`unknown` |

1. 優先由 server deterministic rules 判斷，並版本化 mapping、precedence 與 conflict cases。
   `period_contract.py` 的 outcome、`window_kind`、explicit／implicit periods、requested_days
   與 comparison_modifier 可提供期間／比較線索；只抽取 code／日期等安全 metadata。
   Data tool 目前只有 start／end，不能假裝保留了 preflight 自然語言或 relative window kind。
2. `capability_registry.py` 的 resolution、reason_code、next_action、registry version 與
   metric candidates 可提供支援邊界及主題候選；capability resolution 不直接等於 goal。
   `semantic_catalog.py` 的 metric ID、category、main_metric、dimensions、model、profile
   與 catalog version 可映射 subject；固定 report 使用前述 tool contract mapping，不能任意創造 ID。
3. `main.py` 的 `prepared_metrics`／`PreparedQuery` 是既有 resolved query plan 的可用部分，
   可取實際 metric、date_scope、日期與結果狀態；目前沒有通用的 goal、comparison 或任意
   group-by intent 欄位。固定 report comparison、daily series 是結果形狀，不足以斷言使用者
   想 diagnosis／trend。`all_available_data` 也不能說成實際只讀 requested period。
   缺少線索回 unknown，不新增 SQL parser 或大幅重構 query plan。
4. 無法由結構化參數可靠推導的 diagnosis／recommendation 等語意，可接受 host model 的
   固定 enum hint。Server 驗證型別、enum、長度及與已知事實的一致性，deterministic 證據
   優先；無效或衝突 hint 忽略並安全 fallback，不改變 analytics 結果。
   兩個維度分別判定；若其中採用 hint，整體 `intent_source=host_model_hint`，只有全部
   非 unknown 分類都由規則取得才為 server_rule；全 unknown 為 unknown。
5. 不允許 AI 自由輸出任意 intent 字串。離線 classifier／manual review 僅在受控衍生資料補
   分類，記錄 `intent_source`／taxonomy version，不能阻塞或改變當下查詢結果及權限。
   例如 GA4 與 Meta 廣告比較應保留 goal=comparison、subject=cross_source、
   resolution=unsupported、unsupported_reason=external_source_not_available；不因不支援
   就抹除需求，也不因分類就增加外部資料查詢能力。

#### Adoption funnel and usage metrics

Funnel 以去重的穩定 user ID 連接不同來源；不是每個階段都能由 analytics handler 觀察。

| Stage | 可觀測條件與資料依賴 |
| --- | --- |
| `eligible` | 在觀察期間具使用資格的部門成員；需外部員工／資格名單及生效時間，allowlist 不自動等於完整 denominator。 |
| `authorized` | 已完成服務授權／connection 的獨立 user；需可持久查核的 OAuth／connection lifecycle 台帳，不把 consent page view、refresh 或重連重算新人。未提供時標示不可量測。 |
| `tried` | 已驗證使用者至少一次已知 MCP tool 的有效 schema call；即使 outcome 是 unsupported／clarification 也算嘗試，auth denial／invalid schema 不算。REST 初次使用另報，不冒稱 tool call。 |
| `activated` | 至少一次 `request_kind=analytics` 且 status=success 的獨立 user（MCP 或 REST）；search、preflight success 及僅列出客戶不算。 |
| `retained` | 首次 activation 後的指定觀察期再次成功完成 analytics request；須完整的 cohort follow-up window。 |

REST user 可 activated 而沒有 MCP tried，funnel 應按 transport 揭露非嚴格階梯，不能強制補造
中間事件。沒有 eligible／authorized 台帳時，只報可觀測絕對數及資料缺口，不報虛構轉換率。

統計日界／週界預設 `Asia/Taipei`、週一開週，UTC event_time 轉換後計算；報表標示期間、
資料延遲、identity coverage 與缺漏，未成熟 cohort 不當作零留存。

| Metric | 第一版定義／分母 |
| --- | --- |
| Activated users | Ledger 的 first_success_at 落在報告期間的獨立 user；累積值為指定量測起點至報告期末的 ledger 去重 users。須符合 history coverage 條件，資料不足時降級標示，不從剩餘 180 天 events 推定全歷史首次。 |
| DAU／WAU／MAU | calendar 日／週／月內有成功 analytics 的獨立 user；小樣本優先 WAU 與絕對數，不製造無意義精度。 |
| Active days per user | 期間內各 user 至少一次成功 analytics 的不同日期數。 |
| Analytics requests per user | 各已識別 user 的去重 analytics 呼叫數，含各種 status；平均分母為期間內有 analytics 嘗試的已識別 users。 |
| Successful requests | canonical analytics events 中 status=success 的數量，與多 metric job 數、tool-call count 分開。 |
| Repeat usage rate | 期間內至少兩個不同日期成功 analytics 的 users／同期間至少一次成功的 users；同日重試不算回訪。 |
| 4-week retention | 以具足夠歷史覆蓋的 ledger first_success_at 所在 calendar week 為 W0；W4 再成功使用人數／已完整觀察至 W4 結束的該 cohort 人數。W1–W4 表須有完整且未到期的 follow-up events，窗口到期或 history coverage 不足則標示不可量測。 |
| Supported／needs-clarification／unsupported rate | 在 capability_preflight canonical events 中各 resolution 數／三種已知 resolution 總數；另列 unknown coverage。Data analytics 的 resolution 另表同法計算，禁止混合兩種分母；tenant clarification 另依 status 報告。 |
| Failure rate | analytics status=failure／全部 canonical analytics attempts；另報 denied、unsupported、needs-clarification 占比，不能將它們當 backend failure。 |
| Top analysis goals／subjects | 按 request_kind 分開，以去重 interaction 統計 taxonomy 分布，保留 unsupported、unknown 及 intent_source。 |
| Top metrics／dimensions | 以每個 canonical analytics request 中每個已驗證 ID 至多一次計數；不依結果列數加權，preflight 候選另報。 |
| Top period／comparison patterns | 依標準化 period_type／comparison_type 分布，區分 explicit／implicit／unknown，不以實際掃描範圍替代需求期間。 |
| Top unsupported reasons | unsupported events 的受管理 reason code 分布，分開 preflight 與 data analytics。 |
| Latency／error categories | 依 tool、transport、status 的 p50／p95 latency 及 auth、tenant、validation、policy、timeout、backend／unknown error code 分布；不保存 exception 原文。 |

Tool-call、analytics request、inferred session 為三種獨立單位，dashboard／weekly summary
需明列。來源覆蓋只限實際送達服務的需求；不能用這些排名推論所有 host 對話或全體未使用者。

#### Reliability, rollout and validation

1. Emission 必須非阻塞或 failure-isolated：bounded queue／短 timeout、有限重試、queue full
   時可丟棄並以不含 payload 的計數告警。禁止同步向 BigQuery 寫入或在 request 中等待
   sink。Serializer、redaction、logger、網路故障均不能讓 GA4 request 失敗或改變 tool result。
   Cloud Run 終止前的 buffer loss、process crash 無 terminal event 與 sink delivery delay
   必須量測及揭露，不承諾 exactly-once／零遺失，也不能拿 usage log 取代完整 security audit。
2. 先以合成事件驗證型別、去重、partition、TTL、IAM 與所有分母，再於小群組開啟結構化
   logging，summary 分開開關。Filtered sink 僅允許專用 usage log、環境與已知 schema；
   摘要附件使用獨立短期 log／sink／table，排除所有長期 log bucket 與 sink；ledger 採獨立
   核准保存政策並驗證更新 freshness。不匯出一般 access／error log。BigQuery partition 依 event date、要求時間 filter，設定
   dashboard query bytes 限制與預算；usage 查詢成本另盤點，避免耗盡 GA4 billing quota。
3. Pilot 覆蓋 Claude、ChatGPT（可用時）與 REST；驗證 stable subject、host mapping、
   tenant 權限、舊 client 不帶 summary／hint、拒絕與澄清、重試與多 metrics 的計數。
   未實測 host 列為未驗證；不能把 server fixtures 當作 host tool-choice 驗收。
4. 對照服務端合成／pilot request 計數與 BigQuery dedup views，量測遺失、延遲、重送、
   unknown identity／intent／host 及 failure isolation 的 latency overhead。Rollout 前訂定
   可接受門檻、資料負責人及 freshness 告警，並查核 Logging／BigQuery 副本均無敏感資訊。
5. Rollback 可停用 usage emission／summary、停用 sink 及暫停 dashboard refresh；保留
   analytics 行為與必要 auth boundary，不能為關閉 telemetry 撤銷 tenant 授權檢查。
   停止收集不等於刪除既有資料，依 retention／清理流程驗證。

#### Acceptance criteria

- Logging 成功時產生符合 versioned schema 的 canonical event；MCP／REST 的 success、
  failure、denied、unsupported、needs-clarification 均有一致 mapping，含 middleware
  早期拒絕、schema validation failure 與 handler exceptions，不只測 happy path。
- 每個獨立 request 有唯一 interaction ID，同 request 各層共用；多 metrics、nested hooks、
  retry 與 log duplicate fixtures 證明 request count 不重複，preflight 不誤算 activation。
- Logger exception、serialization／redaction failure、timeout、queue full 及 sink outage
  的 fault-injection 測試證明正常 analytics 回應與原有錯誤行為不變；無敏感 fallback log。
- 測試含 access token、Authorization header、cookie、secret、完整 raw request、query
  result／SQL、email／電話及過長摘要；驗證 allowlist、redaction、500 字上限、nullable
  summary 與 enum source，不把完整 request／result／provenance 寫入 log。
- Taxonomy 與 host hint 只接受固定 enum，deterministic classifier 有 unit fixtures 覆蓋
  period／metric／dimension／comparison／capability、cross-source unsupported、衝突
  hint 與缺少證據；無法分類安全回 unknown，分類與 offline job 不影響查詢或授權。
- Subject 在 MCP／REST、refresh、並行 user、不同 host 間正確傳遞且不互相污染；tenant
  scope 與 tenant ID 來自可信 context。缺失 identity 時人數／留存明確排除並顯示 coverage。
- 同版本欄位型別、nullability、enum、schema migration 與 BigQuery ingestion 相容性有
  contract tests；匿名拒絕、零列成功、多 metric 結果及尚未解析 tenant 都有 fixture。
- Funnel／KPI fixtures 驗證 external denominator 缺失、跨日／週界、W4 未成熟 cohort、
  REST 非階梯 funnel、30 分鐘 inferred session，以及 tool-call／analytics／session
  三種計數；沒有完整對話資料時不能宣稱量測了完整對話數。
- Telemetry mapping／文件範例與既有 tool contract 有一致性驗收：traffic summary metrics
  必須等於 `TRAFFIC_METRICS` IDs，semantic metrics／dimensions 由 catalog 驗證；不得以
  label、SQL alias 或摘要文字替代。明確日期恰好落於上週及一般任意區間，都保持
  explicit_range；沒有本次呼叫的可靠 relative-period／goal 證據時，不從日期、固定比較
  或其他 tool call 倒推原始意圖。Report comparison 可記錄，但不能把它當成使用者 goal。
- 保存期限 fixtures 覆蓋同一 user 第 1 天成功、第 181 天回訪：第 1 天原始 event 到期後，
  ledger 仍保留首次時間，不重算新 activation；重送不加人數、late event 修正首次時間與
  cohort。Ledger 缺失／刪除／到期、identity 斷裂及 pipeline gap 必須觸發 history coverage
  降級；follow-up events 到期的 W4 不得顯示零留存或從 ledger 猜測。
- Canonical 範例及 serializer 均驗證 summary=null、source=unavailable；摘要只存在於
  專用 30 天附件。第 31 天附件與所有文字副本已到期不可讀，但 canonical event 與 request
  count 仍保留；涵蓋附件重送、亂序、缺失、故障、過期重匯入及 join／export 無長期文字副本。
- 發布前核准 ledger 的具體保存及刪除政策，驗證 IAM、清理、identity 輪替與歷史覆蓋告知；
  未核准時長期累積 activation／首次 cohort 不可發布，不得以擴大原始 event TTL 替代。
- Local regression 沿用 OAuth、tenant context、capability、period、query policy 與 report
  suites，未來實作執行 `.venv/bin/python -m unittest discover -s tests -v`，另執行當時
  repository 可用的相關 lint／type check／build；本規劃不代表已新增或跑過 telemetry 測試。
- Production validation 留有 sink 到達／dedup、IAM 正反例、資料隔離、TTL 到期、成本與
  rollback 紀錄；內部告知完成。外部員工／授權資料、Cloud Run buffer 行為、host metadata
  可信度及實際 connector 行為仍需驗證，未完成前不得宣稱完整 adoption／retention 可用。

#### Non-goals

不收集 ChatGPT／Claude 的其他對話、未觸發服務的訊息、host 最終完整回答或完整可靠的
conversation history；不做完整對話錄影、個別員工績效評估、GA4 終端訪客追蹤、任意 SQL
記錄或新的資料來源查詢。不新增 Cloud SQL、OAuth scopes、個人 query quota 或 Phase 10
跨 tool 安全狀態。Summary／hint 的未來 optional tool schema 擴充需獨立實作與相容性驗收；
本次僅更新規劃，不修改程式／測試、不建立 dataset／table／sink，也不部署。

## Ongoing operational work

以下項目是與功能 roadmap 並行的持續性營運工作，每次正式發布前都應持續執行：

1. 維護 tenant registry 名稱、alias、狀態、project 與 ecommerce profile 品質。
2. 對所有 active tenants 執行代表性 metric dry-run，確認 schema 及 IAM 沒有 drift。
3. 監控 BigQuery job bytes、cache hit、失敗原因與每日 quota 使用量。
4. 維護 supported／unsupported intent eval set，避免模型或 tool description 更新後能力邊界退化。
5. 使用固定部署腳本，並在重大 OAuth、IAM 或 report schema 變更時先以 no-traffic revision 驗證。

## Confirmed implementation decisions

1. 未登記的部分名稱不會授權資料查詢；即使只有一個候選，也先要求使用者以正式名稱確認。
2. Project／dataset table path 視為 technical routing details；一般回應不顯示，只有使用者明確要求 technical routing details 或 query provenance 時才提供。
3. Traffic summary 固定使用四個 small-multiple 折線圖，每個 metric 一張圖，圖內各有本期與前期兩條 series。
