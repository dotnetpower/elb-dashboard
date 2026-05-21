# Security audit follow-up — design notes (2026-05-22)

These items came out of the 20-finding security sweep on 2026-05-22 but
require a design pass before any code change. Items #3, #6, and #7 were
implemented in
[docs/features_change/2026-05/2026-05-22-security-audit-3-6-7-fixes.md](../features_change/2026-05/2026-05-22-security-audit-3-6-7-fixes.md);
the items in this file are the next blocks of work.

---

## #1 + #4 — Role-based access control on top of bearer validation

### Problem
[api/auth.py](../../api/auth.py) `require_caller` validates the MSAL bearer
(signature, `iss`, `aud`, `exp`) and returns a `CallerIdentity`. It does
**not** check what the caller is allowed to do. Every authenticated tenant
member therefore reaches every `/api/*` route — ARM proxy, AKS provisioning,
Storage prepare-db, BLAST submit, terminal WebSocket, OpenAPI admin. This is
classic Broken Access Control (OWASP A01).

Finding #4 (`tid` claim not separately verified) is a smaller defense-in-depth
gap in the same module; the issuer check already constrains the tenant, but
an explicit `tid` comparison guards against issuer-list regression.

### Design

**Source of truth: Entra ID App Role assignments on the api App Registration.**
We already require the user to consent to the api app; defining roles on that
same app keeps the model single-tenant-friendly and avoids a parallel
authorization service.

Proposed roles (start minimal, expand later):

| Role value | Display name | Grants |
|---|---|---|
| `Reader` | BLAST Reader | All `GET /api/monitor/*`, `GET /api/me`, `GET /api/audit/log`, `GET /api/operations/{id}` (own), `GET /api/blast/jobs` (own). |
| `Submitter` | BLAST Submitter | Everything Reader has, plus `POST /api/blast/submit`, `POST /api/blast/pre-flight`, `POST /api/warmup/*`, `POST /api/terminal/ticket`, `WS /api/terminal/ws`. |
| `Operator` | Platform Operator | Everything Submitter has, plus `POST /api/aks/*`, `POST /api/acr/*`, `POST /api/resources/ensure-rg`, `POST /api/storage/prepare-db`, `POST /api/storage/local-debug/*`. |
| `Admin` | Platform Admin | All `/api/arm/*` write paths, `POST /api/aks/openapi/*`, role-management routes (future). |

Roles are inclusive (Admin implies Operator implies Submitter implies Reader).
A user can hold multiple roles; the union applies.

### Implementation sketch

1. In `CallerIdentity`, add `roles: frozenset[str]` populated from the
   `roles` claim (Entra emits app role assignments under this claim).
