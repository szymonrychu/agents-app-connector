"""Chart render: admin and app Deployments, singleton app with a PVC, ingress, required values."""

import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest
import yaml

CHART = Path(__file__).resolve().parent.parent / "chart" / "agents-app-connector"
FULLNAME = "ac-agents-app-connector"

DB_PASS = "".join(["db", "-", "fixture", "-", "value"])
ORCH_TOKEN = "".join(["orch", "-", "fixture", "-", "value"])

BASE = {
    "image": {"tag": "1.2.3"},
    "postgres": {"url": "postgresql://" + "u:" + DB_PASS + "@db.example.com:5432/a"},
    "orchestrator": {"url": "http://orchestrator.example.com", "token": ORCH_TOKEN},
}
INGRESSES = {"ingress": {"device": {"enabled": True}, "admin": {"enabled": True}}}


def _merge(a: dict, b: dict) -> dict:
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def _run(values: dict) -> subprocess.CompletedProcess:
    helm = shutil.which("helm")
    assert helm, "helm binary not found on PATH; the chart tests require it"
    with tempfile.TemporaryDirectory() as tmp:
        vf = Path(tmp) / "values.yaml"
        vf.write_text(yaml.safe_dump(values))
        return subprocess.run(
            [helm, "template", "ac", str(CHART), "-n", "agents", "-f", str(vf)],
            capture_output=True,
            text=True,
        )


def render(overrides: dict | None = None, base: dict = BASE) -> list[dict]:
    proc = _run(_merge(base, overrides or {}))
    assert proc.returncode == 0, proc.stderr
    return [d for d in yaml.safe_load_all(proc.stdout) if d]


def deployment(docs: list[dict], component: str) -> dict:
    (dep,) = [
        d
        for d in docs
        if d["kind"] == "Deployment" and d["metadata"]["name"] == f"{FULLNAME}-{component}"
    ]
    return dep


def pod(docs: list[dict], component: str) -> dict:
    return deployment(docs, component)["spec"]["template"]["spec"]


def main(docs: list[dict], component: str) -> dict:
    (container,) = pod(docs, component)["containers"]
    return container


def _env(container: dict) -> dict[str, dict]:
    return {e["name"]: e for e in container.get("env", [])}


def _by_name(docs: list[dict], kind: str) -> dict[str, dict]:
    return {d["metadata"]["name"]: d for d in docs if d["kind"] == kind}


