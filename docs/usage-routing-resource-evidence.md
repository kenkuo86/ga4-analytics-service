# Phase 11.5 資源 schema／TTL 證據摘要

本摘要是 2026-09-22 owner 授權的唯讀盤點結果，供 routing plan、dedup SQL 與 review
使用。它不含 project、tenant、帳號 token、secret 或客戶資料；不取代下一次雲端 apply
前的即時盤點。

## BigQuery raw tables

| 資源 | `labels` schema | 分區 | active TTL | table expiration |
| --- | --- | --- | --- | --- |
| `ga4_mcp_test_events.ga4_mcp_test_v1` | nullable `RECORD`: `usage_validation`, `usage_schema`, `usage_environment` | `timestamp`, DAY；要求 partition filter | 180 天 | 未設定有限期限 |
| `ga4_mcp_test_summary.ga4_mcp_test_summary_v1` | nullable `RECORD`: `usage_schema`, `usage_environment`, `usage_validation` | `timestamp`, DAY；要求 partition filter | 30 天 | 未設定有限期限 |

兩張 raw table 的匯出 schema 使用 nullable RECORD 欄位，因此查核與去重 SQL 必須使用
`labels.usage_probe_run`／`labels.usage_validation` 的欄位參照。若下一次 schema 盤點發現
欄位變成 repeated key/value 或其他形狀，SQL 必須先停用並更新 schema contract；不能讓
欄位路徑失配後把所有 probe 當成正式資料或把所有資料判成遺失。

`defaultPartitionExpirationMs` 是連續使用 raw table 的保存控制。計畫不再設定
`defaultTableExpirationMs`，避免把 table lifetime 與 partition retention 混在一起。
BigQuery Dataset API 也明確說明：設定 default partition expiration 時，partitioned table
不繼承 default table expiration；詳見
[BigQuery Dataset REST resource](https://docs.cloud.google.com/bigquery/docs/reference/rest/v2/datasets)
的 `defaultTableExpirationMs`／`defaultPartitionExpirationMs` 欄位說明。

## 證據來源與重驗規則

上述欄位來自 owner 授權的 BigQuery table metadata／schema inspection；原始輸出曾保存
於本機暫存目錄，沒有將任何 access token 或 secret 提交至 repository。每次重新建立或
更新 exporter-created table 時，必須重新讀取 metadata，逐項確認：

- `labels` 的 mode、type 與子欄位名稱。
- raw 與 `export_errors` table 的 partition field、DAY、TTL 與 `requirePartitionFilter`。
- table 本身沒有有限 creation-time expiration。
- dedup／probe SQL 的欄位參照與當次 schema 完全一致。

metadata 未完成或 schema 不一致時，routing readiness 為未量測，不得以歷史 probe 數字
作為通過證據。
