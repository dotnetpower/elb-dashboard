import { useEffect, useState, useCallback, useRef } from "react";
import { useNavigate } from "react-router-dom";
import {
  Keyboard,
  Info,
  ExternalLink,
  BookOpen,
  Github,
  Server,
  Activity,
  Code2,
  HelpCircle,
  X,
} from "lucide-react";

const SHORTCUTS: { key: string; label: string; action: string }[] = [
  { key: "g d", label: "Go to Dashboard", action: "/" },
  { key: "g t", label: "Go to Terminal", action: "/terminal" },
  { key: "g s", label: "Go to BLAST Submit", action: "/blast/submit" },
  { key: "g j", label: "Go to BLAST Jobs", action: "/blast/jobs" },
  { key: "g a", label: "Go to API Reference", action: "/docs" },
  { key: "?", label: "Show this panel", action: "help" },
  { key: "Esc", label: "Close panel / dialog", action: "close" },
];

export function useKeyboardShortcuts() {
  const [showHelp, setShowHelp] = useState(false);
  const navigate = useNavigate();
  const pendingRef = useRef("");

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
        setTimeout(() => {
          pendingRef.current = "";
        }, 800);
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
  const [tab, setTab] = useState<"shortcuts" | "about" | "links">("shortcuts");

  useEffect(() => {
    const handle = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", handle);
    return () => window.removeEventListener("keydown", handle);
  }, [onClose]);

  const tabs = [
    { id: "shortcuts" as const, label: "Shortcuts", icon: <Keyboard size={13} /> },
    { id: "about" as const, label: "About", icon: <Info size={13} /> },
    { id: "links" as const, label: "Resources", icon: <BookOpen size={13} /> },
  ];

  return (
    <div className="shortcut-overlay" onClick={onClose}>
      <div
        onClick={(e) => e.stopPropagation()}
        style={{
          background: "var(--bg-primary)",
          border: "1px solid var(--border-medium)",
          borderRadius: 16,
          boxShadow: "0 12px 48px rgba(0,0,0,0.5)",
          width: 480,
          maxHeight: "80vh",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "18px 24px 0",
            display: "flex",
            alignItems: "center",
            justifyContent: "space-between",
          }}
        >
          <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
            <HelpCircle size={18} style={{ color: "var(--accent)" }} />
            <h3 style={{ margin: 0, fontSize: 16, fontWeight: 700 }}>
              Help & Information
            </h3>
          </div>
          <button
            onClick={onClose}
            style={{
              width: 28,
              height: 28,
              borderRadius: 6,
              display: "grid",
              placeItems: "center",
              background: "none",
              border: "none",
              color: "var(--text-faint)",
              cursor: "pointer",
            }}
          >
            <X size={16} />
          </button>
        </div>

        {/* Tabs */}
        <div
          style={{
            display: "flex",
            gap: 2,
            padding: "12px 24px 0",
            borderBottom: "1px solid var(--border-weak)",
          }}
        >
          {tabs.map((t) => (
            <button
              key={t.id}
              onClick={() => setTab(t.id)}
              style={{
                display: "flex",
                alignItems: "center",
                gap: 5,
                padding: "8px 14px",
                fontSize: 12,
                fontWeight: tab === t.id ? 600 : 400,
                background: "none",
                border: "none",
                cursor: "pointer",
                color: tab === t.id ? "var(--accent)" : "var(--text-faint)",
                borderBottom:
                  tab === t.id ? "2px solid var(--accent)" : "2px solid transparent",
                marginBottom: -1,
                transition: "all 0.15s",
              }}
            >
              {t.icon} {t.label}
            </button>
          ))}
        </div>

        {/* Content */}
        <div style={{ padding: "16px 24px 24px", overflowY: "auto", flex: 1 }}>
          {tab === "shortcuts" && <ShortcutsTab />}
          {tab === "about" && <AboutTab />}
          {tab === "links" && <LinksTab />}
        </div>
      </div>
    </div>
  );
}

function ShortcutsTab() {
  const navShortcuts = SHORTCUTS.filter(
    (s) => s.action.startsWith("/") || s.action.startsWith("g"),
  );
  const otherShortcuts = SHORTCUTS.filter(
    (s) => !s.action.startsWith("/") && !s.action.startsWith("g"),
  );

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      <ShortcutGroup title="Navigation" shortcuts={navShortcuts} />
      <ShortcutGroup title="General" shortcuts={otherShortcuts} />
      <div style={{ fontSize: 11, color: "var(--text-faint)", marginTop: 4 }}>
        Press <kbd style={kbdStyle}>g</kbd> then a letter within 800ms to navigate.
      </div>
    </div>
  );
}

const kbdStyle: React.CSSProperties = {
  display: "inline-block",
  padding: "2px 6px",
  fontSize: 11,
  fontFamily: "var(--font-mono)",
  background: "var(--bg-tertiary)",
  border: "1px solid var(--border-weak)",
  borderRadius: 4,
  minWidth: 20,
  textAlign: "center",
};

