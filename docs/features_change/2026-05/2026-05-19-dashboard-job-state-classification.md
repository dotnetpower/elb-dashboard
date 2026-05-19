# Dashboard Job State Classification

## Motivation

The dashboard cluster pulse showed submitted BLAST jobs as `unknown` even when the backend returned `phase: "submitted"` and `status: "running"`.

## User-facing Change

Submitted jobs now count as active jobs instead of unknown jobs. Failed submit phases such as `submit_failed` are classified as failed even when the stored error text is missing.

## API/IaC Diff Summary

No API or IaC changes. The frontend job-state classifier now recognises backend phase names and falls back from an unrecognised phase to the job status.

## Validation Evidence

- `npm run test -- jobMapping`