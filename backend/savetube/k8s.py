"""Recorder Jobs, made from a suspended CronJob like `kubectl create job --from=cronjob/...`.

The CronJob is the template: image, sidecar, volumes and resources stay in the
manifests, where Kustomize's image and patch transformers reach them.
The web role reads the recorder Jobs and the worker pods for the Live page.
"""

import copy
from typing import Any

from lightkube import ApiError, AsyncClient
from lightkube.models.meta_v1 import ObjectMeta, OwnerReference
from lightkube.operators import exists
from lightkube.resources.batch_v1 import CronJob, Job
from lightkube.resources.core_v1 import Pod

RECORDER_CONTAINER = "recorder"
JOB_ID_LABEL = "savetube.io/job-id"
VIDEO_ID_ANNOTATION = "savetube.io/video-id"
# Every worker pod carries it. One release per namespace, so no instance label.


def recorder_job(cronjob: CronJob, job_id: int, video_id: str) -> Job:
    template = cronjob.spec.jobTemplate
    spec = copy.deepcopy(template.spec)
    containers = [c for c in spec.template.spec.containers if c.name == RECORDER_CONTAINER]
    if len(containers) != 1:
        raise ValueError(f"CronJob {cronjob.metadata.name} has no container {RECORDER_CONTAINER!r}")
    containers[0].args = ["record", "--job-id", str(job_id)]
    meta = template.metadata or ObjectMeta()
    labels = {**(meta.labels or {}), JOB_ID_LABEL: str(job_id)}
    spec.template.metadata = spec.template.metadata or ObjectMeta()
    spec.template.metadata.labels = {
        **(spec.template.metadata.labels or {}),
        JOB_ID_LABEL: str(job_id),
    }
    return Job(
        metadata=ObjectMeta(
            # A video id holds upper case and `_`, which a DNS-1123 name cannot.
            name=f"{cronjob.metadata.name}-{job_id}",
            labels=labels,
            annotations={**(meta.annotations or {}), VIDEO_ID_ANNOTATION: video_id},
            # Argo CD shows an owned Job as the CronJob's child instead of
            # pruning it as an object missing from git.
            ownerReferences=[
                OwnerReference(
                    apiVersion="batch/v1",
                    kind="CronJob",
                    name=cronjob.metadata.name,
                    uid=cronjob.metadata.uid,
                    controller=False,
                )
            ],
        ),
        spec=spec,
    )


def recorder_states(jobs: list[Job]) -> dict[int, bool]:
    """Each recorder Job's job id, and whether the Job failed every retry."""
    states = {}
    for job in jobs:
        conditions = (job.status.conditions if job.status else None) or []
        failed = any(c.type == "Failed" and c.status == "True" for c in conditions)
        states[int(job.metadata.labels[JOB_ID_LABEL])] = failed
    return states


async def list_recorder_jobs(namespace: str) -> dict[int, bool]:
    async with AsyncClient(namespace=namespace) as client:
        jobs = [j async for j in client.list(Job, labels={JOB_ID_LABEL: exists()})]
    return recorder_states(jobs)


async def create_recorder_job(namespace: str, cronjob_name: str, job_id: int, video_id: str) -> str:
    async with AsyncClient(namespace=namespace) as client:
        cronjob = await client.get(CronJob, cronjob_name)
        job = recorder_job(cronjob, job_id, video_id)
        try:
            await client.create(job)
        except ApiError as e:
            # A Job the list missed: the name holds the job id, so it is this job's.
            if e.status.code != 409:
                raise
        return job.metadata.name


def _job_status(job: Job) -> str:
    status = job.status
    conditions = {c.type for c in (status.conditions or []) if c.status == "True"}
    if "Complete" in conditions:
        return "succeeded"
    if "Failed" in conditions:
        return "failed"
    return "running" if status.active else "pending"


def recorder_summaries(jobs: list[Job], pods: list[Pod]) -> list[dict[str, Any]]:
    """Each recorder Job, newest job id first, with the phase of its newest pod.
    `restarts` counts its failed pods: with restartPolicy Never, a retry is a
    new pod."""
    newest: dict[str, Pod] = {}
    for pod in sorted(pods, key=lambda p: p.metadata.creationTimestamp):
        newest[pod.metadata.labels[JOB_ID_LABEL]] = pod
    rows = []
    for job in jobs:
        job_id = job.metadata.labels[JOB_ID_LABEL]
        pod = newest.get(job_id)
        rows.append(
            {
                "job_id": int(job_id),
                "video_id": (job.metadata.annotations or {}).get(VIDEO_ID_ANNOTATION),
                "name": job.metadata.name,
                "status": _job_status(job),
                "pod_phase": pod.status.phase if pod else None,
                "restarts": job.status.failed or 0,
                "started_at": job.status.startTime,
            }
        )
    return sorted(rows, key=lambda r: r["job_id"], reverse=True)


async def cluster_pods(namespace: str) -> dict[str, list[dict[str, Any]]]:
    """The recorder Jobs, read with get/list only."""
    async with AsyncClient(namespace=namespace) as client:
        jobs = [j async for j in client.list(Job, labels={JOB_ID_LABEL: exists()})]
        recorder_pods = [p async for p in client.list(Pod, labels={JOB_ID_LABEL: exists()})]
    return {
        "recorders": recorder_summaries(jobs, recorder_pods),
    }
