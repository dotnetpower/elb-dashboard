/**
 * FASTA / sequence helpers for the BLAST query editor.
 *
 * All functions are pure and operate on plain strings so they can be reused
 * from the query editor, the analytics page, and tests without pulling in
 * any UI dependencies. Molecular-diagnostics researchers reach for these
 * almost every time they paste a primer / amplicon:
 *
 *   - `reverseComplement` — for primer-pair direction sanity checks.
 *   - `gcContent` / `baseComposition` — to spot primer issues before submit.
 *   - `hasAmbiguousBases` — flags `N`/`Y`/`R`/etc. that some BLAST options
 *     interact badly with (e.g. dust filter on Sanger reads).
 *   - `deduplicateFasta` — removes identical sequences but keeps the union
 *     of their headers as a `|`-joined id.
 */

export interface FastaRecord {
  header: string;
  sequence: string;
}

const _COMPLEMENT: Record<string, string> = {
  A: "T",
  T: "A",
  U: "A",
  G: "C",
  C: "G",
  N: "N",
  // IUPAC ambiguity codes.
  R: "Y",
  Y: "R",
  S: "S",
  W: "W",
  K: "M",
  M: "K",
  B: "V",
  D: "H",
  H: "D",
  V: "B",
  "-": "-",
};

const _UNAMBIGUOUS_NT = new Set(["A", "T", "U", "G", "C", "N"]);
const _AMBIGUOUS_NT = new Set(["R", "Y", "S", "W", "K", "M", "B", "D", "H", "V"]);

/** Parse a FASTA string into records. Tolerates blank lines and stray text
 * before the first header (that prefix is dropped, matching what BLAST CLI
 * tools do). */
export function parseFasta(text: string): FastaRecord[] {
  const lines = text.split(/\r?\n/);
  const records: FastaRecord[] = [];
  let current: FastaRecord | null = null;
  for (const raw of lines) {
    const line = raw.trim();
    if (!line) continue;
    if (line.startsWith(">")) {
      if (current && current.sequence) records.push(current);
      current = { header: line.slice(1).trim(), sequence: "" };
    } else if (current) {
      current.sequence += line;
    }
  }
  if (current && current.sequence) records.push(current);
  return records;
}

export function serializeFasta(records: FastaRecord[], lineWidth = 70): string {
  return records
    .map((rec) => {
      const seq = rec.sequence.replace(/\s+/g, "");
      const wrapped: string[] = [];
      for (let i = 0; i < seq.length; i += lineWidth) {
        wrapped.push(seq.slice(i, i + lineWidth));
      }
      return `>${rec.header}\n${wrapped.join("\n")}`;
    })
    .join("\n");
}

/** Reverse complement a nucleotide string. Non-nucleotide characters are
 * passed through unchanged (so headers / line breaks survive a careless
 * caller). */
export function reverseComplement(sequence: string): string {
  const out: string[] = [];
  for (let i = sequence.length - 1; i >= 0; i--) {
    const ch = sequence[i].toUpperCase();
    out.push(_COMPLEMENT[ch] ?? ch);
  }
  return out.join("");
}

/** Apply `reverseComplement` to every record in a FASTA blob, keeping the
 * original headers but tagging them with `| reverse_complement`. */
export function reverseComplementFasta(text: string): string {
  const records = parseFasta(text);
  const flipped = records.map((r) => ({
    header: `${r.header} | reverse_complement`,
    sequence: reverseComplement(r.sequence),
  }));
  return serializeFasta(flipped);
}

export interface SeqStats {
  length: number;
  gc: number; // percentage 0-100
  at: number;
  /** Count of IUPAC ambiguity codes (R/Y/S/W/K/M/B/D/H/V) — excludes N,
   * matching `hasAmbiguousBases` semantics. */
  ambiguous: number;
  /** Count of N characters (unknown base, not counted in `ambiguous`). */
  nCount: number;
  composition: Record<string, number>;
}

export function baseComposition(sequence: string): SeqStats {
  const composition: Record<string, number> = {};
  let total = 0;
  let gc = 0;
  let at = 0;
  let ambiguous = 0;
  let nCount = 0;
  for (const raw of sequence) {
    const ch = raw.toUpperCase();
    if (ch === " " || ch === "\n" || ch === "\r" || ch === "\t") continue;
    composition[ch] = (composition[ch] ?? 0) + 1;
    total++;
    if (ch === "G" || ch === "C") gc++;
    else if (ch === "A" || ch === "T" || ch === "U") at++;
    if (_AMBIGUOUS_NT.has(ch)) ambiguous++;
    else if (ch === "N") nCount++;
  }
  return {
    length: total,
    gc: total === 0 ? 0 : (gc / total) * 100,
    at: total === 0 ? 0 : (at / total) * 100,
    ambiguous,
    nCount,
    composition,
  };
}

