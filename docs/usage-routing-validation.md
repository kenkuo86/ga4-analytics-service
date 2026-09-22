# Phase 11.5 合成驗收紀錄（2026-09-21–22）

資源已依 owner 授權建立於 `ga4-reports-dev`／`asia-east1`。**四條 sinks 已停用、未部署、
未啟用真實資料收集。**

驗收標準依 ROADMAP「Reliability, rollout and validation」第 1、4 點訂定：Cloud Logging
不承諾 exactly-once，因此本階段**量測並揭露到達率**，而非宣稱零遺失。Owner 核定的
rollout 門檻為 **canonical 事件送達 BigQuery ≥ 95%，以 Wilson 95% 信賴下界判定**；
summary 附件只揭露、不設門檻（沒有任何 KPI 計數依賴它）。判定由
`scripts/probe_usage_routing.py` 的 `gate_check` 產出，不靠人工敘述。

## 2026-09-22 系統性 review 修正（最新）

前兩輪修正逐一處理目的地輪詢與退出碼後，第三輪 review 顯示仍有兩個共同根因：
可選的 Monitoring counter 會中止已完成的權威查核；主流程例外與 sink cleanup 例外
同時發生時，`finally` 會重新拋出原例外而遮蔽 cleanup 狀態。本輪改為集中處理終端狀態：

- Logging／BigQuery exact-key 結果是權威證據；Monitoring `timeSeries` 是輔助證據。
  Monitoring 查詢或解析失敗時記錄 `status=unavailable` 與錯誤型別，保留 gate 結果，
  不因輔助指標不可用而中止驗收。
- `final_exit_code` 統一所有執行路徑的優先序：cleanup 未確認為 disabled 回傳 3；
  主流程失敗回傳 2；只有完整結果才進入 delivery gate。主流程與 cleanup 同時失敗時，
  以 `cleanup_failed`／3 為最終結果，並保存 `probe-final-status.json`。
- `cleanup_sinks` 獨立保存每次嘗試與最終狀態；不再依賴 `finally` 後仍能執行的旗標。
- `docs/usage-routing-resource-evidence.md` 固定去識別化的 raw table schema／TTL 證據。
  labels 以 nullable RECORD 欄位參照；plan 移除不必要的 `defaultTableExpirationMs`，
  dedup／probe SQL 若 schema drift 會顯式失敗，不靜默放行 validation probe。

新增主流程成功／失敗 × cleanup 成功／失敗、Monitoring 不可用、schema shape contract
與 cleanup evidence 的測試。routing **56** 項、完整 regression **267** 項全部通過，
compileall 與 diff check 通過。新版雲端驗收未執行；等待下一輪獨立 review。

## 2026-09-22 第二輪 review 修正

針對 `eb7b6a4` 的兩項新 P1：

- 正式樣本每次輪詢同時逐鍵核對 Logging buckets 與兩張 BQ 原始表，只有四個目的地
  都完整到達且隔離成立才提前結束；BQ 延遲時使用剩餘 `--polls` 次數繼續等待。
  到輪詢上限仍缺失，才以最後一輪觀測結果判定門檻；不宣稱缺失是永久遺失。
  每輪另存 `probe-delivery-poll-N.json`，保留 BQ 延遲演進與查核完整性。
- 正常量測與 `--reconcile-acks` 共用退出碼判定：隔離失敗為 1，查核不完整、未量測或
  canonical 未達 Wilson 門檻為 2；只有完整、隔離成立且 canonical 通過才為 0。
  未完成 job 或有 pageToken 的 BQ 結果不計 gate，標示 NOT_MEASURED；清理失敗仍為 3。
  Summary 缺漏仍只揭露，不另設門檻；因此等待輪詢上限後，完整觀測的 summary 缺漏
  不會單獨令 canonical 門檻失敗。`--reconcile-acks` 僅重新讀取 BQ，不驗證 Logging 隔離。

