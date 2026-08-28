# GA4 Analytics Service roadmap

## Registry data prerequisites

`tenant_registry` 是 Google Sheets external table。2026-08-27 匯出的 77 筆 tenant 中，54 筆為 `active`、23 筆為 `provisioning`，其中 29 筆缺少 `tenant_name`。名稱空白的 tenant 無法由使用者以客戶名稱查詢，應先在來源試算表補齊。

目前名稱解析只接受 registry 的正式 `tenant_name`，不做模糊猜測。若需要支援簡稱、品牌名或中英文別名，應在 registry 增加受管理的 alias 欄位，並對 alias 唯一性做檢查。

候選搜尋應作為獨立功能：例如使用者輸入「東方美」時，可以回傳唯一候選「東方美企業」並要求確認，但在確認前不得直接查詢。若有多個候選，必須列出選項而不能自動選擇。

## Phase 2: cross-project BigQuery access

目標是讓 Cloud Run runtime service account 能讀取每個 active tenant project 的 `ga4_mar` dataset，同時避免讀取同專案的其他資料。

建議做法：

1. 從 registry 讀取所有 active tenant 的 `project_id`。
2. 檢查 `<project_id>.ga4_mar` 是否存在。
3. 產生目前 IAM 缺口報告，不直接修改權限。
4. 經人工確認後，在每個 `ga4_mar` dataset 授予 runtime service account `roles/bigquery.dataViewer`。
5. 保留 `roles/bigquery.jobUser` 在查詢計費專案 `ga4-reports-dev`。
6. 將同步做成可重複執行的管理腳本或 Terraform；管理者身分與 runtime service account 分離。

不建議直接在 tenant project 或共同 folder 授予 `roles/bigquery.dataViewer`，除非其中所有 BigQuery datasets 都允許 MCP 讀取，因為 project／folder 層級授權不會只限定名稱為 `ga4_mar` 的 dataset。

## Phase 3: semantic layer and capability boundaries

第一版 semantic layer 已完成：來源指標表會編譯成 Git 版控的
`semantic/catalog.v1.json`，MCP 透過 `search_ga4_metrics` 搜尋定義，再由
`query_ga4` 執行 catalog 內的固定 SQL。source／medium／campaign 等維度分析不再需要
各寫一個 tool；`traffic_summary` 暫時保留為既有相容介面。

已完成的能力邊界：

1. 在 server instructions 與 tool descriptions 明確要求先搜尋 catalog，禁止虛構 metric ID 或 SQL。
2. 對未發布的 GA4 指標回傳 `unsupported_metric`，對來源定義衝突回傳 `metric_definition_conflict`。
3. 只允許 catalog 中的單一 `SELECT`／`WITH` query 與核准 model，不提供任意 SQL tool。
4. project／dataset 一律由 tenant registry 在服務端解析。
5. 查詢有日期範圍、結果筆數、每次 metric 數量及 bytes billed 上限。
6. 對 catalog builder、runtime compiler、profile 判定與 MCP schema 建立測試，並提供全 catalog BigQuery dry-run。

下一步依優先順序：

1. 將 Google Sheet 匯出、catalog build、單元測試與 BigQuery dry-run 串成 CI；只有全部通過才允許發布 catalog。
2. 為來源表增加 schema 驗證規則，包括 metric ID 唯一性、derived metric dependency、時間維度、model grain 與 owner／變更說明。
3. 視使用情況將重複 SQL 逐步拆成可組合的 base metric、dimension 與 filter definition；在此之前仍以已驗證的固定 SQL template 為執行來源。

2026-08-28 已完成：runtime 讀取 `tenant_registry.ec`，只有 `TRUE` 使用 ecommerce
profile，`FALSE` 或空白使用 non_ecommerce；非電商 `total_users` 也已統一使用
`mar_ga_sessions` 與 `session_date`，catalog 提升至 v1.1.0。

MCP server 可以保證不提供邊界外的資料，但 host model 的自然語言行為仍需透過清楚的 tool description、connector instructions 與驗收測試共同約束。

目前 server instructions 已要求 host model 不得向使用者索取 `tenant_id`、
`project_id` 或 `dataset_id`，且所有查詢都由 server 內部解析 routing。新增指標時應更新
定義表、重新產生 catalog 並通過驗證，不需要新增一組 MCP tool。

`list_available_customers` 會直接列出具有非空白且唯一 `tenant_name`、狀態為
`active`、並已設定 `project_id` 的 registry 項目。對話介面預設回覆此客戶名稱
清單，不以 Google Sheet 連結取代結果；Sheet 僅作為管理來源。
