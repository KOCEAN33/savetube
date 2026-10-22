"""The `log_tail` texts savetube.jobs writes and the web API reads back."""

STALLED_LOG = "worker stopped during the download"
SHUTDOWN_LOG = "worker shut down during the download"
BLOCKED_LOG = "blocked by YouTube"
# The `log_tail` prefix of a run a network error interrupted.
NETWORK_LOG = "network error"
NO_MEMBER = "no cookie profile is a confirmed member of the channel"