新增 7 項離線測試，涵蓋兩類 BQ 延遲、輪詢上限、summary 缺失的既有政策、BQ／Logging
查核不完整，以及 reconcile CLI 的成功／缺失／未完成／分頁／隔離失敗／空樣本退出碼。
沿用下列本機指令驗收：routing 53 項、完整 regression 264 項全部通過，compileall 與
diff check 通過。雲端重跑未執行，不修改 IAM、sinks 或部署，等待新一輪 Codex review。

## 2026-09-22 第一輪 review 修正紀錄

本輪修正三項 P1：readiness 必須同時確認兩個專用 Logging buckets、兩張 BigQuery
原始表逐鍵到達，且 `_Default` 無副本；啟用 sinks 前即建立清理責任，部分 PATCH
失敗也會清理；三次停用後仍無法確認四條 sinks 全部 disabled 時回傳 exit code 3。
BigQuery job 未完成或仍有 pageToken 的讀取不作為完整 readiness 證據。

新增 `tests/test_usage_routing_runner.py`，以離線故障注入覆蓋上述分支、BQ 延遲／缺失、
不完整結果、隔離失敗、停用重試恢復與缺失狀態。沒有認證、雲端寫入、IAM 修改或部署。

**以下 300/300 與 PASS 是修正前歷史量測，不是本輪新版 readiness 的雲端驗收。**
舊 readiness 只確認 Logging buckets，不能據此宣稱已先確認四個目的地；新版雲端
重跑尚未執行，先等待新一輪 Codex review，不將 PR 宣告可合併。

原先三則 vendor/schema review 意見沒有直接當成程式錯誤，而是在本輪轉成可重複的
repository contract：移除 `defaultTableExpirationMs`、固定 nullable RECORD 欄位參照，
並把去識別化 metadata 放入 `docs/usage-routing-resource-evidence.md`。官方依據為
[BigQuery Dataset API](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/datasets)。
暫存快照不視為永久證據，下一次 apply 前仍須重新查核 schema／TTL。

本輪本機驗收指令（於 routing worktree 執行；共用主 workspace 的 virtualenv）：

```bash
/Users/guoqian/Desktop/ga4-analytics-service/.venv/bin/python -m unittest discover -s tests -p 'test_usage_routing*.py' -v
/Users/guoqian/Desktop/ga4-analytics-service/.venv/bin/python -m unittest discover -s tests -v
/Users/guoqian/Desktop/ga4-analytics-service/.venv/bin/python -m compileall -q scripts/ tests/
git diff --check
```

本輪結果：routing 46 項、完整 regression 257 項全部通過；compileall 與 diff check 通過。
Repository 未提供獨立 lint／type check／build 設定。新版雲端驗收未執行；獨立複審待 Codex review。

## 已完成（歷史雲端驗收）

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

## 2026-09-22 交付探針與到達率量測

驗收改由 repository 內的工具執行，取代先前 `/private/tmp` 的一次性腳本（那些腳本已於
本文件標示不可原樣重跑）：

- `scripts/probe_usage_routing.py`：純邏輯，永不連網。寫入契約、filter 建構、完整分頁
  狀態機、逐鍵 multiset 比對、Wilson 區間、readiness 判定、匯出交叉比對與 `gate_check`。
- `scripts/run_usage_routing_probe.py`：只搬移位元組與保存證據。不讀寫 IAM、不部署、
  不啟用真實收集、不輸出憑證；啟用的 sinks 一律在 `finally` 以新憑證重試停用。
- `tests/test_usage_routing_probe.py`：其中一項測試直接比對 `usage_logging.LoggingWriter`
  實際送出的 body 與探針 body，除兩個合成標籤外必須完全相同，防止驗收形狀與 production 漂移。

探針為 production 形狀：**一次 `entries:write` 只送一筆**，與 runtime writer 相同。
兩個刻意差異都是合成標記：`usage_validation=true` 讓去重 views 排除驗收資料；
`usage_probe_run=<uuid>` 讓單一 run 以一個 filter 條件選出。canary 使用**獨立的 run id**，
因此量測 filter 不可能看到 canary。

### 路由生效不是等固定秒數

先前所有輪次都在 sink 啟用後等固定秒數就開始寫入。實測證明這是錯的閘門：

