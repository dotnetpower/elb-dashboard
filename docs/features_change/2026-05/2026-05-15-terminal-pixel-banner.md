# Terminal Pixel Banner

## Motivation

The browser terminal banner needed a more colorful, high-impact visual treatment while staying readable in xterm.js and preserving the plain MOTD fallback.

## User-facing change

The terminal login banner now renders a large retro pixel-style `ELB` logo with a cyan-to-magenta 256-color gradient, dark offset shadow, scanline accent, and `ElasticBLAST Control Plane` wordmark.

## API/IaC diff summary

No API or IaC changes. The update is limited to the terminal MOTD renderer and its regression test.

## Validation evidence

Validated with `bash -n terminal/banner.sh terminal/command_guard.sh terminal/profile.sh terminal/entrypoint.sh`, `uv run ruff check api/tests/test_terminal_banner.py`, and `uv run pytest -q api/tests/test_terminal_banner.py`.