# ElasticBLAST Control Plane

Run large BLAST searches on Azure without becoming the cloud operator.

ElasticBLAST is built for serious sequence search, but the cloud work around it can pull a researcher away from the question they actually care about. Clusters, storage accounts, container images, database preparation, permissions, and job logs all have to line up before a search can run well.

ElasticBLAST Control Plane brings those moving parts into one browser workflow. It helps research teams prepare the Azure workspace, check readiness, submit BLAST jobs, monitor progress, and find results without turning every search into an infrastructure session.

Use it when a query has outgrown a local workstation, but the team still needs a calm, visible path from input sequence to completed result.

## What It Helps With

- Confirm that the cluster, storage, runtime images, databases, and terminal are ready before submitting work.
- Start BLAST searches from the browser instead of assembling commands across local terminals.
- Follow running jobs in one place, including degraded or failed states that need attention.
- Keep result access inside the control plane without exposing Storage links directly to the browser.
- Leave the browser terminal available for advanced workflows without making it the default path.

## A Researcher-First Workflow

1. Open the Dashboard and choose the active Azure workspace.
2. Check whether the required compute, storage, images, and databases are ready.
3. Submit a BLAST search from the browser.
4. Watch progress from Recent searches.
5. Open, inspect, and download results when the job completes.

The goal is not to hide Azure. It is to keep Azure in the background until it matters.

## Start Here

- [Get Started](get-started.md) walks through setup, deployment, sign-in, and a first BLAST smoke test.
- [User Guide](user-guide/index.md) shows how to operate the control plane from the browser.
- [Dashboard](user-guide/dashboard.md) explains the readiness view and the signals to check before submitting work.
- [Change Log](changelog.md) lists recent feature notes and keeps the implementation history searchable.

## For Platform Maintainers

- [Container Apps Migration](container-apps-migration.md) describes the six-sidecar deployment architecture.
- [Auth](auth.md) explains browser sign-in and backend token validation.
- [BLAST SearchSP Discovery](blast-searchsp-discovery.md) tracks SearchSP compatibility work.
- [Web BLAST Compatibility Plan](web-blast-compatibility-implementation-plan.md) describes the web compatibility implementation plan.
- The Agent Reference section documents the repository layout, browser terminal, resource plane, monitoring UI, and glass UI conventions.

## Documentation Capture

- [Screenshot Workflow](contributor-guide/screenshot-workflow.md) defines the repeatable capture process, viewports, masking rules, and acceptance checks for future documentation screenshots.

## Source Repository

The source lives at [dotnetpower/elb-dashboard](https://github.com/dotnetpower/elb-dashboard). Some internal reference pages link to source files in that repository.