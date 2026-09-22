# Phase 11.5 合成驗收紀錄（2026-09-21–22）

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

## 已核准例外與後續驗收

### 過期重送與串流暫存

刻意直接送入 181 天前事件與 31 天前摘要（繞過應用程式已存在的 expiry guard）後，
Logging API 接受請求，BQ 原始表仍各可查到 1 筆過期合成資料，雖然 partition TTL 正確。
兩張表的 metadata 各有 estimatedRows=1 的 streamingBuffer。

這與 BigQuery 對舊日期串流資料先放入 `__UNPARTITIONED__`、稍後移入分區的行為一致；
官方對移出時間沒有 SLA。**不能宣稱 TTL 會在舊事件剛重送時就立即排除原始表中的資料。**
正式函數的時間條件及 application expiry guard 可阻擋一般分析／發送，但不能代替底層副本
驗收。Owner 在了解實際情境後，已明確核准此 POC active streaming buffer 清理延遲例外。

Owner 已核准的 2 天 time travel＋7 天 fail-safe 是資料到期後的復原副本；此處是仍可直接
查詢的串流暫存，屬不同且已另行核准的 POC 例外。繼續保持 application expiry guard、
函數時間範圍與原始 TTL，不藉例外做長期複製。後續追蹤查詢確認兩張原始表與兩張錯誤表
的過期合成資料均為 0；這只證明本次已清除，不代表平台有固定清理期限或已做長期到期演練。

### _Default 排除規則傳播

具名 exclusion 於 08:01:35 UTC 建立；第一批 08:02:50 UTC 合成摘要仍在 `_Default` 留下
1 筆副本。後續的零筆觀察原先未驗證分頁完整，須依下方重新查核結果判讀。觀察符合路由設定傳播尚未
完成的可能情況，但未以此推論所有新路由都已完全生效。未刪共用 bucket 或其他日誌。

該副本只有合成文字，沒有帳號／客戶資料；仍受既有 `_Default` 保存政策管制。真實收集
前必須增加「排除規則已實際生效」的合成驗收關卡，不能只看 API 回覆成功即開始發送。
現階段原始／錯誤／日誌驗收資料保留供審查，未自動清除或刪除資源。

後續 readiness probe 已實測通過：同一新 interaction ID 在兩個專用 Logging bucket
各 1 筆，在 `_Default` 為 0 筆；同組事件在兩張 BQ 原始表亦各 1 筆，
該查詢預估處理 312 bytes、實際計費 20 MiB（BQ 最低計費單位），低於 100 MB 上限。
驗收結束四條 sinks 均已停用。前一輪在啟用後立即發出的
probe 未確認到達，未將其列為通過。未來啟用前須使用有次數上限的合成 probe 確認路由及排除
規則真正生效，而非只等待固定秒數。

2026-09-22 已用完整分頁重查上述 `c0b91a78-92e6-4c3f-826a-75662b72514c`：
六個查詢均無後續頁，兩個專用 bucket 各 1、`_Default` 0，確認該次結果有效。

### 權限正反向測試

Owner 已明確核准以下限時 impersonation 驗收。以 runtime SA 執行確認：兩張原始表
`tabledata.list` 均回 403，`tables.testIamPermissions` 的 getData／updateData 均為空；
identity secret 的 `secretmanager.versions.access` 存在，未讀取金鑰內容。
未代替一般員工登入，該反例仍須在 pilot 安排，不能以 SA 測試代替全體員工的有效權限。

#### 已核准的 runtime SA 驗收範圍

- 在單一 `ga4-analytics-service@ga4-reports-dev.iam.gserviceaccount.com` 的 IAM policy
  暫加 `user:kenkuo@wenk-media.com` 的 `roles/iam.serviceAccountTokenCreator`。不在
  project 層授權，不修改 SA 的既有 BQ／Secret 權限。
- Binding 加 `request.time < timestamp(...)` 條件，截止為實際開始後 1 小時；保留
  原始 policy、合併既有 bindings，使用 etag。此權限允許以該 SA 身分操作其既有資源，
  本次只用於以下 POC 驗收，不查真實 tenant data。
- 只 mint 最長 5 分鐘的短期 access token，存在記憶體，不建立 service account key、
  不寫 ADC、不輸出 token、不把 token 放入 shell argument。
- 使用該 SA 寫 1 組標記 synthetic 的 Logging 事件／摘要；owner 身分驗證專用目的地
  到達與 `_Default` 排除。直接讀兩張 POC BQ 原始表應回 403；對兩張原始表的
  write 權限與單一 secret 的 access 權限使用 testIamPermissions 驗證，不讀 secret 值。
- 不以預期拒絕為理由執行刪除或授權變更測試。不代替一般員工登入，也不把 SA 的
  驗收當成全體員工權限驗收；一般員工反例仍於 pilot 安排。
- 成功或失敗都停用測試 sinks，重新讀 IAM policy、僅移除本次 conditional binding，
  保留他人同期變更；確認已移除。移除 binding 不使已發 token 立即失效，token 最遲
  於發出後 5 分鐘到期。即使過程中斷，binding 的 1 小時期限也不自動展延。

此新增 impersonation 授權已獲 owner 明確同意。首次有效 conditional binding 截止為
2026-09-21T16:22:43Z；後續重試使用同一截止，不自動展延。取得 5 分鐘 token 後即先
移除本次 binding，再執行權限檢查。沒有新增 service account key、改 ADC 或資料角色。

