# Terminal Command Insert Run

## Motivation

Manual and Cockpit terminal command helpers should execute selected commands directly instead of only pasting text into the terminal prompt.

## User-facing change

Manual command rows now show Copy and Insert actions side by side. Insert sends the command to the browser terminal and presses Enter. Cockpit Insert uses the same execute-on-insert behavior.

Insert actions are disabled while the terminal is not connected, and command text is normalized before execution so accidental control characters or pasted newlines do not create surprise multi-line runs. Cockpit and Manual Insert also share the same high-risk command guard: high-risk commands can still be copied for manual review, but cannot be launched by one-click Insert.

## API / IaC diff summary

No API or IaC changes. This is a frontend terminal UI behavior change.

## Validation evidence

- `cd web && npx tsc --noEmit`
- `cd web && npm run build`
- `cd web && npm test -- terminalCockpitModel.test.ts`
- Browser smoke: Manual `pwd` Insert ran immediately in the terminal, and Cockpit `pwd` Insert also ran and printed `/home/azureuser`.
