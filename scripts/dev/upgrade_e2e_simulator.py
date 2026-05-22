"""End-to-end in-process simulator for the self-upgrade flow.

Drives the upgrade state machine through every transition without
needing terminal sidecar, ACR, ARM, or Storage. Every external surface
is stubbed:

* `state` / `build_logs` / `history`: in-memory backends.
* `terminal_exec.run` / `.stream`: emit synthetic git clone + az acr
  build output.
* `aca_template`: fake Container App template with mutable image refs.
* `acr_inventory`: every snapshotted tag exists by default; chaos mode
  flips one to missing.
* `api.__version__`: reload-aware patching so the reconciler observes
  the post-upgrade version.

Each scenario asserts the expected state transitions and prints a
PASS / FAIL line. Exit code = number of failures.

Usage:
  PYTHONPATH=$PWD uv run python scripts/dev/upgrade_e2e_simulator.py
  PYTHONPATH=$PWD uv run python scripts/dev/upgrade_e2e_simulator.py --scenario rollback_missing_tag
"""
# ruff: noqa: E501  -- sandbox script; long argv-builder/dict lines stay readable as one column.

from __future__ import annotations

import argparse
import os
import sys
import traceback
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

# Sandbox env BEFORE importing api.
os.environ.setdefault("AUTH_DEV_BYPASS", "true")
os.environ.setdefault("ELB_ALLOW_INMEMORY_UPGRADE_STATE", "true")
os.environ.setdefault("ELB_ALLOW_INMEMORY_BUILD_LOGS", "true")
os.environ.setdefault("ELB_ALLOW_INMEMORY_UPGRADE_HISTORY", "true")
os.environ.setdefault("UPGRADE_GIT_REMOTE", "https://example.test/sandbox.git")
os.environ.setdefault("PLATFORM_ACR_NAME", "sandboxacr")
os.environ.setdefault("AZURE_SUBSCRIPTION_ID", "11111111-2222-3333-4444-555555555555")
os.environ.setdefault("AZURE_RESOURCE_GROUP", "rg-sandbox")
os.environ.setdefault("CONTAINER_APP_NAME", "ca-sandbox")
os.environ.pop("AZURE_TABLE_ENDPOINT", None)
os.environ.pop("AZURE_BLOB_ENDPOINT", None)

# Now import the api surface.
from api.services import terminal_exec
from api.services.upgrade import (
    aca_template,
    acr_inventory,
    build_logs,
    history,
    state,
)
from api.tasks import upgrade as upgrade_task

# ---------------------------------------------------------------------------
# Stubs
# ---------------------------------------------------------------------------


class _FakeRunner:
    """Stand-in for `api.services.terminal_exec`.

    Mimics both `run()` (git clone, scrub) and `stream()` (az acr build).
    """

    def __init__(
        self,
        *,
        clone_exit: int = 0,
        build_exit: int = 0,
        stderr: str = "",
        scrub_url: str = "https://example.test/sandbox.git",
    ) -> None:
        self.run_calls: list[list[str]] = []
        self.stream_calls: list[list[str]] = []
        self._clone_exit = clone_exit
        self._build_exit = build_exit
        self._stderr = stderr
        self._scrub_url = scrub_url
        # The git_workspace scrub flow calls `git config --get remote.origin.url`
        # then `git config remote.origin.url <masked>`; respond accordingly.
        self.TerminalExecError = terminal_exec.TerminalExecError

    def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
        self.run_calls.append(argv)
        # git config --get remote.origin.url
        if argv[:5] == ["git", "-C", argv[2], "config", "--get"]:
            return {"exit_code": 0, "stdout": self._scrub_url, "stderr": ""}
        if "config" in argv and "--get" in argv:
            return {"exit_code": 0, "stdout": self._scrub_url, "stderr": ""}
        if argv[0] == "git" and argv[1] == "clone":
            return {"exit_code": self._clone_exit, "stdout": "", "stderr": self._stderr}
        if argv[0] == "git" and "config" in argv:
            # scrub write
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if argv[0] == "git" and "clean" in argv:
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        return {"exit_code": 0, "stdout": "", "stderr": ""}

    def stream(self, argv: list[str], *, timeout_seconds: int) -> Iterator[dict[str, Any]]:
        self.stream_calls.append(argv)
        component = "unknown"
        for piece in argv:
            if piece.startswith("elb-"):
                component = piece.split(":")[0]
        yield {"stream": "stdout", "line": f"step 1/3: pulling base image for {component}"}
        yield {"stream": "stdout", "line": f"step 2/3: building {component}"}
        yield {"stream": "stdout", "line": f"step 3/3: pushed {component}"}
        yield {"exit_code": self._build_exit, "duration_ms": 1, "timed_out": False}


