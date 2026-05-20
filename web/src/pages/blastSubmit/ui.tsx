import { useState } from "react";
import { Check, Copy, HelpCircle, Terminal } from "lucide-react";

import { buildCommandString, type FormState, PROGRAMS } from "@/pages/blastSubmitModel";
import type { ToastFn } from "@/pages/blastSubmit/types";

type CommandTokenKind = "command" | "flag" | "number" | "value";

function classifyCommandToken(token: string, index: number): CommandTokenKind {
  if (index === 0) return "command";
  if (token.startsWith("-")) return "flag";
  if (/^[0-9.]+$/.test(token)) return "number";
  return "value";
}

function renderCommandPreview(command: string) {
  let tokenIndex = 0;

  return command.split(/(\s+)/).map((token, index) => {
    if (/^\s+$/.test(token)) return token;

    const kind = classifyCommandToken(token, tokenIndex);
    tokenIndex += 1;

    return (
      <span
        key={`${token}-${index}`}
        className={`blast-cmd-token blast-cmd-token--${kind}`}
      >
        {token}
      </span>
    );
  });
}

export function Tip({ text }: { text: string }) {
  return (
    <span
      title={text}
      style={{
        cursor: "help",
        marginLeft: 4,
        color: "var(--text-faint)",
        verticalAlign: "middle",
      }}
    >
      <HelpCircle size={12} strokeWidth={1.5} />
    </span>
  );
}

export function SectionHeader({
  step,
  icon,
  title,
  subtitle,
}: {
  step: number;
  icon: React.ReactNode;
  title: string;
  subtitle?: string;
}) {
  return (
    <div className="blast-section-hd">
      <span className="blast-step-badge">{step}</span>
      <span className="blast-section-icon">{icon}</span>
      <div>
        <div className="blast-section-title">{title}</div>
        {subtitle && <div className="blast-section-sub">{subtitle}</div>}
      </div>
    </div>
  );
}

export function BlastCommandPreview({
  form,
  programMeta,
  effectiveSearchSpace,
  toast,
}: {
  form: FormState;
  programMeta: (typeof PROGRAMS)[0];
  effectiveSearchSpace?: number;
  toast: ToastFn;
}) {
  const [copied, setCopied] = useState(false);
  const cmd = buildCommandString(form, programMeta, { effectiveSearchSpace });

  const handleCopy = () => {
    navigator.clipboard.writeText(cmd).then(() => {
      setCopied(true);
      toast("Command copied to clipboard", "info");
      setTimeout(() => setCopied(false), 2000);
    });
  };

  return (
    <div className="blast-cmd-preview">
      <div className="blast-cmd-preview__header">
        <Terminal size={13} strokeWidth={1.5} />
        <span>Command Preview</span>
        <button className="blast-cmd-copy" onClick={handleCopy} title="Copy command">
          {copied ? (
            <Check size={12} strokeWidth={2} />
          ) : (
            <Copy size={12} strokeWidth={1.5} />
          )}
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
      <code className="blast-cmd-preview__code">{renderCommandPreview(cmd)}</code>
    </div>
  );
}
