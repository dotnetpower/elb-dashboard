# Per-user command history logging in the browser terminal sidecar

**Date:** 2026-05-16
**Phase:** Phase 3 of the 2026-05-16 terminal hardening series
**Related:** [2026-05-16-terminal-disconnect-hardening.md](./2026-05-16-terminal-disconnect-hardening.md), [2026-05-16-terminal-allow-apt-install.md](./2026-05-16-terminal-allow-apt-install.md)

## Motivation

Operators share the `terminal` sidecar of `ca-elb-control`. Until now, the only forensic record of what was typed in the browser terminal was the `api` sidecar's per-request access log (one entry per WebSocket upgrade) — there was no command-level trail to answer questions like "who ran `kubectl delete ns blast` at 14:07?".

We now persist every command from every interactive shell session to the `terminal-home` Azure Files share (the same volume that stores `~/.azure/`), and we tell the operator about it the moment they connect, so the logging is overt, not silent.

## User-facing change

* **Banner** — the colorized banner (`terminal/banner.sh`) and its plain fallback (`terminal/motd`) now end with two extra lines:
  ```
  Audit: every command in this session is recorded to ~/.elb-history/commands.<pid>.log
         (by using this terminal you consent to this logging)
  ```
  Operators see this every time they open the browser terminal.

* **History layout** — under each operator's `$HOME/.elb-history/` (mode `0700`, owner `azureuser`):
  * `commands.<PID>.log` — one file per shell session, native bash `HISTFILE` format with `#<unix_ts>` timestamp lines (`HISTTIMEFORMAT='%FT%T%z  '`). Mode `0600`. `HISTSIZE=10000`, `HISTFILESIZE=100000`. `HISTCONTROL`/`HISTIGNORE` are deliberately **unset** so duplicates, leading-space tricks, and trivial `cd`/`ls` are also captured.
  * `sessions.log` — append-only ledger, one tab-separated line per shell start and shell exit, e.g.:
    ```
    2026-05-15T16:33:51+00:00\tpid=85\ttty=/dev/pts/0\tuser=azureuser\tstart
    2026-05-15T16:35:12+00:00\tpid=85\tend
    ```

* **Persistence** — in production `~/.elb-history/` lives on the `terminal-home` Azure Files share, so logs survive Container App revisions and replica restarts. In local compose the home is container-local, which is fine for development.

## API / IaC diff summary

Backend, frontend, and Bicep are unchanged. The change is contained to the `terminal` image and its tests.

### `terminal/history.sh` (new)
* Sourced by every interactive bash via `/etc/profile.d/elb-history.sh`.
* Returns early on non-interactive shells (`[[ $- == *i* ]] || return 0`).
* Creates `$HOME/.elb-history/` (mode `0700`), sets `HISTFILE="$ELB_HISTORY_DIR/commands.$$.log"`, and prepends `history -a` to `PROMPT_COMMAND` so every Enter press flushes to disk (no loss on WebSocket drop).
* Writes the start record once per shell, and registers an `EXIT` trap that writes the end record. Both lines use literal `$$` expansion at trap-set time so the trap reports the right PID even if subshells reset it.
* Captures `tty` separately (`ELB_HISTORY_TTY="$(tty 2>/dev/null)" || ELB_HISTORY_TTY="unknown"`) — when stdin is `/dev/null` the `tty` command writes "not a tty" to **stdout** before exiting non-zero, which would otherwise slip a newline into the audit line and split it in two.

### `terminal/Dockerfile`
* `COPY history.sh /etc/profile.d/elb-history.sh` + `chmod +x` so it auto-loads for every interactive shell.

### `terminal/banner.sh` and `terminal/motd`
* Added the two-line "Audit:" notice after the existing `▄▄▄▄▄` baseline. Banner uses bold + dim/muted ANSI; the plain motd mirrors the same wording.

### Tests
* `api/tests/test_terminal_banner.py` — both existing render tests now assert that the new "Audit:" line and the `~/.elb-history/commands.<pid>.log` path appear in the rendered banner (color and plain).
* `api/tests/test_terminal_history.py` (new, 5 tests):
  1. `test_history_directory_created_with_restrictive_mode` — asserts `~/.elb-history/` exists and is mode `0700`.
  2. `test_histfile_uses_per_pid_filename` — asserts the per-PID file is created (with an explicit `history -a` flush, since `bash -i -c` never prints a prompt) and that `$HISTFILE` matches the on-disk path.
  3. `test_session_log_records_start_and_end` — asserts both markers exist, share the same PID, and carry `user=elb-test-user`.
  4. `test_prompt_command_includes_history_append` — asserts `history -a` was prepended to `PROMPT_COMMAND`.
  5. `test_history_disabled_for_non_interactive_shell` — asserts `bash -c` (no `-i`) does **not** create the history directory.

## Validation evidence

### Test suite
```
$ uv run pytest -q api/tests/test_terminal_history.py api/tests/test_terminal_banner.py
.......                                                                  [100%]
7 passed in 0.18s

$ uv run pytest -q api/tests
........................................................................ [ 32%]
........................................................................ [ 64%]
........................................................................ [ 96%]
........                                                                 [100%]
224 passed in 22.45s
```

### Lint
```
$ uv run ruff check api/tests/test_terminal_history.py
All checks passed!
```

