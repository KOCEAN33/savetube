# savetube

Merging: a worktree branch lands on develop locally, without a PR or the user's approval. Rebase it on develop and merge with `--ff-only`. When the rebase conflicts, merge with a merge commit instead. Push develop directly. A GitHub PR is only for develop into main. Open and merge it only when the user asks for a release, because a merge to main deploys to production. Merge it with a merge commit, after any checks on it pass.

Design: `docs/plan/`, split by topic, indexed by `docs/plan/README.md`. Traps: `docs/traps.md`. Test and repro commands: `docs/guides/testing.md`. Unscheduled work and open questions: `docs/candidates.md`. Handoff: `docs/NEXT.md`, next steps only.

Facts this project shares with the homelab repositories live in llm-wiki `vault/projects/savetube.md`, the card that links the homelab pages it depends on.
