# GA4 Analytics Service / MCP Server

這個 PoC 將既有 GA4 Analytics Service 同時暴露為：

- MCP Streamable HTTP endpoint：`/mcp`（同時保留 `/mcp/` 相容路由）
- REST endpoint：`/traffic-summary`

服務提供 `customer_lookup` 與 `traffic_summary`。使用者以 tenant registry 中的正式客戶名稱查詢，不需要知道內部 `tenant_id`。`customer_lookup` 只確認 registry 狀態，不依賴客戶 GA4 dataset 權限；`traffic_summary` 保留固定 SQL 與 BigQuery Service Account 權限邊界。LLM 不會直接產生或執行任意 SQL。

例如：

```text
告訴我維肯媒體部落格上週的流量摘要。
```

名稱解析會忽略前後空白、英文大小寫與 Unicode 相容字元差異，但不會模糊猜測其他客戶。找不到、尚未 active 或名稱重複時，tool 會回傳明確的結構化狀態。

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

測試涵蓋 OAuth metadata、Google OIDC callback stub、email allowlist、consent、PKCE、one-time authorization code、refresh-token rotation、MCP initialize / tools/list，以及 REST bearer protection；不會連線 BigQuery 或修改任何 GCP 資源。
