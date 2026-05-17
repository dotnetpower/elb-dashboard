#!/usr/bin/env bash
# Guarded Azure VM runbook for one-off full-DB core_nt searchsp calibration.
#
# The default action is plan, which has no Azure side effects. The create and
# delete actions require explicit confirmations. VM-side commands are printed by
# vm-runbook so disk formatting and BLAST execution stay visible to the operator.

set -Eeuo pipefail

ACTION="${1:-plan}"
if [[ $# -gt 0 ]]; then
  shift
fi

DATE_TAG="$(date +%Y%m%d)"
RG="rg-elb-core-nt-searchsp-${DATE_TAG}"
VM_NAME="vm-elb-core-nt-searchsp"
LOCATION="eastus"
VM_SIZE="Standard_E96s_v5"
DATA_DISK_GB="1024"
ADMIN_USER="azureuser"
SSH_KEY="${HOME}/.ssh/id_rsa.pub"
CALLER_IP=""
SUBSCRIPTION=""
CONFIRM_RESOURCE_GROUP=""

red() { printf '\033[31m%s\033[0m\n' "$*"; }
green() { printf '\033[32m%s\033[0m\n' "$*"; }
yellow() { printf '\033[33m%s\033[0m\n' "$*"; }
die() { red "ERROR: $*" >&2; exit 1; }

usage() {
  cat <<'EOF'
Usage:
  scripts/dev/core-nt-searchsp-calibration.sh plan [options]
  scripts/dev/core-nt-searchsp-calibration.sh create [options]
  scripts/dev/core-nt-searchsp-calibration.sh vm-runbook [options]
  scripts/dev/core-nt-searchsp-calibration.sh remote-calibrate [options]
  scripts/dev/core-nt-searchsp-calibration.sh fetch-results [options]
  scripts/dev/core-nt-searchsp-calibration.sh status [options]
  scripts/dev/core-nt-searchsp-calibration.sh delete [options] --confirm-resource-group RG

Options:
  --subscription ID         Azure subscription id. Defaults to current az account.
  --rg NAME                 Temporary resource group name.
  --vm-name NAME            VM name.
  --location NAME           Azure region. Default: eastus.
  --vm-size SKU             VM SKU. Default: Standard_E96s_v5.
  --data-disk-gb GB         Data disk size. Default: 1024.
  --admin-user NAME         Admin user. Default: azureuser.
  --ssh-key PATH_OR_VALUE   Public SSH key path or literal public key.
  --caller-ip IP            IPv4 allowed to SSH. Defaults to api.ipify.org.
  --confirm-resource-group RG
                            Required for delete; must exactly match --rg.

Safety gates:
  create requires: ELB_CORE_NT_CREATE_APPROVED=1
  remote-calibrate requires: ELB_CORE_NT_REMOTE_APPROVED=1
  delete requires: ELB_CORE_NT_DELETE=delete-<resource-group>

Runtime environment:
  RUN_SEARCHSP1=auto|1|0  Default: auto. Run the -searchsp 1 fallback only when
                         XML Statistics_eff-space is missing/zero, unless set to 1.
  CORE_NT_DOWNLOAD_JOBS=N Default: 6. Number of core_nt tarballs downloaded in parallel.
  CORE_NT_SPLIT_CONN=N    Reserved for downloader implementations. Default: 4.
  RESULT_DIR=path        Local directory for fetch-results. Default: docs/temp/core-nt-searchsp.

No action in this script runs automatically from CI or the dashboard.
EOF
  exit "${1:-1}"
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --subscription) SUBSCRIPTION="$2"; shift 2 ;;
    --rg) RG="$2"; shift 2 ;;
    --vm-name) VM_NAME="$2"; shift 2 ;;
    --location) LOCATION="$2"; shift 2 ;;
    --vm-size) VM_SIZE="$2"; shift 2 ;;
    --data-disk-gb) DATA_DISK_GB="$2"; shift 2 ;;
    --admin-user) ADMIN_USER="$2"; shift 2 ;;
    --ssh-key) SSH_KEY="$2"; shift 2 ;;
    --caller-ip) CALLER_IP="$2"; shift 2 ;;
    --confirm-resource-group) CONFIRM_RESOURCE_GROUP="$2"; shift 2 ;;
    -h|--help|help) usage 0 ;;
    *) die "unknown flag: $1" ;;
  esac
