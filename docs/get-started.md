# Get Started

Get from a fresh clone to a working ElasticBLAST dashboard on Azure.

Most research teams should use the guided deployment helper. It checks Azure sign-in, prepares the Azure Developer CLI environment, registers the required providers, handles the default resource-group choice, runs `azd up`, and opens the deployed dashboard when it is ready.

```bash
git clone https://github.com/dotnetpower/elb-dashboard.git
cd elb-dashboard
./deploy.sh
```

After deployment, researchers work in the browser: choose the Azure workspace, check readiness, submit BLAST jobs, monitor progress, and open results.

## Before You Start

You need an Azure subscription where you can create the control-plane resources. The easiest first deployment uses an account with `Owner`, or `Contributor` plus `User Access Administrator`.

You also need these local tools:

- Git
- Bash
- Azure CLI: `az`
- Azure Developer CLI: `azd`

Docker is not required for deployment. Python, Node.js, and `uv` are only needed for local development and maintainer validation.

If your organization blocks App Registration creation or admin consent, ask an Entra administrator to create or approve the app once. The deployment can reuse that app with `API_CLIENT_ID`.

## Deploy With The Helper

Start from the repository root:

```bash
./deploy.sh
```

The helper may ask you to sign in to Azure. It uses the active Azure CLI subscription, creates or selects the default `elb-dashboard` azd environment, and sets the common environment values for you.

If the default resource group already exists, the helper asks what to do:

- Delete and reuse `rg-elb-dashboard`.
- Deploy to a numbered group such as `rg-elb-dashboard-01`.
- Abort so you can decide later.

For an unattended run, set one of these values before starting:

```bash
export ELB_EXISTING_RG_ACTION=delete  # or number, abort
./deploy.sh
```

To prepare the environment without deploying yet:

```bash
./deploy.sh --prepare-only
```

## What Happens During Deployment

The helper and `azd up` run the deployment in clear stages:

1. Confirm Azure CLI and azd sign-in.
2. Prepare the `elb-dashboard` azd environment.
3. Register required Azure resource providers.
4. Choose the target resource group.
5. Provision the Container App, Storage, ACR, managed identity, Key Vault, and network resources.
6. Create or reuse the Microsoft Entra App Registration.
7. Build the control-plane images in ACR.
8. Swap the app into the bundled sidecar layout.
9. Wait for `/api/health` and print the dashboard URL.

The deployed control plane is one Azure Container App. Researchers do not need Docker or local image builds for this path.

## Open The Dashboard

When deployment finishes, open the URL printed by the helper:

```text
https://ca-elb-dashboard.<subdomain>.<region>.azurecontainerapps.io
```

Sign in with the same tenant that owns the App Registration. The Dashboard should load Azure workspace readiness from the deployed API sidecar.

If sign-in works locally but not in Azure, the deployed Container App origin may need to be added as a SPA redirect URI. See [Deployment Reference](deployment-reference.md#redirect-uri-after-deployment).

## First Check In The Browser

Use the Dashboard before submitting work:

1. Confirm the active subscription and workspace.
2. Check Storage, ACR, AKS, database, and terminal readiness.
3. Build missing ElasticBLAST runtime images from the ACR card.
4. Prepare or choose a BLAST database from the Storage card.
5. Submit a small BLAST search from New Search.
6. Track the job from Recent searches and open the result when it completes.

The first full BLAST smoke test creates an AKS workload cluster and can add cost. Run it only when your tenant policy allows AKS to stay running long enough for the job lifecycle.

## Cost And Cleanup

The control plane has a standing Azure cost before BLAST workload usage. The optional smoke test adds AKS compute cost.

Stop or delete AKS clusters when they are not actively running searches. To remove the whole control plane:

```bash
azd down --purge --force
```

## More Detail

- [Deployment Reference](deployment-reference.md) covers tool installation, manual `azd` deployment, redirect URI setup, smoke testing, network lockdown, cleanup, and troubleshooting.
- [User Guide](user-guide/index.md) explains day-to-day operation from the browser.
- [Dashboard](user-guide/dashboard.md) explains the readiness signals to check before a search.