function ShortcutGroup({
  title,
  shortcuts,
}: {
  title: string;
  shortcuts: typeof SHORTCUTS;
}) {
  return (
    <div>
      <div
        style={{
          fontSize: 10,
          textTransform: "uppercase",
          letterSpacing: "0.08em",
          color: "var(--text-faint)",
          fontWeight: 700,
          marginBottom: 8,
        }}
      >
        {title}
      </div>
      <div style={{ display: "flex", flexDirection: "column", gap: 1 }}>
        {shortcuts.map((s) => (
          <div
            key={s.key}
            style={{
              display: "flex",
              justifyContent: "space-between",
              alignItems: "center",
              padding: "6px 0",
              borderBottom: "1px solid var(--border-weak)",
            }}
          >
            <span style={{ fontSize: 12, color: "var(--text-muted)" }}>{s.label}</span>
            <span style={{ display: "flex", gap: 4 }}>
              {s.key.split(" ").map((k) => (
                <kbd key={k} style={kbdStyle}>
                  {k}
                </kbd>
              ))}
            </span>
          </div>
        ))}
      </div>
    </div>
  );
}

function AboutTab() {
  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 16 }}>
      {/* App info */}
      <div
        style={{
          padding: "16px 18px",
          borderRadius: 10,
          background: "var(--bg-secondary)",
          border: "1px solid var(--border-weak)",
        }}
      >
        <div style={{ display: "flex", alignItems: "center", gap: 10, marginBottom: 12 }}>
          <div
            style={{
              width: 40,
              height: 40,
              borderRadius: 10,
              background: "linear-gradient(135deg, var(--accent), var(--purple))",
              display: "grid",
              placeItems: "center",
            }}
          >
            <Activity size={20} style={{ color: "#fff" }} />
          </div>
          <div>
            <div style={{ fontSize: 15, fontWeight: 700 }}>
              ElasticBLAST Control Plane
            </div>
            <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
              Browser-based control plane for ElasticBLAST on Azure
            </div>
          </div>
        </div>
        <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
          <InfoItem label="Version" value="0.1.0" />
          <InfoItem label="Runtime" value="Azure Functions + SWA" />
          <InfoItem label="Auth" value="Microsoft Entra ID" />
          <InfoItem label="Backend" value="Python 3.11" />
          <InfoItem label="Frontend" value="React + TypeScript" />
          <InfoItem label="BLAST+" value="2.17.0 (NCBI)" />
        </div>
      </div>

      {/* Stack */}
      <div>
        <div
          style={{
            fontSize: 10,
            textTransform: "uppercase",
            letterSpacing: "0.08em",
            color: "var(--text-faint)",
            fontWeight: 700,
            marginBottom: 8,
          }}
        >
          Technology Stack
        </div>
        <div style={{ display: "flex", flexWrap: "wrap", gap: 6 }}>
          {[
            "Azure Kubernetes Service",
            "Durable Functions",
            "Azure Blob Storage",
            "Azure Container Registry",
            "MSAL.js",
            "Key Vault",
            "Workload Identity",
            "Vite",
            "TanStack Query",
            "Pydantic",
          ].map((t) => (
            <span
              key={t}
              style={{
                padding: "3px 10px",
                borderRadius: 20,
                fontSize: 10,
                background: "var(--bg-tertiary)",
                color: "var(--text-muted)",
                border: "1px solid var(--border-weak)",
              }}
            >
              {t}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

function InfoItem({ label, value }: { label: string; value: string }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", padding: "4px 0" }}>
      <span style={{ fontSize: 11, color: "var(--text-faint)" }}>{label}</span>
      <span
        style={{
          fontSize: 11,
          fontWeight: 500,
          fontFamily: "var(--font-mono)",
          color: "var(--text-muted)",
        }}
      >
        {value}
      </span>
    </div>
  );
}

function LinksTab() {
  const links = [
    {
      icon: <Github size={14} />,
      label: "Source Code",
      desc: "elb-dashboard",
      url: "https://github.com/dotnetpower/elb-dashboard",
    },
    {
      icon: <Github size={14} />,
      label: "BLAST Runtime",
      desc: "elastic-blast-azure",
      url: "https://github.com/dotnetpower/elastic-blast-azure",
    },
    {
      icon: <Server size={14} />,
      label: "NCBI BLAST+",
      desc: "Official BLAST documentation",
      url: "https://blast.ncbi.nlm.nih.gov/doc/elastic-blast/",
    },
    {
      icon: <BookOpen size={14} />,
      label: "Azure AKS Docs",
      desc: "Azure Kubernetes Service",
      url: "https://learn.microsoft.com/azure/aks/",
    },
    {
      icon: <Code2 size={14} />,
      label: "OpenAPI Spec",
      desc: "Interactive API documentation",
      url: "/docs",
      internal: true,
    },
  ];

  return (
    <div style={{ display: "flex", flexDirection: "column", gap: 2 }}>
      {links.map((l) => (
        <a
          key={l.label}
          href={l.url}
          target={l.url.startsWith("http") ? "_blank" : undefined}
          rel={l.url.startsWith("http") ? "noreferrer" : undefined}
          style={{
            display: "flex",
            alignItems: "center",
            gap: 12,
            padding: "10px 12px",
            borderRadius: 8,
            color: "var(--text-muted)",
            textDecoration: "none",
            transition: "background 0.12s",
          }}
          onMouseEnter={(e) => {
            e.currentTarget.style.background = "var(--bg-hover)";
          }}
          onMouseLeave={(e) => {
            e.currentTarget.style.background = "transparent";
          }}
        >
          <span style={{ color: "var(--text-faint)", display: "flex" }}>{l.icon}</span>
          <div style={{ flex: 1 }}>
            <div style={{ fontSize: 12, fontWeight: 500, color: "var(--text-primary)" }}>
              {l.label}
            </div>
            <div style={{ fontSize: 10, color: "var(--text-faint)" }}>{l.desc}</div>
          </div>
          <ExternalLink size={12} style={{ color: "var(--text-faint)", opacity: 0.5 }} />
        </a>
      ))}
    </div>
  );
}