done

case "$ACTION" in
  plan|create|vm-runbook|remote-calibrate|fetch-results|status|delete|-h|--help|help) ;;
  *) usage 1 ;;
esac
[[ "$ACTION" == "-h" || "$ACTION" == "--help" || "$ACTION" == "help" ]] && usage 0

require_az() {
  command -v az >/dev/null 2>&1 || die "az CLI not found"
}

resolve_subscription() {
  if [[ -z "$SUBSCRIPTION" ]]; then
    SUBSCRIPTION="$(az account show --query id -o tsv 2>/dev/null || true)"
    [[ -n "$SUBSCRIPTION" ]] || die "no subscription set; run 'az login' or pass --subscription"
  fi
}

resolve_caller_ip() {
  if [[ -z "$CALLER_IP" ]]; then
    CALLER_IP="$(curl -fsS --max-time 5 https://api.ipify.org 2>/dev/null || true)"
    [[ -n "$CALLER_IP" ]] || die "could not auto-detect caller IP; pass --caller-ip"
  fi
  [[ "$CALLER_IP" =~ ^[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "caller IP must be a bare IPv4 address"
}

tag_args() {
  printf '%s\n' \
    app=elb-dashboard \
    environment=dev \
    managedBy=manual \
    purpose=core-nt-searchsp-calibration \
    safeDelete=resource-group \
    createdOn="$DATE_TAG"
}

print_plan() {
  cat <<EOF
core_nt full-DB searchsp calibration plan

Azure resources:
  subscription:  ${SUBSCRIPTION:-current az account}
  resourceGroup: $RG
  location:      $LOCATION
  vmName:        $VM_NAME
  vmSize:        $VM_SIZE
  dataDiskGb:    $DATA_DISK_GB
  adminUser:     $ADMIN_USER
  sshKey:        $SSH_KEY
  sshSourceIp:   ${CALLER_IP:-auto-detect during create}

Expected flow:
  1. Create a dedicated temporary resource group.
  2. Create VNet, subnet, NSG, public IP, NIC, and one large Ubuntu VM.
  3. Restrict SSH to the caller IP before the VM is created.
  4. Print VM-side commands for mounting the data disk and installing BLAST+ 2.17.0.
  5. Prepare core_nt on the VM-local disk.
  6. Run full DB BLAST+ with the external API default query/options template.
  7. Save XML, parsed statistics, command lines, BLAST version, DB metadata, and VM SKU.
  8. Delete the temporary resource group immediately after results are copied out.

Create command, after human approval:
  ELB_CORE_NT_CREATE_APPROVED=1 $0 create --rg $RG --location $LOCATION --vm-size $VM_SIZE

VM command runbook:
  $0 vm-runbook --rg $RG --vm-name $VM_NAME

Remote calibration, after the VM is reachable:
  ELB_CORE_NT_REMOTE_APPROVED=1 $0 remote-calibrate --rg $RG --vm-name $VM_NAME

Fetch results:
  $0 fetch-results --rg $RG --vm-name $VM_NAME

Delete command, after results are copied out:
  ELB_CORE_NT_DELETE=delete-$RG $0 delete --rg $RG --confirm-resource-group $RG
EOF
}

create_resources() {
  [[ "${ELB_CORE_NT_CREATE_APPROVED:-}" == "1" ]] || die "set ELB_CORE_NT_CREATE_APPROVED=1 after user approval"
  require_az
  resolve_subscription
  resolve_caller_ip

  local sub_flag=(--subscription "$SUBSCRIPTION")
  local tags=()
  while IFS= read -r tag; do
    tags+=("$tag")
  done < <(tag_args)

  local vnet_name="${VM_NAME}-vnet"
  local subnet_name="calibration"
  local nsg_name="${VM_NAME}-nsg"
  local pip_name="${VM_NAME}-pip"
  local nic_name="${VM_NAME}-nic"

  yellow "Creating temporary resource group '$RG' in $LOCATION ..."
  az group create "${sub_flag[@]}" --name "$RG" --location "$LOCATION" --tags "${tags[@]}" -o none

  yellow "Creating network with SSH restricted to $CALLER_IP ..."
  az network vnet create "${sub_flag[@]}" --resource-group "$RG" --location "$LOCATION" \
    --name "$vnet_name" --address-prefixes 10.71.0.0/16 \
    --subnet-name "$subnet_name" --subnet-prefixes 10.71.0.0/24 \
    --tags "${tags[@]}" -o none
  az network nsg create "${sub_flag[@]}" --resource-group "$RG" --location "$LOCATION" \
    --name "$nsg_name" --tags "${tags[@]}" -o none
  az network nsg rule create "${sub_flag[@]}" --resource-group "$RG" --nsg-name "$nsg_name" \
    --name AllowSshFromCaller --priority 1000 --direction Inbound --access Allow \
    --protocol Tcp --source-address-prefixes "$CALLER_IP" --destination-port-ranges 22 -o none
  az network public-ip create "${sub_flag[@]}" --resource-group "$RG" --location "$LOCATION" \
    --name "$pip_name" --sku Standard --allocation-method Static --tags "${tags[@]}" -o none
  az network nic create "${sub_flag[@]}" --resource-group "$RG" --location "$LOCATION" \
    --name "$nic_name" --vnet-name "$vnet_name" --subnet "$subnet_name" \
    --network-security-group "$nsg_name" --public-ip-address "$pip_name" \
    --tags "${tags[@]}" -o none

  yellow "Creating VM '$VM_NAME' ($VM_SIZE) with a ${DATA_DISK_GB} GiB Premium_LRS data disk ..."
  az vm create "${sub_flag[@]}" \
    --resource-group "$RG" \
    --name "$VM_NAME" \
    --location "$LOCATION" \
    --nics "$nic_name" \
    --image Ubuntu2204 \
    --size "$VM_SIZE" \
    --admin-username "$ADMIN_USER" \
    --ssh-key-values "$SSH_KEY" \
    --data-disk-sizes-gb "$DATA_DISK_GB" \
    --storage-sku Premium_LRS \
    --os-disk-size-gb 128 \
    --tags "${tags[@]}" \
    -o none

  local public_ip
  public_ip="$(az vm show "${sub_flag[@]}" --resource-group "$RG" --name "$VM_NAME" -d --query publicIps -o tsv)"
  green "Created temporary calibration VM."
  cat <<EOF

SSH:
  ssh $ADMIN_USER@$public_ip

Next:
  $0 vm-runbook --rg $RG --vm-name $VM_NAME

Cleanup after copying results out:
  ELB_CORE_NT_DELETE=delete-$RG $0 delete --rg $RG --confirm-resource-group $RG
EOF
}

print_vm_runbook() {
  print_vm_script
}

print_vm_script() {
  cat <<'EOF'
# Run inside the temporary calibration VM.
# It formats only the attached throwaway data disk and refuses to continue unless
# FORMAT_CORE_NT_DATA_DISK=1 is set in the environment.

set -Eeuo pipefail

export DATA_DEVICE="${DATA_DEVICE:-/dev/disk/azure/scsi1/lun0}"
export WORK_DIR="${WORK_DIR:-/mnt/elb-calibration}"
export BLAST_VERSION="${BLAST_VERSION:-2.17.0}"
export BLAST_HOME="/opt/ncbi-blast-${BLAST_VERSION}+"
export PATH="$BLAST_HOME/bin:$PATH"
export RUN_SEARCHSP1="${RUN_SEARCHSP1:-auto}"
export BLASTDB="$WORK_DIR/blastdb"
export CORE_NT_DOWNLOAD_JOBS="${CORE_NT_DOWNLOAD_JOBS:-6}"
export CORE_NT_SPLIT_CONN="${CORE_NT_SPLIT_CONN:-4}"

echo "== disk layout =="
lsblk -o NAME,SIZE,TYPE,MOUNTPOINT

sudo apt-get update
sudo DEBIAN_FRONTEND=noninteractive apt-get install -y \
  bzip2 ca-certificates curl file gzip jq libgomp1 libwww-perl parted perl procps \
  python3 rsync tar time tmux unzip util-linux xfsprogs
sudo file -s "$DATA_DEVICE"

if ! mountpoint -q "$WORK_DIR"; then
  if [[ "${FORMAT_CORE_NT_DATA_DISK:-0}" != "1" ]]; then
    echo "ERROR: set FORMAT_CORE_NT_DATA_DISK=1 after confirming DATA_DEVICE is the throwaway data disk" >&2
    exit 2
  fi
  sudo mkfs.xfs -f "$DATA_DEVICE"
  sudo mkdir -p "$WORK_DIR"
  sudo mount "$DATA_DEVICE" "$WORK_DIR"
  disk_uuid="$(sudo blkid -s UUID -o value "$DATA_DEVICE")"
  if ! grep -q "$WORK_DIR" /etc/fstab; then
    echo "UUID=$disk_uuid $WORK_DIR xfs defaults,nofail 0 2" | sudo tee -a /etc/fstab >/dev/null
  fi
fi
sudo chown "$USER:$USER" "$WORK_DIR"
mkdir -p "$WORK_DIR/blastdb" "$WORK_DIR/queries" "$WORK_DIR/results" "$WORK_DIR/metadata"

if [[ ! -x "$BLAST_HOME/bin/blastn" ]]; then
  curl -fsSL "https://ftp.ncbi.nlm.nih.gov/blast/executables/blast+/${BLAST_VERSION}/ncbi-blast-${BLAST_VERSION}+-x64-linux.tar.gz" \
    -o /tmp/ncbi-blast.tgz
  sudo tar -C /opt -xzf /tmp/ncbi-blast.tgz
fi
blastn -version | tee "$WORK_DIR/metadata/blastn-version.txt"

if ! command -v azcopy >/dev/null 2>&1; then
  curl -fsSL https://aka.ms/downloadazcopy-v10-linux -o /tmp/azcopy.tgz
  tar -C /tmp -xzf /tmp/azcopy.tgz
  sudo install -m 0755 "$(find /tmp -path '*/azcopy' -type f | head -1)" /usr/local/bin/azcopy
fi
azcopy --version | tee "$WORK_DIR/metadata/azcopy-version.txt"

cat > "$WORK_DIR/queries/query.fa" <<'FASTA'
>calibration_query_64nt
ACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGTACGT
FASTA
sha256sum "$WORK_DIR/queries/query.fa" | tee "$WORK_DIR/metadata/query.sha256"

cd "$WORK_DIR/blastdb"
curl -fsSL https://ftp.ncbi.nlm.nih.gov/blast/db/ \
  | grep -o 'core_nt[^"<]*\.tar\.gz' \
  | sort -u \
  | sed 's#^#https://ftp.ncbi.nlm.nih.gov/blast/db/#' \
  > "$WORK_DIR/metadata/core_nt-download-urls.txt"

download_one() {
  local url="$1"
  local file="${url##*/}"
  local path="$WORK_DIR/blastdb/$file"
  if [[ -s "$path" ]] && tar -tzf "$path" >/dev/null 2>&1; then
    printf '[%s] skip  %s already complete\n' "$(date -u +%H:%M:%S)" "$file"
    return 0
  fi
  printf '[%s] start %s\n' "$(date -u +%H:%M:%S)" "$file"
  curl \
    --fail \
    --silent \
    --show-error \
    --location \
    --continue-at - \
    --retry 30 \
    --retry-all-errors \
    --retry-delay 20 \
    --connect-timeout 30 \
    --speed-time 120 \
    --speed-limit 1024 \
    --output "$path" \
    "$url"
  tar -tzf "$path" >/dev/null
  printf '[%s] done  %s\n' "$(date -u +%H:%M:%S)" "$file"
}
export -f download_one
export WORK_DIR

xargs -n 1 -P "$CORE_NT_DOWNLOAD_JOBS" bash -c 'download_one "$@"' _ \
  < "$WORK_DIR/metadata/core_nt-download-urls.txt" \
  2>&1 | tee "$WORK_DIR/results/core_nt-download.log"

find "$WORK_DIR/blastdb" -maxdepth 1 -name 'core_nt*.tar.gz' -print0 \
  | xargs -0 -n1 -P "$(nproc)" tar -xzf
blastdbcmd -db core_nt -dbtype nucl -info | tee "$WORK_DIR/metadata/blastdbcmd-core_nt-info.txt"

# External API defaults: program=blastn, outfmt=5, word_size=28, dust=true,
# evalue=10, max_target_seqs=500. blastn's default task is megablast.
export BLAST_OPTS='-word_size 28 -dust yes -evalue 10 -max_target_seqs 500 -outfmt 5'
export BLAST_THREADS=$(nproc)
printf '%s\n' "$BLAST_OPTS" | tee "$WORK_DIR/metadata/blast-options.txt"
printf '%s\n' "$BLAST_THREADS" | tee "$WORK_DIR/metadata/blast-threads.txt"

cd "$WORK_DIR"
/usr/bin/time -v blastn \
  -query "$WORK_DIR/queries/query.fa" \
  -db "$WORK_DIR/blastdb/core_nt" \
  $BLAST_OPTS \
  -num_threads "$BLAST_THREADS" \
  -out "$WORK_DIR/results/core_nt.full.default.xml" \
  2> "$WORK_DIR/results/core_nt.full.default.time.txt"

python3 - <<'PY' > "$WORK_DIR/results/core_nt-default-stats.json"
import json
import xml.etree.ElementTree as ET
from pathlib import Path

work = Path('/mnt/elb-calibration')
default_xml = work / 'results/core_nt.full.default.xml'
root = ET.parse(default_xml).getroot()
rows = []
for index, iteration in enumerate(root.findall('.//Iteration'), start=1):
    node = iteration.find('./Iteration_stat/Statistics')
    rows.append({
        'iteration': index,
        'blast_version': root.findtext('BlastOutput_version'),
        'db': root.findtext('BlastOutput_db'),
        'query_id': iteration.findtext('Iteration_query-ID'),
        'query_def': iteration.findtext('Iteration_query-def'),
        'query_len': iteration.findtext('Iteration_query-len'),
        'eff_space': node.findtext('Statistics_eff-space') if node is not None else None,
        'db_num': node.findtext('Statistics_db-num') if node is not None else None,
        'db_len': node.findtext('Statistics_db-len') if node is not None else None,
        'hsp_len': node.findtext('Statistics_hsp-len') if node is not None else None,
    })
print(json.dumps(rows, indent=2, sort_keys=True))
PY

should_run_searchsp1=0
if [[ "$RUN_SEARCHSP1" == "1" ]]; then
  should_run_searchsp1=1
elif [[ "$RUN_SEARCHSP1" == "auto" ]]; then
  if ! python3 - <<'PY'
import json
from pathlib import Path
rows = json.loads(Path('/mnt/elb-calibration/results/core_nt-default-stats.json').read_text())
ok = bool(rows) and all(row.get('eff_space') not in (None, '', '0') for row in rows)
raise SystemExit(0 if ok else 1)
PY
  then
    should_run_searchsp1=1
  fi
fi

if [[ "$should_run_searchsp1" == "1" ]]; then
  /usr/bin/time -v blastn \
    -query "$WORK_DIR/queries/query.fa" \
    -db "$WORK_DIR/blastdb/core_nt" \
    $BLAST_OPTS \
    -searchsp 1 \
    -num_threads "$BLAST_THREADS" \
    -out "$WORK_DIR/results/core_nt.full.searchsp1.xml" \
    2> "$WORK_DIR/results/core_nt.full.searchsp1.time.txt"
fi

python3 - <<'PY' > "$WORK_DIR/results/core_nt-searchsp-summary.json"
import json
import subprocess
import xml.etree.ElementTree as ET
from pathlib import Path

work = Path('/mnt/elb-calibration')
default_xml = work / 'results/core_nt.full.default.xml'
searchsp1_xml = work / 'results/core_nt.full.searchsp1.xml'

def first_hsps(path: Path, limit: int = 20) -> list[dict[str, str | None]]:
    root = ET.parse(path).getroot()
    rows = []
    for hit in root.findall('.//Hit')[:limit]:
        rows.append({
            'hit_id': hit.findtext('Hit_id'),
            'hit_def': hit.findtext('Hit_def'),
            'evalue': hit.findtext('./Hit_hsps/Hsp/Hsp_evalue'),
            'bit_score': hit.findtext('./Hit_hsps/Hsp/Hsp_bit-score'),
        })
    return rows

default_stats = json.loads((work / 'results/core_nt-default-stats.json').read_text())
summary = {
    'default_stats': default_stats,
    'default_first_hsps': first_hsps(default_xml),
    'blastn_version': subprocess.check_output(['blastn', '-version'], text=True).strip(),
    'blast_options': (work / 'metadata/blast-options.txt').read_text().strip(),
    'query_sha256': (work / 'metadata/query.sha256').read_text().strip(),
}
if searchsp1_xml.exists():
    summary['searchsp1_first_hsps'] = first_hsps(searchsp1_xml)
print(json.dumps(summary, indent=2, sort_keys=True))
PY

tar -C "$WORK_DIR" -czf "$WORK_DIR/core_nt-searchsp-calibration-results.tgz" \
  metadata queries results

echo "Result archive: $WORK_DIR/core_nt-searchsp-calibration-results.tgz"
echo "Use the reported default_stats[].eff_space as the candidate full-DB searchsp."
echo "Pass that same -searchsp value to every shard run for the matching query/options."
EOF
}

vm_public_ip() {
  az vm show --subscription "$SUBSCRIPTION" --resource-group "$RG" --name "$VM_NAME" -d --query publicIps -o tsv
}

remote_calibrate() {
  [[ "${ELB_CORE_NT_REMOTE_APPROVED:-}" == "1" ]] || die "set ELB_CORE_NT_REMOTE_APPROVED=1 after confirming the VM should format its throwaway data disk"
  require_az
  resolve_subscription
  command -v ssh >/dev/null 2>&1 || die "ssh client not found"
  local ip run_searchsp1 download_jobs split_conn
  ip="$(vm_public_ip)"
  [[ -n "$ip" ]] || die "could not resolve VM public IP"
  run_searchsp1="${RUN_SEARCHSP1:-auto}"
  download_jobs="${CORE_NT_DOWNLOAD_JOBS:-6}"
  split_conn="${CORE_NT_SPLIT_CONN:-4}"
  [[ "$run_searchsp1" =~ ^(auto|0|1)$ ]] || die "RUN_SEARCHSP1 must be auto, 0, or 1"
  [[ "$download_jobs" =~ ^[0-9]+$ && "$download_jobs" -ge 1 && "$download_jobs" -le 64 ]] || die "CORE_NT_DOWNLOAD_JOBS must be 1..64"
  [[ "$split_conn" =~ ^[0-9]+$ && "$split_conn" -ge 1 && "$split_conn" -le 16 ]] || die "CORE_NT_SPLIT_CONN must be 1..16"
  print_vm_script | ssh -o ServerAliveInterval=60 -o ServerAliveCountMax=10 -o StrictHostKeyChecking=accept-new \
    "$ADMIN_USER@$ip" "FORMAT_CORE_NT_DATA_DISK=1 RUN_SEARCHSP1=$run_searchsp1 CORE_NT_DOWNLOAD_JOBS=$download_jobs CORE_NT_SPLIT_CONN=$split_conn bash -s"
}

fetch_results() {
  require_az
  resolve_subscription
  command -v scp >/dev/null 2>&1 || die "scp client not found"
  local ip result_dir
  ip="$(vm_public_ip)"
  [[ -n "$ip" ]] || die "could not resolve VM public IP"
  result_dir="${RESULT_DIR:-docs/temp/core-nt-searchsp}"
  mkdir -p "$result_dir"
  scp -o StrictHostKeyChecking=accept-new \
    "$ADMIN_USER@$ip:/mnt/elb-calibration/core_nt-searchsp-calibration-results.tgz" \
    "$result_dir/"
  printf 'Fetched result archive: %s/core_nt-searchsp-calibration-results.tgz\n' "$result_dir"
}

show_status() {
  require_az
  resolve_subscription
  az resource list --subscription "$SUBSCRIPTION" --resource-group "$RG" -o table
}

delete_resources() {
  [[ "$CONFIRM_RESOURCE_GROUP" == "$RG" ]] || die "pass --confirm-resource-group $RG"
  [[ "${ELB_CORE_NT_DELETE:-}" == "delete-$RG" ]] || die "set ELB_CORE_NT_DELETE=delete-$RG"
  require_az
  resolve_subscription

  yellow "Resources that will be deleted with resource group '$RG':"
  az resource list --subscription "$SUBSCRIPTION" --resource-group "$RG" -o table
  yellow "Deleting resource group '$RG' asynchronously ..."
  az group delete --subscription "$SUBSCRIPTION" --name "$RG" --yes --no-wait
  green "Delete submitted. Recheck with: az group exists --name $RG"
}

case "$ACTION" in
  plan)
    print_plan
    ;;
  create)
    create_resources
    ;;
  vm-runbook)
    print_vm_runbook
    ;;
  remote-calibrate)
    remote_calibrate
    ;;
  fetch-results)
    fetch_results
    ;;
  status)
    show_status
    ;;
  delete)
    delete_resources
    ;;
esac