2. Verify `tid == AZURE_TENANT_ID` explicitly inside `_validate_token`; reject
   401 with `tenant_mismatch` on mismatch. (Closes #4.)
3. Add a small helper:
   ```python
   def require_role(*allowed: str) -> Callable[[CallerIdentity], CallerIdentity]:
       def _dep(caller: CallerIdentity = Depends(require_caller)) -> CallerIdentity:
           if not caller.roles & set(allowed) and "Admin" not in caller.roles:
               raise HTTPException(403, {"code": "role_required", "needed": list(allowed)})
           return caller
       return _dep
   ```
4. Per-route Depends:
   ```python
   @router.post("/api/aks/provision")
   def aks_provision(caller: CallerIdentity = Depends(require_role("Operator"))):
       ...
   ```
5. Bicep change: declare the four `appRoles` on the api App Registration in
   [infra/modules/identity.bicep](../../infra/modules/identity.bicep) (or the
   matching module — verify path during the implementation pass). Document
   how to assign roles via Entra portal in
   [docs/copilot/auth-flow.md](./auth-flow.md).
6. `AUTH_DEV_BYPASS=true` synthetic identity should carry `roles={"Admin"}`
   so existing pytest paths keep working.

### Rollout
- Phase 1 (single PR): add `roles` to `CallerIdentity`, add `require_role`
  helper, add `tid` check, switch only `/api/aks/*` and `/api/arm/*` to
  `require_role("Operator")` and `require_role("Admin")` respectively. Default
  posture for unassigned users is "Reader" (most read endpoints stay open to
  any authenticated tenant member). This minimises the chance of locking out
  the dashboard before role assignments land.
- Phase 2: lock down `/api/storage/*`, `/api/acr/*`, `/api/warmup/*`,
  `/api/resources/*`.
- Phase 3: enforce Reader on the read endpoints so unassigned users see a
  clear "no role" 403 instead of a half-broken UI.

### Open questions
- Do we want a per-resource-group scope on Operator (e.g. "Operator on rg-elb
  only")? If yes, push the scope into a structured `scope` claim or fall back
  to Azure RBAC checks at call-time. **Recommendation: defer to Phase 4**;
  start with global app roles.
- How do we onboard new users? Document the "Enterprise Application → Users
  and groups → Add user/group" flow in
  [docs/copilot/auth-flow.md](./auth-flow.md) before Phase 3 lands.

### Validation plan
- Unit tests for `_validate_token` with `tid` mismatch (401).
- Unit tests for `require_role` with empty roles (403), exact role (200),
  Admin superset (200).
- E2E: TestClient with dev bypass forced to a specific role set; hit each
  protected route and assert 403 / 200 matrix.

---

## #2 — Per-ticket tmux session in the browser terminal

### Problem
[terminal/entrypoint.sh](../../terminal/entrypoint.sh) starts `ttyd` with
`tmux new-session -A -s elb`. The `-A` flag attaches to the existing session
when one already exists. Result: **every operator who opens the browser
terminal shares the same PTY, the same scrollback, and the same `az login`
context**. Anyone watching the session sees device codes typed by another
user, sees commands typed by another user, and can take over the input
stream.

### Design

**One tmux session per WebSocket ticket. Session name derived from the
ticket id (which is per-handshake and signed by the api sidecar).** When the
ticket disconnects and its TTL expires, the session is killed.

#### Lifecycle
1. `POST /api/terminal/ticket` issues a short-lived ticket bound to
   `caller.object_id`. (Already implemented in
   [api/routes/terminal_ws.py](../../api/routes/terminal_ws.py).)
2. `WS /api/terminal/ws` validates the ticket, then forwards a query
   parameter (`?ticket=…`) to ttyd via the loopback proxy. ttyd's
   `--client-option` lets us set an env var on the child process; we pass
   `ELB_TICKET=<ticket_id>` and `ELB_OWNER_OID=<caller_oid>`.
3. The ttyd command is no longer a bare `tmux new-session …`. It runs a tiny
   shim:
   ```sh
   #!/bin/sh
   set -e
   : "${ELB_TICKET:?missing}"
   : "${ELB_OWNER_OID:?missing}"
   session="elb-${ELB_OWNER_OID:0:8}-${ELB_TICKET:0:12}"
   exec tmux -2 new-session -A -s "$session" \
     -e "ELB_TICKET=$ELB_TICKET" \
     -e "ELB_OWNER_OID=$ELB_OWNER_OID"
   ```
   The session name now uniquely combines the owner and the ticket. `-A`
   becomes safe — re-attaching is per-ticket, not global.
4. A small reaper (cron in the terminal sidecar, or a Celery beat task) runs
   `tmux ls -F '#{session_name} #{session_activity}'` and kills sessions
   whose last activity is older than the ticket TTL (e.g. 4 h).
5. `az login` shells must run inside the per-session scope. Since the session
   is per-owner, `az` writes to `${HOME}/.azure-${ELB_OWNER_OID:0:8}/` (set
   via `AZURE_CONFIG_DIR` in the shim). This isolates token cache files even
   if two operators happen to land on the same node.

#### Bicep / image changes
- `terminal/Dockerfile` adds the shim under `/usr/local/bin/elb-tmux-shim.sh`.
- `terminal/entrypoint.sh` swaps the ttyd `--writable` argument from
  `tmux new-session -A -s elb` to `/usr/local/bin/elb-tmux-shim.sh`.
- `terminal/exec_server.py` is unaffected (it executes one-shot CLIs, no
  tmux involvement).

### Rollout
1. PR 1: ship the shim + the new session-name scheme. Reaper is a noop in
   this PR; cleanup relies on Container App revision restarts.
2. PR 2: add the reaper (beat schedule entry).
3. PR 3: tighten ttyd `--auth` to enforce ticket presence at the proxy layer
   (defense-in-depth — the api sidecar already gates the WebSocket
   upgrade).

### Validation plan
- Manual: open two browser tabs with two different test accounts (or one
  account with two tickets), confirm `tmux ls` inside the sidecar shows two
  distinct sessions, confirm scrollback is not shared.
- Add a small pytest that imports the shim's session-name builder (extract
  it into a tiny Python helper or shell-tested via `bats`) and asserts the
  collision properties.

### Open questions
- Do we want **one session per ticket** or **one session per owner**? The
  former is stricter (each browser tab is its own scrollback) but loses the
  "reconnect and resume" UX. **Recommendation: per ticket**, with a UI
  affordance for "resume last session" that issues a new ticket bound to the
  same prior session id (we can store `last_session_id` on the JobState row
  if needed).
- `AZURE_CONFIG_DIR` per owner — does this break `azcopy` which reads its own
  token cache? Check during the implementation pass.
