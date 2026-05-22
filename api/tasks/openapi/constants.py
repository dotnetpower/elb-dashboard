"""Constants and well-known identifiers for the ``elb-openapi`` AKS deploy.

Responsibility: Hold the static names that must stay aligned with the sibling
    ``elastic-blast-azure`` repo and the on-cluster manifests (MI name, K8s SA/namespace,
    federated credential name, role definition IDs).
Edit boundaries: Constants only. Any change here must also be applied to the matching
    legacy values in the sibling repo so existing clusters keep a single MI/SA pair.
Key entry points: `MI_NAME`, `K8S_SA_NAME`, `K8S_NAMESPACE`, `FED_CRED_NAME`,
    `ROLE_CONTRIBUTOR`, `ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR`, `ROLE_AKS_CLUSTER_USER`.
Risky contracts: Renaming any constant changes the workload identity contract on
    deployed clusters — existing federated credentials and role assignments would
    appear duplicated, not migrated.
Validation: `uv run pytest -q api/tests/test_openapi_deploy.py` (if present) or the
    package smoke `uv run pytest -q api/tests/test_smoke.py`.
"""

from __future__ import annotations

# Workload-identity / K8s naming — must match the legacy values so existing
# clusters do not see a duplicate MI / SA pair.
MI_NAME = "id-elb-openapi"
K8S_SA_NAME = "elb-openapi-sa"
K8S_NAMESPACE = "default"
FED_CRED_NAME = "fc-elb-openapi"

# Built-in role definition IDs (well-known).
ROLE_CONTRIBUTOR = "b24988ac-6180-42a0-ab88-20f7382dd24c"
ROLE_STORAGE_BLOB_DATA_CONTRIBUTOR = "ba92f5b4-2d11-453d-a403-e96b0029c9fe"
ROLE_AKS_CLUSTER_USER = "4abbcc35-e782-43d8-92c5-2d3f1bd2253f"
