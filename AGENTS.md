# Project instructions

## Phase delivery workflow

- Do not implement a ROADMAP Phase directly on `main`.
- Create at least one dedicated branch for every Phase. Prefer `phase-<number>-<short-description>`.
- Keep each branch and Pull Request scoped to one Phase. If a Phase needs multiple PRs, identify the shared Phase and dependencies in every PR.
- Non-Phase fixes and documentation work also require a dedicated `fix/`, `docs/`, or `chore/` branch.
- After implementation and proportionate verification, create the PR and merge it when all applicable checks pass. Do not merge with failing tests, unresolved product decisions, or unresolved cost, authorization, or tenant-isolation risks.

## Pull Request record

- Use `.github/pull_request_template.md`.
- Write the PR description in Traditional Chinese.
- Every PR must fully document:
  1. 改動重點是什麼。
  2. 目標是什麼。
  3. 怎麼驗收。
  4. 驗收成果如何。
- Include the exact commands and manual scenarios actually used. Clearly label anything not run or requiring post-deployment verification.
- Record relevant BigQuery cost, authentication／authorization, data isolation, compatibility, deployment, and rollback impact.
- Update ROADMAP status after the Phase is merged.

See `CONTRIBUTING.md` for the complete project workflow.