初次工具呼叫因 getIamPolicy HTTP 方法錯誤而失敗，當時尚未新增授權；改用官方 POST
後才執行授權。第一輪 BQ／Secret 權限檢查通過，Logging 寫入 API 成功，但短時間內
未完整確認事件／摘要到達；後續唯讀僅查到主事件，未將整輪到達驗收標為通過。
四條 sinks 與臨時 binding 均清理後，第二輪改成先以 owner canary 驗證 routing readiness，
通過才 mint SA token；第二輪 readiness 未通過，因此未再次授權或 mint token。
兩輪均完成 sinks 停用與本次 binding 清理；原授權期限已過，不自動重新授權。

### 2026-09-22 完整分頁重查

獨立 review 發現 P1 驗收缺陷：舊臨時腳本只讀 `entries.list` 第一頁，未處理
`nextPageToken`，可能把尚未搜尋完判成 0。舊零筆觀察不能直接證明未到達或已隔離。
修正後查詢指定單一 logName、事件前後數分鐘、`timestamp desc`，逐頁保存原始回覆，
直到沒有 token 才判定完成；超過 30 頁則標示未完成，不得作為零筆通過證據。
舊 `runtime-iam.py`／`readiness.py`／`cloud-final-check.py` 不應原樣重跑。

以 owner 唯讀重查原 runtime SA 的四個 interaction ID，六個 bucket/logName 查詢
均在第一頁完整結束。只有 `c2f927a5-cbfa-4519-9f1b-217f8f448367` 的 canonical 在
專用 Logging bucket 可查到 1 筆；四組 summary 均未查到；`_Default` 均為 0。
BQ 兩張原始表查到上述 canonical 及 `350e74d6-1046-49e8-8ee2-f6f0915e3e44`
的 summary 各 1 筆，其餘未找到。查詢預估 624 bytes，實際計費 20 MiB。
這證明 runtime SA 曾成功送達兩類 BQ 資料，但沒有同 UUID 完整到達所有目的地的證據；
不能宣稱 end-to-end acceptance 通過，也不能把缺漏全部歸因於分頁或權限。

### Owner 受控批次與下一輪驗收邊界

2026-09-22 以原 owner 權限啟用四條 sinks，確認 API enabled 後等待 180 秒，再送兩批、
共四組 canonical＋summary（共 8 筆）。每批同時測試 `UUID:event_name` 與獨立 insertId，
原始 write body／成功回覆皆保存；不新增 IAM、不 mint SA token。查詢逐 logName 完整分頁，
最多八輪，觀察時不重送、不切換 sinks。逐筆比對預期與實際
`(interaction_id, event_name, logName, insertId)` multiset，不能只用總數判為通過。

八輪 Logging 查詢結束後，逐鍵核對只找到第一批兩組，第二批兩組未找到：
每個專用 bucket 預期 4、實際 2，`_Default` 0；整輪判定未通過。
BQ 最後查詢亦僅第一批兩組各 canonical／summary，共 4 筆。
不能用這個小樣本推算正式遺失率，也尚不能認定永久遺失或平台故障。

舊授權限定原始一小時且不自動展延。若重新驗證 runtime 的完整同 UUID 路由，須先由
owner 核准新一輪同範圍的一小時 conditional binding／最長五分鐘 token；只對同一 SA
授 Token Creator，取得 token 後立即撤除，owner 負責後續唯讀到達查核。
先通過 owner readiness 才授權，未通過則停止；不改 BQ／Secret 資料角色，不部署或啟用
真實收集。新一輪授權尚未取得、尚未執行。

## 實際指令與證據

本機操作／原始 API 設定快照位於 `/private/tmp/ga4-mcp-test-cloud-audit/`，不含 access token
或 secret 值。合成 fixture SQL 位於 `/private/tmp/ga4-mcp-test-routing-plan/`；不可把暫存
目錄當永久備份。本文件記錄關鍵結果，後續重跑應重新產生並審閱 fixture。

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
python /private/tmp/ga4-mcp-test-runtime-iam.py
# 下列為修正完整分頁後的唯讀重查／owner 合成測試
python /private/tmp/ga4-mcp-test-paginated-recheck.py
python /private/tmp/ga4-mcp-test-readiness-recheck.py
python /private/tmp/ga4-mcp-test-delivery-diagnostic.py
python /private/tmp/ga4-mcp-test-check-delivery-evidence.py
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/delivery-bq-check.sql --execute
python /private/tmp/ga4-mcp-test-cloud-query.py /private/tmp/ga4-mcp-test-routing-plan/runtime-bq-recheck.sql --execute

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

2026-09-22 再次完整測試 216 項通過；完整分頁與逐鍵驗收修正經獨立 reviewer
複核，無剩餘 P0／P1；路由尚未通過仍維持 Draft。
上述程式修正後獨立 reviewer 重跑 logging 10 項與 routing 5 項，無 P0／P1。
嚴格立即清理不成立，依 owner 已核准的 POC 例外揭露；Runtime BQ 拒絕／Secret 權限檢查已通過；完整路由及一般員工驗收尚未通過。查詢回報 totalBytesBilled=0（當時資料在串流區），這不代表 Logging、
Secret Manager 或後續查詢免費，也不能當營運成本估算。

官方依據：
[Logging entries.list 分頁](https://docs.cloud.google.com/logging/docs/reference/v2/rest/v2/entries/list)、
[LogEntry insertId 去重](https://docs.cloud.google.com/logging/docs/reference/v2/rest/v2/LogEntry)、
[BigQuery 串流的分區處理](https://docs.cloud.google.com/bigquery/docs/write-api-rest#time-unit_column_partitioning)。
