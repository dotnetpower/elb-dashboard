# Results

Results pages show the output of a submitted BLAST job, including descriptions, alignments, downloads, and analytics views.

## What To Explain

- Job summary and status.
- Result tabs and when each tab is useful.
- Download actions.
- Empty or still-running result states.
- Result analytics and hit interpretation.

## Screenshot Targets

Screenshots for this page are defined by this manifest target:

- `results-desktop`

The manifest uses `/blast/jobs/{demoJobId}`. Replace `{demoJobId}` with a safe demo job ID before capture.# Results

The Results page presents BLAST output, downloads, and result analytics for a selected job.

## Screenshot Slot

Capture target: `docs/images/screenshots/results-overview.png`

Recommended state before capture:

- Use a completed sample job with a small, non-sensitive query.
- The descriptions or alignments tab has representative rows.
- Download controls are visible but do not expose direct Storage URLs or SAS tokens.

## Notes To Cover

- Moving between descriptions, alignments, graphics, and artifacts.
- Downloading result files through the authenticated API path.
- Recognizing empty, running, and failed result states.