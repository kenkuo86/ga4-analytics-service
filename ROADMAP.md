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

目前 Phase 4–9 已完成，下一階段為 Phase 10 的使用者需求層級日期邊界。其餘工作包含持續監控成本、權限、tenant registry 品質及 connector 行為。

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

以下各階段依成本與資料安全優先，再逐步改善可信度及使用體驗；目前 Phase 4–9 已完成，Phase 10 尚待實作。

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

Status: Planned

Dependencies: Phases 4–5

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
   - `implicit_periods` 是 tool contract 自動加入、但使用者沒有另外要求的區間，例如 `traffic_summary` 的等長 previous comparison period；它不計入 intent-level `requested_days`。
   - `effective_scan_periods` 記錄實際會讀取的 explicit 與 implicit periods，`effective_scan_days` 是其日期聯集天數，供 Phase 4 的 earliest-date、bytes、timeout 及 daily quota policy 使用；不得把 intent-level 天數限制誤述為實際掃描天數限制。
   - `date_scope=all_available_data` 的 semantic metric 沒有可由 request 縮小的有效掃描期間；其 explicit period 仍用於 intent boundary，但 `effective_scan_periods=all_available_data`、`effective_scan_days=null`，結果必須保留既有 `date_scope` 說明，實際成本仍由 Phase 4 bytes policy 保護。
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

   其他無法唯一判斷 window kind、anchor 或數量的表述一律回傳 `needs_clarification`，不得自行選擇語意或查詢客戶資料。
5. 更新 server instructions、`get_ga4_capabilities`、`search_ga4_metrics`、`query_ga4` 與 `traffic_summary` 的公開說明：active limit 適用於完整使用者需求；超限時不得拆分、分頁、改用其他 data tool 或自動重試。README 與 consent limitation 必須區分 intent-level best-effort behavior、單次 tool call server enforcement 與 effective scan cost controls。
6. 新增由 `period_phrase_contract` 驅動的 versioned unit／behavior eval matrix，而不是只累加個別自然語言案例：
   - 每個 window kind、中文／英文 phrase family、unit alias、數量格式與 `resolved`／`needs_clarification`／`invalid_period` outcome 至少有代表案例。
   - 所有相對日期 fixtures 注入固定 today 與 policy timezone；覆蓋 today inclusion、ISO week、月底 clamp、跨年、閏年、明確範圍、多區間聯集及語意不明。
   - 固定 `today=2026-09-14`：「近三個月」與「過去三個月」皆為 `2026-06-15` 至 `2026-09-14`、共 92 天；「過去半年、按月 group」為 `2026-03-15` 至 `2026-09-14`、共 184 天；「過去 13 週」為 `2026-06-16` 至 `2026-09-14`、共 91 天。
   - 固定 `today=2026-09-16`：「前兩週」為 `2026-08-31` 至 `2026-09-13`、共 14 天。固定 `today=2024-02-29`：「過去一年」為 `2023-03-01` 至 `2024-02-29`、共 366 天；「去年」為 `2023-01-01` 至 `2023-12-31`、共 365 天。
   - 單獨的 `2026-09-01` 解析為 `start_date=end_date=2026-09-01`、`requested_days=1`；相同日期使用斜線格式 `2026/09/01` 時回傳 `invalid_period`。
   - 一般邊界以 `max_date_range_days` 參數化：剛好 `max_date_range_days` 天可繼續，`max_date_range_days+1` 天拒絕；另驗證 `GA4_QUERY_MAX_DAYS=31` 時 31 天可繼續、32 天拒絕，metadata、instructions 與錯誤訊息均顯示 31。
   - `traffic_summary` 在預設 limit 90、current period 為 `2026-06-17` 至 `2026-09-14` 時，`requested_days=90`，自動 previous period 為 `2026-03-19` 至 `2026-06-16`，`effective_scan_days=180`；intent boundary 應允許，兩段仍須通過 Phase 4 policy。若使用者明確要求這兩段，則 `requested_days=180` 並拒絕。
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
- 單一／多個 explicit periods、重疊與相鄰期間、implicit comparison periods 及 `date_scope=all_available_data` 均依標準化 period model 得到一致的 `requested_days` 與 boundary decision。
- 相對天數、rolling／completed weeks、rolling／completed calendar months、rolling／completed years、to-date、明確日期、語意不明、跨年、閏年及月底 clamp 均有使用固定 today 的明確 fixture 或部署後驗收紀錄。
- `traffic_summary` 的 intent boundary 只計 current explicit period；自動 previous period 記錄於 `implicit_periods`／`effective_scan_periods`，並繼續受 Phase 4 的 earliest-date 與成本 policy 約束。
- 文件及 consent page 清楚揭露：單次 tool call 限制由服務端強制，完整使用者需求限制在 PoC 階段依賴 connector instructions 與 host behavior。
- Phase 4 既有 semantic、traffic summary、REST 與 MCP 日期／成本測試持續通過。

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
