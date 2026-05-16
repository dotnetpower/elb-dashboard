#!/bin/bash
set -u

check_tool() {
  local label="$1"
  local command_name="$2"
  shift 2

  if ! command -v "$command_name" >/dev/null 2>&1; then
    printf '%-14s missing\n' "$label:"
    return 0
  fi

  local output
  if output=$("$command_name" "$@" 2>&1 | head -n 1); then
    printf '%-14s %s\n' "$label:" "$output"
  else
    printf '%-14s installed (%s)\n' "$label:" "$(command -v "$command_name")"
  fi
}

check_tool "blastn" blastn -version
check_tool "makeblastdb" makeblastdb -version
check_tool "mafft" mafft --version
check_tool "seqkit" seqkit version
check_tool "samtools" samtools --version
check_tool "bcftools" bcftools --version
check_tool "bedtools" bedtools --version
check_tool "fastqc" fastqc --version
check_tool "hmmer" hmmsearch -h
check_tool "emboss" transeq -help
check_tool "clustalo" clustalo --version
check_tool "az" az version --output tsv
check_tool "kubectl" kubectl version --client=true --output=yaml
check_tool "azcopy" azcopy --version
