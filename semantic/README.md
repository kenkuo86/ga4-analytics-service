# Semantic catalog

`catalog.v1.json` 是由兩份 GA4 指標定義 CSV 編譯出的 runtime artifact，請勿手動修改。

Builder 會：

- 驗證來源欄位、model、aggregation、group-by dimension 與 derived metric 基本結構。
- 正規化已知格式差異，例如 aggregation 寫法與 `IS NOT NULL`。
- 合併相同 metric ID 的相同語意定義，保留原始報表 context 供搜尋使用。
- 將同一 profile 中具有多種語意定義的 metric 標為 `conflict`，禁止 runtime 查詢。
- 套用經確認且版控的 canonical policy；目前非電商 `total_users` 固定選擇
  `mar_ga_sessions`／`session_date`，並在 catalog 記錄 resolution。
- 僅接受核准 `ga4_mar` model 的單一 `SELECT`／`WITH` SQL template。

來源表是 authoring source；catalog 是唯一的 runtime source。修改來源後必須重新執行
builder、單元測試和 `scripts/validate_semantic_catalog.py`，並將來源變更與新的 catalog
version 一起審查。
