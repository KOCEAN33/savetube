from datetime import UTC, datetime

import pytest
from lightkube.resources.batch_v1 import CronJob, Job
from lightkube.resources.core_v1 import Pod

from savetube.k8s import recorder_job, recorder_states, recorder_summaries


def _cronjob(container_name: str = "recorder") -> CronJob:
    return CronJob.from_dict(
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "metadata": {"name": "savetube-recorder", "uid": "cj-uid"},
            "spec": {
                "schedule": "0 0 31 2 *",
                "suspend": True,
                "jobTemplate": {
                    "metadata": {"labels": {"app.kubernetes.io/component": "recorder"}},
                    "spec": {
                        "backoffLimit": 3,
                        "template": {
                            "spec": {
                                "restartPolicy": "Never",
                                "initContainers": [
                                    {
                                        "name": "pot-provider",
                                        "image": "bgutil",
                                        "restartPolicy": "Always",
                                    }
                                ],
                                "containers": [
                                    {
                                        "name": container_name,
                                        "image": "savetube:abc",
                                        "args": ["record"],
                                    }
                                ],
                            }
                        },
                    },
                },
            },
        }
    )


def test_recorder_job_copies_template_and_sets_args():
    cj = _cronjob()
    job = recorder_job(cj, 42, "dQw4w9WgXcQ").to_dict()
    assert job["metadata"]["name"] == "savetube-recorder-42"
    assert job["metadata"]["labels"] == {
        "app.kubernetes.io/component": "recorder",
        "savetube.io/job-id": "42",
    }
    assert job["metadata"]["annotations"] == {"savetube.io/video-id": "dQw4w9WgXcQ"}
    assert job["metadata"]["ownerReferences"] == [
        {
            "apiVersion": "batch/v1",
            "kind": "CronJob",
            "name": "savetube-recorder",
            "uid": "cj-uid",
            "controller": False,
        }
    ]
    pod = job["spec"]["template"]["spec"]
    assert pod["containers"][0]["args"] == ["record", "--job-id", "42"]
    assert pod["containers"][0]["image"] == "savetube:abc"
    assert pod["initContainers"][0]["name"] == "pot-provider"
    assert job["spec"]["backoffLimit"] == 3
    assert job["spec"]["template"]["metadata"]["labels"] == {"savetube.io/job-id": "42"}
    # The template itself is untouched for the next Job.
    assert cj.spec.jobTemplate.spec.template.spec.containers[0].args == ["record"]


def test_recorder_job_needs_the_recorder_container():
    with pytest.raises(ValueError):
        recorder_job(_cronjob("other"), 1, "x")


def _job(job_id, *conditions):
    return Job.from_dict(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"savetube-recorder-{job_id}",
                "labels": {"savetube.io/job-id": str(job_id)},
            },
            "status": {
                "conditions": [{"type": t, "status": s} for t, s in conditions],
            },
        }
    )


def test_recorder_states_reads_a_failed_job_by_its_condition():
    jobs = [
        # The controller sets FailureTarget, then Failed, once backoffLimit is spent.
        _job(7, ("FailureTarget", "True"), ("Failed", "True")),
        _job(8, ("SuccessCriteriaMet", "True"), ("Complete", "True")),
        _job(9),
        _job(10, ("Failed", "False")),
    ]
    assert recorder_states(jobs) == {7: True, 8: False, 9: False, 10: False}


def _pod(name, phase, *, created, job_id=None):
    pod = {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {
            "name": name,
            "creationTimestamp": created,
            "labels": {"savetube.io/job-id": str(job_id)} if job_id else {},
        },
        "status": {
            "phase": phase,
            "startTime": created,
        },
    }
    return Pod.from_dict(pod)


def _recorder(job_id, *, active=0, failed=0, conditions=()):
    return Job.from_dict(
        {
            "apiVersion": "batch/v1",
            "kind": "Job",
            "metadata": {
                "name": f"savetube-recorder-{job_id}",
                "labels": {"savetube.io/job-id": str(job_id)},
                "annotations": {"savetube.io/video-id": f"vid{job_id}"},
            },
            "status": {
                "startTime": "2026-10-24T01:00:00Z",
                "active": active,
                "failed": failed,
                "conditions": [{"type": t, "status": "True"} for t in conditions],
            },
        }
    )


def test_a_recorder_shows_its_newest_pod_and_its_failed_retries():
    jobs = [
        _recorder(7, active=1, failed=1),
        _recorder(8, conditions=["Complete"]),
        _recorder(9, failed=4, conditions=["Failed"]),
        _recorder(10),
    ]
    pods = [
        _pod("savetube-recorder-7-b", "Running", created="2026-10-24T01:05:00Z", job_id=7),
        _pod("savetube-recorder-7-a", "Failed", created="2026-10-24T01:00:00Z", job_id=7),
        _pod("savetube-recorder-8-a", "Succeeded", created="2026-10-24T01:00:00Z", job_id=8),
    ]
    rows = {r["job_id"]: r for r in recorder_summaries(jobs, pods)}
    assert rows[7] == {
        "job_id": 7,
        "video_id": "vid7",
        "name": "savetube-recorder-7",
        "status": "running",
        "pod_phase": "Running",
        "restarts": 1,
        "started_at": datetime(2026, 10, 24, 1, 0, tzinfo=UTC),
    }
    assert (rows[8]["status"], rows[8]["pod_phase"]) == ("succeeded", "Succeeded")
    assert (rows[9]["status"], rows[9]["restarts"]) == ("failed", 4)
    # A pod not made yet, or one the TTL removed.
    assert (rows[10]["status"], rows[10]["pod_phase"]) == ("pending", None)
