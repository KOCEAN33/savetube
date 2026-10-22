import argparse
import asyncio
import logging

from prometheus_client import start_http_server

from savetube import logs
from savetube.config import get_settings

log = logging.getLogger("savetube.cli")


def _web(args: argparse.Namespace) -> None:
    import uvicorn

    settings = get_settings()
    start_http_server(settings.metrics_port)
    logs.emit(log, logging.INFO, "web.start", port=args.port)
    # proxy_headers off: redirect URIs come from SAVETUBE_PUBLIC_URL, so no
    # forwarded header is trusted. log_config None keeps the logfmt handler.
    # The probes hit /healthz every few seconds, and the API logs its own
    # writes, so the access log is for DEBUG only.
    uvicorn.run(
        "savetube.web.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
        proxy_headers=False,
        log_config=None,
        access_log=settings.log_level == "DEBUG",
    )


def _scheduler(args: argparse.Namespace) -> None:
    from savetube import health, scheduler

    settings = get_settings()
    start_http_server(settings.metrics_port)

    async def main() -> None:
        async with await health.serve(settings.health_port):
            await scheduler.run(settings)

    asyncio.run(main())


def _worker(args: argparse.Namespace) -> None:
    from savetube import health
    from savetube.queue import ARCHIVE_QUEUE, app

    settings = get_settings()
    start_http_server(settings.metrics_port)

    async def main() -> None:
        logs.emit(
            log, logging.INFO, "worker.start", concurrency=args.concurrency, queue=ARCHIVE_QUEUE
        )
        async with await health.serve(settings.health_port), app.open_async():
            # PgBouncer in transaction mode cannot hold a LISTEN, so the worker polls.
            await app.run_worker_async(
                queues=[ARCHIVE_QUEUE],
                concurrency=args.concurrency,
                listen_notify=False,
                fetch_job_polling_interval=5,
                # The `job` row keeps the history, and every dispatch adds a row.
                delete_jobs="successful",
                # On SIGTERM, cancel the downloads at once instead of waiting
                # for them. Each cancel kills its child, keeps its parts, and
                # puts the job back to waiting, so the next worker resumes it.
                shutdown_graceful_timeout=0,
            )

    asyncio.run(main())


def _record(args: argparse.Namespace) -> None:
    from savetube import jobs

    # A recorder is already one process in its own pod, so no child process.
    try:
        asyncio.run(jobs.run(args.job_id, get_settings(), live=True, runner=jobs.in_process))
    except Exception as e:
        if not jobs.recorded(e):
            logs.emit(log, logging.ERROR, "record.failed", job=args.job_id, exc_info=True)
        # A failed exit, so the Kubernetes Job retries through its backoffLimit.
        raise SystemExit(1) from None


def _migrate(args: argparse.Namespace) -> None:
    from savetube.db.migrate import migrate

    migrate(get_settings())


def _extract_audio(args: argparse.Namespace) -> None:
    from savetube import audio
    from savetube.db import make_engine, make_sessionmaker
    from savetube.integrity import Tools

    settings = get_settings()

    async def main() -> dict[str, int]:
        engine = make_engine(settings.sqlalchemy_url)
        try:
            return await audio.backfill(
                make_sessionmaker(engine),
                settings.archive_root,
                tools=Tools(),
                dry_run=args.dry_run,
            )
        finally:
            await engine.dispose()

    if asyncio.run(main())["failed"]:
        raise SystemExit(1)


def main() -> None:
    parser = argparse.ArgumentParser(prog="savetube")
    sub = parser.add_subparsers(required=True)

    p = sub.add_parser("web", help="API and the built frontend")
    p.add_argument("--port", type=int, default=8000)
    p.set_defaults(func=_web)

    sub.add_parser("scheduler", help="source polls, live detection, recorder Jobs").set_defaults(
        func=_scheduler
    )

    p = sub.add_parser("worker", help="archive downloads from the queue")
    p.add_argument("--concurrency", type=int, default=1)
    p.set_defaults(func=_worker)

    p = sub.add_parser("record", help="record one live from its first segment")
    p.add_argument("--job-id", type=int, required=True)
    p.set_defaults(func=_record)

    sub.add_parser("migrate", help="install or upgrade the database schema").set_defaults(
        func=_migrate
    )

    p = sub.add_parser("extract-audio", help="write the missing audio copy of every stored video")
    p.add_argument("--dry-run", action="store_true", help="list what would be written")
    p.set_defaults(func=_extract_audio)

    args = parser.parse_args()
    logs.configure(get_settings().log_level)
    args.func(args)
