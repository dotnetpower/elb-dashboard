import { AlertTriangle, CheckCircle2, Loader2, Plus } from "lucide-react";

import { ErrorMsg } from "./ErrorMsg";

export interface ResourceRowProps {
  label: string;
  icon: string;
  placeholder: string;
  value: string;
  onChange: (v: string) => void;
  query: {
    isLoading: boolean;
    isError: boolean;
    data?: Array<{ name: string }> | undefined;
  };
  nameKey: string;
  isValid: boolean;
  mutation: {
    isPending: boolean;
    isSuccess: boolean;
    isError: boolean;
    error: Error | null;
    mutate: () => void;
  };
  error?: string;
}

export function ResourceRow({
  label,
  icon,
  placeholder,
  value,
  onChange,
  query,
  isValid,
  mutation,
  error,
}: ResourceRowProps) {
  const found =
    !query.isLoading && value && query.data?.some((r) => r.name === value);
  const duplicate =
    !query.isLoading &&
    value &&
    !found &&
    query.data?.some((r) => r.name.toLowerCase() === value.toLowerCase());
  const noResources =
    !query.isLoading && !query.isError && query.data?.length === 0;

  return (
    <>
      <div
        style={{
          fontSize: 11,
          color: "var(--text-faint)",
          textTransform: "uppercase",
          letterSpacing: "0.06em",
          margin: "14px 0 6px",
          display: "flex",
          alignItems: "center",
          gap: 6,
        }}
      >
        {label}
        {!query.isLoading && query.data && (
          <span
            style={{
              fontSize: 10,
              color: "var(--text-faint)",
              fontWeight: 400,
              textTransform: "none",
              letterSpacing: 0,
            }}
          >
            ({query.data.length} found)
          </span>
        )}
      </div>
      {noResources && (
        <div
          style={{
            fontSize: 11,
            color: "var(--text-muted)",
            marginBottom: 6,
            lineHeight: 1.4,
          }}
        >
          No existing resources found. Enter a name to create one.
        </div>
      )}
      <div
        style={{
          background: "var(--bg-secondary)",
          border: "1px solid var(--border-weak)",
          borderRadius: "var(--radius)",
          padding: "12px 14px",
          display: "flex",
          alignItems: "center",
          gap: 10,
        }}
      >
        <div style={{ fontSize: 16 }}>{icon}</div>
        {query.isError || noResources ? (
          <input
            className="glass-input"
            placeholder={placeholder}
            value={value}
            onChange={(e) => onChange(e.target.value.trim())}
            spellCheck={false}
            style={{ flex: 1, fontSize: 12 }}
          />
        ) : query.isLoading ? (
          <div
            style={{
              flex: 1,
              color: "var(--text-muted)",
              display: "flex",
              alignItems: "center",
              gap: 6,
            }}
          >
            <Loader2 size={14} className="spin" /> Scanning...
          </div>
        ) : (
          <select
            className="glass-input"
            value={value}
            onChange={(e) => onChange(e.target.value)}
            style={{ flex: 1 }}
          >
            {query.data!.map((r) => (
              <option key={r.name} value={r.name}>
                {r.name}
              </option>
            ))}
          </select>
        )}
        {found ? (
          <span className="gt gt-g">
            <CheckCircle2 size={10} /> Found
          </span>
        ) : !query.isLoading && value ? (
          <button
            className="glass-button glass-button--primary"
            style={{ fontSize: 11, padding: "3px 8px", whiteSpace: "nowrap" }}
            disabled={!isValid || mutation.isPending || mutation.isSuccess}
            onClick={() => mutation.mutate()}
          >
            {mutation.isPending ? (
              <Loader2 size={10} className="spin" />
            ) : mutation.isSuccess ? (
              <>
                <CheckCircle2 size={10} /> Created
              </>
            ) : (
              <>
                <Plus size={10} /> Create
              </>
            )}
          </button>
        ) : null}
      </div>
      <ErrorMsg msg={error} />
      {duplicate && (
        <div
          style={{
            display: "flex",
            alignItems: "center",
            gap: 4,
            color: "var(--warning)",
            fontSize: 11,
            marginTop: 4,
          }}
        >
          <AlertTriangle size={11} /> A resource with a similar name exists (case
          mismatch). Check the name.
        </div>
      )}
      {mutation.isError && (
        <div style={{ fontSize: 11, color: "var(--danger)", marginTop: 4 }}>
          {(mutation.error as Error).message}
        </div>
      )}
    </>
  );
}
