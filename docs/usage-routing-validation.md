# Phase 11.5 合成驗收紀錄（2026-09-21）

資源已依 owner 授權建立於 `ga4-reports-dev`／`asia-east1`。**尚未完成 11.5 acceptance，
四條 sinks 已停用、未部署、未啟用真實資料收集。** PR 維持 Draft，不應標為可合併。

## 已完成

| 項目 | 實測結果 |
| --- | --- |
| BQ datasets | `ga4_mcp_test_events`／`ga4_mcp_test_summary`，180／30 天 default partition TTL、48 小時 time travel |
| Logging buckets | 同上兩個名稱，180／30 天 retention，未鎖定 |
| BQ tables | `ga4_mcp_test_events.ga4_mcp_test_v1`、`ga4_mcp_test_summary.ga4_mcp_test_summary_v1`；由 exporter 建立 |
| Error tables | 兩個 dataset 各有 `export_errors`；刻意的合成 schema mismatch 各驗出 1 筆 |
| Partition | 上述 4 張表皆為原 LogEntry `timestamp` DAY partition、對應 TTL、requirePartitionFilter=true |
| Dedup functions | 兩個 dataset 各有 `deduplicated_v1`；正式函數皆回傳 0 筆合成資料，驗收資料不納入 KPI |
| 合成資料 | 字串 tenant ID、匿名拒絕的 null、成功零列、requested_days=7、latency_ms=1350、原 timestamp 均正確；fixture SELECT 按契約鍵去重 |
| Runtime SA | 只新增 project `logging.logWriter` 與單一 identity secret 的 accessor；未新增 BQ 權限 |
| Sink writer | Google 配置共用 `service-398991472921@gcp-sa-logging.iam.gserviceaccount.com`，只新增兩個 POC dataset 的 WRITER ACL |
| Secret | `ga4-mcp-test-identity-key` version 1；32 隨機 bytes 的 base64 字串在記憶體透過 API 寫入，未輸出或寫入本機 |
| 既有 IAM | 保留原 bindings；owner 已接受 Dataform pipeline SA 及既有管理員／管理用 SA 的繼承權限例外 |
| 回復 | 四條 `ga4-mcp-test-{events,summary}-{bq,bucket}-v1` 皆 disabled=true，保留 `_Default` 具名 exclusion |
| Cloud Run | 仍為既有 revision `ga4-analytics-service-00028-jq8`；沒有 USAGE_ENABLED／USAGE_SUMMARY_ENABLED 設定（預設 false），未部署 |

## 驗收發現與已修正問題

1. Logging 對同 project／timestamp／insertId 做去重，跨 log 名稱亦然。原本 canonical 與
   summary 都只用 UUID，可能讓其中一筆在查詢結果消失。Writer 改為 `UUID:event_name`，
   同事件重試仍維持相同 insertId；契約上的 interaction ID 不變。
2. Logging exporter 把 JSON 數字推斷為 FLOAT；JSON_VALUE 取得 `7.0`，直接 SAFE_CAST INT64
   會變 null。去重 SQL 改以 BIGNUMERIC 解析、TRUNC 檢查整數性後轉 INT64；拒絕小數／超界值。
   真實合成資料已驗證 requested_days=7、row_count=0、latency_ms=1350。
3. 更新 unique writerIdentity 的 sink 時，API 要保留 `uniqueWriterIdentity=true`。
   首次 update 被拒絕後修正參數成功；沒有變更 writer 身分或擴大角色。

## 未通過／待決策

### 過期重送與串流暫存

刻意直接送入 181 天前事件與 31 天前摘要（繞過應用程式已存在的 expiry guard）後，
Logging API 接受請求，BQ 原始表仍各可查到 1 筆過期合成資料，雖然 partition TTL 正確。
兩張表的 metadata 各有 estimatedRows=1 的 streamingBuffer。

這與 BigQuery 對舊日期串流資料先放入 `__UNPARTITIONED__`、稍後移入分區的行為一致；
官方對移出時間沒有 SLA。**不能宣稱 TTL 會在舊事件剛重送時就立即排除原始表中的資料。**
正式函數的時間條件及 application expiry guard 可阻擋一般分析／發送，但不能代替底層副本
驗收，也不等於 owner 已核准此 active streaming buffer 例外。

