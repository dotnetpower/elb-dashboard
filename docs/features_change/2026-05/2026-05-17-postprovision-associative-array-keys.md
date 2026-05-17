# Postprovision build wait key quoting

## Motivation

Clean validation caught `scripts/dev/postprovision.sh` failing after all three ACR builds completed:

```
scripts/dev/postprovision.sh: line 157: elb: unbound variable
```

The build tracker used associative array keys such as `elb-terminal` without consistently quoting them under `set -u`.

## User-facing change

`azd up` / postprovision can now wait for dashed image names (`elb-api`, `elb-frontend`, `elb-terminal`) without aborting after successful builds.

## API/IaC diff summary

- Quote associative array keys when assigning, reading, and unsetting `RUNNING` entries in `scripts/dev/postprovision.sh`.

## Validation evidence

- Re-run of `scripts/dev/postprovision.sh` against `rg-elbverify0517` is in progress after the fix.