"""Small FASTA metadata helpers for BLAST pre-flight checks."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class QueryRecordSummary:
    query_id: str
    length: int
    full_header: str
    sequence_lines: tuple[str, ...]

    def as_dict(self) -> dict[str, object]:
        return {
            "query_id": self.query_id,
            "length": self.length,
            "full_header": self.full_header,
        }

    def as_fasta(self) -> str:
        return f">{self.full_header}\n" + "\n".join(self.sequence_lines) + "\n"


@dataclass(frozen=True)
class QueryMetadata:
    query_count: int
    total_letters: int
    min_length: int
    max_length: int
    mixed_lengths: bool
    records: list[QueryRecordSummary] = field(default_factory=list)

    def as_dict(self) -> dict[str, object]:
        return {
            "query_count": self.query_count,
            "total_letters": self.total_letters,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "mixed_lengths": self.mixed_lengths,
            "records": [record.as_dict() for record in self.records],
        }


def parse_fasta_metadata(
    text: str,
    *,
    max_records: int = 10_000,
    max_total_letters: int = 50_000_000,
) -> QueryMetadata:
    """Parse FASTA text and return compact query metadata.

    This intentionally does not validate IUPAC alphabets. The pre-flight needs
    count/length information for precision policy, while BLAST remains the
    authority for sequence alphabet validation.
    """
    records: list[QueryRecordSummary] = []
    seen_query_ids: set[str] = set()
    current_id: str | None = None
    current_header: str | None = None
    current_sequence_lines: list[str] = []
    current_len = 0
    total_letters = 0

    def flush() -> None:
        nonlocal current_id, current_header, current_sequence_lines, current_len
        if current_id is None:
            return
        if current_len <= 0:
            raise ValueError(f"query {current_id!r} has no sequence letters")
        if current_id in seen_query_ids:
            raise ValueError(f"duplicate query ID: {current_id!r}")
        seen_query_ids.add(current_id)
        records.append(
            QueryRecordSummary(
                query_id=current_id,
                length=current_len,
                full_header=current_header or current_id,
                sequence_lines=tuple(current_sequence_lines),
            )
        )
        if len(records) > max_records:
            raise ValueError(f"query FASTA has more than {max_records} records")
        current_id = None
        current_header = None
        current_sequence_lines = []
        current_len = 0

    saw_header = False
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if line.startswith(">"):
            saw_header = True
            flush()
            current_header = line[1:].strip()
            current_id = current_header.split(None, 1)[0]
            if not current_id:
                raise ValueError("FASTA header is missing a query id")
            continue
        if current_id is None:
            raise ValueError("FASTA sequence data appeared before the first header")
        letters = len("".join(line.split()))
        current_sequence_lines.append(line)
        current_len += letters
        total_letters += letters
        if total_letters > max_total_letters:
            raise ValueError(f"query FASTA has more than {max_total_letters} sequence letters")

    if not saw_header:
        raise ValueError("query data is not FASTA; expected at least one header line")
    flush()
    if not records:
        raise ValueError("query FASTA contains no records")

    lengths = [record.length for record in records]
    return QueryMetadata(
        query_count=len(records),
        total_letters=sum(lengths),
        min_length=min(lengths),
        max_length=max(lengths),
        mixed_lengths=len(set(lengths)) > 1,
        records=records,
    )