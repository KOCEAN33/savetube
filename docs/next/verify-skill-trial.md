# Next

## Goal

Find out whether a project-local `verify-savetube` skill is worth keeping: one skill that launches savetube, checks the environment read-only, maps the user-facing features, and drives a feature while keeping evidence. The reason for the trial: harness `claude/research/2026-11-slop-talk/00-analysis.md`, the feature map and verification skill rows.

## Start

Build the skill after `pstack/skills/create-verification-skill/SKILL.md` on `main` of `cursor/plugins` on GitHub, because the trial has no other step before it. The skill holds a launch command, a read-only doctor check, a feature map with one file per feature (sub-features, user entry points, exact driving commands, gotchas), and evidence capture. Done when the skill alone drives one mapped feature end to end and the evidence of that run is on disk.

Orders from the user that still bind:

- The user asked for this trial in savetube after first dropping it, so the report states plainly what the skill adds beyond `docs/guides/testing.md` and the `coding-deep` verify plan.

## Next steps

- Report the trial to the user: the feature driven, the evidence path, the tokens and time the build took, and a keep-or-drop recommendation. Done when the user has the report.

## Pointers

- The harness submodule `claude/references/pstack` is behind `cursor/plugins` `main`. Read the pstack skill from GitHub.
- Which feature to map and drive first is the session's choice.
