---
mode: agent
description: "Full frontend QA review — browser walkthrough with screenshots and severity-ranked report"
---

## Instructions

1. Open `http://localhost:8090` in the browser.
2. If the Setup Wizard appears, complete all steps by selecting appropriate resource groups, storage, ACR, and terminal VM, then click through to the Dashboard.
3. Take a screenshot of the Dashboard (top and bottom via scroll).
4. Navigate to every page in the sidebar (New Search, Jobs, Terminal) and screenshot each one (scroll if content overflows).
5. Toggle light/dark mode and screenshot the Dashboard in the alternate theme.
6. Check the browser console for errors (500s, failed fetches, React warnings).
7. Produce a review with two sections:
   - **Strengths**: layout, UX flow, visual design, real-time updates, accessibility.
   - **Issues table**: columns `#`, `Severity` (Critical/Medium/Low/Minor), `Item`, `Detail`. Cover: data correctness, status colour coding, text readability in both themes, button safety (confirm dialogs), console errors, responsive concerns.
8. End with a one-paragraph overall assessment.
