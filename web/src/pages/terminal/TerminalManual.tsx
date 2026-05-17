import { BookOpen, Copy, SendToBack } from "lucide-react";

import {
  classifyCommand,
  normaliseCommandForTerminalInsert,
} from "@/pages/terminal/terminalCockpitModel";
import { TERMINAL_MANUAL_SECTIONS } from "@/pages/terminal/terminalManualContent";

interface TerminalManualProps {
  onCopyCommand?: (command: string) => void;
  onInsertCommand?: (command: string) => void;
  canInsertCommand?: boolean;
}

export function TerminalManual({
  onCopyCommand,
  onInsertCommand,
  canInsertCommand = true,
}: TerminalManualProps) {
  const insertTitle = (command: string) => {
    const insertCommand = normaliseCommandForTerminalInsert(command);
    const analysis = classifyCommand(insertCommand);
    if (!canInsertCommand) return "Terminal must be connected before running commands";
    if (insertCommand.length === 0) return "Command must not be empty";
    if (analysis.risk === "high") return "High-risk commands must be copied and reviewed manually";
    return "Insert and run command";
  };

  const canInsert = (command: string) => {
    const insertCommand = normaliseCommandForTerminalInsert(command);
    return canInsertCommand && insertCommand.length > 0 && classifyCommand(insertCommand).risk !== "high";
  };

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
                    <div className="terminal-manual__command-text">
                      <code>{item.command}</code>
                      <span>{item.description}</span>
                    </div>
                    <div className="terminal-manual__command-actions">
                      {onCopyCommand && (
                        <button
                          type="button"
                          className="terminal-manual__action"
                          onClick={() => onCopyCommand(item.command)}
                          aria-label={`Copy ${item.command}`}
                          title="Copy command"
                        >
                          <Copy size={12} strokeWidth={1.5} />
                        </button>
                      )}
                      {onInsertCommand && (
                        <button
                          type="button"
                          className="terminal-manual__action terminal-manual__action--insert"
                          onClick={() => onInsertCommand(normaliseCommandForTerminalInsert(item.command))}
                          disabled={!canInsert(item.command)}
                          aria-label={`Insert and run ${item.command}`}
                          title={insertTitle(item.command)}
                        >
                          <SendToBack size={12} strokeWidth={1.5} />
                          Insert
                        </button>
                      )}
                    </div>
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
