import { useEffect, useState, useCallback } from "react";
import { useNavigate } from "react-router-dom";

const SHORTCUTS: { key: string; label: string; action: string }[] = [
  { key: "g d", label: "Go to Dashboard", action: "/" },
  { key: "g t", label: "Go to Terminal", action: "/terminal" },
  { key: "g s", label: "Go to BLAST Submit", action: "/blast/submit" },
  { key: "g j", label: "Go to BLAST Jobs", action: "/blast/jobs" },
  { key: "?", label: "Show keyboard shortcuts", action: "help" },
];

export function useKeyboardShortcuts() {
  const [showHelp, setShowHelp] = useState(false);
  const navigate = useNavigate();
  const pendingRef = { current: "" };

  const handleKey = useCallback(
    (e: KeyboardEvent) => {
      // Skip if typing in input/textarea
      const tag = (e.target as HTMLElement).tagName;
      if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT") return;

      if (e.key === "?" && !e.ctrlKey && !e.metaKey) {
        e.preventDefault();
        setShowHelp((p) => !p);
        return;
      }

      if (e.key === "Escape") {
        setShowHelp(false);
        pendingRef.current = "";
        return;
      }

      if (e.key === "g" && !e.ctrlKey && !e.metaKey) {
        pendingRef.current = "g";
        setTimeout(() => { pendingRef.current = ""; }, 800);
        return;
      }

      if (pendingRef.current === "g") {
        const combo = `g ${e.key}`;
        const match = SHORTCUTS.find((s) => s.key === combo);
        if (match && match.action !== "help") {
          e.preventDefault();
          navigate(match.action);
          setShowHelp(false);
        }
        pendingRef.current = "";
      }
    },
    [navigate],
  );

  useEffect(() => {
    window.addEventListener("keydown", handleKey);
    return () => window.removeEventListener("keydown", handleKey);
  }, [handleKey]);

  return { showHelp, setShowHelp };
}

export function ShortcutOverlay({ onClose }: { onClose: () => void }) {
  useEffect(() => {
    const handle = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [onClose]);

  return (
    <div className="shortcut-overlay" onClick={onClose}>
      <div className="shortcut-dialog" onClick={(e) => e.stopPropagation()}>
        <h3>Keyboard Shortcuts</h3>
        {SHORTCUTS.map((s) => (
          <div key={s.key} className="shortcut-row">
            <span style={{ color: "var(--text-muted)" }}>{s.label}</span>
            <span className="shortcut-key">
              {s.key.split(" ").map((k) => (
                <kbd key={k}>{k}</kbd>
              ))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}
