"""Tests for the OpenAPI public HTTPS pipeline (ingress-nginx + cert-manager).

Responsibility: Verify the manifest builders, kubectl-driven setup/disable
    Celery tasks, runtime cache fallback, and route enqueue contracts for the
    `setup_openapi_public_https` / `disable_openapi_public_https` flow.
Edit boundaries: Keep assertions focused on shape + idempotency + cache contract.
    Network, real cert issuance, and live AKS are out of scope — those belong
    in deploy validation, not unit tests.
Key entry points: `test_dns_label_is_stable_per_cluster`,
    `test_ingress_manifest_has_tls_redirect_and_cert_issuer`,
    `test_setup_public_https_runs_full_pipeline`,
    `test_setup_public_https_idempotent_kubeconfig_reuse`,
    `test_disable_public_https_clears_cache`,
    `test_public_tls_base_url_falls_back_to_runtime_cache`.
Risky contracts: Do not require network access, real kubectl, or real Redis.
Validation: `uv run pytest -q api/tests/test_openapi_public_https.py`.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

# ----------------------------------------------------------------------- builders


def test_dns_label_is_stable_per_cluster() -> None:
    from api.services.k8s.ingress import dns_label_for_cluster

    label_a = dns_label_for_cluster(subscription_id="sub-1", cluster_name="elb-cluster")
    label_b = dns_label_for_cluster(subscription_id="sub-1", cluster_name="elb-cluster")
    label_c = dns_label_for_cluster(subscription_id="sub-1", cluster_name="other-cluster")
    label_d = dns_label_for_cluster(subscription_id="sub-2", cluster_name="elb-cluster")

    assert label_a == label_b, "same inputs must produce same label (idempotent)"
    assert label_a != label_c, "different cluster names must differ"
    assert label_a != label_d, "different subscriptions must differ"
    assert label_a.startswith("elb-openapi-")
    # Azure DNS label limit is 63 chars; our format is always well under.
    assert len(label_a) <= 63


def test_cloudapp_fqdn_format() -> None:
    from api.services.k8s.ingress import cloudapp_fqdn

    assert (
        cloudapp_fqdn(dns_label="elb-openapi-abc123", region="koreacentral")
        == "elb-openapi-abc123.koreacentral.cloudapp.azure.com"
    )


def test_cluster_issuer_uses_letsencrypt_prod_with_http01() -> None:
    from api.services.k8s.ingress import build_cluster_issuer

    issuer = json.loads(build_cluster_issuer(email="ops@example.com"))
    assert issuer["kind"] == "ClusterIssuer"
    assert issuer["metadata"]["name"] == "letsencrypt-prod"
    spec = issuer["spec"]["acme"]
    assert spec["server"].endswith("/acme-v02.api.letsencrypt.org/directory")
    assert spec["email"] == "ops@example.com"
    http01 = spec["solvers"][0]["http01"]["ingress"]
    assert http01["class"] == "nginx"
    # The HTTP-01 solver Pod that cert-manager spawns per Challenge must
    # carry the same blastpool toleration + user-mode nodeSelector as the
    # ingress-nginx / cert-manager install patch — otherwise on a cluster
    # whose only schedulable pool is the blastpool (`workload=blast:NoSchedule`)
    # the solver Pod sits in `Pending` forever and Let's Encrypt times out
    # the challenge with a 503. Regression on elb-cluster-01, 2026-05-28.
    pod_spec = http01["podTemplate"]["spec"]
    assert pod_spec["nodeSelector"] == {"kubernetes.azure.com/mode": "user"}
    assert any(
        t.get("key") == "workload"
        and t.get("operator") == "Equal"
        and t.get("value") == "blast"
        and t.get("effect") == "NoSchedule"
        for t in pod_spec["tolerations"]
    )


def test_ingress_manifest_has_tls_redirect_and_cert_issuer() -> None:
    from api.services.k8s.ingress import build_openapi_ingress

    fqdn = "elb-openapi-abc123.koreacentral.cloudapp.azure.com"
    ingress = json.loads(build_openapi_ingress(fqdn=fqdn))
    assert ingress["kind"] == "Ingress"
    annotations = ingress["metadata"]["annotations"]
    assert annotations["cert-manager.io/cluster-issuer"] == "letsencrypt-prod"
    assert annotations["nginx.ingress.kubernetes.io/ssl-redirect"] == "true"
    # 100m matches the existing api-sidecar streaming proxy ceiling so the
    # ingress does not 413 BLAST query uploads before the proxy can.
    assert annotations["nginx.ingress.kubernetes.io/proxy-body-size"] == "100m"
    tls = ingress["spec"]["tls"][0]
    assert tls["hosts"] == [fqdn]
    assert tls["secretName"] == "elb-openapi-tls"
    rule = ingress["spec"]["rules"][0]
    assert rule["host"] == fqdn
    backend = rule["http"]["paths"][0]["backend"]["service"]
    assert backend["name"] == "elb-openapi"
    assert backend["port"]["number"] == 80


def test_dns_label_patch_uses_azure_annotation() -> None:
    from api.services.k8s.ingress import build_dns_label_patch

    patch = json.loads(build_dns_label_patch(dns_label="elb-openapi-abc123"))
    assert (
        patch["metadata"]["annotations"]["service.beta.kubernetes.io/azure-dns-label-name"]
        == "elb-openapi-abc123"
    )


# --------------------------------------------------------------------- workload-pool patch
# Anchors:
#   - api/services/k8s/ingress.py::patch_manifest_for_workload_pool
#   - api/services/k8s/ingress.py::fetch_install_manifest_for_workload_pool
# Regression for the 2026-05-28 incident: the previous systempool-only patch
# left ingress-nginx Pending on default DS2_v2 (2 vCPU) systempools whose
# allocatable was 99% requested by AKS system add-ons. The patch now moves
# ingress-nginx + cert-manager onto the BLAST workload pool, which is large
# (D4s+) and has CPU headroom. The toleration switches from
# `CriticalAddonsOnly:Exists` to `workload=blast:NoSchedule` and the
# nodeSelector switches from `mode=system` to `mode=user`. The
# `SYSTEM_POOL_*` / `_for_system_pool` aliases were removed in the
# follow-up clean-up; the canonical names live under `WORKLOAD_POOL_*`
# and `_for_workload_pool`.


def _safe_load_all(text: str) -> list[Any]:
    import yaml as _yaml

    return list(_yaml.safe_load_all(text))


def test_patch_manifest_for_workload_pool_injects_toleration_and_node_selector() -> None:
    import yaml as _yaml
    from api.services.k8s.ingress import patch_manifest_for_workload_pool

    raw = _yaml.safe_dump_all(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "cert-manager", "namespace": "cert-manager"},
                "spec": {
                    "template": {"spec": {"containers": [{"name": "controller"}]}},
                },
            },
            {
                "apiVersion": "batch/v1",
                "kind": "Job",
                "metadata": {
                    "name": "ingress-nginx-admission-create",
                    "namespace": "ingress-nginx",
                },
                "spec": {
                    "template": {"spec": {"containers": [{"name": "create"}]}},
                },
            },
        ]
    )

    patched = _safe_load_all(patch_manifest_for_workload_pool(raw))

    for doc in patched:
        pod_spec = doc["spec"]["template"]["spec"]
        tolerations = pod_spec["tolerations"]
        assert any(
            t.get("key") == "workload"
            and t.get("operator") == "Equal"
            and t.get("value") == "blast"
            and t.get("effect") == "NoSchedule"
            for t in tolerations
        ), f"missing workload=blast toleration on {doc['kind']}"
        # systempool toleration must NOT be injected — the policy moved to
        # the blastpool (user-mode pool), so `CriticalAddonsOnly` would
        # incorrectly admit the pod onto the starved systempool.
        assert not any(t.get("key") == "CriticalAddonsOnly" for t in tolerations), (
            f"{doc['kind']} must not tolerate CriticalAddonsOnly anymore"
        )
        assert pod_spec["nodeSelector"]["kubernetes.azure.com/mode"] == "user"


def test_patch_manifest_for_workload_pool_preserves_existing_settings() -> None:
    import yaml as _yaml
    from api.services.k8s.ingress import patch_manifest_for_workload_pool

    raw = _yaml.safe_dump(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "ingress-nginx-controller"},
            "spec": {
                "template": {
                    "spec": {
                        "containers": [{"name": "controller"}],
                        "tolerations": [
                            {
                                "key": "node-role.kubernetes.io/control-plane",
                                "operator": "Exists",
                                "effect": "NoSchedule",
                            },
                        ],
                        "nodeSelector": {"kubernetes.io/os": "linux"},
                    },
                },
            },
        }
    )

    patched = _safe_load_all(patch_manifest_for_workload_pool(raw))[0]
    pod_spec = patched["spec"]["template"]["spec"]
    keys = [t.get("key") for t in pod_spec["tolerations"]]
    # Existing operator-managed toleration is preserved AND the workload-pool
    # toleration is appended (rather than the list being replaced).
    assert "node-role.kubernetes.io/control-plane" in keys
    assert "workload" in keys
    # Existing nodeSelector keys must survive the patch.
    assert pod_spec["nodeSelector"]["kubernetes.io/os"] == "linux"
    assert pod_spec["nodeSelector"]["kubernetes.azure.com/mode"] == "user"


def test_patch_manifest_for_workload_pool_is_idempotent() -> None:
    import yaml as _yaml
    from api.services.k8s.ingress import patch_manifest_for_workload_pool

    raw = _yaml.safe_dump(
        {
            "apiVersion": "apps/v1",
            "kind": "Deployment",
            "metadata": {"name": "cert-manager-webhook"},
            "spec": {"template": {"spec": {"containers": [{"name": "webhook"}]}}},
        }
    )

    once = patch_manifest_for_workload_pool(raw)
    twice = patch_manifest_for_workload_pool(once)

    once_pod = _safe_load_all(once)[0]["spec"]["template"]["spec"]
    twice_pod = _safe_load_all(twice)[0]["spec"]["template"]["spec"]
    # Re-running the transform on already-patched YAML must not duplicate
    # the workload-pool toleration (regression guard for "every retry of
    # setup_openapi_public_https doubles the toleration list").
    workload_count = sum(1 for t in twice_pod["tolerations"] if t.get("key") == "workload")
    assert workload_count == 1
    assert once_pod["nodeSelector"] == twice_pod["nodeSelector"]


def test_patch_manifest_for_workload_pool_passes_through_non_workload_docs() -> None:
    import yaml as _yaml
    from api.services.k8s.ingress import patch_manifest_for_workload_pool

    raw = _yaml.safe_dump_all(
        [
            {
                "apiVersion": "v1",
                "kind": "ConfigMap",
                "metadata": {"name": "ingress-nginx-controller"},
                "data": {"allow-snippet-annotations": "false"},
            },
            {
                "apiVersion": "v1",
                "kind": "Namespace",
                "metadata": {"name": "cert-manager"},
            },
            None,  # Empty `---` separator doc (trailing in cert-manager.yaml)
        ]
    )

    serialised = patch_manifest_for_workload_pool(raw)
    docs = _safe_load_all(serialised)
    # ConfigMap + Namespace must not have a spec.template injected.
    cm = next(d for d in docs if d and d.get("kind") == "ConfigMap")
    ns = next(d for d in docs if d and d.get("kind") == "Namespace")
    assert "spec" not in cm or "template" not in cm.get("spec", {})
    assert "spec" not in ns or "template" not in ns.get("spec", {})
    # Empty/null docs MUST be dropped before re-serialising — otherwise
    # the trailing `---` in upstream cert-manager.yaml becomes
    # `--- null\n` after dump and `kubectl apply -f -` rejects the whole
    # stream with "invalid Yaml document separator: null". Observed live
    # on elb-cluster-01, 2026-05-27.
    assert "null" not in serialised, (
        "transformed manifest must not contain `--- null` separators that kubectl rejects"
    )


def test_patch_manifest_for_workload_pool_shrinks_container_requests() -> None:
    import yaml as _yaml
    from api.services.k8s.ingress import (
        WORKLOAD_POOL_LOW_CPU_REQUEST,
        WORKLOAD_POOL_LOW_MEMORY_REQUEST,
        patch_manifest_for_workload_pool,
    )

    raw = _yaml.safe_dump_all(
        [
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "ingress-nginx-controller"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "controller",
                                    "resources": {
                                        "requests": {"cpu": "100m", "memory": "90Mi"},
                                        "limits": {"cpu": "1", "memory": "256Mi"},
                                    },
                                }
                            ],
                            "initContainers": [
                                {
                                    "name": "setup",
                                    "resources": {"requests": {"cpu": "250m"}},
                                }
                            ],
                        }
                    }
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "cert-manager"},
                "spec": {
                    "template": {
                        "spec": {"containers": [{"name": "controller"}]},
                    }
                },
            },
            {
                "apiVersion": "apps/v1",
                "kind": "Deployment",
                "metadata": {"name": "tiny-already"},
                "spec": {
                    "template": {
                        "spec": {
                            "containers": [
                                {
                                    "name": "x",
                                    "resources": {"requests": {"cpu": "5m"}},
                                }
                            ]
                        }
                    }
                },
            },
        ]
    )

    patched = _safe_load_all(patch_manifest_for_workload_pool(raw))

    # ingress-nginx-controller: 100m → 20m, 90Mi → 64Mi (both lowered);
    # init-container 250m → 20m; limits untouched.
    controller = next(d for d in patched if d["metadata"]["name"] == "ingress-nginx-controller")
    c_pod = controller["spec"]["template"]["spec"]
    c_main = c_pod["containers"][0]
    assert c_main["resources"]["requests"]["cpu"] == WORKLOAD_POOL_LOW_CPU_REQUEST
    assert c_main["resources"]["requests"]["memory"] == WORKLOAD_POOL_LOW_MEMORY_REQUEST
    assert c_main["resources"]["limits"] == {"cpu": "1", "memory": "256Mi"}
    assert (
        c_pod["initContainers"][0]["resources"]["requests"]["cpu"] == WORKLOAD_POOL_LOW_CPU_REQUEST
    )

    # cert-manager controller had no resources block at all → floor written in.
    cm = next(d for d in patched if d["metadata"]["name"] == "cert-manager")
    cm_main = cm["spec"]["template"]["spec"]["containers"][0]
    assert cm_main["resources"]["requests"]["cpu"] == WORKLOAD_POOL_LOW_CPU_REQUEST
    assert cm_main["resources"]["requests"]["memory"] == WORKLOAD_POOL_LOW_MEMORY_REQUEST

    # tiny-already had cpu=5m (below 20m floor) → preserved (monotonic decreasing).
    tiny = next(d for d in patched if d["metadata"]["name"] == "tiny-already")
    tiny_main = tiny["spec"]["template"]["spec"]["containers"][0]
    assert tiny_main["resources"]["requests"]["cpu"] == "5m"
    # memory had no existing request → floor written in.
    assert tiny_main["resources"]["requests"]["memory"] == WORKLOAD_POOL_LOW_MEMORY_REQUEST


# ------------------------------------------------------------------------ runtime cache


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def set(self, key: str, value: str) -> None:
        self.store[key] = value

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0


def test_public_tls_base_url_falls_back_to_runtime_cache(monkeypatch) -> None:
    from api.services.openapi import runtime

    fake_redis = _FakeRedis()
    monkeypatch.delenv("OPENAPI_PUBLIC_BASE_URL", raising=False)
    monkeypatch.setattr(runtime, "get_ops_redis_client", lambda **_kwargs: fake_redis)

    # No env, no cache → empty string (legacy IP path)
    assert runtime.get_public_tls_base_url() == ""

    # Setup task wrote to the cache → fallback kicks in
    runtime.save_openapi_public_base_url(
        "https://elb-openapi-abc.koreacentral.cloudapp.azure.com",
        metadata={"dns_label": "elb-openapi-abc", "region": "koreacentral"},
        client=fake_redis,
    )
    assert (
        runtime.get_public_tls_base_url()
        == "https://elb-openapi-abc.koreacentral.cloudapp.azure.com"
    )

    payload = runtime.get_openapi_public_base_url(client=fake_redis)
    assert payload["base_url"].endswith("cloudapp.azure.com")
    assert payload["metadata"]["dns_label"] == "elb-openapi-abc"
    assert "updated_at" in payload

    runtime.clear_openapi_public_base_url(client=fake_redis)
    assert runtime.get_openapi_public_base_url(client=fake_redis) == {}

    # Env still wins over cache when both present
    runtime.save_openapi_public_base_url(
        "https://elb-openapi-abc.koreacentral.cloudapp.azure.com",
        client=fake_redis,
    )
    monkeypatch.setenv("OPENAPI_PUBLIC_BASE_URL", "https://override.example.com")
    assert runtime.get_public_tls_base_url() == "https://override.example.com"


# ------------------------------------------------------------------------ task


def _ok(stdout: str = "ok") -> dict[str, Any]:
    return {"exit_code": 0, "stdout": stdout, "stderr": ""}


def _stub_cluster(region: str = "koreacentral") -> Any:
    class _Cluster:
        location = region

    class _AKS:
        class managed_clusters:
            @staticmethod
            def get(_rg: str, _name: str) -> Any:
                return _Cluster()

    return _AKS()


def test_setup_public_https_runs_full_pipeline(monkeypatch) -> None:
    from api.services.openapi import runtime
    from api.tasks.openapi import public_https as task_module

    # Stub Azure SDK
    monkeypatch.setattr(task_module, "get_credential", lambda: object())
    monkeypatch.setattr(task_module, "aks_client", lambda *_a, **_kw: _stub_cluster())

    # Stub kubectl auth
    monkeypatch.setattr(
        task_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake",  # noqa: S108 - fake path returned by mocked auth helper,
    )

    # Stub the network fetch of upstream install manifests so the
    # systempool patch flow goes through `apply -f -` with predictable
    # stdin content the test can assert against. The real transform is
    # exercised by `test_patch_manifest_for_workload_pool_*` below.
    monkeypatch.setattr(
        task_module,
        "fetch_install_manifest_for_workload_pool",
        lambda url, **_kw: f"# patched manifest fetched from {url}\n",
    )

    # Stub kubectl invocations + remember sequence (args + stdin)
    calls: list[list[str]] = []
    stdins: list[str | None] = []

    def fake_kubectl_run(
        args: list[str],
        *,
        kubeconfig_path: str,
        stdin: str | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        calls.append(args)
        stdins.append(stdin)
        # get-service jsonpath → return EXTERNAL-IP
        if args[:3] == ["get", "service", "ingress-nginx-controller"]:
            return _ok(stdout="20.41.1.2")
        if args[:2] == ["rollout", "status"]:
            return _ok()
        if args[:1] == ["wait"]:
            return _ok()
        if args[:1] == ["patch"]:
            return _ok()
        if args[:1] == ["apply"]:
            return _ok()
        if args[:2] == ["get", "certificate"]:
            return _ok(stdout="2026-08-25T00:00:00Z")
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    # Stub runtime cache to in-memory dict
    fake_redis = _FakeRedis()
    monkeypatch.setattr(runtime, "get_ops_redis_client", lambda **_kw: fake_redis)
    monkeypatch.setattr(
        task_module,
        "save_openapi_public_base_url",
        lambda url, **kw: runtime.save_openapi_public_base_url(url, client=fake_redis, **kw),
    )

    result = task_module.setup_openapi_public_https.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        operator_email="ops@example.com",
    )

    assert result["status"] == "succeeded"
    assert result["fqdn"].endswith(".koreacentral.cloudapp.azure.com")
    assert result["public_base_url"].startswith("https://")
    assert result["ingress_lb_ip"] == "20.41.1.2"
    assert result["cert_expires_at"] == "2026-08-25T00:00:00Z"
    assert result["cluster_issuer"] == "letsencrypt-prod"

    # Verify the 8 expected kubectl steps fired in order (the 9th is the
    # runtime cache write which doesn't go through kubectl)
    apply_targets = [c[2] for c in calls if c[:2] == ["apply", "-f"]]
    # Step 1 + step 4 now stream the patched manifest through stdin, so
    # the args are always ["apply", "-f", "-"]. The upstream installer
    # is identified by stdin content instead.
    assert apply_targets, "expected at least one apply step"
    assert all(t == "-" for t in apply_targets), (
        "install manifests must stream through stdin so the systempool patch can be injected"
    )
    install_stdins = [
        s
        for c, s in zip(calls, stdins, strict=True)
        if c[:3] == ["apply", "-f", "-"] and s and "patched manifest fetched" in s
    ]
    assert any("ingress-nginx" in s for s in install_stdins)
    assert any("cert-manager" in s for s in install_stdins)
    # The admission-webhook Job pre-delete must run before the
    # ingress-nginx apply (immutable Job spec from a previous
    # toleration-less install would otherwise block reconciliation).
    delete_indexes = [
        i
        for i, c in enumerate(calls)
        if c[:1] == ["delete"]
        and "job" in c
        and "app.kubernetes.io/component=admission-webhook" in c
    ]
    assert delete_indexes, "expected admission-webhook Job pre-delete"
    apply_indexes = [
        i
        for i, c in enumerate(calls)
        if c[:3] == ["apply", "-f", "-"] and stdins[i] and "ingress-nginx" in (stdins[i] or "")
    ]
    assert apply_indexes and delete_indexes[0] < apply_indexes[0], (
        "Job pre-delete must run before the ingress-nginx apply"
    )
    # Step 2 patch, step 5 wait webhook, step 8 wait certificate
    assert any(c[:1] == ["patch"] for c in calls)
    assert any(c[:1] == ["wait"] and "cert-manager-webhook" in " ".join(c) for c in calls)
    # Regression: must wait for the ingress-nginx controller Deployment
    # to be Available before applying the Ingress, otherwise the
    # admission webhook
    # (``ingress-nginx-controller-admission``) Service has no endpoints
    # and the apply fails with "failed calling webhook
    # validate.nginx.ingress.kubernetes.io".
    assert any(
        c[:1] == ["wait"] and "deployment/ingress-nginx-controller" in " ".join(c) for c in calls
    ), "must wait for ingress-nginx controller Deployment Available"
    # Layered defense (regression for the 2026-05-27 incident family):
    # after the Deployment wait the pipeline must ALSO (b) wait for the
    # admission bootstrap Jobs to Complete (so ``caBundle`` is patched
    # in ValidatingWebhookConfiguration \u2014 otherwise the apiserver
    # rejects the webhook call with "x509: certificate signed by
    # unknown authority") and (c) poll the admission Service's
    # endpoints to close the EndpointSlice publish race.
    assert any(
        c[:1] == ["wait"]
        and "job/ingress-nginx-admission-create" in " ".join(c)
        and "--for=condition=Complete" in " ".join(c)
        for c in calls
    ), "must wait for ingress-nginx-admission-create Job Complete"
    assert any(
        c[:1] == ["wait"]
        and "job/ingress-nginx-admission-patch" in " ".join(c)
        and "--for=condition=Complete" in " ".join(c)
        for c in calls
    ), "must wait for ingress-nginx-admission-patch Job Complete"
    assert any(
        c[:2] == ["get", "endpoints"] and "ingress-nginx-controller-admission" in c for c in calls
    ), "must poll admission Service endpoints before applying Ingress"
    # And the wait must come BEFORE the Ingress apply (which is the
    # second apply-from-stdin that streams the elb-openapi Ingress
    # YAML, after the ingress-nginx + cert-manager installs and the
    # ClusterIssuer).
    ingress_apply_indexes = [
        i
        for i, c in enumerate(calls)
        if c[:3] == ["apply", "-f", "-"] and stdins[i] and "elb-openapi" in (stdins[i] or "")
    ]
    controller_wait_indexes = [
        i
        for i, c in enumerate(calls)
        if c[:1] == ["wait"] and "deployment/ingress-nginx-controller" in " ".join(c)
    ]
    admission_jobs_wait_indexes = [
        i
        for i, c in enumerate(calls)
        if c[:1] == ["wait"] and "job/ingress-nginx-admission-" in " ".join(c)
    ]
    endpoints_probe_indexes = [
        i
        for i, c in enumerate(calls)
        if c[:2] == ["get", "endpoints"] and "ingress-nginx-controller-admission" in c
    ]
    assert ingress_apply_indexes and controller_wait_indexes, (
        "expected both ingress-nginx controller wait and elb-openapi Ingress apply to fire"
    )
    assert controller_wait_indexes[0] < ingress_apply_indexes[0], (
        "ingress-nginx controller wait must run before the Ingress apply"
    )
    assert admission_jobs_wait_indexes and (
        admission_jobs_wait_indexes[0] < ingress_apply_indexes[0]
    ), "admission Jobs wait must run before the Ingress apply"
    assert endpoints_probe_indexes and (endpoints_probe_indexes[0] < ingress_apply_indexes[0]), (
        "admission endpoints probe must run before the Ingress apply"
    )
    # And the layered defense MUST be in the documented order:
    # Deployment Available \u2192 Jobs Complete \u2192 endpoints published \u2192 apply.
    assert (
        controller_wait_indexes[0]
        < admission_jobs_wait_indexes[0]
        < endpoints_probe_indexes[0]
        < ingress_apply_indexes[0]
    ), "layered defense steps must fire in the documented order"
    assert any(c[:1] == ["wait"] and "certificate/elb-openapi-tls" in " ".join(c) for c in calls)

    # Cache contract: SPA's public-https GET reads from here without a
    # kubectl round trip.
    status = task_module.get_openapi_public_https_status()
    assert status["enabled"] is True
    assert status["fqdn"].endswith(".koreacentral.cloudapp.azure.com")
    assert status["ingress_lb_ip"] == "20.41.1.2"


def test_disable_public_https_clears_cache(monkeypatch) -> None:
    from api.services.openapi import runtime
    from api.tasks.openapi import public_https as task_module

    fake_redis = _FakeRedis()
    monkeypatch.setattr(runtime, "get_ops_redis_client", lambda **_kw: fake_redis)
    # Pre-seed cache so disable has something to clear. Use the same
    # cluster_arm_id metadata the task derives so the per-cluster +
    # legacy clear paths both target the seeded entry.
    runtime.save_openapi_public_base_url(
        "https://elb-openapi-abc.koreacentral.cloudapp.azure.com",
        cluster_arm_id=(
            "/subscriptions/sub-1/resourceGroups/rg-elb"
            "/providers/Microsoft.ContainerService/managedClusters/elb-cluster"
        ),
        metadata={
            "subscription_id": "sub-1",
            "resource_group": "rg-elb",
            "cluster_name": "elb-cluster",
        },
        client=fake_redis,
    )

    monkeypatch.setattr(task_module, "get_credential", lambda: object())
    monkeypatch.setattr(
        task_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake",  # noqa: S108 - fake path returned by mocked auth helper,
    )

    deleted: list[str] = []

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:1] == ["delete"]:
            deleted.append(args[1] + "/" + args[2])
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    monkeypatch.setattr(
        task_module,
        "clear_openapi_public_base_url",
        lambda **kw: runtime.clear_openapi_public_base_url(client=fake_redis, **kw),
    )

    result = task_module.disable_openapi_public_https.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
    )

    assert result["status"] == "succeeded"
    assert "ingress/elb-openapi-tls" in deleted
    assert "certificate/elb-openapi-tls" in deleted
    # Cache cleared → SPA flips baseUrl back to internal LB IP path
    assert task_module.get_openapi_public_https_status() == {"enabled": False}


def test_setup_public_https_propagates_kubectl_failure(monkeypatch) -> None:
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module, "get_credential", lambda: object())
    monkeypatch.setattr(task_module, "aks_client", lambda *_a, **_kw: _stub_cluster())
    monkeypatch.setattr(
        task_module,
        "ensure_admin_kubeconfig",
        lambda **_kw: "/tmp/exec/kubeconfig-fake",  # noqa: S108 - fake path returned by mocked auth helper,
    )
    # Avoid hitting the network for the systempool-patch fetch — the
    # apply step is what we are testing.
    monkeypatch.setattr(
        task_module,
        "fetch_install_manifest_for_workload_pool",
        lambda url, **_kw: f"# stub for {url}\n",
    )

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:1] == ["apply"]:
            return {"exit_code": 1, "stderr": "Unable to connect to the server"}
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    result = task_module.setup_openapi_public_https.run(
        subscription_id="sub-1",
        resource_group="rg-elb",
        cluster_name="elb-cluster",
        operator_email="ops@example.com",
    )

    assert result["status"] == "failed"
    assert "Unable to connect" in result["error"]


def test_operator_email_resolution(monkeypatch) -> None:
    import pytest as _pytest
    from api.tasks.openapi import public_https as task_module

    monkeypatch.delenv("ELB_OPERATOR_EMAIL", raising=False)
    # No caller value and no env override \u2192 the task must refuse to run.
    # Let's Encrypt rejects ACME account registration on the previous
    # `noreply@elb-dashboard.local` fallback with
    # `urn:ietf:params:acme:error:invalidContact`, and the SPA already
    # blocks empty values via the Enable button gate \u2014 a stale call
    # path must surface the misconfiguration loudly instead of silently
    # bricking the public-https pipeline.
    with _pytest.raises(ValueError):
        task_module._resolve_operator_email("")
    assert task_module._resolve_operator_email("user@example.com") == "user@example.com"

    monkeypatch.setenv("ELB_OPERATOR_EMAIL", "env@example.com")
    assert task_module._resolve_operator_email("") == "env@example.com"
    # Explicit body value still wins over env
    assert task_module._resolve_operator_email("override@example.com") == "override@example.com"


def test_route_validate_operator_email_blocks_private_tlds() -> None:
    """Public-https enable route must reject `.local` / empty emails up
    front so a stale browser tab can never re-introduce the
    `noreply@elb-dashboard.local` regression (Let's Encrypt rejects ACME
    registration on private-use TLDs with `invalidContact`).
    """
    import pytest as _pytest
    from api.routes.aks.openapi import _validate_operator_email
    from fastapi import HTTPException

    with _pytest.raises(HTTPException) as exc_info:
        _validate_operator_email("")
    assert exc_info.value.status_code == 400

    for bad in (
        "noreply@elb-dashboard.local",
        "ops@elb.internal",
        "x@localhost",
        "ops@example",  # no TLD
        "not-an-email",
    ):
        with _pytest.raises(HTTPException) as exc_info:
            _validate_operator_email(bad)
        assert exc_info.value.status_code == 400, bad

    assert _validate_operator_email("ops@example.com") == "ops@example.com"
    assert _validate_operator_email("  user.name+tag@sub.example.co.kr  ") == (
        "user.name+tag@sub.example.co.kr"
    )


def test_email_masking_does_not_leak_local_part() -> None:
    from api.tasks.openapi.public_https import _mask_email

    assert _mask_email("") == ""
    assert _mask_email("not-an-email") == ""
    assert _mask_email("a@b.com") == "a*@b.com"
    assert _mask_email("ops@example.com").startswith("o")
    assert "@example.com" in _mask_email("ops@example.com")
    assert "ps" not in _mask_email("ops@example.com")


# ---------------------------------------------------------------------------
# rollout wait hardening (Pillar D — cold-cluster race fix)
# Anchors:
#   - api/tasks/openapi/public_https.py::_wait_for_cert_manager_webhook
#   - api/tasks/openapi/public_https.py::_wait_for_ingress_nginx_controller
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    (
        "helper_name",
        "deployment_name",
        "interval_name",
        "retries_name",
    ),
    [
        (
            "_wait_for_cert_manager_webhook",
            "cert-manager-webhook",
            "_CERT_MANAGER_WEBHOOK_PROBE_INTERVAL_SECONDS",
            "_CERT_MANAGER_WEBHOOK_PROBE_RETRIES",
        ),
        (
            "_wait_for_ingress_nginx_controller",
            "ingress-nginx-controller",
            "_INGRESS_NGINX_CONTROLLER_PROBE_INTERVAL_SECONDS",
            "_INGRESS_NGINX_CONTROLLER_PROBE_RETRIES",
        ),
    ],
)
def test_rollout_wait_helpers_sleep_between_rollout_retries(
    monkeypatch,
    helper_name: str,
    deployment_name: str,
    interval_name: str,
    retries_name: str,
) -> None:
    """Regression for the cold-cluster race: rollout wrappers must
    sleep between rollout-status retries, otherwise a fast ``NotFound``
    response burns through the advertised retry budget in milliseconds.
    """
    from api.tasks.openapi import public_https as task_module

    sleeps: list[float] = []
    monkeypatch.setattr(task_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    def fake_kubectl_run(
        args: list[str],
        *,
        kubeconfig_path: str,
        stdin: str | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        if args[:2] == ["rollout", "status"]:
            return {
                "exit_code": 1,
                "stderr": (
                    f'Error from server (NotFound): deployments.apps "{deployment_name}" not found'
                ),
                "stdout": "",
            }
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    with pytest.raises(RuntimeError):
        getattr(task_module, helper_name)(kubeconfig_path="/fake")

    assert sleeps == [
        getattr(task_module, interval_name),
    ] * (getattr(task_module, retries_name) - 1)


@pytest.mark.parametrize(
    "helper_name,error_match",
    [
        ("_wait_for_cert_manager_webhook", "rollout status cert-manager-webhook"),
        ("_wait_for_ingress_nginx_controller", "rollout status ingress-nginx-controller"),
    ],
)
def test_rollout_wait_helpers_raise_when_rollout_never_succeeds(
    monkeypatch,
    helper_name: str,
    error_match: str,
) -> None:
    """If the rollout probes exhaust without success, the wrapper raises a
    clear RuntimeError with no fall-through to the Available wait.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)

    wait_called = False

    def fake_kubectl_run(
        args: list[str],
        *,
        kubeconfig_path: str,
        stdin: str | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        nonlocal wait_called
        if args[:2] == ["rollout", "status"]:
            return {
                "exit_code": 1,
                "stderr": "timed out",
                "stdout": "",
            }
        if args[:1] == ["wait"]:
            wait_called = True
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    with pytest.raises(RuntimeError, match=error_match):
        getattr(task_module, helper_name)(kubeconfig_path="/fake")

    assert wait_called is False, "Available wait must not run when rollout probes exhausted"


@pytest.mark.parametrize(
    "helper_name,deployment_name,wait_substrings",
    [
        (
            "_wait_for_cert_manager_webhook",
            "cert-manager-webhook",
            ("cert-manager-webhook",),
        ),
        (
            "_wait_for_ingress_nginx_controller",
            "ingress-nginx-controller",
            ("deployment/ingress-nginx-controller", "-n ingress-nginx"),
        ),
    ],
)
def test_rollout_wait_helpers_succeed_after_retries(
    monkeypatch,
    helper_name: str,
    deployment_name: str,
    wait_substrings: tuple[str, ...],
) -> None:
    """Cold-cluster happy path: a few NotFound probes, then rollout flips
    to ready, then the single Available wait fires with the documented
    generous 300 s timeout.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)

    calls: list[list[str]] = []
    rollout_attempts = 0

    def fake_kubectl_run(
        args: list[str],
        *,
        kubeconfig_path: str,
        stdin: str | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        nonlocal rollout_attempts
        calls.append(args)
        if args[:2] == ["rollout", "status"]:
            rollout_attempts += 1
            if rollout_attempts < 3:
                return {
                    "exit_code": 1,
                    "stderr": (
                        "Error from server (NotFound): "
                        f'deployments.apps "{deployment_name}" not found'
                    ),
                    "stdout": "",
                }
            return _ok()
        if args[:1] == ["wait"]:
            return _ok()
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    getattr(task_module, helper_name)(kubeconfig_path="/fake")

    rollout_calls = [c for c in calls if c[:2] == ["rollout", "status"]]
    wait_calls = [c for c in calls if c[:1] == ["wait"]]
    assert len(rollout_calls) == 3
    assert len(wait_calls) == 1
    joined_wait = " ".join(wait_calls[0])
    for expected in wait_substrings:
        assert expected in joined_wait
    assert "--timeout=300s" in wait_calls[0]


# ---------------------------------------------------------------------------
# Admission-webhook layered defense (regression for the repeated
# 2026-05-27 "no endpoints available for service
# ingress-nginx-controller-admission" production incidents).
# Anchors:
#   - api/tasks/openapi/public_https.py::_wait_for_ingress_nginx_admission_jobs
#   - api/tasks/openapi/public_https.py::_wait_for_admission_endpoints_ready
#   - api/tasks/openapi/public_https.py::_apply_ingress_with_webhook_retry
# ---------------------------------------------------------------------------


def test_wait_for_ingress_nginx_admission_jobs_waits_for_each_job(monkeypatch) -> None:
    """Both bootstrap Jobs (``-create`` and ``-patch``) must be waited
    on, in either order, so the ``caBundle`` patch on the
    ValidatingWebhookConfiguration is guaranteed complete before the
    Ingress apply. Skipping either leaves a window where the apiserver
    rejects the webhook call with "x509: certificate signed by unknown
    authority".
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)
    calls: list[list[str]] = []

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        calls.append(args)
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    task_module._wait_for_ingress_nginx_admission_jobs(kubeconfig_path="/fake")

    wait_targets = {
        c[2] for c in calls if c[:1] == ["wait"] and len(c) > 2 and c[2].startswith("job/")
    }
    assert wait_targets == {
        "job/ingress-nginx-admission-create",
        "job/ingress-nginx-admission-patch",
    }
    # Must use the Complete condition explicitly (NOT
    # ``--for=condition=Ready`` which Jobs never get).
    for c in calls:
        if c[:1] == ["wait"]:
            assert "--for=condition=Complete" in c


