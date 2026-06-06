"""Golden tests for the security-posture rule catalog and the spec framework.

Responsibility: Pin the WAF Security-pillar checks (AKS/Storage/ACR hardening)
    and the declarative spec evaluator (skip-on-None, ok/bad mapping).
Edit boundaries: Pure-function assertions only.
Key entry points: the `test_*` functions below.
Risky contracts: A permission-denied snapshot MUST yield `indeterminate`, never
    `critical`. An unavailable field (None) MUST be skipped, never fabricated.
Validation: `uv run pytest -q api/tests/test_diagnostics_security_rules.py`.
"""

from __future__ import annotations

from api.services.diagnostics.models import ResourceSnapshot
from api.services.diagnostics.rules import evaluate_security
from api.services.diagnostics.rules.specs import (
    RuleSpec,
    evaluate_specs,
    set_and_not,
    want_false,
    want_true,
)


def _by_id(findings):
    return {f.id: f for f in findings}


# ---------------------------------------------------------------- spec framework


def _spec(**kw) -> RuleSpec:
    base = dict(
        id="x.flag",
        resource_kind="aks",
        pillar="Security",
        field="flag",
        title_ok="ok",
        title_bad="bad",
        detail_ok="ok",
        detail_bad="bad",
        recommendation="do it",
        doc_url="https://learn.microsoft.com/azure",
    )
    base.update(kw)
    return RuleSpec(**base)


def test_spec_want_true_maps_ok_bad_skip() -> None:
    spec = _spec(compliant=want_true)
    assert (
        evaluate_specs([spec], {"flag": True}, category="security", resource_name="r")[0].severity
        == "ok"
    )
    assert (
        evaluate_specs([spec], {"flag": False}, category="security", resource_name="r")[0].severity
        == "warning"
    )
    # None → unavailable → skipped (no finding fabricated).
    assert evaluate_specs([spec], {"flag": None}, category="security", resource_name="r") == []
    assert evaluate_specs([spec], {}, category="security", resource_name="r") == []


def test_spec_want_false_inverts() -> None:
    spec = _spec(compliant=want_false)
    assert (
        evaluate_specs([spec], {"flag": False}, category="security", resource_name="r")[0].severity
        == "ok"
    )
    assert (
        evaluate_specs([spec], {"flag": True}, category="security", resource_name="r")[0].severity
        == "warning"
    )


def test_spec_set_and_not() -> None:
    spec = _spec(field="np", compliant=set_and_not("none"))
    assert (
        evaluate_specs([spec], {"np": "azure"}, category="security", resource_name="r")[0].severity
        == "ok"
    )
    assert (
        evaluate_specs([spec], {"np": "none"}, category="security", resource_name="r")[0].severity
        == "warning"
    )
    assert (
        evaluate_specs([spec], {"np": ""}, category="security", resource_name="r")[0].severity
        == "warning"
    )


def test_spec_bad_severity_respected() -> None:
    spec = _spec(compliant=want_true, bad_severity="critical")
    assert (
        evaluate_specs([spec], {"flag": False}, category="security", resource_name="r")[0].severity
        == "critical"
    )


def test_spec_predicate_exception_is_skipped_not_propagated() -> None:
    """A malformed value that makes the predicate raise must skip the spec, not
    abort the whole category."""

    def _explodes(value):
        raise ValueError("unexpected type")

    spec = _spec(compliant=_explodes)
    good = _spec(id="x.ok", field="ok", compliant=want_true)
    findings = evaluate_specs(
        [spec, good], {"flag": "weird", "ok": True}, category="security", resource_name="r"
    )
    # The exploding spec is skipped; the sibling still evaluates.
    assert _by_id(findings).keys() == {"x.ok"}


# ------------------------------------------------------------------ AKS security