Owner 已核准的 2 天 time travel＋7 天 fail-safe 是資料到期後的復原副本；此處是仍可直接
查詢的串流暫存，屬不同例外。暫停後續實作與真實收集，待 owner 決定是否接受 POC 平台
清理延遲，或重新設計 ingestion／保存架構。此紀錄是當下快照，未經長期到期演練。

### _Default 排除規則傳播

具名 exclusion 於 08:01:35 UTC 建立；第一批 08:02:50 UTC 合成摘要仍在 `_Default` 留下
1 筆副本，後續修正 insertId 的測試紀錄沒有出現在該 bucket。觀察符合路由設定傳播尚未
完成的可能情況，但未以此推論所有新路由都已完全生效。未刪共用 bucket 或其他日誌。

該副本只有合成文字，沒有帳號／客戶資料；仍受既有 `_Default` 保存政策管制。真實收集
前必須增加「排除規則已實際生效」的合成驗收關卡，不能只看 API 回覆成功即開始發送。
現階段原始／錯誤／日誌驗收資料保留供審查，未自動清除或刪除資源。

### 權限正反向測試

設定檢查已完成，但目前 owner CLI 身分呼叫 runtime SA generateAccessToken 回傳 403，
因此未能以該 SA 實際執行讀寫拒絕驗收；也未代替一般同事登入測試。沒有自行新增
Token Creator 或 impersonation 權限。此兩項不得標示為通過，需在後續驗收安排執行身分。

## 實際指令與證據

本機操作／原始 API 設定快照位於 `/private/tmp/ga4-mcp-test-cloud-audit/`，不含 access token
或 secret 值。合成 fixture SQL 位於 `/private/tmp/ga4-mcp-test-routing-plan/`；不可把暫存
目錄當永久備份。本文件記錄關鍵結果，後续重跑應重新產生並審閱 fixture。

```bash
# 離線產生計畫與 SQL
.venv/bin/python scripts/plan_usage_routing.py \
  --owner-principal user:kenkuo@wenk-media.com \
  --output-dir /private/tmp/ga4-mcp-test-routing-plan

# 實際執行時使用 root workspace 的 .venv/bin/python
python /private/tmp/ga4-mcp-test-cloud-apply.py storage
python /private/tmp/ga4-mcp-test-cloud-apply.py sinks
python /private/tmp/ga4-mcp-test-cloud-apply.py permissions
python /private/tmp/ga4-mcp-test-cloud-apply.py secret
python /private/tmp/ga4-mcp-test-cloud-apply.py exclusion
python /private/tmp/ga4-mcp-test-cloud-apply.py enable-synthetic
python /private/tmp/ga4-mcp-test-cloud-validate.py seed
python /private/tmp/ga4-mcp-test-cloud-validate.py inspect
python /private/tmp/ga4-mcp-test-cloud-validate.py lock-tables
python /private/tmp/ga4-mcp-test-cloud-validate.py cases
python /private/tmp/ga4-mcp-test-cloud-apply.py disable

# 每份 SQL 都先 jobs.insert dryRun，再 jobs.query；maximumBytesBilled=100000000
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/events-dedup.sql --execute
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/summary-dedup.sql --execute
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/events-synthetic-dedup-check.sql --execute
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/function-exclusion-check.sql --execute
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/expired-storage-check.sql --execute
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/error-storage-check.sql --execute

.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m compileall -q usage_logging.py scripts/plan_usage_routing.py tests/test_usage_logging.py tests/test_usage_routing_plan.py
git diff --check
```

上述完整測試 216 項通過；修正後獨立 reviewer 重跑 logging 10 項與 routing 5 項，無 P0／P1。
TTL acceptance 仍未通過。查詢回報 totalBytesBilled=0（當時資料在串流區），這不代表 Logging、
Secret Manager 或後續查詢免費，也不能當營運成本估算。

官方依據：
[LogEntry insertId 去重](https://docs.cloud.google.com/logging/docs/reference/v2/rest/v2/LogEntry)、
[BigQuery 串流的分區處理](https://docs.cloud.google.com/bigquery/docs/write-api-rest#time-unit_column_partitioning)。