def test_two_components_app_is_single_recreate_with_pvc():
    docs = render()
    assert sorted(_by_name(docs, "Deployment")) == [f"{FULLNAME}-admin", f"{FULLNAME}-app"]
    admin = deployment(docs, "admin")
    assert admin["spec"]["replicas"] == 1
    assert "strategy" not in admin["spec"]
    assert all(v.get("persistentVolumeClaim") is None for v in pod(docs, "admin")["volumes"])
    app = deployment(docs, "app")
    assert app["spec"]["replicas"] == 1
    assert app["spec"]["strategy"] == {"type": "Recreate"}
    spec = pod(docs, "app")
    (data,) = [v for v in spec["volumes"] if v["name"] == "data"]
    assert data["persistentVolumeClaim"]["claimName"] == f"{FULLNAME}-data"
    container = main(docs, "app")
    assert {"name": "data", "mountPath": "/data"} in container["volumeMounts"]
    assert _env(container)["APPCONN_DATA_DIR"]["value"] == "/data"
    (pvc,) = [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    assert pvc["metadata"]["name"] == f"{FULLNAME}-data"
    assert pvc["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "5Gi"
    assert "storageClassName" not in pvc["spec"]


def test_commands_ports_and_migrate_init():
    docs = render()
    for component, port in (("admin", 8000), ("app", 8001)):
        spec = pod(docs, component)
        (init,) = spec["initContainers"]
        assert init["name"] == "migrate"
        assert init["args"] == ["migrate"]
        assert "command" not in init
        container = main(docs, component)
        assert container["name"] == component
        assert container["args"] == [component]
        assert container["image"] == "registry.example.com/agents-app-connector:1.2.3"
        assert container["ports"] == [{"name": "http", "containerPort": port, "protocol": "TCP"}]
        assert container["readinessProbe"]["httpGet"] == {"path": "/readyz", "port": port}
        assert container["livenessProbe"]["httpGet"] == {"path": "/healthz", "port": port}
        assert spec["automountServiceAccountToken"] is False
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap"]
    assert cm["data"]["APPCONN_HTTP_PORT"] == "8000"
    assert cm["data"]["APPCONN_DEVICE_PORT"] == "8001"
    assert cm["data"]["APPCONN_ORCHESTRATOR_URL"] == "http://orchestrator.example.com"


def test_services_map_80_to_component_ports():
    docs = render()
    services = _by_name(docs, "Service")
    assert sorted(services) == [f"{FULLNAME}-admin", f"{FULLNAME}-app"]
    for component, port in (("admin", 8000), ("app", 8001)):
        svc = services[f"{FULLNAME}-{component}"]
        assert [(p["port"], p["targetPort"]) for p in svc["spec"]["ports"]] == [(80, "http")]
        assert svc["spec"]["selector"]["app.kubernetes.io/component"] == component
        assert main(docs, component)["ports"][0]["containerPort"] == port


def test_ingress_off_by_default_and_routes_only_device_prefix_and_admin():
    assert not [d for d in render() if d["kind"] == "Ingress"]
    docs = render(INGRESSES)
    ingresses = _by_name(docs, "Ingress")
    assert sorted(ingresses) == [f"{FULLNAME}-admin", f"{FULLNAME}-device"]
    paths = {}
    for ing in ingresses.values():
        for rule in ing["spec"]["rules"]:
            for p in rule["http"]["paths"]:
                paths[p["path"]] = p["backend"]["service"]["name"]
                assert p["pathType"] == "Prefix"
    assert paths == {
        "/app/v1/sketch": f"{FULLNAME}-app",
        "/app/admin": f"{FULLNAME}-admin",
    }
    only_device = render({"ingress": {"device": {"enabled": True}}})
    assert [d["metadata"]["name"] for d in only_device if d["kind"] == "Ingress"] == [
        f"{FULLNAME}-device"
    ]


def test_device_ingress_body_size_and_rate_annotations():
    docs = render(INGRESSES)
    ann = _by_name(docs, "Ingress")[f"{FULLNAME}-device"]["metadata"]["annotations"]
    assert ann["nginx.ingress.kubernetes.io/proxy-body-size"] == "9m"
    assert ann["nginx.ingress.kubernetes.io/limit-rps"] == "5"
    assert "nginx.ingress.kubernetes.io/denylist-source-range" not in ann
    admin_ann = _by_name(docs, "Ingress")[f"{FULLNAME}-admin"]["metadata"].get("annotations") or {}
    assert "nginx.ingress.kubernetes.io/proxy-body-size" not in admin_ann
    docs = render(
        {
            **INGRESSES,
            "ingress": {**INGRESSES["ingress"], "denySourceRanges": ["10.0.0.0/8", "192.0.2.0/24"]},
        }
    )
    ann = _by_name(docs, "Ingress")[f"{FULLNAME}-device"]["metadata"]["annotations"]
    assert ann["nginx.ingress.kubernetes.io/denylist-source-range"] == "10.0.0.0/8,192.0.2.0/24"
    # the 9m proxy cap covers the default device body cap (png 6 MiB + strokes 2 MiB + overhead)
    assert 6291456 + 2097152 + 16384 + 65536 < 9 * 1024 * 1024


def test_admin_pod_annotations_and_cidrs():
    docs = render(
        {
            "adminAllowedCidrs": ["10.1.0.0/16", "192.0.2.7/32"],
            "components": {"admin": {"podAnnotations": {"sso/enabled": "true"}}},
        }
    )
    admin = deployment(docs, "admin")["spec"]["template"]
    assert admin["metadata"]["annotations"]["sso/enabled"] == "true"
    env = _env(main(docs, "admin"))
    assert env["APPCONN_ADMIN_ALLOWED_CIDRS"]["value"] == "10.1.0.0/16,192.0.2.7/32"
    assert "APPCONN_ADMIN_ALLOWED_CIDRS" not in _env(main(docs, "app"))
    default = _env(main(render(), "admin"))
    assert default["APPCONN_ADMIN_ALLOWED_CIDRS"]["value"] == "127.0.0.1/32"


def test_secrets_by_reference_and_inline():
    docs = render()
    secrets = _by_name(docs, "Secret")
    assert secrets[f"{FULLNAME}-database"]["stringData"] == {"uri": BASE["postgres"]["url"]}
    assert secrets[f"{FULLNAME}-orchestrator"]["stringData"] == {"token": ORCH_TOKEN}
    for component in ("admin", "app"):
        env = _env(main(docs, component))
        assert env["APPCONN_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
            "name": f"{FULLNAME}-database",
            "key": "uri",
        }
        assert env["APPCONN_ORCHESTRATOR_TOKEN"]["valueFrom"]["secretKeyRef"] == {
            "name": f"{FULLNAME}-orchestrator",
            "key": "token",
        }
        # migrate needs only the database
        (init,) = pod(docs, component)["initContainers"]
        assert "APPCONN_ORCHESTRATOR_TOKEN" not in _env(init)
    existing = {
        "postgres": {"url": "", "existingSecret": "my-db", "existingSecretKey": "dsn"},
        "orchestrator": {"token": "", "existingSecret": "my-orch", "existingSecretKey": "tok"},
    }
    docs = render(existing)
    assert not [d for d in docs if d["kind"] == "Secret"]
    env = _env(main(docs, "app"))
    assert env["APPCONN_DATABASE_URL"]["valueFrom"]["secretKeyRef"] == {
        "name": "my-db",
        "key": "dsn",
    }
    assert env["APPCONN_ORCHESTRATOR_TOKEN"]["valueFrom"]["secretKeyRef"] == {
        "name": "my-orch",
        "key": "tok",
    }
    assert ORCH_TOKEN not in yaml.safe_dump(docs)


def test_persistence_options():
    docs = render({"persistence": {"size": "10Gi", "storageClass": "fast"}})
    (pvc,) = [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    assert pvc["spec"]["resources"]["requests"]["storage"] == "10Gi"
    assert pvc["spec"]["storageClassName"] == "fast"
    docs = render({"persistence": {"existingClaim": "my-claim"}})
    assert not [d for d in docs if d["kind"] == "PersistentVolumeClaim"]
    (data,) = [v for v in pod(docs, "app")["volumes"] if v["name"] == "data"]
    assert data["persistentVolumeClaim"]["claimName"] == "my-claim"


@pytest.mark.parametrize("replicas", [0, 2, 3])
def test_app_replicas_cannot_be_raised(replicas):
    proc = _run(_merge(BASE, {"components": {"app": {"replicas": replicas}}}))
    assert proc.returncode != 0
    assert "singleton" in proc.stderr


def test_app_replicas_one_is_accepted():
    docs = render({"components": {"app": {"replicas": 1}}})
    assert deployment(docs, "app")["spec"]["replicas"] == 1


@pytest.mark.parametrize(
    ("override", "message"),
    [
        ({"postgres": {"url": ""}}, "Postgres connection is required"),
        (
            {"postgres": {"existingSecret": "x"}},
            "only one of postgres.url and postgres.existingSecret",
        ),
        ({"orchestrator": {"url": ""}}, "orchestrator.url is required"),
        ({"orchestrator": {"token": ""}}, "orchestrator token is required"),
        (
            {"orchestrator": {"existingSecret": "x"}},
            "only one of orchestrator.token and orchestrator.existingSecret",
        ),
        ({"env": {"OTHER_KEY": "1"}}, "must start with APPCONN_"),
    ],
)
def test_required_values(override, message):
    proc = _run(_merge(BASE, override))
    assert proc.returncode != 0
    assert message in proc.stderr


def test_extra_env_lands_in_configmap():
    docs = render({"env": {"APPCONN_RETENTION_DAYS": 14}})
    (cm,) = [d for d in docs if d["kind"] == "ConfigMap"]
    assert cm["data"]["APPCONN_RETENTION_DAYS"] == "14"


def test_pods_locked_down():
    docs = render()
    for component in ("admin", "app"):
        spec = pod(docs, component)
        assert spec["securityContext"]["runAsNonRoot"] is True
        assert spec["securityContext"]["seccompProfile"] == {"type": "RuntimeDefault"}
        assert {"name": "tmp", "emptyDir": {}} in spec["volumes"]
        for container in [*spec["initContainers"], *spec["containers"]]:
            sc = container["securityContext"]
            assert sc["allowPrivilegeEscalation"] is False
            assert sc["readOnlyRootFilesystem"] is True
            assert sc["capabilities"] == {"drop": ["ALL"]}
            assert {"name": "tmp", "mountPath": "/tmp"} in container["volumeMounts"]


def test_no_internal_hostnames_in_defaults():
    text = (CHART / "values.yaml").read_text() + (CHART / "ci" / "lint-values.yaml").read_text()
    assert "szymonrichert" not in text