| 輪次 | 等待設定 | canary 結果 |
| --- | --- | --- |
| 2026-09-22 第一次 readiness 版 | 240 秒 | **第 0 次 canary 在再等約 120 秒後仍未送達**；第 1 次才確認生效 |
| 2026-09-22 第二次 readiness 版 | 240 秒 | 第 0 次即確認生效 |

也就是 sink 啟用後真正生效的時間**是變動的，且可超過 300 秒**。在傳播窗口內寫入的
entry 會被靜默丟棄，因此任何跨越該窗口取得的遺失率都受污染，不得作為穩態數據。
本文件先前「等 180 秒後即可發送」的作法已作廢；`await_routing_ready` 改為寫入 canary
並確認四個目的地逐鍵到達後才開始量測，未確認則拒絕量測（不產出數字）。

### 遺失機制：兩種都存在

`exports/error_count` 在所有輪次**全程為空**，平台不回報任何匯出錯誤。實測到兩種丟棄：

1. **單一 sink 獨立丟棄（export 端）**：owner 輪有一筆 summary 進入 BigQuery，卻不在
   Logging bucket。兩條 summary sink 的 filter 完全相同，匯出計數器仍分別是 49 與 48。
2. **所有 sink 一起丟棄（ingestion 端）**：readiness 版有 3 筆在四個目的地皆不存在，
   bucket 與 BigQuery 缺的是同一組鍵。

直接後果：**Logging bucket 的副本不能用來稽核 BigQuery 的副本，反之亦然**。KPI views 讀
BigQuery，因此權威來源明定為 BigQuery，驗收必須對 BigQuery 逐鍵比對；探針早期只比對
bucket，屬量錯目的地，已修正。

### 量測結果

量測用三種互相獨立的方法互相佐證：Logging bucket 逐鍵、BigQuery 逐鍵、
`exports/log_entry_count` 計數器。前兩者是權威來源；計數器只作佐證。

**計數器有顯著且緩慢收斂的 ingestion 延遲**：n=300 那輪在寫入結束後約 1 分鐘查得 169，
其後重查依序為 198、224，仍未達實際的 301，而同一批資料的逐鍵比對早已確認 600/600 到達。
放寬查詢窗口不改變結果，確認是延遲而非窗口設定。因此計數器低於預期時，只有在窗口已沉澱
（預設 900 秒）後才算短少，否則回報 `inconclusive`；`match` 與 `excess` 則隨時有意義。
先前幾輪計數器能完全吻合，是因為那些輪次在查指標前已跑了 8–13 分鐘。
**計數器永遠不得推翻逐鍵比對的結論。**

| 輪次 | 條件 | 寫入接受 | canonical → BQ | summary → BQ | `_Default` |
| --- | --- | --- | --- | --- | --- |
| Owner | 固定 180 秒，1.0s 間隔，n=50 | 100/100 | 50/50 | 49/50 | 0，隔離成立 |
| Runtime SA | 固定 180 秒，0.5s 間隔，n=50 | 100/100 | 39/50 | 42/50 | 0，隔離成立 |
| Readiness 閘門 | canary 確認生效，1.0s 間隔，n=75 | 150/150 | 74/75 | 73/75 | 0，隔離成立 |
| **Readiness 閘門（定案）** | canary 確認生效，1.0s 間隔，**n=300** | 600/600 | **300/300** | **300/300** | 0，隔離成立 |

### Rollout 門檻判定

以 n=300 這輪為準（`gate_check` 產出，非人工敘述）：

| 項目 | 值 |
| --- | --- |
| 來源 | `ga4-reports-dev.ga4_mcp_test_events.ga4_mcp_test_v1` |
| canonical 應到／實到 | 300 / 300 |
| 送達率 | 1.000 |
| 送達率 95% 信賴下界 | **0.9874** |
| 門檻 | 0.95 |
| 判定 | **PASS** |

兩輪 readiness 閘門合計 canonical 374/375（99.73%），信賴下界仍高於門檻。零遺失**不**代表
平台保證不遺失——n=75 那輪就掉了 3 筆——只代表在路由確認生效後，遺失率低到本樣本量測不到。
依 ROADMAP 規定仍不得宣稱 exactly-once。

