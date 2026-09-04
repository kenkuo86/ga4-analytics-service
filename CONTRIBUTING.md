# 開發與 Pull Request 流程

本專案後續依 [ROADMAP.md](ROADMAP.md) 的 Phase 推進。每個 Phase 都必須留下可在 GitHub 查閱的 branch、Pull Request、實作說明與驗收紀錄。

## Branch 規則

1. 不直接在 `main` 實作 ROADMAP Phase。
2. 每個 Phase 至少建立一個專用 branch，建議命名為 `phase-<number>-<short-description>`，例如 `phase-4-query-cost-controls`。
3. 建立 Phase branch 前，先同步最新的 `main`。
4. 一個 branch／PR 原則上只處理一個 Phase，避免把不相關的功能或整理混入。
5. 若一個 Phase 需要拆成多個 PR，每個 PR 都要標示相同 Phase，並在說明中列出彼此的依賴與尚未完成範圍。
6. 緊急修正、文件或維運工作若不屬於 ROADMAP Phase，也必須使用獨立 branch，建議採用 `fix/`、`docs/` 或 `chore/` 前綴。

## Pull Request 規則

每個 Phase 至少要有一個 Pull Request。PR description 必須使用繁體中文，並完整記錄：

1. **改動重點是什麼**：實際變更的行為、介面、資料結構、設定與文件。
2. **目標是什麼**：要解決的問題、預期結果及本次不處理的範圍。
3. **怎麼驗收**：可重現的自動化測試、手動步驟、測試資料或操作情境。
4. **驗收成果如何**：實際執行結果、通過／失敗狀態，以及尚未或只能部署後驗證的項目。

建立 PR 時使用 [.github/pull_request_template.md](.github/pull_request_template.md)，不得刪除上述四個必要段落。除必要段落外，還應記錄 BigQuery 成本、authentication／authorization、跨 tenant 資料隔離、schema 相容性及回復方式等相關影響。

## 驗收與合併

1. 測試強度必須與變更風險相稱；至少執行與修改範圍直接相關的單元測試。
2. 牽涉共用查詢、OAuth、MCP schema、tenant routing 或 semantic catalog 時，應執行完整測試：

   ```bash
   .venv/bin/python -m unittest discover -s tests -v
   ```

3. 牽涉 BigQuery schema 或 IAM 時，依 README 的流程執行 dry-run／跨 tenant validation；不得以實際 tenant data query 取代 dry-run 驗收。
4. PR 中只能填寫實際完成的驗收結果。未執行、因權限無法執行或需要部署後確認的項目必須明確標示。
5. Required checks、適用測試及人工驗收通過後，可以直接合併 PR。
6. 測試失敗、產品決策尚未確認、存在未解決的成本或資料隔離風險時，不得合併。
7. 合併後更新 ROADMAP 的進度或完成狀態，並保留 PR 作為該 Phase 的主要實作與驗收紀錄。

## 安全與資料原則

- 不得提交 secrets、OAuth credentials、private keys、access tokens 或客戶資料。
- 不得讓使用者或模型直接指定 BigQuery project、dataset、table 或任意 SQL。
- 查詢限制必須由服務端執行，不能只依賴 prompt 或 tool description。
- 任何模糊 tenant 解析都必須 fail closed；多候選時不得猜測或查詢。
- 新增能力時，同步更新 capability metadata、OAuth consent、tool descriptions、README 與相關測試。
