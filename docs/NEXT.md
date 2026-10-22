# Next

## Goal

Confirm `v0.13.0` in production: the Downloads page, the `members_all` and `playlists_pass` switches, and the `main` cookie profile.

## Next steps

1. After the next CULUA poll, check that `membership` holds a `main` row for CULUA and that Settings, Memberships shows it.
2. The user checks Home, the Downloads page, the channel tabs and the Settings membership table on `savetube.lab.hiriri.com`.
3. When the user adds a channel with `playlists_pass` on, check in `poll.done` that its playlist entries queue without the include words.
4. Production has never run the format fallback. When the worker log shows `download.fallback`, check that the same job reaches `job.done`. The fallback runs only after a run that adds no bytes, as described in `docs/plan/archive.md`, Resuming and verifying downloads. Network interruptions that resume do not count.
