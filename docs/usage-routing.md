# Phase 11.5 雲端資源建立計畫（尚未 apply）

資料專案固定 `ga4-reports-dev`、地區 `asia-east1`，符合現有 Cloud Run 與 datasets 地區。
此分支只提供離線資源 manifest 與 dedup table functions，不能宣稱 routing／IAM／TTL 已完成。

## 授權與執行狀態

Owner 已核准本文件所列 `ga4_mcp_test_*`／`ga4-mcp-test-*` 資源建立及合成驗收，
並同意 BigQuery active TTL 後 2 天 time travel 與 7 天 fail-safe 例外。
不包含部署或啟用真實收集。

建立前重新盤點發現：`dev-dataform-workflow-executor@ga4-reports-dev.iam.gserviceaccount.com`
持有 project 級 `roles/bigquery.dataEditor`，包含 tables.getData／updateData／export／delete。
因此新 dataset 即使只有明確 owner ACL，此 Dataform pipeline SA 仍可讀寫原始事件與摘要。
這項 pipeline 存取尚待 owner 確認是否納入既有管理員／管理用 SA 例外；暫停建立資源，
未變更任何 cloud IAM、未建立資源或寫入合成事件，也不自行撤銷 Dataform 既有權限。

本次唯讀盤點另確認無同名 dataset／bucket／sink／secret；project／folder／organization
只有 _Default／_Required sinks，未發現 includeChildren 的額外上層複製路由。

## 可審閱計畫

```bash
.venv/bin/python scripts/plan_usage_routing.py \
  --owner-principal 'user:YOUR_WORKSPACE_EMAIL' \
  --output-dir /tmp/ga4-mcp-test-routing-plan
```

產出 `usage-routing-plan.json`、`events-dedup.sql`、`summary-dedup.sql`；不登入、不連線、不建立
任何雲端資源。真實 owner principal 僅存在產出的本機 plan，不提交 repository。

| 資源 | 名稱 | 保存／權限 |
| --- | --- | --- |
| BigQuery dataset | ga4_mcp_test_events | 180天partition TTL；明確owner ACL，不使用預設projectReaders／projectWriters |
| BigQuery dataset | ga4_mcp_test_summary | 30天partition TTL；摘要及export_errors都留此短期dataset |
| Logging buckets | ga4_mcp_test_events、ga4_mcp_test_summary | 180／30天，不鎖定以保留核准刪除能力 |
| Logging sinks | ga4-mcp-test-{events,summary}-{bq,bucket}-v1 | 4個filter限定logName、project、pilot、schema1.0、event_name；初始停用 |
| Runtime SA | ga4-analytics-service@ga4-reports-dev.iam.gserviceaccount.com | 新增project logging.logWriter、單一secret的secretAccessor；不授BigQuery寫入 |
| Sink writer | 建立BQ sink後取得writerIdentity | 只授對應dataset bigquery.dataEditor，不授project層級 |
| Secret Manager | ga4-mcp-test-identity-key | 32隨機bytes以base64保存；stdin寫入、不列印，不重用OAuth signing key、不自動輪替 |
| _Default sink exclusion | ga4-mcp-test-isolated-storage | 只排除兩個專用usage logs；保留既有所有exclusions與其他access／error logs |

原始資料直接存取限owner；已核准的上層GCP管理員與管理用SA繼承權限維持。
不新增一般同事、dashboard reader或pipeline SA；11.6再依具體job／views另給最小權限。
Ledger的量測起點與固定周年到期日在pilot啟用前建立，本計畫不預設永久保存或假造起點。

## Apply順序與保護

1. Owner先核准此計畫。重新唯讀盤點project／folder／organization sinks與IAM，保存原始設定
   供rollback；已有同名資源時比對，遇drift停下，不直接replace ACL或重生secret。
2. 建立兩個dataset與兩個bucket。Dataset default partition TTL與table TTL從建立時設定。
   非partitioned意外表仍有table TTL，但不能以它取代原event timestamp TTL驗證。
