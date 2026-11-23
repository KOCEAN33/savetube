# Next

## Goal

The frontend API types come from the backend schema, so `frontend/src/lib/api/types.ts` holds no hand-written copy of it. About 110 field comments in that file restate backend facts and drift. Evidence: harness `claude/research/2026-11-slop-talk/02-comments.md`, pattern 3.

## Start

Generate the types from the FastAPI OpenAPI schema and replace the hand-written ones, because the user decided this fix. Done when `types.ts` is generated or imports generated types, a check fails when the generated file differs from the schema, `.comment-lint-baseline.json` is deleted and `make lint-comments` passes without it, and `pnpm run typecheck` and `scripts/test-db.sh make e2e-smoke` pass. The baseline's 50 entries are field sentences `types.ts` shares with the backend schema, so generating the types removes them.

Orders from the user that still bind:

- The user chose schema generation over keeping the comments in sync by hand.

## Pointers

- A field fact the frontend needs, such as a null meaning, moves to the backend model's field description, which the generator can carry.
- The generator is a new dependency: name it in the work plan.
- The vault report `reports/engineering-practice/frontend-api-client.md`, section "Generated output and sync gate", covers this subject.
