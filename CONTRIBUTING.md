# 開發與 Pull Request 流程

本專案依 [ROADMAP.md](ROADMAP.md) 推進，並將規劃、實作、審查及合併視為四個獨立階段。Branch、worktree 與 Pull Request 是階段之間的正式交接點。

開發 agent 可以建立及推送 branch、發出 Pull Request，但不可以直接合併或開啟 auto-merge。Repository owner 保留最後合併權。

## 1. 實作前規劃

開始任何 ROADMAP feature 前，先閱讀全部未完成項目；規劃階段不得修改程式。

每個未完成 feature 都要分析：

- 能否獨立實作。
- 依賴哪些未完成項目，以及哪些項目依賴它。
- 預期會修改的核心檔案或模組。
- 平行實作是否容易產生 merge conflict。
- 建議 branch 名稱。

至少以以下格式整理：

| Feature | 可平行實作 | 依賴關係 | 可能重疊檔案 | 建議 branch |
| ------- | ---------- | -------- | ------------ | ----------- |

若依賴或實作順序仍不清楚，應先提出；不得為了平行化而忽略技術依賴或高衝突風險。

## 2. Branch、worktree 與實作範圍

1. 不直接在 `main` 實作 ROADMAP feature。
2. 每個 Phase 至少要留下一個專用 branch 與 Pull Request；可獨立實作的 feature 原則上各自使用 branch／PR。
3. 建議 branch 命名為 `feat/phase-<number>-<short-description>`，例如 `feat/phase-4-query-cost-controls`。
4. 平行實作必須使用不同 worktree 與 branch，每個實作 agent 只處理被指派的 feature。
5. 一個 branch／PR 原則上只處理一個 feature；不得混入無關重構。
6. 若實作途中發現未知依賴，停止該 feature 並回報，不建立脆弱的暫時 workaround。
7. 不屬於 ROADMAP 的修正、文件或維運工作，也必須使用獨立的 `fix/`、`docs/` 或 `chore/` branch。

## 3. Definition of Done

發出 Pull Request 前，實作者必須：

1. 依 ROADMAP 完成功能。
2. 驗證所有適用的 acceptance criteria。
3. 執行專案可用且與改動相關的 tests、lint、type checking 與 build。
4. 審查最後 diff，排除意外或無關改動。
5. 更新受影響的文件。
6. Commit 完整變更。
7. Push feature branch。
8. 發出 Pull Request。

程式可以編譯不代表完成；實際使用者行為與 acceptance criteria 都必須成立。

牽涉共用查詢、OAuth、MCP schema、tenant routing 或 semantic catalog 時，應執行完整測試：

```bash
.venv/bin/python -m unittest discover -s tests -v
```

牽涉 BigQuery schema 或 IAM 時，依 README 執行 dry-run／跨 tenant validation，不得用實際 tenant data query 取代 dry-run。

## 4. Pull Request 紀錄

每個 feature 原則上各自建立 Pull Request，並使用 [.github/pull_request_template.md](.github/pull_request_template.md)。

PR title 與 description 必須使用繁體中文，完整記錄：

1. **改動重點**：實際變更的行為、介面、資料結構、設定及文件。
2. **目標**：要解決的問題、預期成果與不在本次範圍內的項目。
3. **主要實作決策**：技術方向、取捨與架構影響。
4. **驗收方式**：可重現的自動化測試、手動步驟、測試資料或操作情境。
5. **驗收成果**：實際通過／失敗結果，以及尚未或只能部署後驗證的項目。
6. **限制與風險**：已知限制、風險、後續工作及回復方式。
7. **範圍外變更**：是否修改原本預期以外的檔案及其必要性。

不得刪除 template 的必要段落。BigQuery 成本、authentication／authorization、跨 tenant 資料隔離、schema 相容性與部署影響也必須依實際情況記錄。

## 5. 獨立審查

實作者不能作為自己 PR 的最終 reviewer。PR 建立後，應由獨立 reviewer 從以下面向審查：

- 正確性及 ROADMAP requirement coverage。
- Regression 與 edge cases。
- Maintainability、非必要複雜度及架構一致性。
- Security、permissions 及跨 tenant 資料隔離。
- 缺少的測試。
- 非預期副作用與範圍外變更。

若 CI 已處理格式或 lint，不應把主要 review 精力放在這些 deterministic checks。

Finding severity：

- **P0**：重大安全、資料或服務問題，禁止合併。
- **P1**：重要 correctness、security 或 regression 問題，合併前必須修正。
- **P2**：有意義的改善，可視風險決定是否阻擋。
- **P3**：輕微建議或 cleanup。

沒有 syntax error 不等於通過審查。需求不完整或行為不正確時，必須明確提出。

## 6. 修正與重新審查

發現 blocking findings 時，依下列循環處理：

```text
實作者
  → Pull Request
  → 獨立審查
  → 修正 P0／P1 findings
  → 重新執行 tests／lint／type check／build
  → 更新 PR 驗收紀錄
  → 獨立重新審查
```

直到沒有 blocking findings 為止。修正程式後不得沿用先前的測試結果，也不得略過 final-state validation。

## 7. Merge gate

PR 只有在下列條件全部成立時，才可以標記為 ready for owner：

- ROADMAP acceptance criteria 已滿足。
- 適用的 CI 與本機 checks 通過。
- 沒有未解決的 P0／P1 findings。
- Branch 足夠新，可以安全合併。
- 沒有 unresolved conflicts。

Agent 必須停在已建立、已審查的 PR，回報狀態並等待 repository owner。不得執行 `gh pr merge`、開啟 auto-merge，或以其他方式合併 PR。

## 8. 多個 PR 的處理

推薦 merge 順序前，先重新檢查開啟中 PR 的依賴：

1. 共用基礎或 foundational changes 優先。
2. 依賴該基礎的 features 隨後。
3. 其餘獨立 PR 可依安全順序合併。

任一 PR 合併後，重新確認其他 branch 是否需要 update／rebase，是否產生衝突，或使原本假設失效。

## 9. ROADMAP 是範圍依據

`ROADMAP.md` 是 feature scope、status、dependencies、goal 與 acceptance criteria 的主要依據。

- 不得靜默重新解釋會實質改變行為的模糊需求。
- 重大歧義應在實作前提出。
- 輕微實作細節可以作合理工程決策，但必須記錄在 PR。
- Owner 合併 feature 後，應以適當的後續 branch／PR 更新 ROADMAP status。

## 10. 安全與資料原則

- 不得提交 secrets、OAuth credentials、private keys、access tokens 或客戶資料。
- 不得讓使用者或模型直接指定 BigQuery project、dataset、table 或任意 SQL。
- 查詢限制必須由服務端執行，不能只依賴 prompt 或 tool description。
- 任何模糊 tenant 解析都必須 fail closed；多候選時不得猜測或查詢。
- 新增能力時，同步更新 capability metadata、OAuth consent、tool descriptions、README 與相關測試。
