"""The `log_tail` texts savetube.jobs writes and the web API reads back."""

STALLED_LOG = "worker stopped during the download"
SHUTDOWN_LOG = "worker shut down during the download"
BLOCKED_LOG = "blocked by YouTube"
PROFILE_BLOCKED_LOG = "refused for this cookie profile"
# The `log_tail` prefix of a run a network error interrupted.
NETWORK_LOG = "network error"
# The `log_tail` prefix of a run yt-dlp could not solve YouTube's JS challenges
# for. Every download fails until a yt-dlp release catches up with the player.
CHALLENGE_LOG = "yt-dlp failed YouTube's JS challenges"
NO_MEMBER = "no cookie profile is a confirmed member of the channel"
