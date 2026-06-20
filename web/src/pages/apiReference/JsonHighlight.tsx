import type { CSSProperties, ReactNode } from "react";

export function JsonHighlight({ text }: { text: string }) {
  const styles: Record<string, CSSProperties> = {
    key: { color: "var(--json-key)" },
    str: { color: "var(--json-str)" },
    num: { color: "var(--json-num)" },
    bool: { color: "var(--json-bool)" },
    nil: { color: "var(--json-nil)", fontStyle: "italic" },
    brace: { color: "var(--json-brace)" },
  };

  const parts: ReactNode[] = [];
  const re =
    /("(?:[^"\\]|\\.)*")(\s*)(:?)|(\b(?:true|false)\b)|(\bnull\b)|([\d](?:[\d.eE+\-])*)|([{}[\],])/g;
  let last = 0;
  let match: RegExpExecArray | null;
  let index = 0;

  while ((match = re.exec(text)) !== null) {
    if (match.index > last) parts.push(text.slice(last, match.index));
    if (match[1]) {
      const stringStyle = match[3] ? styles.key : styles.str;
      parts.push(
        <span key={index} style={stringStyle}>
          {match[1]}
        </span>,
      );
      if (match[2]) parts.push(match[2]);
      if (match[3]) {
        parts.push(
          <span key={`${index}c`} style={styles.brace}>
            {match[3]}
          </span>,
        );
      }
    } else if (match[4]) {
      parts.push(
        <span key={index} style={styles.bool}>
          {match[4]}
        </span>,
      );
    } else if (match[5]) {
      parts.push(
        <span key={index} style={styles.nil}>
          {match[5]}
        </span>,
      );
    } else if (match[6]) {
      parts.push(
        <span key={index} style={styles.num}>
          {match[6]}
        </span>,
      );
    } else if (match[7]) {
      parts.push(
        <span key={index} style={styles.brace}>
          {match[7]}
        </span>,
      );
    }
    last = match.index + match[0].length;
    index++;
  }
  if (last < text.length) parts.push(text.slice(last));

  return <>{parts}</>;
}