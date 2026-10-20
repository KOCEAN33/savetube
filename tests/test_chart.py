"""The Helm chart, tested at its rendered output and at its contract with the code."""

import shutil
import subprocess
from pathlib import Path

import pytest
import yaml
from lightkube.resources.batch_v1 import CronJob

from savetube.config import Settings
from savetube.k8s import RECORDER_CONTAINER, recorder_job

CHART = Path(__file__).parent.parent / "charts" / "savetube"
IMAGE = "harbor.lab.hiriri.com/savetube/savetube:abc123"

VALUES = {
    "image": {"repository": "harbor.lab.hiriri.com/savetube/savetube", "tag": "abc123"},
    "publicUrl": "https://savetube.lab.hiriri.com",
    "oidc": {"clientId": "391734900258046999", "requiredGroup": "savetube-admin"},
    "archive": {"existingClaim": "savetube-archive"},
    "cookies": {"existingSecret": "savetube-cookies"},
}


def render(values: dict = VALUES, release: str = "savetube") -> list[dict]:
    helm = shutil.which("helm") or pytest.fail("helm is not installed")
    out = subprocess.run(
        [helm, "template", release, str(CHART), "--namespace", "savetube-prod", "-f", "-"],
        input=yaml.safe_dump(values),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [d for d in yaml.safe_load_all(out) if d]


def one(docs: list[dict], kind: str, name: str) -> dict:
    found = [d for d in docs if d["kind"] == kind and d["metadata"]["name"] == name]
    assert len(found) == 1, f"{kind}/{name}: {len(found)} found"
    return found[0]


def pod_spec(obj: dict) -> dict:
    if obj["kind"] == "CronJob":
        return obj["spec"]["jobTemplate"]["spec"]["template"]["spec"]
    return obj["spec"]["template"]["spec"]


def workloads(docs: list[dict]) -> dict[str, dict]:
    return {
        "web": one(docs, "Deployment", "savetube-web"),
        "scheduler": one(docs, "Deployment", "savetube-scheduler"),
        "worker": one(docs, "Deployment", "savetube-worker"),
        "recorder": one(docs, "CronJob", "savetube-recorder"),
    }


# Anchor 1: one image everywhere, a suspended recorder template, and the PO token
# sidecar on loopback in every pod that runs yt-dlp.
def test_every_role_runs_the_one_image_and_yt_dlp_pods_carry_the_sidecar():
    w = workloads(render())
    for role, obj in w.items():
        spec = pod_spec(obj)
        app = [c for c in spec["containers"] if c["name"] != "pot-provider"]
        assert [c["image"] for c in app] == [IMAGE], role

    assert w["recorder"]["spec"]["suspend"] is True

    for role in ("scheduler", "worker", "recorder"):
        sidecars = [
            c for c in pod_spec(w[role]).get("initContainers", []) if c["name"] == "pot-provider"
        ]
        assert len(sidecars) == 1, role
        assert sidecars[0]["restartPolicy"] == "Always", role
        assert sidecars[0]["args"] == ["--host", "127.0.0.1"], role
    web_init = pod_spec(w["web"]).get("initContainers", [])
    assert [c["name"] for c in web_init] == []


# Anchor 2: the rendered template goes through the scheduler's own code.
def test_rendered_cronjob_makes_a_recorder_job():
    cj = CronJob.from_dict(one(render(), "CronJob", "savetube-recorder"))
    job = recorder_job(cj, 42, "dQw4w9WgXcQ").to_dict()

    assert job["metadata"]["name"] == "savetube-recorder-42"
    spec = job["spec"]["template"]["spec"]
    (recorder,) = [c for c in spec["containers"] if c["name"] == RECORDER_CONTAINER]
    assert recorder["image"] == IMAGE
    assert recorder["args"] == ["record", "--job-id", "42"]
    assert "pot-provider" in [c["name"] for c in spec["initContainers"]]

    volumes = {v["name"]: v for v in spec["volumes"]}
    mounts = {m["mountPath"]: m for m in recorder["volumeMounts"]}
    archive = volumes[mounts["/archive"]["name"]]
    assert archive["persistentVolumeClaim"]["claimName"] == "savetube-archive"

    env = {e["name"]: e for e in recorder["env"]}
    cookie_path = Path(env["SAVETUBE_COOKIES_FILE"]["value"])
    (cookie_mount,) = [
        m for m in recorder["volumeMounts"] if m["mountPath"] == str(cookie_path.parent)
    ]
    assert cookie_mount.get("readOnly") is True
    cookie_volume = volumes[cookie_mount["name"]]
    assert cookie_volume["secret"]["secretName"] == "savetube-cookies"
    assert [i["path"] for i in cookie_volume["secret"]["items"]] == [cookie_path.name]


# Anchor 3: least privilege and a single migrator.
def test_only_the_scheduler_talks_to_the_api_server():
    docs = render()
    role = one(docs, "Role", "savetube-scheduler")
    assert role["rules"] == [
        {"apiGroups": ["batch"], "resources": ["cronjobs"], "verbs": ["get"]},
        {"apiGroups": ["batch"], "resources": ["jobs"], "verbs": ["create", "list"]},
    ]
    binding = one(docs, "RoleBinding", "savetube-scheduler")
    assert binding["roleRef"] == {
        "apiGroup": "rbac.authorization.k8s.io",
        "kind": "Role",
        "name": "savetube-scheduler",
    }
    scheduler_spec = pod_spec(one(docs, "Deployment", "savetube-scheduler"))
    assert binding["subjects"] == [
        {
            "kind": "ServiceAccount",
            "name": scheduler_spec["serviceAccountName"],
            "namespace": "savetube-prod",
        }
    ]
    w = workloads(docs)
    for role_name in ("web", "worker", "recorder"):
        assert pod_spec(w[role_name])["automountServiceAccountToken"] is False, role_name


def test_scheduler_is_a_single_migrating_replica():
    scheduler = one(render(), "Deployment", "savetube-scheduler")
    assert scheduler["spec"]["replicas"] == 1
    assert scheduler["spec"]["strategy"] == {"type": "Recreate"}
    init = {c["name"]: c for c in pod_spec(scheduler)["initContainers"]}
    assert init["migrate"]["image"] == IMAGE
    assert init["migrate"]["args"] == ["migrate"]


# Contract with Settings: every variable the chart sets is one the code reads,
# and the scheduler looks for the CronJob the chart actually renders.
def test_chart_env_names_are_settings_fields():
    fields = {f"SAVETUBE_{name.upper()}" for name in Settings.model_fields}
    for role, obj in workloads(render()).items():
        spec = pod_spec(obj)
        for c in spec["containers"] + spec.get("initContainers", []):
            for e in c.get("env", []):
                if e["name"].startswith("SAVETUBE_"):
                    assert e["name"] in fields, f"{role}/{c['name']}: {e['name']}"


@pytest.mark.parametrize("release", ["savetube", "other"])
def test_scheduler_names_the_rendered_cronjob(release):
    docs = render(release=release)
    (cronjob,) = [d for d in docs if d["kind"] == "CronJob"]
    (scheduler,) = [
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"].endswith("-scheduler")
    ]
    (app,) = [c for c in pod_spec(scheduler)["containers"] if c["name"] == "scheduler"]
    env = {e["name"]: e for e in app["env"]}
    assert env["SAVETUBE_RECORDER_CRONJOB"]["value"] == cronjob["metadata"]["name"]
    assert env["SAVETUBE_NAMESPACE"]["valueFrom"] == {
        "fieldRef": {"fieldPath": "metadata.namespace"}
    }


def test_oidc_values_reach_web():
    web = one(render(), "Deployment", "savetube-web")
    (app,) = pod_spec(web)["containers"]
    env = {e["name"]: e.get("value") for e in app["env"]}
    assert env["SAVETUBE_OIDC_CLIENT_ID"] == "391734900258046999"
    assert env["SAVETUBE_OIDC_REQUIRED_GROUP"] == "savetube-admin"
    assert env["SAVETUBE_PUBLIC_URL"] == "https://savetube.lab.hiriri.com"


# Expected shape from homelab-platform docs/deploying-to-kubernetes.md: every
# Gateway API default written out, or Argo CD reads OutOfSync forever.
def test_httproute_writes_gateway_defaults_out():
    values = {
        **VALUES,
        "httpRoute": {
            "enabled": True,
            "hostnames": ["savetube.lab.hiriri.com"],
            "parentRefs": [{"name": "homelab", "namespace": "gateway"}],
        },
    }
    route = one(render(values), "HTTPRoute", "savetube")
    assert route["apiVersion"] == "gateway.networking.k8s.io/v1"
    assert route["spec"] == {
        "parentRefs": [
            {
                "group": "gateway.networking.k8s.io",
                "kind": "Gateway",
                "name": "homelab",
                "namespace": "gateway",
            }
        ],
        "hostnames": ["savetube.lab.hiriri.com"],
        "rules": [
            {
                "matches": [{"path": {"type": "PathPrefix", "value": "/"}}],
                "backendRefs": [
                    {
                        "group": "",
                        "kind": "Service",
                        "name": "savetube-web",
                        "port": 80,
                        "weight": 1,
                    }
                ],
            }
        ],
    }


def test_no_httproute_unless_enabled():
    assert [d for d in render() if d["kind"] == "HTTPRoute"] == []
