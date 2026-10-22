from fastapi import APIRouter, Depends

from savetube.web.api import channels, jobs, scan, settings, sources, stats, videos
from savetube.web.api.deps import require_csrf_header
from savetube.web.api.jobs import job_reason
from savetube.web.api.scan import get_scanner
from savetube.web.auth import current_user

__all__ = ["get_scanner", "job_reason", "router"]

router = APIRouter(
    prefix="/api", dependencies=[Depends(current_user), Depends(require_csrf_header)]
)
for module in (settings, stats, channels, sources, scan, jobs, videos):
    router.include_router(module.router)