3. 建立四個停用sinks；BQ sink create API用 `uniqueWriterIdentity=true`，取回writerIdentity後
   加dataset級writer權限。所有IAM更新合併既有bindings並使用etag，避免覆蓋他人變更。
4. Bucket routing與IAM確認後，才將具名exclusion加至_Default。重新檢查上層aggregated
   sinks不會產生額外副本；遇新sink或廣域複製先停止。
5. 維持app兩個usage開關false，僅為合成驗證暫時啟用sinks。合成canonical／attachment
   使用新UUID、當下UTC時間、`labels.usage_validation=true`，不得查tenant data。
6. Logging首次事件決定匯出table schema。先驗證nullable STRING tenant ID、arrays、numeric
   欄位與top-level timestamp，再設定raw／export_errors為DAY timestamp partition、require
   partition filter及180／30天TTL。若export schema不符契約則停下修正，不以真實資料試錯。
7. 驗證後才建立dedup table functions。函數要求Asia/Taipei起訖日，最多180／30天，且排除
   synthetic事件。Nullable payload字段由JSON選取轉型，canonical永遠投影summary=null。
   Schema範例是logical payload，不拿來直接建立Logging envelope table。
8. 驗證完仍不部署或開真實收集。啟用需完成11.6／11.7、內部告知、成本與遺失延遲門檻。

## 必須實際驗收的項目

- 真實sink到達、dup/reorder、匿名拒絕、零列、null／string tenant IDs與schema mismatch error表。
- Writer可寫但runtime不能直接讀／寫usage BigQuery；未授權一般同事不得讀原始資料；
  既有管理員例外另列，不能宣稱dataset ACL可撤銷繼承權限。
- canonical／summary實際partition欄位為log entry timestamp，不是接收／匯入日期；重送不展延。
  第31天摘要不可查，過期重送不復活；正常表、export_errors、Logging buckets、匯出副本都驗證。
- BigQuery time travel設定最小48小時，另有平台fail-safe保護；Logging也有平台清理行為。
  **本manifest的180／30天只設定active partition TTL，仍可能有可復原的歷史副本。**
  這與ROADMAP「所有副本均同期限或更短」原文有差異，owner已明確核准此平台備援保留例外；
  真實收集仍須完成後續驗收，不以view隱藏代替實際保存政策。
- usage query與GA4 job共用billing project quota。驗收SQL先dry-run，查詢initial maximum_bytes_billed
  設100MB，每次只查合成事件的短窗口；這是初始工程限制，不代表已核准營運預算。
- API存取、Streaming、Logging retention與查詢可能新增費用，尚未量測成本。

## 回復

先保持／關閉app emission與summary，再停四個專用sinks。不要撤銷GA4原有IAM或改其他logs。
_Default exclusion在資料清理期間先保留，避免意外恢復把summary寫入共享bucket；只有確認
usage來源全停且不會重送，才移除本次新增exclusion，其他exclusions不變。停止sink不刪資料；
依核准TTL／刪除程序清理dataset、bucket、secret，刪除前再由owner確認。

## 官方依據

- [Logging匯出schema、timestamp分區與export_errors](https://docs.cloud.google.com/logging/docs/export/bigquery)
- [BigQuery partition expiration](https://docs.cloud.google.com/bigquery/docs/managing-partitioned-tables)
- [Logging bucket retention](https://docs.cloud.google.com/logging/docs/buckets)
- [BigQuery time travel與fail-safe](https://docs.cloud.google.com/bigquery/docs/time-travel)
- [Dataset API](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/datasets)

已對兩份SELECT函數本體，以inline合成JSON取代尚不存在的raw tables進行BigQuery dry-run，
皆驗證通過、預估0 bytes。這不驗證真實Logging envelope schema、CREATE TABLE FUNCTION、
實際去重結果或任何cloud acceptance；上述項目仍待授權建立資源後驗收。
