#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat >&2 <<'USAGE'
usage: scripts/dev/eq15-core-nt-webxml-example-suite.sh <list|prepare|start-one> [example-slug]

Prepares the 10 benchmark FASTA files for the AKS Web BLAST equivalence runner
and starts parameterized EQ14 strict-oracle jobs one example at a time.

Commands:
  list                 Print the supported example slugs.
  prepare              Create/update the tools ConfigMap consumed by EQ14.
  start-one SLUG       Prepare tools and start one detached AKS runner Job.
Each Job still runs independently and writes evidence under /workspace/evidence.
Collect with: scripts/dev/aks-equivalence-runner.sh job-collect <job-name>
USAGE
}

SCRIPT_DIR=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
PROJECT_ROOT=$(cd -- "$SCRIPT_DIR/../.." && pwd)
RUNNER_NAMESPACE=${RUNNER_NAMESPACE:-elb-equivalence}
TOOLS_CONFIGMAP=${EQ14_TOOLS_CONFIGMAP:-eq14-core-nt-webxml-tools}
SOURCE_QUERY_DIR=${SOURCE_QUERY_DIR:-/home/moonchoi/dev/elastic-blast-azure/benchmark/private/260420_elastic_blast_test_fasta_file_10ea}
EQ14_SCRIPT=${EQ14_SCRIPT:-$PROJECT_ROOT/scripts/dev/eq14-core-nt-webxml-sharded.sh}
RUNNER_SCRIPT=${RUNNER_SCRIPT:-$PROJECT_ROOT/scripts/dev/aks-equivalence-runner.sh}
MAX_TARGET_SEQS=${EQ15_MAX_TARGET_SEQS:-50000}

EXAMPLES=(
  "mpxv-f3l-nc-003310|mpxv-f3l-nc-003310.fa|MPXV_F3L_NC_003310.1.fasta|10244|txid10244[Organism:exp]"
  "mpxv-f3l-nc-063383|mpxv-f3l-nc-063383.fa|MPXV_F3L_NC_063383.1.fasta|10244|txid10244[Organism:exp]"
  "pf-18s-chr1|pf-18s-chr1.fa|PF_18S rRNA_NC_004325.2[473739..475887].fa|5833|txid5833[Organism:exp]"
  "pf-18s-chr5|pf-18s-chr5.fa|PF_18S rRNA_NC_004326.2[1289601..1291692].fa|5833|txid5833[Organism:exp]"
  "pf-18s-chr7|pf-18s-chr7.fa|PF_18S rRNA_NC_004328.3[1083551..1086055].fa|5833|txid5833[Organism:exp]"
  "pf-18s-chr13|pf-18s-chr13.fa|PF_18S rRNA_NC_004331.3[2800004..2802154].fa|5833|txid5833[Organism:exp]"
  "pf-18s-chr11|pf-18s-chr11.fa|PF_18S rRNA_NC_037282.1[1925779..1928358].fa|5833|txid5833[Organism:exp]"
  "sars-cov-2-n|sars-cov-2-n.fa|SARS-CoV-2_N_NC_045512.2.fasta|2697049|txid2697049[Organism:exp]"
  "sars-cov-2-rdrp|sars-cov-2-rdrp.fa|SARS-CoV-2_RdRP_NC_045512.2.fasta|2697049|txid2697049[Organism:exp]"
  "sars-cov-2-orf1ab|sars-cov-2-orf1ab.fa|SARS-CoV-2_orf1ab_NC_045512.2.fasta|2697049|txid2697049[Organism:exp]"
)

need() {
  command -v "$1" >/dev/null 2>&1 || {
    echo "ERROR: required command not found: $1" >&2
    exit 1
  }
}

list_examples() {
  local row slug key file taxids entrez_query
  for row in "${EXAMPLES[@]}"; do
    IFS='|' read -r slug key file taxids entrez_query <<<"$row"
    printf '%-20s %-24s %s taxids=%s entrez=%s\n' "$slug" "$key" "$file" "$taxids" "$entrez_query"
  done
}

example_row() {
  local requested=$1 row slug
  for row in "${EXAMPLES[@]}"; do
    IFS='|' read -r slug _ <<<"$row"
    if [[ "$slug" == "$requested" ]]; then
      printf '%s\n' "$row"
      return 0
    fi
  done
  echo "ERROR: unknown example slug: $requested" >&2
  list_examples >&2
  exit 2
}

prepare_tools_configmap() {
  need kubectl
  local args=()
  args+=(--from-file="compare-blast-web-xml-outfmt6.py=$PROJECT_ROOT/scripts/dev/compare-blast-web-xml-outfmt6.py")
  args+=(--from-file="merge-sharded-results.sh=$PROJECT_ROOT/terminal/merge-sharded-results.sh")

  local row slug key file taxids entrez_query source_path
  for row in "${EXAMPLES[@]}"; do
    IFS='|' read -r slug key file taxids entrez_query <<<"$row"
    source_path="$SOURCE_QUERY_DIR/$file"
    if [[ ! -f "$source_path" ]]; then
      echo "ERROR: source FASTA not found: $source_path" >&2
      exit 1
    fi
    args+=(--from-file="$key=$source_path")
  done

  kubectl -n "$RUNNER_NAMESPACE" create configmap "$TOOLS_CONFIGMAP" \
    "${args[@]}" \
    --dry-run=client -o yaml | kubectl apply -f -
  printf 'Prepared ConfigMap %s/%s with %s benchmark FASTA files.\n' \
    "$RUNNER_NAMESPACE" "$TOOLS_CONFIGMAP" "${#EXAMPLES[@]}"
}

start_one() {
  local requested=$1 row slug key file taxids entrez_query job_slug
  row=$(example_row "$requested")
  IFS='|' read -r slug key file taxids entrez_query <<<"$row"
  prepare_tools_configmap
  job_slug=$(date -u +%Y%m%d%H%M%S)
  printf 'Starting strict Web XML oracle job for %s (%s).\n' "$slug" "$file"
  RUNNER_JOB_NAME="eq15-${slug}-${job_slug}" \
    EQ14_TOOLS_CONFIGMAP="$TOOLS_CONFIGMAP" \
    EQ14_DB_NAME="core_nt" \
    EQ14_WEB_DATABASE="core_nt" \
    "$RUNNER_SCRIPT" job-file "$EQ14_SCRIPT" -- "$key" "$taxids" "$entrez_query" "$MAX_TARGET_SEQS"
}

main() {
  local command=${1:-}
  case "$command" in
    list)
      list_examples
      ;;
    prepare)
      prepare_tools_configmap
      ;;
    start-one)
      if [[ $# -ne 2 ]]; then
        usage
        exit 2
      fi
      start_one "$2"
      ;;
    -h|--help|help|"")
      usage
      ;;
    *)
      echo "ERROR: unknown command: $command" >&2
      usage
      exit 2
      ;;
  esac
}

main "$@"
