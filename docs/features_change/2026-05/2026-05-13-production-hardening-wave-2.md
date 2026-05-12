# Production Hardening Wave 2

**Date**: 2026-05-13

## Motivation

A broad critique pass found recurring reliability and recovery risks across the browser control plane: transient Azure/API failures can interrupt user workflows, render errors can strand users on a blank route, and hardening work needs a traceable catalog rather than one-off fixes.

## User-facing Change

- Authenticated API requests now include a client request ID for traceability.
- Transient API failures now retry with bounded exponential backoff, `Retry-After` support, and per-attempt timeouts.
- User-cancelled requests are respected before any network attempt starts.
- The global error screen now offers separate recovery actions: copy details, retry render, reload, or return to the dashboard.

## API / IaC Diff Summary

- `web/src/api/resilience.ts` adds shared request timeout, retry, retry-delay, and request-id helpers.
- `web/src/api/client.ts` routes all authenticated API calls through the shared resilience helper.
- `web/src/api/resilience.test.ts` covers retryability, `Retry-After` parsing, backoff, transient recovery, and cancellation.
- `web/src/components/ErrorBoundary.tsx` improves user recovery after render failures.
- `web/eslint.config.js` restores ESLint 9 flat-config validation for TypeScript and React Hooks rules.
- Hook dependency fixes stabilize shortcut, ACR, storage, job-list, and submit-page derived state.
- No backend API contract or IaC changes.

## Critique Catalog

### Functionality

1. Add typed response validation for monitoring endpoints.
2. Add typed response validation for BLAST job endpoints.
3. Add typed response validation for terminal endpoints.
4. Add typed response validation for storage endpoints.
5. Add typed response validation for ACR endpoints.
6. Show partial data when one dashboard resource fails.
7. Add job retry from failed terminal states.
8. Add failed-job clone-to-new-search action.
9. Add downloadable orchestrator history.
10. Add downloadable step logs.
11. Add completed-job export integrity checks.
12. Add result-file checksum display when available.
13. Add result preview fallback for compressed outputs.
14. Add explicit no-results-after-completion diagnosis.
15. Add duplicate submission detection.
16. Add dry-run validation before cloud submission.
17. Add BLAST config preview diff before submit.
18. Add database/query molecule compatibility preflight.
19. Add storage container existence preflight.
20. Add ACR image tag mismatch preflight.
21. Add AKS cluster readiness preflight.
22. Add quota warning before node provisioning.
23. Add region/SKU availability validation.
24. Add public network access propagation countdown.
25. Add safe resume for interrupted job status polling.
26. Add terminal az-login stale warning.
27. Add terminal cloud-init completion proof.
28. Add terminal tool-version verification display.
29. Add VM password copy-once acknowledgement.
30. Add safer resource-group selection confirmation.

### UI / UX

31. Add per-card stale-data labels.
32. Add per-card retry buttons for failed loads.
33. Add per-card collapsible details for errors.
34. Add global disconnected/API-unavailable banner.
35. Add skeleton states for all async panels.
36. Keep page headers stable during loading.
37. Keep action buttons stable during polling.
38. Disable destructive actions while mutation is pending.
39. Add confirmation reason text for destructive actions.
40. Add keyboard focus management for dialogs.
41. Add accessible labels to icon-only buttons.
42. Add tooltip text for unfamiliar actions.
43. Add copy feedback to every clipboard action.
44. Add progress labels for long-running mutations.
45. Add last successful refresh timestamp.
46. Add failed refresh timestamp.
47. Add query polling pause while tab is hidden.
48. Add reduced-motion fallback for spinners/rings.
49. Add mobile layout checks for dense result tables.
50. Add responsive wrapping for long resource IDs.
51. Add line wrapping for long Azure errors.
52. Add visual distinction between warning and fatal states.
53. Add empty-state variants for setup, loading, failed, and terminal.
54. Add consistent status chip labels across pages.
55. Add dashboard first-run workspace picker recovery.
56. Add direct dashboard recovery from route render failures.
57. Add user-readable auth setup missing state.
58. Add explicit session-expired state.
59. Add action-specific loading labels.
60. Add result export loading state per format.

