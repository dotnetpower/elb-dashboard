import { BookOpen, Copy } from "lucide-react";

import { TERMINAL_MANUAL_SECTIONS } from "@/pages/terminal/terminalManualContent";

interface TerminalManualProps {
  onCopyCommand?: (command: string) => void;
}

export function TerminalManual({ onCopyCommand }: TerminalManualProps) {
  return (
    <aside className="terminal-manual" aria-label="Terminal user manual">
      <div className="terminal-manual__header">
        <div className="terminal-manual__title">
          <BookOpen size={15} strokeWidth={1.5} />
          Terminal Manual
        </div>
        <div className="terminal-manual__subtitle">Linux and bioinformatics quick guide</div>
      </div>

      <div className="terminal-manual__sections">
        {TERMINAL_MANUAL_SECTIONS.map((section) => {
          const Icon = section.icon;
          return (
            <section className="terminal-manual__section" key={section.id}>
              <div className="terminal-manual__section-title">
                <Icon size={14} strokeWidth={1.5} />
                {section.label}
              </div>
              <p>{section.summary}</p>
              <div className="terminal-manual__commands">
                {section.commands.map((item) => (
                  <div className="terminal-manual__command" key={item.command}>
                    <code>{item.command}</code>
                    <span>{item.description}</span>
                    {onCopyCommand && (
                      <button
                        type="button"
                        className="terminal-manual__copy"
                        onClick={() => onCopyCommand(item.command)}
                        aria-label={`Copy ${item.command}`}
                        title="Copy command"
                      >
                        <Copy size={12} strokeWidth={1.5} />
                      </button>
                    )}
                  </div>
                ))}
              </div>
            </section>
          );
        })}
      </div>
    </aside>
  );
}