export function gcContent(sequence: string): number {
  return baseComposition(sequence).gc;
}

/** True if the input contains any IUPAC ambiguity code beyond `N`. */
export function hasAmbiguousBases(sequence: string): boolean {
  for (const raw of sequence) {
    const ch = raw.toUpperCase();
    if (_AMBIGUOUS_NT.has(ch)) return true;
  }
  return false;
}

/** True if every non-whitespace character is a recognised nucleotide or
 * IUPAC ambiguity code. Protein sequences therefore fail this check, which
 * is intentional — the warning is scoped to blastn-style submissions. */
export function looksLikeNucleotide(sequence: string): boolean {
  let total = 0;
  let recognised = 0;
  for (const raw of sequence) {
    const ch = raw.toUpperCase();
    if (ch === " " || ch === "\n" || ch === "\r" || ch === "\t") continue;
    total++;
    if (_UNAMBIGUOUS_NT.has(ch) || _AMBIGUOUS_NT.has(ch) || ch === "-") recognised++;
  }
  return total > 0 && recognised / total >= 0.9;
}

export interface DedupResult {
  text: string;
  removed: number;
  kept: number;
}

/** Remove records whose sequences (case-insensitive, whitespace-stripped)
 * are exact duplicates. The first occurrence is kept; headers from later
 * duplicates are appended to the surviving record's header as `| alias=...`
 * so the researcher does not silently lose label information. */
export function deduplicateFasta(text: string): DedupResult {
  const records = parseFasta(text);
  const seen = new Map<string, FastaRecord>();
  let removed = 0;
  for (const rec of records) {
    const key = rec.sequence.replace(/\s+/g, "").toUpperCase();
    const existing = seen.get(key);
    if (existing) {
      existing.header = `${existing.header} | alias=${rec.header}`;
      removed++;
    } else {
      seen.set(key, { header: rec.header, sequence: rec.sequence });
    }
  }
  const survivors = Array.from(seen.values());
  return {
    text: serializeFasta(survivors),
    removed,
    kept: survivors.length,
  };
}

// ─────────────────────────────────────────────────────────────────────
// Primer / short-oligo diagnostics
//
// Molecular-diagnostics researchers paste primer pairs or amplicons into
// the BLAST submit form constantly. Surfacing Tm + obvious secondary
// structure problems *before* they hit Submit catches the common "this
// primer pair will never anneal" / "this primer folds on itself" issues
// without a round trip to Primer3 / OligoAnalyzer.
//
// All formulas are intentionally simple and well-cited:
//   - Wallace rule for primers ≤13 nt: Tm = 2·(A+T) + 4·(G+C)        °C
//   - Salt-adjusted GC formula for 14–60 nt (Marmur–Schildkraut +
//     standard Na+ correction at 50 mM):
//         Tm = 64.9 + 41·(G+C-16.4)/N
//     This is the same approximation Primer3 falls back to when nearest-
//     neighbour parameters are not configured, and matches the values
//     IDT's OligoAnalyzer prints for short PCR primers within ±2 °C.
//   - Sequences >60 nt return null: nearest-neighbour is required for any
//     useful number and we deliberately don't ship that table here.
//
// Hairpin and self-dimer scores are *advisory* — they flag obvious
// problems (≥4 nt complementary stretch) so the researcher can iterate.
// They are NOT a substitute for Primer3's thermodynamic check.
// ─────────────────────────────────────────────────────────────────────

/**
 * Melting temperature (°C) for a short oligo. Returns `null` if the input
 * is too long for the heuristic formulas to be trustworthy, or if the
 * sequence contains characters other than A/T/U/G/C (ambiguity codes
 * cannot be assigned a single Tm). Whitespace is stripped before counting.
 */
export function meltingTemperatureC(sequence: string): number | null {
  let a = 0;
  let t = 0;
  let g = 0;
  let c = 0;
  for (const raw of sequence) {
    const ch = raw.toUpperCase();
    if (ch === " " || ch === "\n" || ch === "\r" || ch === "\t") continue;
    if (ch === "A") a++;
    else if (ch === "T" || ch === "U") t++;
    else if (ch === "G") g++;
    else if (ch === "C") c++;
    else return null; // unknown / ambiguous → refuse to guess
  }
  const n = a + t + g + c;
  if (n === 0 || n > 60) return null;
  if (n <= 13) {
    return 2 * (a + t) + 4 * (g + c);
  }
  // Salt-adjusted GC formula.
  return 64.9 + (41 * (g + c - 16.4)) / n;
}

