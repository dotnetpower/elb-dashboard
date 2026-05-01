import { useState, useRef, useEffect, type ReactNode } from "react";
import { HelpCircle } from "lucide-react";

interface Props {
  content: ReactNode;
  /** Width of the tooltip popup. Default 320. */
  width?: number;
}

export function Tooltip({ content, width = 320 }: Props) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  // Close on outside click
  useEffect(() => {
    if (!open) return;
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, [open]);

  return (
    <span ref={ref} className="tooltip-wrap">
      <button
        type="button"
        className="tooltip-trigger"
        onMouseEnter={() => setOpen(true)}
        onMouseLeave={() => setOpen(false)}
        onClick={() => setOpen((o) => !o)}
        aria-label="More info"
      >
        <HelpCircle size={13} strokeWidth={1.5} />
      </button>
      {open && (
        <div className="tooltip-popup" style={{ width }}>
          {content}
        </div>
      )}
    </span>
  );
}
