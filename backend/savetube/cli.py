import argparse
import asyncio
import logging

from prometheus_client import start_http_server

from savetube.config import get_settings


def _web(args: argparse.Namespace) -> None:
    import uvicorn

    start_http_server(get_settings().metrics_port)
    # proxy_headers off: redirect URIs come from SAVETUBE_PUBLIC_URL, so no
    # forwarded header is trusted.
    uvicorn.run(
        "savetube.web.app:create_app",
        factory=True,
        host="0.0.0.0",
        port=args.port,
        proxy_headers=False,
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
        async with await health.serve(settings.health_port), app.open_async():
            # PgBouncer in transaction mode cannot hold a LISTEN, so the worker polls.
            await app.run_worker_async(
                queues=[ARCHIVE_QUEUE],
                concurrency=args.concurrency,
                listen_notify=False,
                fetch_job_polling_interval=5,
            )

    asyncio.run(main())


def _record(args: argparse.Namespace) -> None:
    from savetube import jobs

    # A recorder is already one process in its own pod, so no child process.
    asyncio.run(jobs.run(args.job_id, get_settings(), live=True, runner=jobs.in_process))


def _migrate(args: argparse.Namespace) -> None:
    from savetube.db.migrate import migrate

    migrate(get_settings())


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

    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    args.func(args)
