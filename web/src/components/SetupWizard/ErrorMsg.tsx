import { AlertTriangle } from "lucide-react";

export function ErrorMsg({ msg }: { msg?: string }) {
  if (!msg) return null;
  return (
    <div
      style={{
        display: "flex",
        alignItems: "center",
        gap: 4,
        color: "var(--danger)",
        fontSize: 11,
        marginTop: 4,
      }}
    >
      <AlertTriangle size={11} /> {msg}
    </div>
  );
}