def _hardened_cluster() -> dict:
    return {
        "name": "c1",
        "aad_managed": True,
        "azure_rbac": True,
        "disable_local_accounts": True,
        "network_policy": "azure",
        "addon_azure_policy": True,
        "defender_enabled": True,
        "workload_identity": True,
        "oidc_issuer_enabled": True,
        "addon_keyvault_secrets": True,
        "identity_type": "UserAssigned",
        "private_cluster": True,
        "authorized_ip_ranges": [],
    }


def test_hardened_aks_is_all_ok() -> None:
    snap = {"aks": ResourceSnapshot(kind="aks", data={"clusters": [_hardened_cluster()]})}
    findings = evaluate_security(snap)
    assert findings, "expected AKS security findings"
    assert all(f.severity == "ok" for f in findings if f.resource_kind == "aks")


def test_unhardened_aks_flags_warnings() -> None:
    cluster = _hardened_cluster()
    cluster.update(
        {
            "aad_managed": False,
            "disable_local_accounts": False,
            "network_policy": "none",
            "private_cluster": False,
            "authorized_ip_ranges": [],
        }
    )
    findings = _by_id(
        evaluate_security({"aks": ResourceSnapshot(kind="aks", data={"clusters": [cluster]})})
    )
    assert findings["aks.aad_managed"].severity == "warning"
    assert findings["aks.local_accounts_disabled"].severity == "warning"
    assert findings["aks.network_policy"].severity == "warning"
    assert findings["aks.api_server_exposure"].severity == "warning"


def test_api_exposure_ok_with_authorized_ips() -> None:
    cluster = _hardened_cluster()
    cluster["private_cluster"] = False
    cluster["authorized_ip_ranges"] = ["203.0.113.0/24"]
    findings = _by_id(
        evaluate_security({"aks": ResourceSnapshot(kind="aks", data={"clusters": [cluster]})})
    )
    assert findings["aks.api_server_exposure"].severity == "ok"


def test_aks_security_denied_is_indeterminate_never_critical() -> None:
    snap = {
        "aks": ResourceSnapshot(kind="aks", available=False, reason="forbidden", access="denied")
    }
    findings = _by_id(evaluate_security(snap))
    assert findings["aks.security"].severity == "indeterminate"
    assert all(f.severity != "critical" for f in findings.values())


# -------------------------------------------------------------- Storage security


def test_storage_https_off_is_critical() -> None:
    data = {"name": "st", "https_only": False, "min_tls_version": "TLS1_2"}
    findings = _by_id(evaluate_security({"storage": ResourceSnapshot(kind="storage", data=data)}))
    assert findings["storage.https_only"].severity == "critical"
    assert findings["storage.min_tls"].severity == "ok"


def test_storage_public_disabled_is_ok_by_charter() -> None:
    data = {"name": "st", "public_network_access": "Disabled"}
    finding = _by_id(evaluate_security({"storage": ResourceSnapshot(kind="storage", data=data)}))[
        "storage.public_network_access"
    ]
    assert finding.severity == "ok"
    assert finding.expected_by_charter is True


def test_storage_shared_key_enabled_is_warning() -> None:
    data = {"name": "st", "allow_shared_key_access": True}
    finding = _by_id(evaluate_security({"storage": ResourceSnapshot(kind="storage", data=data)}))[
        "storage.shared_key_disabled"
    ]
    assert finding.severity == "warning"


def test_storage_unavailable_field_skipped() -> None:
    # Only name present → every spec reads None → all skipped, no fabrication.
    data = {"name": "st"}
    findings = evaluate_security({"storage": ResourceSnapshot(kind="storage", data=data)})
    assert [f for f in findings if f.resource_kind == "storage"] == []


# ------------------------------------------------------------------ ACR security


def test_acr_admin_user_enabled_is_warning() -> None:
    data = {"name": "acr", "admin_user_enabled": True, "anonymous_pull_enabled": False}
    findings = _by_id(evaluate_security({"acr": ResourceSnapshot(kind="acr", data=data)}))
    assert findings["acr.admin_user_disabled"].severity == "warning"
    assert findings["acr.anonymous_pull_disabled"].severity == "ok"
