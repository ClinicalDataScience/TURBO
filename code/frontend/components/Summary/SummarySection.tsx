import React, { useState } from 'react';
import { SourceBadges } from '../SourceBadge';
import { ChevronDown, ChevronUp, RefreshCw, Loader2, Info } from 'lucide-react';
import type { ItemWithSources } from '../../types';
import MarkdownText from './MarkdownText';

export interface SectionProps {
  title: string;
  icon: React.ReactNode;
  children: React.ReactNode;
  sourceIds?: string[];
  sourceType?: 'fhir' | 'milvus' | 'user_input';
  expandable?: boolean;
  defaultExpanded?: boolean;
  highlight?: boolean;
  fieldName?: string;
  onRegenerate?: (fieldName: string, feedback: string) => Promise<void>;
  isRegenerating?: boolean;
  variant?: 'default' | 'inferred';
  badge?: React.ReactNode;
  footerAction?: React.ReactNode;
}

export const Section: React.FC<SectionProps> = ({
  title,
  icon,
  children,
  sourceIds = [],
  sourceType,
  expandable = false,
  defaultExpanded = true,
  highlight = false,
  fieldName,
  onRegenerate,
  isRegenerating = false,
  variant = 'default',
  badge,
  footerAction,
}) => {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const [showFeedback, setShowFeedback] = useState(false);
  const [feedbackText, setFeedbackText] = useState('');

  const isInferred = variant === 'inferred';

  const handleSubmit = async () => {
    if (!fieldName || !onRegenerate || !feedbackText.trim()) return;
    setShowFeedback(false);
    await onRegenerate(fieldName, feedbackText.trim());
    setFeedbackText('');
  };

  // Compute color classes based on variant and highlight
  let outerClasses: string;
  let iconColor: string;
  let titleColor: string;
  let contentColor: string;
  let hoverBg: string;
  let feedbackBorder: string;
  let feedbackBg: string;
  let inputBorder: string;
  let regenIdleColor: string;

  if (isInferred) {
    outerClasses = 'bg-red-50 border-red-300';
    iconColor = 'text-red-600';
    titleColor = 'text-red-900';
    contentColor = 'text-red-900';
    hoverBg = 'hover:bg-red-100/50';
    feedbackBorder = 'border-red-200';
    feedbackBg = 'bg-red-50/50';
    inputBorder = 'border-red-200';
    regenIdleColor = 'text-red-400 hover:text-red-600';
  } else if (highlight) {
    outerClasses = 'border-amber-300 bg-amber-50';
    iconColor = 'text-amber-600';
    titleColor = 'text-amber-900';
    contentColor = 'text-slate-700';
    hoverBg = 'hover:bg-amber-100/50';
    feedbackBorder = 'border-slate-100';
    feedbackBg = 'bg-slate-50';
    inputBorder = 'border-slate-200';
    regenIdleColor = 'text-slate-400 hover:text-slate-600';
  } else {
    outerClasses = 'bg-white border-slate-200';
    iconColor = 'text-slate-500';
    titleColor = 'text-slate-900';
    contentColor = 'text-slate-700';
    hoverBg = 'hover:bg-slate-50';
    feedbackBorder = 'border-slate-100';
    feedbackBg = 'bg-slate-50';
    inputBorder = 'border-slate-200';
    regenIdleColor = 'text-slate-400 hover:text-slate-600';
  }

  return (
    <div className={`break-inside-avoid rounded-lg border ${outerClasses}`}>
      <button
        className={`w-full px-4 py-3 flex items-center justify-between ${expandable ? `cursor-pointer ${hoverBg}` : 'cursor-default'}`}
        onClick={() => expandable && setExpanded(!expanded)}
      >
        <div className="flex items-center gap-2">
          <span className={iconColor}>{icon}</span>
          <h3 className={`font-semibold text-sm ${titleColor}`}>{title}</h3>
          {badge}
          {sourceIds.length > 0 && <SourceBadges sourceIds={sourceIds} maxVisible={2} sourceType={sourceType} />}
        </div>
        <div className="flex items-center gap-1">
          {fieldName && onRegenerate && (
            <span
              role="button"
              tabIndex={0}
              onClick={(e) => {
                e.stopPropagation();
                if (!isRegenerating) setShowFeedback(!showFeedback);
              }}
              onKeyDown={(e) => {
                if (e.key === 'Enter' || e.key === ' ') {
                  e.stopPropagation();
                  if (!isRegenerating) setShowFeedback(!showFeedback);
                }
              }}
              className={`p-1 rounded transition-colors ${isRegenerating ? 'text-blue-500' : regenIdleColor}`}
              title="Regenerate this section with feedback"
            >
              {isRegenerating ? <Loader2 className="w-3.5 h-3.5 animate-spin" /> : <RefreshCw className="w-3.5 h-3.5" />}
            </span>
          )}
          {expandable && (
            <span className="text-slate-400">
              {expanded ? <ChevronUp className="w-4 h-4" /> : <ChevronDown className="w-4 h-4" />}
            </span>
          )}
        </div>
      </button>
      {showFeedback && !isRegenerating && (
        <div className={`px-4 py-3 border-t ${feedbackBorder} ${feedbackBg}`} onClick={(e) => e.stopPropagation()}>
          <textarea
            value={feedbackText}
            onChange={(e) => setFeedbackText(e.target.value)}
            placeholder="What should be corrected? e.g., 'The staging is wrong, it should be T2N1M0' or 'Missing the recent CT scan from January'"
            className={`w-full text-sm border ${inputBorder} rounded-md p-2 resize-none focus:outline-none focus:ring-1 focus:ring-blue-400 focus:border-blue-400`}
            rows={2}
            autoFocus
          />
          <div className="flex gap-2 mt-2">
            <button
              onClick={handleSubmit}
              disabled={!feedbackText.trim()}
              className="px-3 py-1.5 text-xs font-medium text-white bg-blue-600 hover:bg-blue-700 disabled:bg-slate-300 disabled:cursor-not-allowed rounded-md transition-colors"
            >
              Regenerate
            </button>
            <button
              onClick={() => { setShowFeedback(false); setFeedbackText(''); }}
              className="px-3 py-1.5 text-xs font-medium text-slate-600 hover:text-slate-800 hover:bg-slate-200 rounded-md transition-colors"
            >
              Cancel
            </button>
          </div>
        </div>
      )}
      {(!expandable || expanded) && (
        <div className={`px-4 pb-4 pt-1 text-sm ${contentColor} ${isRegenerating ? 'opacity-50 pointer-events-none' : ''}`}>
          {children}
          {footerAction}
        </div>
      )}
    </div>
  );
};

