# ElasticBLAST Control Plane

ElasticBLAST Control Plane is a browser-only dashboard for running ElasticBLAST on Azure. The deployed control plane bundles the React frontend, FastAPI backend, Celery worker, Celery beat, Redis broker, and browser terminal into one Azure Container App.

## Start Here

- [Get Started](get-started.md) covers local prerequisites, first deployment, sign-in, and the optional end-to-end BLAST smoke test.
- [Auth](auth.md) explains the browser sign-in and backend token validation model.
- [Change Log](changelog.md) lists the recent feature notes and keeps the full archive searchable.
- [Container Apps Migration](container-apps-migration.md) is the architecture reference for the six-sidecar deployment target.
- [User Guide](user-guide/index.md) is the public walkthrough for operating the control plane from the browser.

## Architecture Notes

- [BLAST SearchSP Discovery](blast-searchsp-discovery.md) tracks the SearchSP compatibility work.
- [Web BLAST Compatibility Plan](web-blast-compatibility-implementation-plan.md) describes the web compatibility implementation plan.
- The Agent Reference section documents the repository layout, auth flow, browser terminal, resource plane, monitoring UI, and glass UI conventions.

## Documentation Capture

- [Screenshot Workflow](contributor-guide/screenshot-workflow.md) defines the repeatable capture process, viewports, masking rules, and acceptance checks for future documentation screenshots.

## Source Repository

The source lives at [dotnetpower/elb-dashboard](https://github.com/dotnetpower/elb-dashboard). Some internal reference pages link to source files in that repository.