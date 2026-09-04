# Project instructions

## Core workflow

- Treat this repository like a small software team with separate planning, implementation, review, and merge stages.
- Use Git branches, worktrees, and Pull Requests as the handoff points between stages.
- Do not implement ROADMAP features directly on `main`.
- Do not automatically implement all unfinished roadmap items sequentially.
- Agents may create and push branches and open Pull Requests. Agents must not merge Pull Requests. The human repository owner is the final merge owner.

## Planning before implementation

Before changing code for ROADMAP features:

1. Read `ROADMAP.md` and analyze all unfinished features.
2. Do not modify code during this planning stage.
3. For every unfinished feature, determine:
   - whether it can be implemented independently;
   - dependencies in either direction;
   - likely core files or modules;
   - expected merge-conflict risk;
   - a recommended branch name.
4. Summarize the analysis in a table:

   | Feature | Parallelizable | Dependencies | Likely overlapping files | Recommended branch |
   | ------- | -------------- | ------------ | ------------------------ | ------------------ |

5. Report unclear dependencies or implementation order before starting.
6. Do not force parallel work when features have meaningful dependencies or are likely to modify the same core files.

## Branches, worktrees, and scope

- Every ROADMAP Phase must be represented by at least one dedicated branch and Pull Request.
- Each independently implementable feature should normally use its own branch and Pull Request.
- Prefer branch names such as `feat/phase-4-query-cost-controls`.
- Parallel implementations must use separate worktrees and separate branches.
- Each implementation agent works only on its assigned feature.
- Do not include unrelated refactors or changes unless required by the feature.
- If implementation reveals an unknown dependency, stop that feature and report it instead of creating a fragile workaround.
- Non-Phase fixes and documentation work also require a dedicated `fix/`, `docs/`, or `chore/` branch.

## Definition of Done

Before opening a Pull Request, the implementation owner must:

1. Implement the feature according to `ROADMAP.md`.
2. Verify every applicable acceptance criterion.
3. Run all relevant repository checks, including tests, lint, type checking, and build when available.
4. Review the final diff for accidental or unrelated changes.
5. Update documentation when behavior changes.
6. Commit the completed work.
7. Push the feature branch.
8. Open a Pull Request.

Compiling successfully is not sufficient. The intended user behavior and acceptance criteria must be satisfied.

## Pull Request record

- Use `.github/pull_request_template.md`.
- Write the PR title and description in Traditional Chinese.
- Every PR must document:
  1. 改動重點是什麼。
  2. 目標是什麼。
  3. 主要實作決策。
  4. 怎麼驗收。
  5. 驗收成果如何。
  6. 限制、已知風險與後續工作。
  7. 是否修改原先預期範圍以外的檔案。
- Include the exact commands and manual scenarios actually used.
- Clearly label checks that were not run or require post-deployment verification.
- Record relevant BigQuery cost, authentication／authorization, tenant isolation, compatibility, deployment, and rollback impact.

## Independent review

- The implementation owner is not the final reviewer of its own work.
- After a PR is opened, review it from an independent reviewer perspective.
- Focus review on correctness, requirement coverage, regressions, edge cases, maintainability, unnecessary complexity, architecture consistency, security and permissions, missing tests, and unexpected side effects.
- Do not spend review effort mainly on deterministic formatting or lint issues already covered by CI.
- Classify meaningful findings as:
  - **P0** — critical; must not merge.
  - **P1** — correctness, security, or regression issue; fix before merge.
  - **P2** — meaningful improvement that may not block merge.
  - **P3** — minor suggestion or cleanup.
- Do not treat the absence of syntax errors as approval.

## Fix and re-review loop

When review finds blocking issues:

1. The implementation owner fixes all P0/P1 findings.
2. Run the applicable tests, lint, type checks, and build again.
3. Update the PR validation record.
4. Perform an independent re-review.
5. Repeat until no blocking findings remain.

Do not declare a PR ready based on an earlier test run after additional fixes.

## Merge gate

A PR may be marked ready for the human owner only when:

- ROADMAP acceptance criteria are satisfied.
- Relevant CI and local checks pass.
- No unresolved P0/P1 findings remain.
- The branch is up to date enough to merge safely.
- No unresolved conflicts remain.

Agents must stop at an open, reviewed PR and report its status. Do not run `gh pr merge`, enable auto-merge, or otherwise merge the PR.

When multiple PRs are open, assess dependencies before recommending merge order. Prefer foundational changes first, then dependent features. After another PR is merged, check whether remaining branches need an update or rebase and whether earlier assumptions remain valid.

## ROADMAP as source of truth

- Treat `ROADMAP.md` as the primary source for feature scope, status, dependencies, goals, and acceptance criteria.
- Do not silently reinterpret material ambiguity. Report it before implementation.
- Make reasonable decisions for minor implementation details and document them in the PR.
- After the human owner merges a feature, update the ROADMAP status in a follow-up branch or as part of an explicitly scoped documentation update.

See `CONTRIBUTING.md` for the human-readable project workflow.
