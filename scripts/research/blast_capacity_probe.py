"""BLAST capacity probe — single-query memory/CPU footprint measurement.

Responsibility: Sample blastpool node + per-pod metrics from a running AKS cluster
during a BLAST job and persist a CSV + JSON summary so the admission-control slot
manager can be sized safely.
Edit boundaries: Research helper. No production code path imports this. Does not
mutate cluster state — read-only metrics polling. Issue the BLAST submit
separately (dashboard, elastic-blast CLI, or sibling OpenAPI service).
Key entry points: ``main``.
Risky contracts: Requires ``DefaultAzureCredential`` to resolve a token with at
least ``Azure Kubernetes Service Cluster User Role`` on the target cluster. Uses
``api.services.k8s.metrics`` so production paths are exercised end-to-end.
Validation: ``uv run python scripts/research/blast_capacity_probe.py --help``.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import signal
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.services.k8s.metrics import k8s_top_nodes, k8s_top_pods  # noqa: E402
from azure.identity import DefaultAzureCredential  # noqa: E402


@dataclass
class PodSample:
    ts: float
    name: str
    namespace: str
    cpu_m: int
    mem_mi: int


@dataclass
class NodeSample:
    ts: float
    name: str
    pool: str
    cpu_m: int
    cpu_pct: int
    mem_mi: int
    mem_pct: int


@dataclass
class ProbeState:
    pod_samples: list[PodSample] = field(default_factory=list)
    node_samples: list[NodeSample] = field(default_factory=list)
    pod_first_seen_ts: dict[str, float] = field(default_factory=dict)
    pod_last_seen_ts: dict[str, float] = field(default_factory=dict)
    stop: bool = False


def _matches(name: str, ns: str, name_match: str | None, ns_filter: str | None) -> bool:
    if ns_filter and ns != ns_filter:
        return False
    if name_match and name_match.lower() not in name.lower():
        return False
    return True


def _print_tick(
    elapsed: float,
    blast_pods: list[dict[str, Any]],
    blast_nodes: list[dict[str, Any]],
) -> None:
    if blast_pods:
        peak_pod = max(blast_pods, key=lambda p: p["mem_mi"])
        sum_cpu_m = sum(p["cpu_m"] for p in blast_pods)
        sum_mem_mi = sum(p["mem_mi"] for p in blast_pods)
        pod_line = (
            f"pods={len(blast_pods)} cpu_sum={sum_cpu_m}m mem_sum={sum_mem_mi}Mi "
            f"peak={peak_pod['name']} ({peak_pod['cpu_m']}m, {peak_pod['mem_mi']}Mi)"
        )
    else:
        pod_line = "pods=0"
    node_line_parts: list[str] = []
    for n in blast_nodes:
        node_line_parts.append(
            f"{n['name'][-6:]}={n['cpu_pct']}%cpu/{n['memory_pct']}%mem"
        )
    node_line = " ".join(node_line_parts) if node_line_parts else "no-nodes"
    sys.stdout.write(f"[{elapsed:6.1f}s] {pod_line} | {node_line}\n")
    sys.stdout.flush()


def _summarise(state: ProbeState) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "pod_count": len({s.name for s in state.pod_samples}),
        "sample_count": len(state.pod_samples),
    }
    per_pod: dict[str, dict[str, Any]] = {}
    for sample in state.pod_samples:
        bucket = per_pod.setdefault(
            sample.name,
            {
                "namespace": sample.namespace,
                "mem_mi": [],
                "cpu_m": [],
                "first_ts": sample.ts,
                "last_ts": sample.ts,
            },
        )
        bucket["mem_mi"].append(sample.mem_mi)
        bucket["cpu_m"].append(sample.cpu_m)
        bucket["last_ts"] = max(bucket["last_ts"], sample.ts)
        bucket["first_ts"] = min(bucket["first_ts"], sample.ts)

    pod_breakdown: dict[str, Any] = {}
    peak_mem_mi = 0
    peak_pod_name = ""
    p95_pool: list[int] = []
    for name, bucket in per_pod.items():
        mems = bucket["mem_mi"]
        cpus = bucket["cpu_m"]
        if not mems:
            continue
        pmax = max(mems)
        pavg = int(statistics.mean(mems))
        pmedian = int(statistics.median(mems))
        cmax = max(cpus)
        cavg = int(statistics.mean(cpus))
        wallclock = bucket["last_ts"] - bucket["first_ts"]
        pod_breakdown[name] = {
            "namespace": bucket["namespace"],
            "wallclock_s": round(wallclock, 1),
            "samples": len(mems),
            "mem_mi_peak": pmax,
            "mem_mi_avg": pavg,
            "mem_mi_median": pmedian,
            "cpu_m_peak": cmax,
            "cpu_m_avg": cavg,
        }
        p95_pool.append(pmax)
        if pmax > peak_mem_mi:
            peak_mem_mi = pmax
            peak_pod_name = name

    summary["pods"] = pod_breakdown
    summary["peak"] = {
        "pod": peak_pod_name,
        "mem_mi": peak_mem_mi,
        "mem_gib": round(peak_mem_mi / 1024, 2),
    }
    if p95_pool:
        p95_pool.sort()
        summary["peak_p95_mem_mi"] = p95_pool[int(0.95 * (len(p95_pool) - 1))]
    return summary


def _write_csvs(state: ProbeState, out_dir: Path) -> None:
    pods_csv = out_dir / "pods.csv"
    nodes_csv = out_dir / "nodes.csv"
    with pods_csv.open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["ts", "namespace", "name", "cpu_m", "mem_mi"])
        for s in state.pod_samples:
            w.writerow([s.ts, s.namespace, s.name, s.cpu_m, s.mem_mi])
    with nodes_csv.open("w", newline="") as fp:
        w = csv.writer(fp)
        w.writerow(["ts", "name", "pool", "cpu_m", "cpu_pct", "mem_mi", "mem_pct"])
        for n in state.node_samples:
            w.writerow([n.ts, n.name, n.pool, n.cpu_m, n.cpu_pct, n.mem_mi, n.mem_pct])


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--subscription",
        default=os.environ.get("AZURE_SUBSCRIPTION_ID", ""),
        help="Azure subscription ID (defaults to $AZURE_SUBSCRIPTION_ID).",
    )
    parser.add_argument(
        "--resource-group",
        default="rg-elb-dashboard",
        help="AKS resource group (default: rg-elb-dashboard).",
    )
    parser.add_argument(
        "--cluster",
        default="aks-elb-e2e-core-nt",
        help="AKS cluster name (default: aks-elb-e2e-core-nt).",
    )
    parser.add_argument(
        "--namespace",
        default=None,
        help="Limit pod metrics to a single namespace. Default: all namespaces.",
    )
    parser.add_argument(
        "--match",
        default="blast",
        help="Case-insensitive substring filter against pod name (default: 'blast').",
    )
    parser.add_argument(
        "--node-pool",
        default="blastpool",
        help="Node pool to surface in the per-tick output (default: blastpool).",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=5.0,
        help="Polling interval in seconds (default: 5).",
    )
    parser.add_argument(
        "--duration",
        type=float,
        default=900.0,
        help="Maximum probe duration in seconds (default: 900 = 15 min).",
    )
    parser.add_argument(
        "--idle-stop",
        type=float,
        default=60.0,
        help=(
            "Stop early when no matching pods have been seen for this many seconds "
            "after at least one was observed. 0 disables idle stop. Default: 60."
        ),
    )
    parser.add_argument(
        "--out-dir",
        default=None,
        help="Directory for CSV/JSON output. Default: .logs/research/blast-probe-<ts>/",
    )
    args = parser.parse_args()

    if not args.subscription:
        sys.stderr.write(
            "AZURE_SUBSCRIPTION_ID is required (env var or --subscription).\n"
        )
        return 2

    out_dir = (
        Path(args.out_dir)
        if args.out_dir
        else REPO_ROOT
        / ".logs"
        / "research"
        / f"blast-probe-{time.strftime('%Y%m%d-%H%M%S')}"
    )
    out_dir.mkdir(parents=True, exist_ok=True)

    cred = DefaultAzureCredential()
    state = ProbeState()

    def _handle(_sig: int, _frame: Any) -> None:
        state.stop = True

    signal.signal(signal.SIGINT, _handle)
    signal.signal(signal.SIGTERM, _handle)

    started = time.monotonic()
    last_seen_pod_ts: float | None = None
    sys.stdout.write(
        f"# blast-capacity-probe cluster={args.cluster} ns={args.namespace or 'ALL'} "
        f"match='{args.match}' pool={args.node_pool} interval={args.interval}s "
        f"duration<={args.duration}s out={out_dir}\n"
    )
    sys.stdout.flush()

    while not state.stop:
        now = time.monotonic()
        elapsed = now - started
        if elapsed > args.duration:
            sys.stdout.write("# duration cap reached\n")
            break
        try:
            nodes = k8s_top_nodes(cred, args.subscription, args.resource_group, args.cluster)
        except Exception as exc:
            sys.stdout.write(f"[{elapsed:6.1f}s] node poll error: {exc}\n")
            nodes = []
        try:
            pods = k8s_top_pods(
                cred,
                args.subscription,
                args.resource_group,
                args.cluster,
                namespace=args.namespace,
            )
        except Exception as exc:
            sys.stdout.write(f"[{elapsed:6.1f}s] pod poll error: {exc}\n")
            pods = []

        ts = time.time()
        blast_nodes = [n for n in nodes if n.get("pool") == args.node_pool]
        for n in blast_nodes:
            state.node_samples.append(
                NodeSample(
                    ts=ts,
                    name=n["name"],
                    pool=n.get("pool", ""),
                    cpu_m=int(n.get("cpu_m", 0)),
                    cpu_pct=int(n.get("cpu_pct", 0)),
                    mem_mi=int(n.get("mem_ki", 0)) // 1024,
                    mem_pct=int(n.get("memory_pct", 0)),
                )
            )

        blast_pods = [
            p for p in pods if _matches(p["name"], p["namespace"], args.match, args.namespace)
        ]
        for p in blast_pods:
            state.pod_samples.append(
                PodSample(
                    ts=ts,
                    name=p["name"],
                    namespace=p["namespace"],
                    cpu_m=int(p["cpu_m"]),
                    mem_mi=int(p["mem_mi"]),
                )
            )
            state.pod_first_seen_ts.setdefault(p["name"], now)
            state.pod_last_seen_ts[p["name"]] = now

        if blast_pods:
            last_seen_pod_ts = now
        _print_tick(elapsed, blast_pods, blast_nodes)

        if (
            args.idle_stop > 0
            and last_seen_pod_ts is not None
            and (now - last_seen_pod_ts) > args.idle_stop
        ):
            sys.stdout.write(
                f"# no matching pods for {args.idle_stop:.0f}s after last sighting — stopping\n"
            )
            break

        time.sleep(args.interval)

    _write_csvs(state, out_dir)
    summary = _summarise(state)
    (out_dir / "summary.json").write_text(json.dumps(summary, indent=2))
    sys.stdout.write("\n# summary\n")
    sys.stdout.write(json.dumps(summary, indent=2) + "\n")
    sys.stdout.write(f"# csv: {out_dir / 'pods.csv'}\n")
    sys.stdout.write(f"# csv: {out_dir / 'nodes.csv'}\n")
    sys.stdout.write(f"# json: {out_dir / 'summary.json'}\n")
    sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