前兩輪跨越傳播窗口，**不得作為穩態遺失率**：兩輪同時差了寫入者身分、寫入間隔與各自一次
sink 冷啟動，屬混淆實驗，無法歸因；兩輪的遺失也都集中在最前面的寫入。列出僅為完整揭露。

Runtime SA 的結果另有一項獨立價值：**runtime service account 確實可用 `logging.logWriter`
寫入兩個正式 log 並完成路由**（100 筆全部 HTTP 200，39／42 筆確認到達），這是 11.2 身分
前置在真實雲端的正向驗證。其偏低的到達率歸因於冷啟動與混淆，不作為 SA 身分的缺陷結論。

### 驗收期間在探針本身發現並修正的缺陷

這些缺陷都出在驗收工具、不在服務程式，但其中兩項若未發現會直接產出錯誤結論，因此列入紀錄：

| 缺陷 | 後果 | 修正 |
| --- | --- | --- |
| 以「嘗試寫入」為分母 | token 過期導致 33 筆 401 後，仍把未送出的 entry 算成應到未到，會產出看似合理的約 40% 假遺失率 | 分母改為已被接受的寫入；有拒絕時明確警告並排除 |
| owner token 不更新 | readiness 閘門拉長執行時間後，中途起全部寫入變 401 | 定時刷新並於 401 重試；impersonated token 刻意不刷新，短效期即是目的 |
| 清理路徑共用同一過期 token | `finally` 停用 sinks 失敗，**四條 sink 被留在啟用狀態** | 清理前強制換發憑證、重試三次、仍失敗則輸出 CRITICAL |
| 匯出指標缺值視為 0 | Monitoring 有數分鐘 ingestion 延遲，缺值被報成「零匯出」，與先前分頁未讀完報成 0 屬同一類錯誤 | 缺值回報 `unavailable`，永不等同 0 |
| canary 未計入匯出預期值 | canary 的匯出落在指標窗口內，造成匯出交叉比對假性短少 | 預期值納入 canary；canary 另用獨立 run id，量測 filter 看不到它 |
| 以逐一列舉 interaction id 建 filter | 約數百筆樣本即逼近 Logging filter 長度上限，會在樣本大到有意義時才失效 | 改以單一 `usage_probe_run` 標籤選取，filter 長度與樣本無關並有測試把關 |

前三項於實際執行中發生並已實測修復，其餘由測試覆蓋。`finally` 失效那次的四條 sink 已於
發現當下以新憑證手動停用並確認 `disabled=true`。

## 實際指令與證據

2026-09-22 到達率量測的證據位於 `/private/tmp/ga4-mcp-test-probe-*-20260922/`（每輪的寫入
ack、逐頁原始回覆、BigQuery dry-run 與結果、匯出指標、sink 開關狀態、readiness 紀錄），
不含 access token 或 secret 值。重跑指令：

```bash
# 離線：只產生 manifest，不認證、不變更
.venv/bin/python scripts/probe_usage_routing.py --output-dir DIR --probes 300

# 雲端：需 --apply；結束一律停用 sinks
.venv/bin/python scripts/run_usage_routing_probe.py --output-dir DIR   --fixture DIR/fixture.json --probes 300 --apply --enable-sinks   --spacing-seconds 1.0 --settle-seconds 240 --poll-seconds 60 --polls 4

# 只對既有 ack 重新比對 BigQuery，不寫入任何新資料
.venv/bin/python scripts/run_usage_routing_probe.py --apply --output-dir DIR   --reconcile-acks DIR/probe-write-acks.json --run-id <run>   --window-lower <RFC3339> --window-upper <RFC3339>
```

結束碼：0 通過門檻，1 隔離失敗，2 查核不完整／未量測／未達門檻，3 無法確認 sinks 全部停用。
其他執行例外以非零碼結束；即使量測通過，清理失敗也不得回傳 0。

先前輪次的本機操作／原始 API 設定快照位於 `/private/tmp/ga4-mcp-test-cloud-audit/`，不含
access token 或 secret 值。合成 fixture SQL 位於 `/private/tmp/ga4-mcp-test-routing-plan/`；不可把暫存
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
