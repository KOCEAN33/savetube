"""The recorder Jobs and the worker pods, read from the Kubernetes API."""

import os
from datetime import datetime

from fastapi import APIRouter, Request

from savetube import k8s
from savetube.web.api.schemas import _Out

router = APIRouter()


class RecorderOut(_Out):
    job_id: int
    video_id: str | None
    name: str
    # succeeded, failed, running (a pod is active), or pending.
    status: str
    # The newest pod's phase. Null before the pod exists or after the TTL removed it.
    pod_phase: str | None
    # Failed pods, one per retry.
    restarts: int
    started_at: datetime | None


class WorkerPodOut(_Out):
    name: str
    phase: str | None
    ready: bool
    restarts: int
    started_at: datetime | None


class PodsOut(_Out):
    # False outside a cluster, where no Kubernetes API answers.
    available: bool
    recorders: list[RecorderOut]
    workers: list[WorkerPodOut]


@router.get("/pods")
async def pods(request: Request) -> PodsOut:
    # Kubernetes sets this in every pod. compose.yaml and a local serve have no API.
    if not os.environ.get("KUBERNETES_SERVICE_HOST"):
        return PodsOut(available=False, recorders=[], workers=[])
    found = await k8s.cluster_pods(request.app.state.namespace)
    return PodsOut(available=True, **found)
