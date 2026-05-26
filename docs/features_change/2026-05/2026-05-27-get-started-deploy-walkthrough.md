# Get Started — deploy.sh walkthrough refresh

## Motivation

`docs/get-started.md` described the deploy flow as a 9-step bulleted list whose
text predated several behaviour changes in `deploy.sh`:

- The helper now prints an explicit `azd up progress map` (steps `0/8`–`8/8`).
- A caller permission pre-check runs before `azd up`.
- Three post-deploy helpers now run automatically: local-debug RBAC grant,
  MI RBAC doctor (`--auto-fix` by default), and cluster-RG bootstrap
  (default `Y` in interactive shells).
- The "Useful environment overrides" snippet was missing the newer flags
  (`ELB_AUTO_FIX_RBAC`, `ELB_BOOTSTRAP_CLUSTER_RG`,
  `ELB_CLUSTER_RG_NAME` / `_REGION`, `ELB_RESOURCE_NAME_SUFFIX`,
  `ELB_ALLOW_AZD_ENV_RETARGET`, `ELB_SKIP_LOCAL_RBAC`).

The summary list also gave no insight into *what each stage actually does* or
*what a healthy run looks like*, which made the first deploy hard to debug.

## User-facing change

`docs/get-started.md`:

- New "Environment Overrides" table covering every documented
  `deploy.sh` flag.
- "What Happens During Deployment" now shows the actual `azd up progress map`
  output verbatim.
- New "Detailed Walkthrough" section with one collapsible
  (Material `???`) admonition per stage (0/8 – 8/8 plus a Post-deploy
  block) documenting *what runs*, *what you'll see*, and *common issues*.
- Inline `<!-- TODO: screenshot — … -->` markers next to the stages where
  the maintainer plans to drop terminal captures
  (`deploy-progress-map.png`, `deploy-step0-bootstrap.png`,
  `deploy-step2-rg-choice.png`, `deploy-post-complete.png`).
- JSON-LD `HowToStep` for "Deploy with the helper" rewritten to match the
  new prose (no longer claims the helper only "registers providers, picks
  a RG, runs azd up, swaps the template").

`docs/deployment-reference.md`:

- "What azd up Does" replaced by a 9-row table (`0/8`–`8/8`) listing the
  exact helper script per stage, plus a follow-up bullet list for the
  three post-deploy helpers (with the flags that disable each).
- Useful-overrides snippet extended with `ELB_AUTO_FIX_RBAC` /
  `ELB_BOOTSTRAP_CLUSTER_RG` / `ELB_CLUSTER_RG_NAME` and a pointer to the
  full table in Get Started.

## API / IaC diff summary

None. Documentation only.

## Validation

- `uv run mkdocs build --strict` — `Documentation built in 13.85 seconds`,
  no warnings or broken links.
- Verified the new env-override flags match the documented defaults in
  `deploy.sh` (`ELB_AUTO_FIX_RBAC` default `true`,
  `ELB_BOOTSTRAP_CLUSTER_RG` default `true`,
  `ELB_CLUSTER_RG_NAME` default `rg-elb-cluster`).
- Verified the step labels (`0/8` … `8/8`) and titles match
  `scripts/dev/azd-progress.sh plan`.