def test_wait_for_ingress_nginx_admission_jobs_skips_when_not_found(monkeypatch) -> None:
    """A missing Job (operator customised the manifest) is benign \u2014 we
    log and continue rather than fail the whole pipeline, because the
    subsequent endpoint probe + apply retry will surface a clearer
    error if anything is genuinely broken downstream.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        return {
            "exit_code": 1,
            "stderr": (
                "Error from server (NotFound): "
                'jobs.batch "ingress-nginx-admission-create" not found'
            ),
            "stdout": "",
        }

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    # Must NOT raise.
    task_module._wait_for_ingress_nginx_admission_jobs(kubeconfig_path="/fake")


def test_wait_for_ingress_nginx_admission_jobs_raises_on_real_timeout(monkeypatch) -> None:
    """A genuine wait timeout on an existing Job (cluster broken) MUST
    fail the pipeline so cert-manager challenge does not silently fail
    later with a misleading "ACME order pending" loop.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        return {
            "exit_code": 1,
            "stderr": "timed out waiting for the condition on jobs/ingress-nginx-admission-create",
            "stdout": "",
        }

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    with pytest.raises(RuntimeError, match="ingress-nginx-admission-create did not Complete"):
        task_module._wait_for_ingress_nginx_admission_jobs(kubeconfig_path="/fake")


