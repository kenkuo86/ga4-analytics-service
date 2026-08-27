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

## Phase 3: capability boundaries

目前 MCP 只提供 GA4 traffic summary：sessions、total users、new users、returning users，以及前一期比較。

後續應：

1. 在 server instructions 與 tool descriptions 明確列出可用資料。
2. 對未支援的 GA4 指標回傳 `unsupported_metric`。
3. 對營收、訂單、廣告、SEO keyword、CRM 等非 GA4 traffic summary 問題回傳 `data_unavailable`。
4. 禁止把一般知識或推測描述成客戶的實際資料。
5. 為能力邊界與錯誤狀態建立端到端測試。

MCP server 可以保證不提供邊界外的資料，但 host model 的自然語言行為仍需透過清楚的 tool description、connector instructions 與驗收測試共同約束。
