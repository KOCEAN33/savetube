"""Recorder Jobs, made from a suspended CronJob like `kubectl create job --from=cronjob/...`.

The CronJob is the template: image, sidecar, volumes and resources stay in the
manifests, where Kustomize's image and patch transformers reach them.
"""

import copy

from lightkube import AsyncClient
from lightkube.models.meta_v1 import ObjectMeta, OwnerReference
from lightkube.resources.batch_v1 import CronJob, Job

RECORDER_CONTAINER = "recorder"
JOB_ID_LABEL = "savetube.io/job-id"
VIDEO_ID_ANNOTATION = "savetube.io/video-id"


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


async def create_recorder_job(namespace: str, cronjob_name: str, job_id: int, video_id: str) -> str:
    async with AsyncClient(namespace=namespace) as client:
        cronjob = await client.get(CronJob, cronjob_name)
        job = await client.create(recorder_job(cronjob, job_id, video_id))
        return job.metadata.name
