import React from 'react';
import ReactMarkdown, { type Components } from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { SourceBadges } from '../SourceBadge';

/**
 * Regex to match inline source references in various formats:
 *   (Source: <uuid>, ...)
 *   (source_id: <uuid>, ...)
 *   [SOURCE: <uuid>, ...]
 * UUIDs are standard 8-4-4-4-12 hex format.
 */
const SOURCE_RE = /(?:\((?:Source|source_id):\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\s*,\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})*)\)|\[SOURCE:\s*([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}(?:\s*,\s*[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})*)\])/gi;

interface MarkdownTextProps {
  /** The raw text (may contain markdown and (Source: uuid) references). */
  children: string;
  /** Render inline (no wrapping block elements). Default: true. */
  inline?: boolean;
  className?: string;
}

interface ProcessedLine {
  text: string;
  sourceIds: string[];
}

/** Strip source refs from each non-empty line, collecting IDs per line. */
function processLines(text: string): { lines: ProcessedLine[]; hasAnySources: boolean } {
  const lines: ProcessedLine[] = [];
  let hasAnySources = false;

  for (const raw of text.split('\n')) {
    if (!raw.trim()) continue;
    const ids: string[] = [];
    const cleaned = raw.replace(SOURCE_RE, (_m, parenGroup: string | undefined, bracketGroup: string | undefined) => {
      const captured = parenGroup || bracketGroup || '';
      captured.split(',').map((s) => s.trim()).filter(Boolean).forEach((id) => ids.push(id));
      return '';
    }).trimEnd();
    if (!cleaned.trim()) continue;
    const uniqueIds = [...new Set(ids)];
    if (uniqueIds.length > 0) hasAnySources = true;
    lines.push({ text: cleaned, sourceIds: uniqueIds });
  }

  return { lines, hasAnySources };
}

/**
 * Component overrides that collapse all block-level markdown elements into
 * inline equivalents so everything stays on a single line.
 */
const INLINE_COMPONENTS: Components = {
  p: ({ children }) => <>{children}</>,
  div: ({ children }) => <>{children}</>,
  blockquote: ({ children }) => <span>{children}</span>,
  ul: ({ children }) => <span>{children}</span>,
  ol: ({ children }) => <span>{children}</span>,
  li: ({ children }) => <span>{children} </span>,
  h1: ({ children }) => <strong>{children}</strong>,
  h2: ({ children }) => <strong>{children}</strong>,
  h3: ({ children }) => <strong>{children}</strong>,
  h4: ({ children }) => <strong>{children}</strong>,
  h5: ({ children }) => <strong>{children}</strong>,
  h6: ({ children }) => <strong>{children}</strong>,
};

/**
 * Component overrides for block-mode rendering that apply compact Tailwind
 * styles to lists and paragraphs (Tailwind preflight strips all defaults).
 */
const BLOCK_COMPONENTS: Components = {
  ul: ({ children }) => <ul className="list-disc pl-5 space-y-0.5">{children}</ul>,
  ol: ({ children }) => <ol className="list-decimal pl-5 space-y-0.5">{children}</ol>,
  li: ({ children }) => <li className="leading-snug">{children}</li>,
  p: ({ children }) => <p>{children}</p>,
};

/**
 * Renders a string as markdown with source badges at the end of each line.
 *
 * Any `(Source: <uuid>)` or `(source_id: <uuid>)` references are stripped from
 * the text and rendered as hoverable SourceBadge icons at the end of their line.
 *
 * - Inline / single-line: badges appear at the end of the text.
 * - Block / multi-line with sources: each line becomes a bullet with badges at
 *   the end (like InlineSourceList).
 */
const MarkdownText: React.FC<MarkdownTextProps> = ({ children: text, inline = true, className }) => {
  if (!text) return null;

  const { lines, hasAnySources } = processLines(text);
  const components = inline ? INLINE_COMPONENTS : undefined;

  // Fast path: no source references — render markdown as-is.
  if (!hasAnySources) {
    if (inline) {
      return (
        <span className={className}>
          <ReactMarkdown remarkPlugins={[remarkGfm]} components={components}>
            {text}
          </ReactMarkdown>
        </span>
      );
    }
    return (
      <ReactMarkdown remarkPlugins={[remarkGfm]} components={BLOCK_COMPONENTS}>
        {text}
      </ReactMarkdown>
    );
  }

  // Inline or single-line: render cleaned text with all badges at the end.
  if (inline || lines.length <= 1) {
    const allIds = [...new Set(lines.flatMap((l) => l.sourceIds))];
    const cleanedText = lines.map((l) => l.text).join(' ');
    return (
      <span className={className}>
        <ReactMarkdown remarkPlugins={[remarkGfm]} components={INLINE_COMPONENTS}>
          {cleanedText}
        </ReactMarkdown>
        {allIds.length > 0 && (
          <SourceBadges
            sourceIds={allIds}
            maxVisible={2}
            sourceType="fhir"
            className="inline-flex ml-0.5 align-middle"
          />
        )}
      </span>
    );
  }

  // Multi-line block: render as bullet list with per-line badges at end.
  return (
    <ul className={`list-disc list-inside space-y-1 ${className || ''}`}>
      {lines.map((line, i) => {
        // Strip leading markdown bullet markers to avoid double bullets
        const stripped = line.text.replace(/^\s*[-*+]\s+/, '').replace(/^\s*\d+\.\s+/, '');
        return (
          <li key={i} className="leading-relaxed">
            <ReactMarkdown remarkPlugins={[remarkGfm]} components={INLINE_COMPONENTS}>
              {stripped}
            </ReactMarkdown>
            {line.sourceIds.length > 0 && (
              <SourceBadges
                sourceIds={line.sourceIds}
                maxVisible={2}
                sourceType="fhir"
                className="ml-1 inline-flex"
              />
            )}
          </li>
        );
      })}
    </ul>
  );
};

export default MarkdownText;
