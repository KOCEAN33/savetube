import pytest
from lightkube.resources.batch_v1 import CronJob

from savetube.k8s import recorder_job


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
