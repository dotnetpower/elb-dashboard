# New Search command preview styling

## Motivation

The New Search command preview was hard to scan because it rendered as a single colored line inside the run rail.

## User-facing change

The command preview now uses the proposal-style terminal block: black background, preserved line breaks, continuation markers, and distinct colors for commands, flags, values, numbers, and the preview comment.

## API/IaC diff summary

- Frontend: formatted the display-only command preview into multiple shell-style lines while keeping the Copy button output unchanged.
- API: no changes.
- IaC: no changes.

## Validation evidence

- `cd /home/moonchoi/dev/elb-dashboard && npm --prefix web run build` passed.
- Browser visual check on `/blast/submit` confirmed the command preview renders as a black multiline terminal block with colored command, flag, value, number, continuation, and comment tokens.