### Live container
```
$ docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml build terminal
 Image elb-terminal:dev Built

$ docker compose -p elb-control-local -f scripts/dev/docker-compose.full.yml up -d terminal
 Container elb-control-local-terminal-1 Recreated
 Container elb-control-local-terminal-1 Started

$ docker exec -i elb-control-local-terminal-1 bash -lic '...'
██           ElasticBlast CLI  v0.1
    ██         Signed in with Azure  /session
      ██       Guard: protected shell  /help
    ██         Trace: browser >>> api >>> ttyd >>> shell
  ██     ▄▄▄▄▄

  Audit: every command in this session is recorded to ~/.elb-history/commands.<pid>.log
         (by using this terminal you consent to this logging)

# sessions.log:
2026-05-15T16:33:41+00:00	pid=19	tty=unknown	user=azureuser	start
2026-05-15T16:33:41+00:00	pid=19	end
2026-05-15T16:33:51+00:00	pid=85	tty=unknown	user=azureuser	start

# Per-PID HISTFILE (mode 0600):
PROMPT_COMMAND=[history -a]
HISTFILE=/home/azureuser/.elb-history/commands.85.log
-rw------- 1 azureuser azureuser 30 May 15 16:33 commands.85.log

# Content (bash native HISTFILE format with timestamps):
#1778862831
audit-test-marker
```

## Notes & follow-ups

* **`tty=unknown` in compose** — `docker exec -i` does not allocate a PTY by default, so the captured `tty` is literally `unknown` here. Real WebSocket sessions through `ttyd` get a proper `/dev/pts/N` value.
* **Tamper resistance** — these files are writable by `azureuser`; an operator with shell access can `rm` or rewrite them. We deliberately accept this trade-off for now: the `api` sidecar's per-WebSocket access log (request id, MSAL `oid`, timestamp) is the tamper-resistant baseline, and `~/.elb-history/` is the rich correlation source. A future change can ship the files to a write-once sink (e.g. immutable Storage container, Log Analytics) if stronger guarantees are needed.
* **Auditor correlation** — to answer "who ran X at time T", grep `commands.<PID>.log` for the timestamp, then look up `pid=<PID>` in `sessions.log` for the session window, and cross-reference with the `api` sidecar's WebSocket access log for the operator identity.
* **MSAL identity gap** — all sessions show `user=azureuser` because the `terminal` sidecar runs as a single OS user. The MSAL `oid` → PID mapping has to be reconstructed from the `api` sidecar's WebSocket access log timestamp window. Closing this gap would require ttyd to forward a per-client env var to the spawned shell, which ttyd does not natively support.
* **No Azure Functions / Durable Functions code touched** — pure sidecar change.

## 2026-05-16 — Critique hardening pass

After Phase 3 landed I did a critical re-read of the new code and found three weak spots, fixed in the same day:

1. **`sessions.log` was world-readable (mode `0644`).** Even though `~/.elb-history/` is `0700`, on shared mounts the file mode itself is what's compared by some scanners, and it was inconsistent with `commands.<PID>.log` which is `0600`. Wrapped both the start-record append and the `EXIT` trap append in `( umask 0177; ... )` subshells and added a defensive `chmod 0600` after the first write. New regression test `test_sessions_log_is_owner_only` asserts the mode.

2. **Command guard could be bypassed by alias/builtin tricks.** `\sudo` (alias-bypass) and `command sudo` (function-bypass) both invoke the real sudo binary but the old regex `(^|[[:space:]])sudo([[:space:]]|$)` did not match the leading backslash, so the guard would silently let the command pass to sudoers. The OS-level defense (sudoers `NOPASSWD` only for `apt-get/apt update|install`) catches it anyway, but the user got a confusing "password required" prompt instead of a clear guard message. Tightened the regex to `(^|[^a-zA-Z0-9_./-])[\\]?sudo([[:space:]]|$)` and added a separate `command[[:space:]]+sudo` branch. Used `[\\]?` (character class) instead of `(\\)?` (group) because bash strips one level of backslash before the regex engine sees it. The leading-allow regex got the same `[\\]?` treatment so `\sudo apt install htop` keeps working. Three new tests:
   * `test_guard_blocks_alias_bypassing_backslash_sudo` (`\sudo rm -rf /etc` → blocked)
   * `test_guard_allows_alias_bypassing_backslash_sudo_apt_install` (`\sudo apt install htop` → allowed)
   * `test_guard_blocks_command_builtin_sudo` (`command sudo apt install htop` → blocked, deliberately stricter than plain sudo)

3. **No regression test for re-source idempotency.** Sourcing `history.sh` twice in the same shell (e.g. nested `bash -l`) must not write a duplicate start record; the `ELB_HISTORY_SESSION_LOGGED` latch guards this. Added `test_resourcing_does_not_duplicate_start_record`.

4. **Test harness backslash-quoting bug.** While adding the `\sudo` tests I discovered that `_guard_check` was inlining the command via Python's `repr()` (`{command_text!r}`), which escapes a single backslash to `\\` — bash's single-quote rules then preserved both backslashes literally, so the test was actually checking `\\sudo`, not `\sudo`. Refactored the helper to pass the command as `argv[1]` (`"$1"` inside the script body) so no shell quoting layer can mutate it. All 19 pre-existing guard tests continue to pass under the new harness.

### Validation evidence (hardening pass)

```
$ uv run pytest -q api/tests
229 passed in 20.98s   # +5 over the Phase 3 baseline (224)

$ docker exec -i elb-control-local-terminal-1 bash -lic '...'
-rw------- 1 azureuser azureuser 66 May 15 16:41 sessions.log     # was 0644
ELB terminal guard blocked: sudo is restricted to 'apt update' and 'apt install' in the browser terminal
                                                                  # \sudo apt remove curl
ELB terminal guard blocked: sudo is restricted to 'apt update' and 'apt install' in the browser terminal
                                                                  # command sudo apt install foo
WARNING: apt does not have a stable CLI interface. Use with caution in scripts.
                                                                  # \sudo apt install htop -y → allowed
```
