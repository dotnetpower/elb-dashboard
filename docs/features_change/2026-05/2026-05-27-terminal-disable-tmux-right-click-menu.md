# Disable tmux right-click context menu in browser terminal

## Motivation
Right-clicking inside the browser terminal popped up tmux's default
`display-menu` (Copy Line / Horizontal Split / Vertical Split / Kill / …).
For a researcher who expects a plain shell, that menu is noise — it exposes
tmux pane management commands that should not be reachable from the UI and
gets in the way of the browser's own paste behaviour.

## User-facing change
Right-click and middle-click inside the browser terminal no longer open
tmux's context menu. Mouse-based scrolling, selection, and clipboard
integration are unchanged.

## Diff summary
* `terminal/tmux.conf` — unbind `MouseDown3Pane`, `MouseDown3Status`,
  `MouseDown3StatusLeft`, `MouseDown3StatusRight`, `M-MouseDown3Pane`, and
  `MouseDown2Pane`. No other tmux options touched.

## Validation
* `bash -n` syntax check on the tmux.conf is N/A (it is not a bash file);
  the file is loaded by tmux at sidecar start via `COPY tmux.conf
  /etc/tmux.conf` (see `terminal/Dockerfile` L147, `terminal/Dockerfile.runtime`
  L13). Manual verification will land with the next terminal-sidecar build:
  rebuild via `scripts/dev/quick-deploy.sh terminal`, open the browser
  terminal, right-click — no menu should appear.
* No backend / frontend tests are affected (tmux config is sidecar-only).