### Reliability

61. Add API request retry for 408.
62. Add API request retry for 429.
63. Add API request retry for 500.
64. Add API request retry for 502.
65. Add API request retry for 503.
66. Add API request retry for 504.
67. Respect `Retry-After` seconds.
68. Respect `Retry-After` HTTP dates.
69. Bound retry delays.
70. Add jitter to retry delays.
71. Add per-attempt request timeout.
72. Respect caller cancellation before fetch.
73. Avoid retrying auth failures.
74. Avoid retrying RBAC failures.
75. Avoid retrying missing resources.
76. Add client request IDs to API calls.
77. Preserve existing request headers.
78. Preserve caller abort signals.
79. Avoid timeout timer leaks during backoff.
80. Add tests for retry status selection.
81. Add tests for retry-after parsing.
82. Add tests for transient response recovery.
83. Add tests for cancellation behavior.
84. Add tests for deterministic backoff.
85. Add shared API error formatting for transient failures.
86. Add background polling backoff after repeated failures.
87. Add circuit breaker for repeated API outages.
88. Add idempotency keys for mutation requests.
89. Add retry-safe export downloads.
90. Add retry-safe monitoring calls.

### Recovery

91. Add global render error boundary.
92. Add copyable render error details.
93. Add render retry without full reload.
94. Add full reload recovery action.
95. Add dashboard escape hatch from error screen.
96. Add stable failed-job terminal UI.
97. Hide Cancel for terminal failed jobs.
98. Expand inferred failed execution step.
99. Mark post-failure steps skipped.
100. Show failed-step-specific results empty state.
101. Suppress stale terminal-state toasts on first load.
102. Recover from protected export link failures with authenticated downloads.
103. Add export failure toast with API message.
104. Add request timeout recovery message.
105. Add network failure recovery message.

### Security / Operations

106. Validate all HTTP triggers have bearer validation.
107. Audit raw `fetch` usage in components.
108. Avoid leaking subscription IDs in user-visible logs where not needed.
109. Sanitize SAS tokens from logs and previews.
110. Sanitize bearer tokens from logs and previews.
111. Sanitize Key Vault secret URIs when copied into logs.
112. Keep SSH NSG limited to caller IP.
113. Delete terminal secrets during teardown.
114. Keep storage public network access temporary.
115. Add public network access watchdog cleanup.
116. Add audit events for destructive actions.
117. Add correlation between frontend request ID and backend logs.
118. Add CI lint configuration for ESLint 9.
119. Add SWA smoke test after deploy.
120. Add Function App health smoke test after deploy.

## Implemented In This Wave

- 61-83: shared API retry, timeout, cancellation, request-id, and tests.
- 91-95: improved global render recovery actions.
- 104-105: user-facing transient/network error handling is now backed by retry before surfacing failures.
- 118: ESLint 9 validation is restored with TypeScript and React Hooks checks.

## Validation

- `npm run lint`: passed.
- `npm run test`: passed, 6 Vitest tests.
- `npm run build`: passed.
- `pytest -q api/tests`: passed, 13 tests.
- `npm audit --audit-level=high`: no high/critical vulnerabilities reported; npm still reports existing moderate Vite/esbuild development-server advisories that require a breaking `npm audit fix --force` upgrade.
- `azd deploy web --no-prompt`: deployed to `https://kind-coast-0eb698500.7.azurestaticapps.net/`.
- Browser smoke check on `/blast/jobs/job-8e7f852e3406`: page loads, keeps `Job Failed at Warmup`, keeps Cancel hidden, expands the Warmup failure log, and shows the Warmup-specific no-results state.