def test_wait_for_admission_endpoints_ready_returns_on_first_truthy_ip(monkeypatch) -> None:
    """The probe must exit the moment the admission Service has at
    least one endpoint address. On a warm cluster this is the very
    first probe, so wall-clock cost is negligible.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)
    probes = 0

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        nonlocal probes
        if args[:2] == ["get", "endpoints"]:
            probes += 1
            return _ok(stdout="10.0.1.42")
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    task_module._wait_for_admission_endpoints_ready(kubeconfig_path="/fake")
    assert probes == 1


def test_wait_for_admission_endpoints_ready_polls_until_ip_appears(monkeypatch) -> None:
    """Cold cluster: EndpointSlice publish lags Pod-Ready by a few
    seconds. The probe must keep polling until an IP appears \u2014 NOT
    treat an empty ``stdout`` as success.
    """
    from api.tasks.openapi import public_https as task_module

    sleeps: list[float] = []
    monkeypatch.setattr(task_module.time, "sleep", lambda seconds: sleeps.append(seconds))
    probes = 0

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        nonlocal probes
        if args[:2] == ["get", "endpoints"]:
            probes += 1
            if probes < 4:
                return _ok(stdout="")  # endpoints exist but no addresses yet
            return _ok(stdout="10.0.1.42")
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    task_module._wait_for_admission_endpoints_ready(kubeconfig_path="/fake")
    assert probes == 4
    # Sleeps fire BETWEEN probes (not after the successful one): 3
    # not-ready responses \u2192 3 sleeps at the documented interval.
    assert (
        sleeps
        == [
            task_module._ADMISSION_ENDPOINTS_PROBE_INTERVAL_SECONDS,
        ]
        * 3
    )


def test_wait_for_admission_endpoints_ready_raises_when_never_appears(monkeypatch) -> None:
    """If the EndpointSlice controller is broken (kube-controller-manager
    crashloop or RBAC denial), the probe budget exhausts and the
    pipeline fails with a clear error \u2014 better than the silent
    "no endpoints available" the Ingress apply would otherwise surface.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        if args[:2] == ["get", "endpoints"]:
            return _ok(stdout="")
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    with pytest.raises(RuntimeError, match="never got endpoint addresses"):
        task_module._wait_for_admission_endpoints_ready(kubeconfig_path="/fake")


