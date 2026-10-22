# Traps

Failures paid for once that the code does not show.

## Permission classifier

- `.claude/settings.local.json` allows `gh pr merge`, `git rebase`, `git merge`, `git pull`, and `git push origin develop`. Without these entries the auto mode classifier blocks them as "Merge Without Review".
- A production `psql` read passes only with one query per call. See `docs/guides/testing.md` for the command.
- Copying an archive file out of the worker pod with `kubectl exec ... cat` is denied as "Production Reads". Download the same video with the local `savetube:after` image instead.
- A kubectl loop that watches a deploy is denied as "Production Deploy", and an Argo CD refresh annotation is denied too. Neither is needed: Argo CD deploys a release through webhooks, and the user does not want a deploy polled.

## Tools

- The harness `isolation: worktree` starts from an old main merge commit, not develop. Make worktrees with `git worktree add -b <branch> ../savetube-wt-<name> origin/develop`.
- A command run through `kubectl exec` writes nothing to pod logs or Loki, and Python buffers its stdout until exit.
- `bsk` on `savetube.lab.hiriri.com` fails, because the page holds another extension's frame. The user checks the production UI.
- Host `uvx yt-dlp` lacks a JS challenge solver and may miss formats. Use the local image.
- respx cannot mock Authlib or Starlette's TestClient, because both use `httpx2`.
- pytest-cov fails 200 tests with `TypeError: 'InternalTraversal' object is not callable` from SQLAlchemy's Cython `_cache_key_cy.py`, with every coverage core. `DISABLE_SQLALCHEMY_CEXT_RUNTIME=1` does not help. A `sys.monitoring` line collector works: `/tmp/wt-run-split-plugin/linecov_plugin.py`, loaded with `-p linecov_plugin`.
- `make talos-config` fails off the home LAN. Use `talosctl patch mc --mode no-reboot` over the tailnet.

## Code

- `web/api/` and `jobs/` are packages whose `__init__.py` re-exports the old public names. A test patches a name on the submodule whose call site reads it, such as `jobs.runners.download`, `jobs.guards.PAUSE_POLL_SECONDS` or `web.api.scan.CHANNEL_PLAYLISTS_TIMEOUT`. Patching the package name has no effect.
- `cli.py` keeps lazy imports on purpose: `savetube.queue` reads Settings at import, and the rest are role-specific heavy packages.
- `savetube_proto` on pg-dev holds seeded demo data. Its complete videos have no `dir_path` or `info`, so a playback test points a row at files and reverts it after.
