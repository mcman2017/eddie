import type { ReactNode } from "react";

/** Inline `code` and **bold** inside otherwise plain text. Nothing else is interpreted. */
function inline(text: string): ReactNode[] {
  return text.split(/(`[^`]+`|\*\*[^*]+\*\*)/).map((part, i) => {
    if (part.length > 2 && part.startsWith("`") && part.endsWith("`")) return <code key={i}>{part.slice(1, -1)}</code>;
    if (part.length > 4 && part.startsWith("**") && part.endsWith("**")) return <strong key={i}>{part.slice(2, -2)}</strong>;
    return part;
  });
}

/**
 * A small, safe subset of Markdown: paragraphs, "- " lists, "#" headings, `code` and **bold**.
 * Text is rendered as React children, so nothing in it can become markup.
 */
export function Prose({ text }: { text: string }) {
  const blocks = text.replace(/\r/g, "").split(/\n{2,}/).map((b) => b.trim()).filter(Boolean);
  return (
    <>
      {blocks.map((block, i) => {
        const lines = block.split("\n");
        if (lines.every((l) => /^[-*] /.test(l))) {
          return (
            <ul key={i}>
              {lines.map((l, j) => (
                <li key={j}>{inline(l.slice(2))}</li>
              ))}
            </ul>
          );
        }
        const heading = /^(#{1,3}) (.+)$/.exec(block);
        if (heading && lines.length === 1) return <h3 key={i}>{inline(heading[2])}</h3>;
        return <p key={i}>{inline(lines.join(" "))}</p>;
      })}
    </>
  );
}
