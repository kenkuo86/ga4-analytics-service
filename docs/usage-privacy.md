# Phase 11 使用分析契約與已確認政策

本文件記錄 repository owner 於 2026-09-18 在實作對話確認的政策。
11.1 僅提供可執行 contract、schema 與合成案例；尚未開啟收集或部署資源。

## 身分、用途與存取

- 用途為內部 GA Analytics 產品採用、回訪及需求分析，不作員工績效評估。
- 資料負責人為 repository owner 郭謙；首次 rollout 的原始事件及摘要檢閱限 owner。
  部門彙總建議另以受限 view 提供，群組獨立人數少於 5 時隱藏細分數值；
  此呈現門檻在 11.6 發布前由 owner 確認，目前不對部門開放報表。
  管理員固有權限仍須於部署前盤點，不能宣稱 dataset IAM 可覆蓋專案繼承權限。
- Owner 已確認 OAuth allowlist 內所有同事可讀同一集合的全部 active tenants。
  政策代碼 `department-active-tenants-v1`；不是對未登入者或任意 Google Workspace
  帳號開放。保留既有 allowlist、scope、active 狀態及名稱解析邊界。
- 使用經驗證 Google OIDC subject 與 issuer namespace，透過專用 HMAC key 產生
  64 字元十六進位 user ID；不保存原始 subject、email 或 token。
  Google Workspace 使用習慣不取代服務端驗證；不新增只靠 email domain 的授權。
- MCP／REST OAuth 身分須安全傳入 request context。無可信終端身分的 IAM 呼叫
  只記 `user_id=null`、identity unavailable，不算獨立人數或留存。
- Host 是受管理 client mapping 的產品屬性，不能授權 tenant；無法確認時為 other。
  Token refresh 不換 user ID。Key 不自動輪替；必要輪替若不能保持連續性，啟用新
  measurement version、標示歷史斷裂，不保存原始身分來繞過限制。

## 保存與刪除

| 資料 | 保存上限 | 隔離方式 |
| --- | --- | --- |
| Canonical 結構化事件 | 180 天，以原事件時間起算 | 專用 log／BigQuery dataset |
| 已清理摘要附件 | 30 天，以相同原事件時間起算 | 獨立 log、bucket、dataset；不進長期副本 |
| Activation ledger | 量測起點後一個曆年 | 獨立 table，到固定期限清除，不隨回訪展延 |

使用資料與衍生資料存放在 `ga4-reports-dev`。Ledger 僅含 pseudonymous user_id、
first_success_at、measurement_version；例如起點為 2026-10-01T00:00:00Z，
到期為 2027-10-01T00:00:00Z。閏年 2 月 29 日的周年採次年 2 月 28 日。
實際 measurement 起點於 pilot 啟用時設定，未啟用不得假造開始日期。
到期預設清除；延長必須先經 owner 重新核准及更新告知。

刪除要求由 owner 受理，建議 7 天內完成可查詢資料與衍生副本清理並記錄驗證結果；
此作業期限須在 11.5／ledger 發布前由 owner 確認。
不建立未核准 user tombstone。Google Cloud 底層 time travel、fail-safe 與 Logging
到期後儲存行為需在 11.5 盤點，不能將不可查詢等同物理立即抹除。
Ledger 刪除、到期、identity 中斷或 pipeline gap 均降低 history coverage；必要時
整個 measurement version 停止發布累積 activation／首次 cohort。

## Wire contract 與隱私邊界

`usage_contract.py` 為程式與 checked-in JSON schema 的共同來源；輸出前重新驗證。
同版本欄位型別固定。破壞性修改須新版本與新 BigQuery view，不改舊表型別。
BigQuery payload schema **不是** Log Sink 自動匯出的 envelope schema；11.5 須處理
timestamp、jsonPayload 及 Logging 欄位名稱轉換，不可直接拿 payload schema 建 sink 表。

- 每個服務端接收呼叫建立一個 UUID；terminal canonical 去重鍵為
  `(schema_version, interaction_id, event_name)`。Client retry 新 ID，log 重送原 ID。
- Canonical 的 summary 永遠為 null／unavailable。附件是另一筆獨立 schema，
  不增加任何 request 分母；附件晚到、缺失、到期不影響 canonical。
- 模型僅檢查型別、enum 與長度，**不是摘要 sanitizer**；11.3 必須先遮罩或捨棄
  不安全文字，再建附件。不能直接傳入 request body、結果、provenance 或 claims。
- Tenant ID 原樣保留字串（例如 `5`、`005`、`tenant-a`），不 trim 或數值轉型。
  非字串／過長來源轉 null，僅產生不含原值的品質代碼，不干擾 analytics。
- Metrics／dimensions 必須由既有 catalog／report 驗證後提供；語法通過不代表
  catalog 存在。Traffic metrics 直接取 `TRAFFIC_METRICS`。陣列上限 100，各 ID 128 字元。
- requested_days 為正整數或 null；統計 explicit periods 聯集，單日為 1，不含
  implicit previous、不以 scan days／result rows 代替，可靠超限天數仍保留。
- 明確日期一律 explicit_range，不從日期恰逢上週或固定比較推論使用者 goal。
- 所有 validation／serializer 錯誤必須由 11.4 failure isolation 丟棄，禁止記錄
  exception、輸入或 fallback payload。驗證錯誤的 `.errors()` 仍可能帶 input。

## 指標資料充分性

| 用途 | 必要來源 | 缺失／保存處理 |
| --- | --- | --- |
| Eligible／authorized funnel | 外部資格名單及持久 connection 台帳 | 目前未提供，不發布比例 |
| Tool-call／analytics requests | 去重 terminal、transport、tool、kind、status | 180 天；unclassified 另列 |
| Activation／cohort | Verified 成功 analytics + 持續更新 ledger | 歷史缺口降級；preflight 不算 activation |
| DAU／WAU／MAU、回訪、sessions | Verified user、event_time、成功 analytics | 180 天；未知 user 排除，日界 Asia/Taipei |
| W1–W4 retention | 完整 ledger cohort 與 follow-up events | 未成熟或到期標不可量測，不能填零 |
| 成功、失敗、resolution、latency | status、resolution、latency、受管理 error code | preflight／analytics 分母分開；unknown coverage 另列 |
| Goals／subjects／metrics／dimensions | 當次可信 taxonomy、catalog／report IDs | unknown 保留、空陣列不猜候選 |
| 期間／比較分布 | period_type、requested_days、comparison_type | 摘要到期後仍能區分 7／90 天 |
| 摘要檢閱 | 經清理附件與 interaction_id | 30 天；不作 KPI 分母或長期文字匯出 |

## 發布前與後續工作

11.2–11.7 仍需實作與驗證：身分與 tenant 政策查核、summary sanitizer、分類、
failure-isolated emission、Cloud Logging／BigQuery 分流、ledger、KPI views、pilot。
未完成 host 實測不可宣稱 Claude／ChatGPT 行為已驗收。

雲端 apply 前由 owner 授權具體資源與 IAM；目前不授 runtime SA BigQuery 寫入權。
需先檢查所有 project／folder／organization sinks，避免摘要留下長期副本。
實際收集前須完成 consent／內部說明更新與通知；本文件本身不代表通知已送達。
回復時關閉 emission／summary、停 sink／dashboard，不撤掉原有 auth／tenant 邊界。