def test_apply_ingress_with_webhook_retry_retries_on_transient_error(monkeypatch) -> None:
    """The final safety-net retry must absorb the canonical
    "no endpoints available for service" error \u2014 the exact error
    string the user saw on elb-cluster-small, 2026-05-27.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)
    attempts = 0

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return {
                "exit_code": 1,
                "stderr": (
                    "Error from server (InternalError): error when creating "
                    '"STDIN": Internal error occurred: failed calling webhook '
                    '"validate.nginx.ingress.kubernetes.io": ... no endpoints '
                    'available for service "ingress-nginx-controller-admission"'
                ),
                "stdout": "",
            }
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    task_module._apply_ingress_with_webhook_retry(
        kubeconfig_path="/fake",
        ingress_yaml="apiVersion: networking.k8s.io/v1\nkind: Ingress\n",
    )
    assert attempts == 3


def test_apply_ingress_with_webhook_retry_fails_fast_on_non_transient_error(
    monkeypatch,
) -> None:
    """A genuine misconfiguration (RBAC denial, wrong CRD version,
    syntactically invalid Ingress) MUST NOT be retried \u2014 the
    operator should see the error immediately instead of waiting
    ~30 s for an apply that will never succeed.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)
    attempts = 0

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return {
            "exit_code": 1,
            "stderr": (
                "Error from server (Forbidden): "
                "ingresses.networking.k8s.io is forbidden: "
                'User "system:serviceaccount:default:elb" cannot create '
                'resource "ingresses" in API group "networking.k8s.io"'
            ),
            "stdout": "",
        }

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    with pytest.raises(RuntimeError, match="Forbidden"):
        task_module._apply_ingress_with_webhook_retry(
            kubeconfig_path="/fake",
            ingress_yaml="apiVersion: networking.k8s.io/v1\nkind: Ingress\n",
        )
    assert attempts == 1, "non-transient error must fail on the first attempt"


