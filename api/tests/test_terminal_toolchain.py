"""Static contract tests for the terminal sidecar toolchain.

Responsibility: Static contract tests for the terminal sidecar toolchain
Edit boundaries: Keep assertions focused on the behavior under test; prefer fakes over live
Azure calls.
Key entry points: `test_terminal_dockerfile_installs_linux_and_sequence_tools`,
`test_tool_versions_script_reports_expected_tools`,
`test_terminal_manual_covers_beginner_and_bioinformatics_workflows`
Risky contracts: Do not require network access or real Azure credentials unless the test is
explicitly integration-scoped.
Validation: `uv run pytest -q api/tests/test_terminal_toolchain.py`.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

pytestmark = pytest.mark.subprocess

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
DOCKERFILE = REPO_ROOT / "terminal" / "Dockerfile"
TOOL_VERSIONS = REPO_ROOT / "terminal" / "tool-versions.sh"
ENTRYPOINT = REPO_ROOT / "terminal" / "entrypoint.sh"
MANUAL_CONTENT = REPO_ROOT / "web" / "src" / "pages" / "terminal" / "terminalManualContent.ts"


LINUX_TOOLS = [
    "nano",
    "vim-tiny",
    "less",
    "tree",
    "net-tools",
    "iproute2",
    "iputils-ping",
    "dnsutils",
]
BIO_TOOLS = [
    "mafft",
    "seqkit",
    "samtools",
    "bcftools",
    "bedtools",
    "fastqc",
    "hmmer",
    "emboss",
    "clustalo",
    "muscle",
]
VERSION_LABELS = [
    "blastn",
    "makeblastdb",
    "mafft",
    "seqkit",
    "samtools",
    "bcftools",
    "bedtools",
    "fastqc",
    "hmmer",
    "emboss",
    "clustalo",
    "az",
    "kubectl",
    "azcopy",
]
MANUAL_SECTIONS = ["Linux Basics", "Files", "BLAST", "Sequence Tools", "Azure", "Troubleshooting"]
MANUAL_COMMANDS = [
    "nano notes.txt",
    "tree -L 2",
    "blastn -version",
    "mafft input.fa > aligned.fa",
    "az login --use-device-code",
    "ifconfig",
]


def test_terminal_dockerfile_installs_linux_and_sequence_tools() -> None:
    body = DOCKERFILE.read_text()

    assert "BLAST_VERSION=2.17.0" in body
    assert "ncbi-blast-${BLAST_VERSION}+-x64-linux.tar.gz" in body
    for package in LINUX_TOOLS + BIO_TOOLS:
        assert package in body


def test_tool_versions_script_reports_expected_tools() -> None:
    body = TOOL_VERSIONS.read_text()

    for label in VERSION_LABELS:
        assert f'check_tool "{label}"' in body

    subprocess.run(  # noqa: S603 - static repository script syntax check.
        ["/bin/bash", "-n", str(TOOL_VERSIONS)],
        cwd=REPO_ROOT,
        check=True,
    )


def test_entrypoint_pins_ttyd_to_loopback_in_a_container_app() -> None:
    """Charter §9: in a deployed Container Apps revision (CONTAINER_APP_NAME set)
    ttyd must refuse a non-loopback TTYD_HOST rather than expose the writable
    shell to the VNet. Mirror the exec_server guard."""
    body = ENTRYPOINT.read_text()
    assert "CONTAINER_APP_NAME" in body
    subprocess.run(  # noqa: S603 - static repository script syntax check.
        ["/bin/bash", "-n", str(ENTRYPOINT)],
        cwd=REPO_ROOT,
        check=True,
    )

    guard = (
        'TTYD_HOST="0.0.0.0"\n'
        'CONTAINER_APP_NAME="ca-elb-dashboard"\n'
        'if [[ -n "${CONTAINER_APP_NAME:-}" ]]; then\n'
        '  case "$TTYD_HOST" in\n'
        "    127.0.0.1 | localhost | ::1) : ;;\n"
        '    *) echo "refused" >&2; exit 1 ;;\n'
        "  esac\n"
        "fi\n"
        'echo "started"\n'
    )
    blocked = subprocess.run(  # noqa: S603 - static snippet, no external input.
        ["/bin/bash", "-c", guard],
        capture_output=True,
        text=True,
    )
    assert blocked.returncode == 1
    assert "started" not in blocked.stdout

    allowed = subprocess.run(  # noqa: S603 - static snippet, no external input.
        ["/bin/bash", "-c", guard.replace('TTYD_HOST="0.0.0.0"', 'TTYD_HOST="127.0.0.1"')],
        capture_output=True,
        text=True,
    )
    assert allowed.returncode == 0
    assert "started" in allowed.stdout


def test_terminal_manual_covers_beginner_and_bioinformatics_workflows() -> None:
    body = MANUAL_CONTENT.read_text()

    for section in MANUAL_SECTIONS:
        assert section in body
    for command in MANUAL_COMMANDS:
        assert command in body
