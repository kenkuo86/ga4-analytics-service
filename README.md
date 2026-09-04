# GA4 Analytics Service / MCP Server

這個 PoC 將既有 GA4 Analytics Service 同時暴露為：

- MCP Streamable HTTP endpoint：`/mcp`（同時保留 `/mcp/` 相容路由）
- REST endpoint：`/traffic-summary`

服務提供 `customer_lookup`、`list_available_customers`、`search_ga4_metrics`、
`query_ga4` 與相容用的 `traffic_summary`。使用者以 tenant registry 中的正式客戶名稱查詢，不需要知道內部
`tenant_id`。`customer_lookup` 只確認 registry 狀態，不依賴客戶 GA4 dataset
權限；`list_available_customers` 回傳目前可唯一解析且已設定 GA4 project 的 active
客戶名稱；`search_ga4_metrics` 搜尋已發布的指標定義；`query_ga4` 依 catalog 中的固定
SQL 模板查詢一至五個指標。LLM 不會直接產生或執行任意 SQL。

例如：

```text
告訴我維肯媒體部落格上週的流量摘要。
```

名稱解析會忽略前後空白、英文大小寫與 Unicode 相容字元差異，但不會模糊猜測其他客戶。找不到、尚未 active 或名稱重複時，tool 會回傳明確的結構化狀態。

Tool result 會附上 registry 解析出的 `project_id` 與固定的 `ga4_mar`
dataset，供 host model 保留為內部 routing context；使用者不需要知道或提供這些
識別資訊，回覆時也不應主動顯示。source／medium／campaign 等分析由 semantic
catalog 決定可用指標與查詢方法；不在 catalog 的問題會回傳 `unsupported_metric`，
不得要求使用者提供 project、dataset 或 SQL 來繞過能力邊界。

當使用者詢問「目前有哪些客戶可以查詢」時，connector 應直接呼叫
`list_available_customers` 並列出客戶名稱。Registry Google Sheet 是管理介面，
不作為預設回答，以免暴露不必要的內部欄位或讓使用者受 Sheet 分享權限影響。

## Semantic layer

指標定義的資料流如下：

```text
指標定義 CSV / Google Sheet
  -> build_semantic_catalog.py（正規化、衝突與 SQL 安全檢查）
  -> semantic/catalog.v1.json（Git 版控的發布產物）
  -> search_ga4_metrics / query_ga4
  -> tenant registry 路由
  -> BigQuery ga4_mar
```

目前 catalog 有 `ecommerce`（電商）與 `non_ecommerce`（非電商）兩個 profile。
`query_ga4` 會從 `tenant_registry.ec` 自動選擇：只有 `TRUE` 使用 ecommerce，`FALSE`
或空白使用 non_ecommerce。profile 不開放成 query tool 輸入，避免對話內容覆寫 registry
設定。`search_ga4_metrics` 仍可選擇 profile 來縮小定義搜尋範圍。

若同一 profile 中同一個 `metric_id` 有多種語意定義，builder 預設會將它標記為
`conflict`，runtime 不會發布或執行。非電商 `total_users` 已依確認過的 canonical
policy 統一為 `mar_ga_sessions`、`session_date`、`COUNT(DISTINCT user_pseudo_id)`；該
resolution 會一併記錄在 catalog，不會靜默選擇版本。

重新產生 catalog：

```bash
python scripts/build_semantic_catalog.py \
  --non-ecommerce /path/to/non-ecommerce.csv \
  --ecommerce /path/to/ecommerce.csv \
  --output semantic/catalog.v1.json \
  --catalog-version 1.1.0
```

發布前，以一個已授權且具有完整 schema 的客戶 dry-run 所有已發布指標：

```bash
python scripts/validate_semantic_catalog.py --customer-name '客戶正式名稱'
```

完成跨專案 dataset 授權後，先查詢一次 registry，再以 Cloud Run runtime
service account 對所有 active tenant dry-run `total_users`：

```bash
.venv/bin/python scripts/validate_all_tenants.py \
  --billing-project ga4-reports-dev \
  --impersonate-service-account 'RUNTIME_SA_EMAIL' \
  --output /tmp/ga4-tenant-validation.json
```

若指令已經在 runtime service account 身分下執行，可省略
`--impersonate-service-account`。每個 tenant 的指標查詢都強制設為 BigQuery
dry run，不會實際查詢 tenant 資料或產生 dry-run query 費用；只有取得
active tenant 清單的 registry query 會實際執行一次。若要同時驗證所有已發布
metric 的 schema，可加上 `--all-published-metrics`。
若本機 Application Default Credentials 與 `gcloud` 的主動帳號行為不一致，
可加上 `--use-gcloud-source-credentials`；該模式只在記憶體中將 `gcloud`
access token 交換為 runtime service account token，不會寫入報告。

