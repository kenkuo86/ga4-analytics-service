# 對應階段

<!--
必填。請填寫這個 PR 對應的 ROADMAP Phase、使用的 branch，以及相關 issue。
一個 PR 原則上只處理一個 Phase；若為 Phase 內的後續 PR，請說明與前一個 PR 的關係。
-->

- ROADMAP Phase：
- ROADMAP feature：
- Branch：
- Dependencies：
- Related issue／PR：

## 改動重點

<!-- 必填。請以繁體中文摘要這次實際修改了哪些行為、介面、資料結構或文件。 -->

## 目標

<!-- 必填。請以繁體中文說明要解決的問題、預期成果，以及不在本次範圍內的項目。 -->

## 實作方式與重要決策

<!--
必填。請說明主要實作方向、重要取捨，以及是否變更 MCP tool schema、OAuth、
tenant routing、semantic catalog、BigQuery query 或 report schema。
-->

## 限制、已知風險與後續工作

<!--
必填。請列出尚未處理的限制、已知風險、需要後續 Phase／PR 完成的工作。
若沒有，也請明確填寫「無」。
-->

## 預期範圍外的變更

<!--
必填。是否修改規劃階段未預期的檔案或模組？若有，請逐項說明原因；
若沒有，請填寫「無」。
-->

## 驗收方式

<!--
必填。列出可重現的自動化與手動驗收步驟。
請填寫實際指令、測試案例或操作路徑，不要只寫「已測試」。
-->

### 自動化驗收

- [ ] 指令／測試：

### 手動驗收

- [ ] 情境／步驟：

## 驗收成果

<!--
必填。請以繁體中文記錄每個驗收項目的實際結果。
若有未執行、失敗或只能在部署後驗證的項目，必須明確列出，不能省略。
-->

- 自動化測試結果：
- 手動驗收結果：
- 尚未驗證項目：

## 獨立審查

<!--
此區由獨立 reviewer 更新。請聚焦 correctness、需求涵蓋、regression、
edge cases、maintainability、架構一致性、安全／權限、測試缺口及副作用。
-->

- Reviewer：
- Review 狀態：尚未審查／有 blocking findings／無 blocking findings
- Findings：
  - P0：
  - P1：
  - P2：
  - P3：
- 修正後重新審查結果：

## 影響、風險與回復方式

- BigQuery 成本影響：
- Authentication／authorization／資料隔離影響：
- 相容性或 schema 影響：
- 部署與回復方式：

## Checklist

- [ ] 此 PR 來自獨立 branch，沒有直接在 `main` 實作。
- [ ] 此 PR 的範圍對應單一 feature，或已清楚說明拆分／合併原因。
- [ ] 「改動重點」、「目標」、「實作方式與重要決策」、「限制、已知風險與後續工作」、「預期範圍外的變更」、「驗收方式」及「驗收成果」七個必要段落皆已用繁體中文完整填寫。
- [ ] 已新增或更新與改動風險相稱的測試。
- [ ] 已執行適用的自動化測試，並在上方記錄實際結果。
- [ ] 已檢查 final diff，沒有意外或未說明的範圍外改動。
- [ ] 已檢查 BigQuery 成本、權限與跨 tenant 資料隔離風險。
- [ ] 已更新適用的 README、ROADMAP、環境變數範例或操作文件。
- [ ] 沒有提交密碼、token、private key、客戶資料或其他 secrets。
- [ ] 已完成獨立 review，且沒有未解決的 P0／P1 findings。
- [ ] 所有適用 checks 通過，branch 沒有 unresolved conflicts。
- [ ] 此 PR 只會由 repository owner 最終合併；agent 未執行 merge 或 auto-merge。