def test_apply_ingress_with_webhook_retry_exhausts_and_raises(monkeypatch) -> None:
    """Persistent transient failure (cluster genuinely cannot reach the
    webhook) eventually raises with the retry budget in the error
    message so the operator can correlate with the cluster state.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)
    attempts = 0

    def fake_kubectl_run(args: list[str], **_kw: Any) -> dict[str, Any]:
        nonlocal attempts
        attempts += 1
        return {
            "exit_code": 1,
            "stderr": 'no endpoints available for service "ingress-nginx-controller-admission"',
            "stdout": "",
        }

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)
    with pytest.raises(RuntimeError, match="transient-webhook retries"):
        task_module._apply_ingress_with_webhook_retry(
            kubeconfig_path="/fake",
            ingress_yaml="apiVersion: networking.k8s.io/v1\nkind: Ingress\n",
        )
    assert attempts == task_module._INGRESS_APPLY_RETRY_ATTEMPTS


# ---------------------------------------------------------------------------
# Certificate pre-existence probe (companion to the cert-manager webhook
# hardening above — same NotFound race on a different resource).
# Anchors:
#   - api/tasks/openapi/public_https.py::_wait_for_certificate_object_to_exist
#   - api/tasks/openapi/public_https.py::_wait_for_certificate_ready
# ---------------------------------------------------------------------------


def test_wait_for_certificate_ready_polls_existence_before_wait_condition(
    monkeypatch,
) -> None:
    """cert-manager's ingress-shim creates the Certificate CR asynchronously
    after the Ingress is applied. Without a pre-existence probe the
    immediate ``kubectl wait --for=condition=Ready`` returns ``NotFound``
    in <1 s on older kubectl builds. The hardened helper polls
    ``kubectl get certificate`` with sleeps until the object exists,
    then issues the wait once.
    """
    from api.tasks.openapi import public_https as task_module

    sleeps: list[float] = []
    monkeypatch.setattr(task_module.time, "sleep", lambda seconds: sleeps.append(seconds))

    get_attempts = 0
    wait_called = False

    def fake_kubectl_run(
        args: list[str],
        *,
        kubeconfig_path: str,
        stdin: str | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        nonlocal get_attempts, wait_called
        # The new existence probe uses `-o name` to keep the response small.
        if args[:2] == ["get", "certificate"] and "name" in args:
            get_attempts += 1
            if get_attempts < 3:
                return {"exit_code": 1, "stderr": "Error from server (NotFound)", "stdout": ""}
            return _ok(stdout=f"certificate.cert-manager.io/{args[2]}")
        if args[:1] == ["wait"]:
            wait_called = True
            return _ok()
        # The post-failure jsonpath probe and the expiry read should not
        # fire on the happy path.
        return _ok()

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    task_module._wait_for_certificate_ready(kubeconfig_path="/fake", timeout_seconds=300)

    # 2 not-found + 1 found = 3 probes; one wait after.
    assert get_attempts == 3
    assert wait_called is True
    # Sleeps fire BETWEEN existence probes (not after the successful one),
    # so 2 not-found responses → 2 sleeps, each at the documented interval.
    assert (
        sleeps
        == [
            task_module._CERTIFICATE_EXISTS_PROBE_INTERVAL_SECONDS,
        ]
        * 2
    )


def test_wait_for_certificate_object_to_exist_returns_silently_when_budget_exhausted(
    monkeypatch,
) -> None:
    """When the Certificate CR never appears (cert-manager misconfigured or
    ingress-shim never picked up the Ingress), the pre-existence probe
    returns silently. The subsequent ``kubectl wait`` is left to surface
    the eventual failure with its richer condition-message probe, so
    operators never see a less informative "certificate not found" error
    that hides the real cause.
    """
    from api.tasks.openapi import public_https as task_module

    monkeypatch.setattr(task_module.time, "sleep", lambda _seconds: None)

    def fake_kubectl_run(
        args: list[str],
        *,
        kubeconfig_path: str,
        stdin: str | None = None,
        timeout_seconds: int = 60,
    ) -> dict[str, Any]:
        return {"exit_code": 1, "stderr": "NotFound", "stdout": ""}

    monkeypatch.setattr(task_module, "kubectl_run", fake_kubectl_run)

    # Must not raise. The subsequent wait will eventually surface the
    # error via _wait_for_certificate_ready's condition probe.
    task_module._wait_for_certificate_object_to_exist(kubeconfig_path="/fake")