Runtime BigQuery client 可透過 `BIGQUERY_BILLING_PROJECT` 明確指定 query job
與計費專案；PoC 部署設為 `ga4-reports-dev`。該變數未設定或只有空白時，
程式會維持原有行為，使用 Application Default Credentials 推斷的 project。

## PoC deployment

一般改動使用固定的部署腳本：

```bash
scripts/deploy_poc.sh
```

腳本會先檢查 `gcloud` 登入、拒絕未提交的 worktree、執行完整測試，
再以固定的 project、region、runtime service account 與 billing project 從 source
部署。新 revision 建立成功後會將流量恢復為 `LATEST=100%`，驗證正式
`/health` 並檢查新 revision 的 ERROR logs。若驗證失敗，腳本會停止並顯示
rollback 指令，不會自動回滾。

只在已人工確認差異時，才允許部署 dirty worktree：

```bash
scripts/deploy_poc.sh --allow-dirty
```

重大 OAuth／IAM 變更仍應改用 `--no-traffic` 與 revision tag 進行 preview。

Catalog 只接受 `SELECT`／`WITH` 單一查詢，且只能引用核准的 `ga4_mar` model。
日期使用 BigQuery parameters，project 與 dataset 由 registry 解析；每次 query 另有結果
筆數與 `SEMANTIC_MAX_BYTES_BILLED` 上限。`semantic/catalog.v1.json` 是產生物，請勿
手動修改。若來源定義未使用日期維度，查詢結果會標示
`date_scope=all_available_data`；對話回覆不得把這類數字描述成指定期間的結果。

## Authentication modes

服務支援兩種互斥模式，預設為既有模式：

### `cloud-run-iam`

Cloud Run IAM 是外層 authentication boundary，適合目前由本機 Claude Code 搭配 Google identity token 呼叫的方式。

```text
AUTH_MODE=cloud-run-iam
```

在這個模式下，應維持 Cloud Run private；應用程式不會另外要求 MCP OAuth token。

### `oauth`

供 claude.ai Custom Connector 使用的 PoC 模式。Cloud Run transport 必須允許 Anthropic 的 request 抵達服務，但 `/mcp/` 與 `/traffic-summary` 仍由應用程式驗證 bearer token，並非公開資料 API。

流程如下：

```text
Claude Web
  -> 本服務的 OAuth authorization endpoint
  -> Google OIDC 登入與 email allowlist
  -> 使用者 consent
  -> 本服務簽發 audience-bound MCP access token
  -> /mcp/ 或 /traffic-summary
```

Google token 只用來驗證登入者。本服務不接受 Google access token 作為 MCP bearer token，也不把 Google token 傳給 BigQuery。

必要設定請參考 [`.env.example`](.env.example)。Google client secret 與 RSA private key 都必須從部署環境或 Secret Manager 注入，不可 commit 到 Git。

Claude Custom Connector 使用預先註冊的 public client：

- Connector URL：`https://ga4-analytics-service-398991472921.asia-east1.run.app/mcp`
- Callback URL：`https://claude.ai/api/mcp/auth_callback`
- Scope：`ga4:read`
- Dynamic Client Registration：停用
- PKCE：S256
- Client ID：必填
- Client Secret：留白

服務同時提供 OAuth Authorization Server Metadata、OAuth Protected Resource Metadata 與 JWKS endpoint，供 MCP client discovery 與 token 驗證使用。

後續的跨專案 BigQuery 授權自動化與能力邊界規劃請見 [`ROADMAP.md`](ROADMAP.md)。

## PoC security boundary

這個內建 authorization server 刻意維持最小範圍，適合單一使用者、單一 Cloud Run instance 的 PoC：

- Google 帳號必須通過 `OAUTH_ALLOWED_EMAILS` allowlist。
- Authorization code 與 refresh token 都是一次性使用；refresh 時會 rotation。
- Access token 使用至少 2048-bit RSA key，以 RS256 簽署，並驗證 issuer、audience、expiration 與 `ga4:read` scope。
- OAuth login state、consent、authorization code 與 refresh token 暫存在 process memory。

因此 `AUTH_MODE=oauth` 部署時必須先維持 Cloud Run `max instances = 1`。instance restart 會使尚未完成的登入流程與既有 refresh token 失效，使用者需要重新連線。若要多 instance、持久 refresh token、完整撤銷與稽核，應把 authorization server 狀態移到持久 storage，或改用正式的獨立 authorization server。

## Local verification

安裝依賴後執行：

```bash
python -m unittest discover -s tests -v
```

測試涵蓋 OAuth metadata、Google OIDC callback stub、email allowlist、consent、PKCE、one-time authorization code、refresh-token rotation、MCP initialize / tools/list、REST bearer protection，以及 semantic catalog 的 profile、衝突、SQL 編譯與查詢保護；不會連線 BigQuery 或修改任何 GCP 資源。BigQuery schema 相容性另外由上方的 dry-run script 驗證。