@dataclass
class _Container:
    name: str
    image: str


@dataclass
class _Template:
    containers: list[_Container]
    revision_suffix: str = ""


@dataclass
class _Properties:
    template: _Template
    latest_revision_name: str = "ca-sandbox--initial"


@dataclass
class _AppResource:
    properties: _Properties
    name: str = "ca-sandbox"


class _FakeAca:
    """Stand-in for `api.services.upgrade.aca_template` module surface."""

    def __init__(self, *, initial_tag: str = "v0.2.0") -> None:
        acr = "sandboxacr.azurecr.io"
        self.app = _AppResource(
            properties=_Properties(
                template=_Template(
                    containers=[
                        _Container("api", f"{acr}/elb-api:{initial_tag}"),
                        _Container("worker", f"{acr}/elb-api:{initial_tag}"),
                        _Container("beat", f"{acr}/elb-api:{initial_tag}"),
                        _Container("frontend", f"{acr}/elb-frontend:{initial_tag}"),
                        _Container("terminal", f"{acr}/elb-terminal:{initial_tag}"),
                        _Container("redis", "redis:7-alpine"),
                    ]
                )
            )
        )
        self.swap_calls: list[tuple[str, str]] = []
        self.apply_calls: list[aca_template.SidecarImages] = []

    def read_current_images(self) -> aca_template.SidecarImages:
        api_image = next(c.image for c in self.app.properties.template.containers if c.name == "api")
        front = next(c.image for c in self.app.properties.template.containers if c.name == "frontend")
        term = next(c.image for c in self.app.properties.template.containers if c.name == "terminal")
        return aca_template.SidecarImages(api=api_image, frontend=front, terminal=term)

    def swap_images(self, *, target_version: str, revision_suffix: str | None = None):
        self.swap_calls.append((target_version, revision_suffix or ""))
        target = aca_template.compute_target_images(target_version)
        # Apply to the fake template so subsequent reads see the new refs.
        for container in self.app.properties.template.containers:
            if container.name in {"api", "worker", "beat"}:
                container.image = target.api
            elif container.name == "frontend":
                container.image = target.frontend
            elif container.name == "terminal":
                container.image = target.terminal
        if revision_suffix:
            self.app.properties.template.revision_suffix = revision_suffix
            self.app.properties.latest_revision_name = f"ca-sandbox--{revision_suffix}"
        previous = aca_template.SidecarImages(
            api="sandboxacr.azurecr.io/elb-api:v0.2.0",
            frontend="sandboxacr.azurecr.io/elb-frontend:v0.2.0",
            terminal="sandboxacr.azurecr.io/elb-terminal:v0.2.0",
        )
        return ("poller", previous, target)

    def apply_images(self, *, images: aca_template.SidecarImages, revision_suffix: str | None = None):
        self.apply_calls.append(images)
        for container in self.app.properties.template.containers:
            if container.name in {"api", "worker", "beat"}:
                container.image = images.api
            elif container.name == "frontend":
                container.image = images.frontend
            elif container.name == "terminal":
                container.image = images.terminal
        return "poller-rb"

    def latest_revision_name(self) -> str:
        return self.app.properties.latest_revision_name


class _FakeWatcher:
    def __init__(
        self,
        *,
        running: str = "Running",
        provisioning: str = "Provisioned",
        replicas: int = 1,
        active: bool = True,
    ) -> None:
        self.running = running
        self.provisioning = provisioning
        self.replicas = replicas
        self.active = active

    def revision_status(self, name: str):
        return type(
            "S",
            (),
            {
                "name": name,
                "running_state": self.running,
                "provisioning_state": self.provisioning,
                "health_state": "Healthy",
                "replicas": self.replicas,
                "active": self.active,
            },
        )()


class _AlwaysExistsAcr:
    def get_tag_properties(self, _repo: str, _tag: str):
        return type("P", (), {"created_on": datetime.now(UTC)})()

    def close(self) -> None:
        pass


class _MissingTagAcr:
    def get_tag_properties(self, _repo: str, _tag: str):
        raise Exception("TagNotFound: simulated")

    def close(self) -> None:
        pass


# ---------------------------------------------------------------------------
# Scenario runner
# ---------------------------------------------------------------------------


