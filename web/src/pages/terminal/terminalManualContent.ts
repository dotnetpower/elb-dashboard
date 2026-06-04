import { Cloud, Dna, FileText, HelpCircle, TerminalSquare, Wrench } from "lucide-react";
import type { LucideIcon } from "lucide-react";

export interface TerminalManualCommand {
  command: string;
  description: string;
}

export interface TerminalManualSection {
  id: string;
  label: string;
  icon: LucideIcon;
  summary: string;
  commands: TerminalManualCommand[];
}

export const TERMINAL_MANUAL_SECTIONS: TerminalManualSection[] = [
  {
    id: "basics",
    label: "Linux Basics",
    icon: TerminalSquare,
    summary: "Start here if the command line is new to you.",
    commands: [
      { command: "pwd", description: "Show the current folder." },
      { command: "ls -lh", description: "List files with readable sizes." },
      { command: "cd queries", description: "Move into a folder." },
      { command: "mkdir work", description: "Create a folder." },
      { command: "nano notes.txt", description: "Open a beginner-friendly text editor." },
      { command: "less large.log", description: "Read a large file without loading it all at once." },
      { command: "tree -L 2", description: "Show a folder tree two levels deep." },
      { command: "cp input.fa copy.fa", description: "Copy a file." },
      { command: "mv old.fa new.fa", description: "Rename or move a file." },
    ],
  },
  {
    id: "files",
    label: "Files",
    icon: FileText,
    summary: "Inspect, compress, and stage sequence files safely.",
    commands: [
      { command: "head -20 query.fa", description: "Preview the first records." },
      { command: "tail -50 run.log", description: "Check the latest log lines." },
      { command: "file sample.fastq.gz", description: "Identify a file type." },
      { command: "pigz -dc reads.fastq.gz | head", description: "Preview compressed FASTQ content." },
      { command: "tar -tzf archive.tgz | head", description: "List an archive before extracting it." },
      { command: "rsync -av source/ target/", description: "Copy a folder while preserving structure." },
    ],
  },
  {
    id: "blast",
    label: "BLAST",
    icon: Dna,
    summary: "Run local BLAST utilities or submit ElasticBLAST jobs.",
    commands: [
      { command: "blastn -version", description: "Check the installed BLAST+ version." },
      { command: "makeblastdb -in refs.fa -dbtype nucl -out refs", description: "Build a nucleotide database." },
      { command: "blastn -query query.fa -db refs -outfmt 6 -out hits.tsv", description: "Run a local tabular BLAST search." },
      { command: "elb-cfg --program blastn --db blast-db/16S/16S --queries q.fa --results run-1 -o ~/elastic-blast.ini", description: "Generate an elastic-blast.ini from platform defaults." },
      { command: "elb-cfg --check ~/elastic-blast.ini", description: "Validate an existing config before submitting." },
      { command: "elastic-blast --help", description: "Open ElasticBLAST CLI help." },
      { command: "elb-tool-versions", description: "Print installed terminal tool versions." },
    ],
  },
  {
    id: "sequence",
    label: "Sequence Tools",
    icon: Wrench,
    summary: "Common utilities for FASTA, FASTQ, BAM, VCF, and alignments.",
    commands: [
      { command: "seqkit stats *.fa", description: "Summarise FASTA/FASTQ files." },
      { command: "seqkit grep -p seq-id refs.fa", description: "Extract sequences by ID." },
      { command: "mafft input.fa > aligned.fa", description: "Create a multiple sequence alignment." },
      { command: "samtools faidx refs.fa", description: "Index a FASTA file." },
      { command: "bedtools intersect -a a.bed -b b.bed", description: "Find overlapping genomic intervals." },
      { command: "fastqc reads.fastq.gz", description: "Run read quality checks." },
    ],
  },
  {
    id: "azure",
    label: "Azure",
    icon: Cloud,
    summary: "Authenticate and inspect your Azure workspace from the sidecar.",
    commands: [
      { command: "az login --use-device-code", description: "Sign in interactively from the browser terminal." },
      { command: "az account show -o table", description: "Confirm the active Azure subscription." },
      { command: "az aks list -o table", description: "List AKS clusters visible to your account." },
      { command: "azcopy --version", description: "Confirm AzCopy is installed." },
      { command: "kubectl version --client", description: "Check the Kubernetes client." },
    ],
  },
  {
    id: "trouble",
    label: "Troubleshooting",
    icon: HelpCircle,
    summary: "Small checks for common terminal surprises.",
    commands: [
      { command: "command -v nano", description: "Check whether a command is installed." },
      { command: "which blastn", description: "Show which executable will run." },
      { command: "df -h", description: "Check available disk space." },
      { command: "htop", description: "Inspect CPU and memory usage interactively." },
      { command: "ifconfig", description: "Show network interfaces for familiar Linux workflows." },
      { command: "ping -c 3 example.com", description: "Check basic network reachability." },
    ],
  },
];