/** Self-contained "LLM inferred" info icon with hover tooltip */
export const InferredInfoIcon: React.FC<{ tooltip?: string }> = ({
  tooltip = 'This section is inferred by the LLM from available clinical data, not directly quoted.',
}) => {
  const [show, setShow] = useState(false);
  const [pos, setPos] = useState<{ left: number; top: number }>({ left: 0, top: 0 });

  const handleEnter = (e: React.MouseEvent | React.FocusEvent) => {
    const rect = (e.currentTarget as HTMLElement).getBoundingClientRect();
    setPos({ left: rect.left + rect.width / 2, top: rect.bottom + 8 });
    setShow(true);
  };

  return (
    <>
      <span
        className="inline-flex items-center"
        title={tooltip}
        onMouseEnter={handleEnter}
        onMouseLeave={() => setShow(false)}
        onFocus={handleEnter}
        onBlur={() => setShow(false)}
      >
        <Info className="w-3.5 h-3.5 text-red-500" />
      </span>
      {show && (
        <div
          className="pointer-events-none fixed z-50 w-80 -translate-x-1/2 rounded-lg border border-slate-200 bg-white px-3 py-2 text-sm text-slate-700 shadow-xl"
          style={{ left: pos.left, top: pos.top }}
        >
          {tooltip}
        </div>
      )}
    </>
  );
};

/** "LLM inferred" badge pill */
export const LLMInferredBadge: React.FC = () => (
  <span className="px-2 py-0.5 rounded text-xs font-medium bg-red-100 text-red-700">
    LLM inferred
  </span>
);

/** Safely resolve an ItemWithSources -- handles both new format and legacy plain strings */
export const resolveItem = (item: ItemWithSources | string): { text: string; source_ids: string[] } => {
  if (typeof item === 'string') return { text: item, source_ids: [] };
  return { text: item.text, source_ids: item.source_ids || [] };
};

/** Render a bulleted list of ItemWithSources with inline source badges */
export const InlineSourceList: React.FC<{ items: (ItemWithSources | string)[]; className?: string }> = ({ items, className = '' }) => (
  <ul className={`list-disc list-inside space-y-1 ${className}`}>
    {items.map((raw, i) => {
      const item = resolveItem(raw);
      return (
        <li key={i} className="leading-relaxed">
          <MarkdownText>{item.text}</MarkdownText>
          {item.source_ids.length > 0 && (
            <SourceBadges sourceIds={item.source_ids} maxVisible={1} sourceType="fhir" className="ml-1 inline-flex" />
          )}
        </li>
      );
    })}
  </ul>
);