/**
 * Approximate per-base GC content of the longest run of G+C in `sequence`.
 * Used as a secondary signal for primer "GC clamp" health — a 3' clamp of
 * ≥3 G/C is good, but a long internal G/C stretch can cause mispriming.
 */
export function longestGcRun(sequence: string): number {
  let best = 0;
  let cur = 0;
  for (const raw of sequence) {
    const ch = raw.toUpperCase();
    if (ch === "G" || ch === "C") {
      cur++;
      if (cur > best) best = cur;
    } else if (ch !== " " && ch !== "\n" && ch !== "\r" && ch !== "\t") {
      cur = 0;
    }
  }
  return best;
}

export interface SecondaryStructureFinding {
  /** Length (bp) of the complementary stretch detected. */
  length: number;
  /** Start index in the original sequence (0-based). */
  start: number;
  /** End index (exclusive). */
  end: number;
}

/**
 * Detect the longest self-complementary stretch within `sequence` — a
 * cheap hairpin proxy. We slide a small window and check whether
 * `seq[i..i+w]` is the reverse complement of any later sub-window in the
 * same strand. Only A/T/U/G/C are considered (ambiguous bases break the
 * complement check and are skipped). Returns `null` if no stretch ≥ 4 nt
 * is found.
 *
 * Complexity is O(n²·w) which is fine for the ≤200 nt primers / probes the
 * UI deals with. We cap the search at 200 nt for safety.
 */
export function findHairpin(
  sequence: string,
  minStem = 4,
): SecondaryStructureFinding | null {
  const seq = sequence
    .toUpperCase()
    .replace(/[^ATUGC]/g, "")
    .slice(0, 200);
  if (seq.length < minStem * 2) return null;
  let best: SecondaryStructureFinding | null = null;
  for (let w = seq.length >> 1; w >= minStem; w--) {
    for (let i = 0; i + w * 2 <= seq.length; i++) {
      const stem = seq.slice(i, i + w);
      const rc = reverseComplement(stem);
      // Search for the reverse complement somewhere downstream of the
      // stem (need a gap of at least 3 nt — biological hairpins fold
      // around a small loop).
      const searchFrom = i + w + 3;
      const idx = seq.indexOf(rc, searchFrom);
      if (idx !== -1) {
        if (!best || w > best.length) {
          best = { length: w, start: i, end: idx + w };
        }
      }
    }
    // Found something at the largest window — no need to keep scanning
    // smaller windows since they'd return shorter stems.
    if (best) return best;
  }
  return best;
}

/**
 * Detect the longest self-dimer (sequence binds to a copy of itself in
 * antiparallel orientation). Same complexity / caveats as `findHairpin`.
 * Returns the longest matching stretch length, or 0 if no complementary
 * run of `minStem` or longer is found.
 */
export function findSelfDimer(sequence: string, minStem = 4): number {
  const seq = sequence
    .toUpperCase()
    .replace(/[^ATUGC]/g, "")
    .slice(0, 200);
  if (seq.length < minStem) return 0;
  const rc = reverseComplement(seq);
  let best = 0;
  // Compare seq against shifted rc (antiparallel alignment).
  for (let shift = -(seq.length - minStem); shift <= seq.length - minStem; shift++) {
    let run = 0;
    for (let i = 0; i < seq.length; i++) {
      const j = i + shift;
      if (j < 0 || j >= rc.length) {
        run = 0;
        continue;
      }
      if (seq[i] === rc[j]) {
        run++;
        if (run > best) best = run;
      } else {
        run = 0;
      }
    }
  }
  return best >= minStem ? best : 0;
}

export interface PrimerDiagnostics {
  /** Estimated Tm in °C (null if unknown). */
  tm: number | null;
  /** GC%, 0–100. */
  gc: number;
  /** Longest internal G/C run. */
  gcRun: number;
  /** Length (bp) of the longest detected hairpin stem, or 0 if none ≥ 4 nt. */
  hairpinLength: number;
  /** Length (bp) of the longest detected self-dimer, or 0 if none ≥ 4 nt. */
  selfDimerLength: number;
}

/**
 * Convenience aggregator for the primer UI — single call returns Tm + GC%
 * + secondary-structure warnings. Returns `null` if the sequence isn't
 * worth analysing (empty, too long, or non-nucleotide).
 */
export function primerDiagnostics(sequence: string): PrimerDiagnostics | null {
  const stats = baseComposition(sequence);
  if (stats.length === 0 || stats.length > 200) return null;
  if (!looksLikeNucleotide(sequence)) return null;
  const hairpin = findHairpin(sequence);
  return {
    tm: meltingTemperatureC(sequence),
    gc: stats.gc,
    gcRun: longestGcRun(sequence),
    hairpinLength: hairpin?.length ?? 0,
    selfDimerLength: findSelfDimer(sequence),
  };
}
