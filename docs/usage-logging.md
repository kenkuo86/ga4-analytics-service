# Phase 11.4 Logging 運作與驗收

## 收集邊界

MCP `tools/call` 在 SDK validation 前開始觀察，handler wrapper 在 validation 後標示已進入。
每個 RPC 的 identity context 持有一個 server UUID，嵌套 hooks 不另發事件。重送 log 使用相同
interaction ID；client retry 是新呼叫。未知工具與 dispatch 前 auth denial 不猜測 tool。
REST `/traffic-summary` 使用相同模型，ASGI boundary 補上 auth denial／422／未進 handler 的
失敗。外層不讀 body 或 response payload，嵌套 middleware 共用 transport state 防重複。
initialize、tools/list、health、OAuth callback 不產生 canonical usage。

Canonical 只記 allowlisted outcome、identity、tenant、period 與分類；不序列化 request、
claims、capability result、query result 或 exception。已驗證 metrics 在正常 profile resolution
後取用；日期在共用 query policy 正常解析成功後取用，包含可靠超限天數。
零列成功仍為成功；semantic row count 加總各 metric，traffic 僅 daily_series。

MCP 新增三個可省略欄位：request_summary、analysis_goal_hint、analysis_subject_hint。
Schema 公布 enum／型別；BeforeValidator 對非法 optional 值安全捨棄，不讓它們破壞原查詢。
REST GET 不新增文字摘要參數，只提供 enum hints；不可將完整使用者需求塞入 URL。
舊 client 不帶這些欄位仍可使用；server 可從安全 metadata 生成短摘要。

## Failure isolation 與容量

- `USAGE_ENABLED=false` 預設停用收集；tenant auth guard 不隨此旗標撤銷。
- `USAGE_SUMMARY_ENABLED=false` 預設不產生摘要附件；canonical 一直是 null／unavailable。
- Queue 容量256，request 使用 put_nowait，滿載丟棄並增加無payload計數。
- Worker 為 daemon thread，Logging POST timeout 2秒、最多兩次嘗試。ADC／token refresh
  也只在worker，可能比POST timeout更久；queue保持有界，不讓呼叫等待credential或sink。
- 收集目的固定 ga4-reports-dev；log names為 ga4_usage_v1／ga4_usage_summary_v1，
  labels usage_environment=pilot、usage_schema=1.0。使用原terminal event_time作Logging
  timestamp，UUID作insertId；BigQuery仍必須以contract key去重，不承諾exactly-once。
- 摘要只寫獨立log，不寫stdout。Worker每30秒將僅有固定code／整數的diagnostics寫stdout；
  計數包括enqueued、delivered、queue_full、delivery_failure／dropped、expired_dropped、
  observation／serialization failure及tenant品質。這些計數不含event payload或exception。
- 送出前檢查event_time：到期30／180天的事件不重新匯入。到期清理仍依賴雲端TTL配置。

Cloud Run request-based CPU可能在回應後暫停背景thread，終止／crash也可能丟buffer或完全
沒有terminal event。Pilot需量測emitted／delivered／dedup差距與event-time到receive-time延遲，
不能將這份telemetry當完整security audit。若不足，需另行核准always-allocated CPU或持久queue，
不默默變更Cloud Run計費模式。此PR不宣稱零遺失或背景送達已在production驗證。

## 發布與回復

1. 先建立與驗證專用Logging buckets、filtered sinks、BigQuery TTL／partition／schema／IAM，
   摘要須排除Default與其他長期副本；只用合成事件驗收。
2. Secret Manager注入persistent HMAC key與managed host mapping。預設不啟用logging。
3. 內部通知完成後，以小群組先開結構化事件；確認失敗隔離、延遲與成本，再另外開摘要。
   Consent在啟用時顯示180天、摘要開關／30天、一年ledger上限與owner／管理員存取例外。
4. Rollback關閉兩個usage開關、停sink／dashboard refresh，保持auth與tenant guard。
   停收不等於刪資料，另驗證所有副本到期。

當前只完成local fixtures，未部署、未啟用真實收集；11.5–11.7仍待雲端驗收與pilot。
