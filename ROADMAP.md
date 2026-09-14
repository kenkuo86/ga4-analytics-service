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

目前 Phase 4–9 的功能 roadmap 已全部完成。其餘工作以持續監控成本、權限、tenant registry 品質及 connector 行為為主。

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

以下各階段依成本與資料安全優先，再逐步改善可信度及使用體驗；目前 Phase 4–9 已全部完成。

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

## Ongoing operational work

以下項目是功能 roadmap 完成後的持續性營運工作，每次正式發布前都應持續執行：

1. 維護 tenant registry 名稱、alias、狀態、project 與 ecommerce profile 品質。
2. 對所有 active tenants 執行代表性 metric dry-run，確認 schema 及 IAM 沒有 drift。
3. 監控 BigQuery job bytes、cache hit、失敗原因與每日 quota 使用量。
4. 維護 supported／unsupported intent eval set，避免模型或 tool description 更新後能力邊界退化。
5. 使用固定部署腳本，並在重大 OAuth、IAM 或 report schema 變更時先以 no-traffic revision 驗證。

## Confirmed implementation decisions

1. 未登記的部分名稱不會授權資料查詢；即使只有一個候選，也先要求使用者以正式名稱確認。
2. Project／dataset table path 視為 technical routing details；一般回應不顯示，只有使用者明確要求 technical routing details 或 query provenance 時才提供。
3. Traffic summary 固定使用四個 small-multiple 折線圖，每個 metric 一張圖，圖內各有本期與前期兩條 series。