def _reset_backends() -> None:
    state.set_backend(state.InMemoryBackend())
    build_logs.set_backend(build_logs.InMemoryBuildLogBackend())
    history.set_backend(history.InMemoryHistoryBackend())


def _set_running_version(value: str) -> None:
    """Patch `api.tasks.upgrade.__version__` for the reconciler test."""
    upgrade_task.__version__ = value


def _print(label: str, ok: bool, detail: str = "") -> None:
    mark = "PASS" if ok else "FAIL"
    print(f"  [{mark}] {label}" + (f" — {detail}" if detail else ""))


def scenario_happy_path() -> int:
    """idle → queued → fetching → building → patching → rolling_out → succeeded → rollback."""
    print("\n--- Scenario: happy path ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    aca = _FakeAca(initial_tag="v0.2.0")
    runner = _FakeRunner()

    try:
        after_start = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="abc1234",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,  # we will run execute_upgrade_inline directly
        )
        _print("start → queued", after_start.state == state.STATE_QUEUED)
        failures += 0 if after_start.state == state.STATE_QUEUED else 1
        job_id = after_start.job_id

        after_exec = upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="abc1234",
            started_by_oid="oid-test",
            job_id=job_id,
            runner=runner,
            aca=aca,
        )
        ok = after_exec.state == state.STATE_ROLLING_OUT
        _print(
            "execute → rolling_out",
            ok,
            f"state={after_exec.state} progress={after_exec.phase_progress}",
        )
        failures += 0 if ok else 1

        ok = len(aca.swap_calls) == 1 and aca.swap_calls[0][0] == "0.3.0"
        _print("ARM PATCH (swap_images) called once with target_version", ok)
        failures += 0 if ok else 1

        ok = len(runner.stream_calls) == 3
        _print(
            "3 az acr build invocations streamed (api, frontend, terminal)",
            ok,
            f"actual={len(runner.stream_calls)}",
        )
        failures += 0 if ok else 1

        # Build logs persisted.
        for component in ("api", "frontend", "terminal"):
            blob = build_logs.read_blob(job_id, component)
            ok = b"step 3/3" in blob
            _print(f"build log present for {component}", ok)
            failures += 0 if ok else 1

        snap = after_exec.rollback_target()
        ok = all(snap.get(role, "").endswith(":v0.2.0") for role in ("api", "frontend", "terminal"))
        _print("rollback snapshot captured (all 3 roles at v0.2.0)", ok, str(snap))
        failures += 0 if ok else 1

        # Reconciler: simulate that __version__ became target_version (new revision booted).
        _set_running_version("0.3.0")
        after_rec = upgrade_task.reconcile_rolling_out_inline(
            aca=aca, watcher=_FakeWatcher(), now=lambda: datetime.now(UTC)
        )
        ok = after_rec.state == state.STATE_SUCCEEDED
        _print(
            "reconcile → succeeded",
            ok,
            f"state={after_rec.state} running_version={after_rec.running_version}",
        )
        failures += 0 if ok else 1

        # History should include start, escape_hatch, succeeded events.
        events = history.tail_events(limit=10)
        names = [e.event for e in events]
        ok = "succeeded" in names and "start" in names and "escape_hatch" in names
        _print("history has start + escape_hatch + succeeded", ok, str(names))
        failures += 0 if ok else 1

        # Rollback round-trip.
        after_rb = upgrade_task.start_rollback_inline(
            started_by_oid="oid-test", aca=aca, watcher=_FakeWatcher()
        )
        ok = after_rb.state == state.STATE_ROLLED_BACK
        _print(
            "rollback → rolled_back",
            ok,
            f"state={after_rb.state} applied_count={len(aca.apply_calls)}",
        )
        failures += 0 if ok else 1

        ok = len(aca.apply_calls) == 1 and aca.apply_calls[0].api.endswith(":v0.2.0")
        _print("rollback PATCH targeted the snapshot (v0.2.0)", ok)
        failures += 0 if ok else 1

    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_build_failure() -> int:
    """`az acr build` exit!=0 → failed_pre, no PATCH, no rollback snapshot."""
    print("\n--- Scenario: build failure → failed_pre ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    aca = _FakeAca()
    runner = _FakeRunner(build_exit=1)

    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        after = upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        ok = after.state == state.STATE_FAILED_PRE
        _print("execute → failed_pre", ok, f"state={after.state}")
        failures += 0 if ok else 1

        ok = aca.swap_calls == []
        _print("ARM PATCH never invoked", ok)
        failures += 0 if ok else 1

    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_rollback_missing_tag() -> int:
    """Successful upgrade, then ACR retention purged the snapshot → rollback refused."""
    print("\n--- Scenario: rollback refused (ACR retention purged) ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    aca = _FakeAca()
    runner = _FakeRunner()

    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        # Now simulate ACR purge before the rollback is attempted.
        acr_inventory.set_client_factory_for_tests(lambda _ep: _MissingTagAcr())
        refused = False
        try:
            upgrade_task.start_rollback_inline(
                started_by_oid="oid-test", aca=aca, watcher=_FakeWatcher()
            )
        except upgrade_task.RollbackStartRefused as exc:
            refused = True
            detail = str(exc)
        _print(
            "rollback refused with `ACR no longer carries`",
            refused and "ACR no longer carries" in detail,
        )
        failures += 0 if refused else 1

        # apply_images never called (rollback CAS was never attempted).
        ok = aca.apply_calls == []
        _print("no rollback PATCH issued", ok)
        failures += 0 if ok else 1

    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_fast_fail_stuck_rolling_out() -> int:
    """rolling_out with stale ACA template + >2min → fast fail_rollout."""
    print("\n--- Scenario: rolling_out fast-fail ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    aca = _FakeAca(initial_tag="v0.2.0")
    runner = _FakeRunner()

    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        # Roll the fake aca back to the OLD images to simulate "PATCH did
        # not land". Also wind `started_at` back so the 120-second grace
        # has elapsed.
        for c in aca.app.properties.template.containers:
            if c.name in {"api", "worker", "beat"}:
                c.image = "sandboxacr.azurecr.io/elb-api:v0.2.0"

        # Inject an aged started_at.
        def _wind_back(s_state: state.UpgradeState) -> None:
            s_state.started_at = "2026-05-22T13:00:00+00:00"

        state.update_state(_wind_back)
        def now_func():
            return datetime(2026, 5, 22, 13, 5, 0, tzinfo=UTC)

        after_rec = upgrade_task.reconcile_rolling_out_inline(
            aca=aca, watcher=_FakeWatcher(running="Processing"), now=now_func
        )
        ok = after_rec.state == state.STATE_FAILED_ROLLOUT
        _print(
            "reconcile fast-fail → failed_rollout",
            ok,
            f"state={after_rec.state} detail={after_rec.phase_detail!r}",
        )
        failures += 0 if ok else 1

    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_credential_scrub_failure() -> int:
    """Scrub-write fails → clone aborts → failed_pre. PAT never reaches build."""
    print("\n--- Scenario: credential scrub write failure ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    aca = _FakeAca()

    class _ScrubBoom:
        TerminalExecError = terminal_exec.TerminalExecError

        def __init__(self) -> None:
            self.run_calls: list[list[str]] = []
            self.stream_calls: list[list[str]] = []

        def run(self, argv: list[str], *, cwd: str | None, timeout_seconds: int) -> dict[str, Any]:
            self.run_calls.append(argv)
            if argv[0] == "git" and argv[1] == "clone":
                return {"exit_code": 0, "stdout": "", "stderr": ""}
            if argv[0] == "git" and "config" in argv and "--get" in argv:
                return {
                    "exit_code": 0,
                    "stdout": "https://x-access-token:supersecret@example.test/foo.git",
                    "stderr": "",
                }
            if argv[0] == "git" and "config" in argv:
                raise terminal_exec.TerminalExecError("simulated scrub-write failure")
            return {"exit_code": 0, "stdout": "", "stderr": ""}

        def stream(self, argv: list[str], *, timeout_seconds: int) -> Iterator[dict[str, Any]]:
            self.stream_calls.append(argv)
            yield {"exit_code": 0}

    runner = _ScrubBoom()
    # Need to set UPGRADE_GIT_REMOTE to a PAT URL for the scrub guard
    # to even attempt to write.
    os.environ["UPGRADE_GIT_REMOTE"] = (
        "https://x-access-token:supersecret@example.test/sandbox.git"
    )
    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        after = upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        ok = after.state == state.STATE_FAILED_PRE
        _print("execute → failed_pre", ok, f"state={after.state}")
        failures += 0 if ok else 1

        ok = runner.stream_calls == []
        _print("az acr build never invoked", ok)
        failures += 0 if ok else 1

        ok = "scrub" in after.phase_detail.lower() or "credential" in after.phase_detail.lower()
        _print("phase_detail mentions scrub/credential", ok, after.phase_detail)
        failures += 0 if ok else 1

    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        os.environ["UPGRADE_GIT_REMOTE"] = "https://example.test/sandbox.git"
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_double_start() -> int:
    """Concurrent start CAS: second call must 409 (UpgradeStartRefused)."""
    print("\n--- Scenario: double-start CAS ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")
    try:
        upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-a",
            enqueue=lambda *args: None,
        )
        refused = False
        try:
            upgrade_task.start_upgrade_inline(
                target_version="0.4.0",
                target_sha="",
                started_by_oid="oid-b",
                enqueue=lambda *args: None,
            )
        except upgrade_task.UpgradeStartRefused:
            refused = True
        _print("second start refused", refused)
        failures += 0 if refused else 1
        row = state.get_state()
        ok = row.target_version == "0.3.0" and row.started_by_oid == "oid-a"
        _print(
            "first caller's target/oid preserved",
            ok,
            f"{row.target_version}/{row.started_by_oid}",
        )
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_reconciler_arm_outage() -> int:
    """ARM read failure during reconcile must not corrupt the row."""
    print("\n--- Scenario: reconciler tolerates ARM read failure ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    aca = _FakeAca()
    runner = _FakeRunner()
    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )

        class _BrokenAca(_FakeAca):
            def latest_revision_name(self) -> str:
                raise aca_template.TemplateError("simulated ARM outage")

            def read_current_images(self) -> aca_template.SidecarImages:
                raise aca_template.TemplateError("simulated ARM outage")

        broken = _BrokenAca()
        broken.app = aca.app  # preserve image refs (so fast-fail check fails-open)
        _set_running_version("0.2.0")
        after = upgrade_task.reconcile_rolling_out_inline(
            aca=broken, watcher=_FakeWatcher(), now=lambda: datetime.now(UTC)
        )
        ok = after.state == state.STATE_ROLLING_OUT
        _print(
            "row stays rolling_out under ARM outage",
            ok,
            f"state={after.state}",
        )
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_history_record_safe() -> int:
    """record_event must never raise even when the backend is broken."""
    print("\n--- Scenario: history record_event swallows backend errors ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    class _BoomHistory:
        def append(self, payload: bytes) -> None:
            raise RuntimeError("simulated history backend failure")

        def read_all(self) -> bytes:
            return b""

    history.set_backend(_BoomHistory())
    try:
        upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        _print("start_upgrade_inline survived broken history backend", True)
    except Exception as exc:
        _print(
            "start_upgrade_inline propagated history error",
            False,
            f"{type(exc).__name__}: {exc}",
        )
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_reconcile_idle_only_syncs_running() -> int:
    """reconcile in idle state must only update running_version, no state change."""
    print("\n--- Scenario: reconcile in idle only syncs running_version ---")
    failures = 0
    _reset_backends()
    _set_running_version("0.5.0")
    try:
        before = state.get_state()
        ok = before.state == state.STATE_IDLE
        _print("precondition: state=idle", ok)
        failures += 0 if ok else 1
        after = upgrade_task.reconcile_rolling_out_inline(
            aca=_FakeAca(), watcher=_FakeWatcher(), now=lambda: datetime.now(UTC)
        )
        ok = after.state == state.STATE_IDLE and after.running_version == "0.5.0"
        _print(
            "state stays idle, running_version synced",
            ok,
            f"state={after.state} running={after.running_version}",
        )
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    return failures


def scenario_rollback_idle_refused() -> int:
    """rollback in idle must refuse with `rollback only valid after PATCH`."""
    print("\n--- Scenario: rollback refused when row is idle ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    try:
        refused = False
        detail = ""
        try:
            upgrade_task.start_rollback_inline(
                started_by_oid="oid-test", aca=_FakeAca(), watcher=_FakeWatcher()
            )
        except upgrade_task.RollbackStartRefused as exc:
            refused = True
            detail = str(exc)
        _print(
            "rollback in idle refused",
            refused and "only valid after PATCH" in detail,
            detail if refused else "no exception",
        )
        failures += 0 if refused else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_stuck_guard_15min() -> int:
    """Even when fast-fail can't fire (template already shows new image),
    the 15-minute stuck guard still moves rolling_out → failed_rollout."""
    print("\n--- Scenario: 15-min stuck guard ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")
    aca = _FakeAca()
    runner = _FakeRunner()
    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )

        def _wind_back(s_state: state.UpgradeState) -> None:
            s_state.started_at = "2026-05-22T12:00:00+00:00"

        state.update_state(_wind_back)
        def now_func():
            return datetime(2026, 5, 22, 12, 16, 0, tzinfo=UTC)
        after = upgrade_task.reconcile_rolling_out_inline(
            aca=aca, watcher=_FakeWatcher(running="Processing"), now=now_func
        )
        ok = after.state == state.STATE_FAILED_ROLLOUT
        _print(
            "15-min stuck guard fired",
            ok,
            f"state={after.state} detail={after.phase_detail!r}",
        )
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_swap_images_raises_after_cas() -> int:
    """When aca.swap_images raises AFTER the rolling_out CAS commit,
    the row must move straight to failed_rollout (not failed_pre)."""
    print("\n--- Scenario: swap_images raises post-CAS ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")

    class _SwapBoomAca(_FakeAca):
        def swap_images(self, *, target_version: str, revision_suffix=None):
            self.swap_calls.append((target_version, revision_suffix or ""))
            raise aca_template.TemplateError("simulated begin_update failure")

    aca = _SwapBoomAca()
    runner = _FakeRunner()
    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        after = upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        ok = after.state == state.STATE_FAILED_ROLLOUT
        _print("state → failed_rollout (not failed_pre)", ok, f"state={after.state}")
        failures += 0 if ok else 1
        ok = len(aca.swap_calls) == 1
        _print("swap_images invoked exactly once", ok, f"count={len(aca.swap_calls)}")
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_double_execute_is_noop() -> int:
    """Calling execute_upgrade_inline twice with the same job_id when
    the row already advanced past queued must noop, not corrupt state."""
    print("\n--- Scenario: double execute is no-op ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")
    aca = _FakeAca()
    runner = _FakeRunner()
    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        first = upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        ok = first.state == state.STATE_ROLLING_OUT
        _print("first execute landed in rolling_out", ok, f"state={first.state}")
        failures += 0 if ok else 1
        prior_swaps = len(aca.swap_calls)
        prior_streams = len(runner.stream_calls)
        second = upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        ok = second.state == state.STATE_ROLLING_OUT
        _print("second execute returned current row", ok)
        failures += 0 if ok else 1
        ok = len(aca.swap_calls) == prior_swaps
        _print("swap_images NOT re-invoked", ok)
        failures += 0 if ok else 1
        ok = len(runner.stream_calls) == prior_streams
        _print("az acr build NOT re-invoked", ok)
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_build_log_large_flush() -> int:
    """BuildLogWriter must auto-flush large lines (>64 KiB threshold)."""
    print("\n--- Scenario: build log flushes large payloads ---")
    failures = 0
    _reset_backends()
    try:
        writer = build_logs.open_writer("jobBig", "api")
        big_line = "x" * 10_000  # 10 KiB
        for i in range(20):  # 200 KiB total
            writer.write_line(f"{i}: {big_line}")
        writer.flush()
        blob = build_logs.read_blob("jobBig", "api")
        ok = len(blob) >= 200_000
        _print("all 200 KiB persisted", ok, f"blob_size={len(blob)}")
        failures += 0 if ok else 1
        ok = b"19: " in blob and b"0: " in blob
        _print("first and last lines present", ok)
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    return failures


def scenario_ssrf_guard_blocks_imds() -> int:
    """remote_tags must refuse IMDS / loopback / link-local / bad-shape URLs."""
    print("\n--- Scenario: SSRF guard blocks unsafe URLs ---")
    failures = 0
    from api.services.upgrade import remote_tags

    cases = [
        "https://169.254.169.254/repo.git",
        "https://localhost/repo.git",
        "https://127.0.0.1/repo.git",
        "https://[::1]/repo.git",
        "git@github.com:foo/bar.git",
        "not-a-url",
    ]
    for url in cases:
        refused = False
        try:
            remote_tags.fetch_release_tags(url)
        except remote_tags.RemoteTagsError:
            refused = True
        _print(f"refused {url!r}", refused)
        failures += 0 if refused else 1
    return failures


def scenario_enqueue_failure_sanitised() -> int:
    """When the Celery broker is unreachable, start_upgrade_inline must:
       1) roll the row back to idle,
       2) record `phase_detail` with the exception TYPE only (no broker URL),
       3) propagate the original exception to the route layer."""
    print("\n--- Scenario: enqueue failure sanitised ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())

    def _boom_enqueue(*_args):
        # Simulate a Celery/Redis broker auth failure whose default
        # repr includes the broker URL with embedded credentials.
        raise ConnectionRefusedError(
            "Cannot connect to redis://:topsecret-broker-password@10.0.0.5:6379/0"
        )

    try:
        raised = False
        try:
            upgrade_task.start_upgrade_inline(
                target_version="0.3.0",
                target_sha="",
                started_by_oid="oid-test",
                enqueue=_boom_enqueue,
            )
        except ConnectionRefusedError:
            raised = True
        _print("exception propagated to caller", raised)
        failures += 0 if raised else 1

        row = state.get_state()
        ok = row.state == state.STATE_IDLE
        _print("row rolled back to idle", ok, f"state={row.state}")
        failures += 0 if ok else 1

        # phase_detail must contain the exception TYPE but NOT the broker password.
        ok = "ConnectionRefusedError" in row.phase_detail
        _print("phase_detail mentions exception type", ok, row.phase_detail)
        failures += 0 if ok else 1

        ok = "topsecret-broker-password" not in row.phase_detail
        _print(
            "phase_detail does NOT leak broker credentials",
            ok,
            row.phase_detail,
        )
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_escape_hatch_env_placeholders() -> int:
    """escape_hatch.build_plan falls back to placeholders when env unset."""
    print("\n--- Scenario: escape-hatch placeholders when env missing ---")
    failures = 0
    from api.services.upgrade import escape_hatch
    from api.services.upgrade.aca_template import SidecarImages

    saved_app = os.environ.pop("CONTAINER_APP_NAME", None)
    saved_rg = os.environ.pop("AZURE_RESOURCE_GROUP", None)
    try:
        plan = escape_hatch.build_plan(
            SidecarImages(api="acr/elb-api:v1", frontend="acr/elb-frontend:v1", terminal="acr/elb-terminal:v1")
        )
        all_cmds = "\n".join(plan.commands)
        _print("falls back to <container-app>", "<container-app>" in all_cmds)
        failures += 0 if "<container-app>" in all_cmds else 1
        _print("falls back to <resource-group>", "<resource-group>" in all_cmds)
        failures += 0 if "<resource-group>" in all_cmds else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        if saved_app is not None:
            os.environ["CONTAINER_APP_NAME"] = saved_app
        if saved_rg is not None:
            os.environ["AZURE_RESOURCE_GROUP"] = saved_rg
    return failures


def scenario_first_write_race() -> int:
    """Two operators race past `cas_state(IDLE -> QUEUED)` on a fresh row.

    The backend must refuse the second no-etag write so only one start
    wins. Before the hardening, the second writer silently overwrote
    the first on the shared row.
    """
    print("\n--- Scenario: first-write race ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")
    try:
        first = state.UpgradeState(state=state.STATE_QUEUED, job_id="first-job")
        state._backend().upsert(first, expected_etag="")
        refused = False
        try:
            second = state.UpgradeState(state=state.STATE_QUEUED, job_id="second-job")
            state._backend().upsert(second, expected_etag="")
        except state.RowEtagMismatch:
            refused = True
        _print("second no-etag upsert refused", refused)
        failures += 0 if refused else 1
        row = state.get_state()
        ok = row.job_id == "first-job"
        _print("first writer's job_id preserved", ok, f"job_id={row.job_id}")
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_pre_patch_stuck_timeout() -> int:
    """Worker dies mid-build → row sits in BUILDING forever.

    The reconciler's pre-PATCH timeout guard must move the row to
    `failed_pre` once `PRE_PATCH_TIMEOUT_SECONDS` is exceeded. Without
    this guard the SPA's progress bar spun indefinitely and the only
    recovery path was a manual row edit.
    """
    from datetime import UTC, datetime, timedelta

    print("\n--- Scenario: pre-PATCH stuck timeout (worker died mid-build) ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")
    try:
        started = datetime(2026, 5, 22, 12, 0, 0, tzinfo=UTC)
        state.update_state(
            lambda s: (
                setattr(s, "state", state.STATE_BUILDING),
                setattr(s, "started_at", started.isoformat(timespec="seconds")),
                setattr(s, "job_id", "dead-worker"),
                setattr(s, "target_version", "0.3.0"),
            )[-1]
        )
        fake_now = lambda: started + timedelta(minutes=40)  # noqa: E731
        after = upgrade_task.reconcile_rolling_out_inline(
            aca=_FakeAca(), watcher=_FakeWatcher(), now=fake_now
        )
        ok = after.state == state.STATE_FAILED_PRE
        _print("stuck pre-PATCH row escalated to failed_pre", ok, f"state={after.state}")
        failures += 0 if ok else 1
        ok2 = "stuck in" in after.phase_detail
        _print("phase_detail explains the stuck-state cause", ok2, after.phase_detail)
        failures += 0 if ok2 else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_degraded_running_state() -> int:
    """`running_state=Degraded` must be treated as a terminal rollout
    failure (CrashLoopBackOff masquerading as Provisioned). Without
    this detection the operator waited the full 15-min stuck-guard
    window for an actionable signal.
    """
    print("\n--- Scenario: degraded running_state → failed_rollout ---")
    failures = 0
    _reset_backends()
    acr_inventory.set_client_factory_for_tests(lambda _ep: _AlwaysExistsAcr())
    _set_running_version("0.2.0")
    aca = _FakeAca()
    runner = _FakeRunner()
    try:
        s = upgrade_task.start_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            enqueue=lambda *args: None,
        )
        upgrade_task.execute_upgrade_inline(
            target_version="0.3.0",
            target_sha="",
            started_by_oid="oid-test",
            job_id=s.job_id,
            runner=runner,
            aca=aca,
        )
        original_version = upgrade_task.__version__
        try:
            upgrade_task.__version__ = "0.2.0"  # pin so success branch does not fire
            degraded_watcher = _FakeWatcher(running="Degraded", provisioning="Provisioned")
            after = upgrade_task.reconcile_rolling_out_inline(
                aca=aca, watcher=degraded_watcher
            )
            ok = after.state == state.STATE_FAILED_ROLLOUT
            _print(
                "Degraded running_state escalated to failed_rollout",
                ok,
                f"state={after.state}",
            )
            failures += 0 if ok else 1
            ok2 = (
                "Degraded" in after.phase_detail or "running_state" in after.phase_detail
            )
            _print("phase_detail explains the failure cause", ok2, after.phase_detail)
            failures += 0 if ok2 else 1
        finally:
            upgrade_task.__version__ = original_version
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    finally:
        acr_inventory.set_client_factory_for_tests(None)
    return failures


def scenario_history_dedupes_double_write() -> int:
    """Append-blob backends are at-least-once. A double-written event
    must appear only once in `tail_events` (event_id dedup).
    """
    print("\n--- Scenario: history dedupes double-write ---")
    failures = 0
    _reset_backends()
    try:
        history.record_event("start", job_id="jdup", target_version="0.3.0")
        raw = history._backend().read_all()
        line = raw.strip().split(b"\n")[-1]
        history._backend().append(line + b"\n")
        history._backend().append(line + b"\n")
        events = history.tail_events(limit=10)
        ok = len(events) == 1 and events[0].event == "start"
        _print("three writes deduped to one logical event", ok, f"len={len(events)}")
        failures += 0 if ok else 1
    except Exception as exc:
        _print("scenario crashed", False, f"{type(exc).__name__}: {exc}")
        traceback.print_exc()
        failures += 1
    return failures


SCENARIOS = {
    "happy_path": scenario_happy_path,
    "build_failure": scenario_build_failure,
    "rollback_missing_tag": scenario_rollback_missing_tag,
    "fast_fail": scenario_fast_fail_stuck_rolling_out,
    "scrub_failure": scenario_credential_scrub_failure,
    "double_start": scenario_double_start,
    "arm_outage": scenario_reconciler_arm_outage,
    "history_safety": scenario_history_record_safe,
    "reconcile_idle_noop": scenario_reconcile_idle_only_syncs_running,
    "rollback_idle_refused": scenario_rollback_idle_refused,
    "stuck_guard_15min": scenario_stuck_guard_15min,
    "swap_raises": scenario_swap_images_raises_after_cas,
    "double_execute": scenario_double_execute_is_noop,
    "build_log_flush": scenario_build_log_large_flush,
    "ssrf_guard": scenario_ssrf_guard_blocks_imds,
    "enqueue_sanitised": scenario_enqueue_failure_sanitised,
    "escape_hatch_placeholders": scenario_escape_hatch_env_placeholders,
    "first_write_race": scenario_first_write_race,
    "pre_patch_stuck": scenario_pre_patch_stuck_timeout,
    "degraded_state": scenario_degraded_running_state,
    "history_dedup": scenario_history_dedupes_double_write,
}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--scenario",
        choices=[*list(SCENARIOS), "all"],
        default="all",
    )
    args = parser.parse_args()
    targets = [args.scenario] if args.scenario != "all" else list(SCENARIOS)

    total_failures = 0
    for name in targets:
        total_failures += SCENARIOS[name]()
    print()
    if total_failures == 0:
        print("=== ALL SCENARIOS PASSED ===")
    else:
        print(f"=== {total_failures} FAILURE(S) ===")
    return total_failures


if __name__ == "__main__":
    sys.exit(main())
