interface Props {
  seconds: number | null;
  total: number;
}

export function RefreshRing({ seconds, total }: Props) {
  if (seconds == null || total <= 0) return null;

  const r = 9;
  const circumference = 2 * Math.PI * r;
  const progress = Math.max(0, Math.min(1, seconds / total));
  const offset = circumference * (1 - progress);

  return (
    <div className="refresh-ring" title={`Next refresh in ${seconds}s`}>
      <svg width="22" height="22" viewBox="0 0 22 22">
        <circle
          cx="11" cy="11" r={r}
          fill="none"
          stroke="var(--border-weak)"
          strokeWidth="2"
        />
        <circle
          cx="11" cy="11" r={r}
          fill="none"
          stroke="var(--text-faint)"
          strokeWidth="2"
          strokeLinecap="round"
          strokeDasharray={circumference}
          strokeDashoffset={offset}
          transform="rotate(-90 11 11)"
          style={{ transition: "stroke-dashoffset 1s linear" }}
        />
      </svg>
      <span className="refresh-ring__text">{seconds}</span>
    </div>
  );
}
