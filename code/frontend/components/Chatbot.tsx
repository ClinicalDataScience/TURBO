import React, { useState, useRef, useEffect } from 'react';
import { Send, Sparkles, User, Bot, RefreshCw, Database, FileText, RotateCcw, ChevronDown } from 'lucide-react';
import type { ChatMessage, QuerySourceRef, Patient } from '../types';
import { streamQuery, getPatientMetadata } from '../services/api';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import SourceBadge, { SourceBadges } from './SourceBadge';

/** Parse [SOURCE: uuid] inline citations from text. Returns cleaned text and extracted IDs. */
function parseInlineSourceCitations(text: string): { cleanText: string; sourceIds: string[] } {
  const ids: string[] = [];
  const cleanText = text.replace(/\[SOURCE:\s*([^\]]+)\]/g, (_match, group: string) => {
    group.split(',').forEach((id) => {
      const trimmed = id.trim();
      if (trimmed && !ids.includes(trimmed)) ids.push(trimmed);
    });
    return '';
  }).trim();
  return { cleanText, sourceIds: ids };
}

interface ChatbotProps {
  patientId: string;
  patientName?: string;
  initialQuestion?: string;
  welcomeMessage?: string;
  suggestions?: string[];
  queryContext?: string;
  processingMessage?: string;
  onBotResponse?: (userText: string, botText: string) => void;
}

const GUIDELINE_OPTIONS = [
  { value: 'nsclc', label: 'NSCLC Guidelines' },
  { value: 'sclc', label: 'SCLC Guidelines' },
];

const Chatbot: React.FC<ChatbotProps> = ({
  patientId,
  patientName,
  initialQuestion,
  welcomeMessage,
  suggestions: customSuggestions,
  queryContext,
  processingMessage,
  onBotResponse
}) => {
  const [input, setInput] = useState('');
  const defaultWelcome = `Hello. I'm ready to assist with **${patientName || 'this patient'}'s** case.\n\nI have access to their medical records, imaging results, and relevant clinical guidelines. How can I help?`;
  const [messages, setMessages] = useState<ChatMessage[]>([
    {
      id: 'welcome',
      role: 'assistant',
      text: welcomeMessage || defaultWelcome,
    }
  ]);
  const [isLoading, setIsLoading] = useState(false);
  const [statusMessage, setStatusMessage] = useState('');
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [followUpQuestions, setFollowUpQuestions] = useState<string[]>([]);
  const [contextWarning, setContextWarning] = useState(false);
  const [guidelineTypes, setGuidelineTypes] = useState<string[]>(['nsclc']);
  const [showGuidelineDropdown, setShowGuidelineDropdown] = useState(false);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  const abortRef = useRef<AbortController | null>(null);
  const guidelineDropdownRef = useRef<HTMLDivElement>(null);

  const defaultSuggestions = [
    "Summarize the latest imaging",
    "Check for drug interactions",
    "List key pathology findings",
    "What treatment options are available?"
  ];

  const scrollToBottom = () => {
    messagesEndRef.current?.scrollIntoView({ behavior: "smooth" });
  };

  useEffect(() => {
    scrollToBottom();
  }, [messages]);

  // Fetch patient's pre-classified guideline preference
  useEffect(() => {
    getPatientMetadata(patientId).then(meta => {
      if (meta.guideline_cancer_types?.length > 0) {
        setGuidelineTypes(meta.guideline_cancer_types);
      }
    });
  }, [patientId]);

  // Close guideline dropdown on outside click
  useEffect(() => {
    if (!showGuidelineDropdown) return;
    const handler = (e: MouseEvent) => {
      if (guidelineDropdownRef.current && !guidelineDropdownRef.current.contains(e.target as Node)) {
        setShowGuidelineDropdown(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [showGuidelineDropdown]);

  // Handle initial question from parent component
  useEffect(() => {
    if (initialQuestion) {
      handleSend(initialQuestion);
    }
  }, [initialQuestion]);

  const toggleGuidelineType = (value: string) => {
    setGuidelineTypes(prev => {
      if (prev.includes(value)) {
        // Don't allow deselecting the last item
        if (prev.length === 1) return prev;
        return prev.filter(t => t !== value);
      }
      return [...prev, value];
    });
  };

  const handleSend = async (text: string = input) => {
    if (!text.trim()) return;

    // Abort any in-flight query before starting a new one
    if (abortRef.current) {
      abortRef.current.abort();
    }
    const controller = new AbortController();
    abortRef.current = controller;
    const { signal } = controller;

    const userMsg: ChatMessage = {
      id: Date.now().toString(),
      role: 'user',
      text: text
    };

    setMessages(prev => [...prev, userMsg]);
    setInput('');
    setIsLoading(true);
    setStatusMessage(processingMessage || '');
    setFollowUpQuestions([]);
    setContextWarning(false);

    try {
      const queryText = queryContext ? `${queryContext}\n\nUser message: ${text}` : text;
      const stream = streamQuery({
        query: queryText,
        patient_id: patientId,
        conversation_id: conversationId || undefined,
        guideline_cancer_types: guidelineTypes,
      }, signal);

      for await (const event of stream) {
        if (signal.aborted) return;
        if (event.type === 'status') {
          const isGenericAnalysisStatus = event.message === 'Analyzing your question...';
          setStatusMessage(
            processingMessage && isGenericAnalysisStatus ? processingMessage : event.message
          );
        } else if (event.type === 'complete') {
          if (event.conversation_id) {
            setConversationId(event.conversation_id);
          }
          if (event.follow_up_questions?.length > 0) {
            setFollowUpQuestions(event.follow_up_questions);
          }
          const botMsg: ChatMessage = {
            id: (Date.now() + 1).toString(),
            role: 'assistant',
            text: event.answer,
            sources: event.sources
          };
          setMessages(prev => [...prev, botMsg]);

          if (onBotResponse) {
            onBotResponse(text, event.answer);
          }

          if (messages.length > 8) {
            setContextWarning(true);
          }
        } else if (event.type === 'error') {
          let errorText = event.detail || 'An error occurred. Please try again.';

          if (errorText.includes('conversation has become too long') ||
              errorText.includes('Context limit exceeded') ||
              errorText.includes('context size')) {
            errorText = `⚠️ **Conversation Too Long**\n\nThe conversation history has exceeded the AI's context limit. Please start a new conversation using the "New Conversation" button below.`;
            setContextWarning(true);
          }

          const errorMsg: ChatMessage = {
            id: (Date.now() + 1).toString(),
            role: 'assistant',
            text: errorText,
            isError: true
          };
          setMessages(prev => [...prev, errorMsg]);
        }
      }
    } catch (error) {
      // Silently ignore aborted requests — a newer query is taking over
      if (error instanceof DOMException && error.name === 'AbortError') return;
      if (signal.aborted) return;
      const errorText = `I'm sorry, I encountered an error while processing your request. ${error instanceof Error ? error.message : 'Please try again.'}`;
      const errorMsg: ChatMessage = {
        id: (Date.now() + 1).toString(),
        role: 'assistant',
        text: errorText,
        isError: true
      };
      setMessages(prev => [...prev, errorMsg]);
    } finally {
      // Only clear loading state if this request wasn't aborted
      if (!signal.aborted) {
        setIsLoading(false);
        setStatusMessage('');
      }
    }
  };

  const handleNewConversation = () => {
    setMessages([
      {
        id: 'welcome',
        role: 'assistant',
        text: welcomeMessage || defaultWelcome,
      }
    ]);
    setConversationId(null);
    setFollowUpQuestions([]);
    setContextWarning(false);
    setInput('');
  };

  const renderSources = (sources: QuerySourceRef[], inlineSourceIds: string[] = []) => {
    const hasStructured = sources && sources.length > 0;
    const hasInline = inlineSourceIds.length > 0;
    if (!hasStructured && !hasInline) return null;

    // Deduplicate inline IDs against structured source IDs
    const structuredIds = new Set(sources?.map(s => s.source_id) ?? []);
    const extraIds = inlineSourceIds.filter(id => !structuredIds.has(id));
    const total = (sources?.length ?? 0) + extraIds.length;

    return (
      <div className="mt-3 pt-3 border-t border-slate-100">
        <div className="text-xs text-slate-500 font-medium mb-2 flex items-center gap-1">
          <Database className="w-3 h-3" />
          Sources ({total})
        </div>
        <div className="flex flex-wrap gap-1">
          {(sources ?? []).map((source, idx) => (
            <SourceBadge
              key={source.source_id}
              sourceId={source.source_id}
              label={source.title?.slice(0, 15) || `Source ${idx + 1}`}
              sourceType={source.source_type as 'fhir' | 'milvus' | 'user_input'}
            />
          ))}
          {extraIds.length > 0 && (
            <SourceBadges sourceIds={extraIds} maxVisible={5} />
          )}
        </div>
      </div>
    );
  };

  const suggestions = followUpQuestions.length > 0 ? followUpQuestions : (customSuggestions || defaultSuggestions);

  return (
    <div className="flex flex-col h-full bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
      {/* Header */}
      <div className="p-4 border-b border-slate-100 bg-slate-50 flex items-center justify-between">
        <div className="flex items-center gap-2">
          <Sparkles className="w-4 h-4 text-lmu-green" />
          <div>
            <h3 className="text-sm font-semibold text-slate-700 uppercase tracking-wide">TURBO Assistant</h3>
            <p className="text-[10px] text-slate-500 uppercase tracking-wide">Powered by HAI-DEF</p>
          </div>
        </div>
        <button
          onClick={handleNewConversation}
          className="flex items-center gap-1.5 px-3 py-1.5 text-xs text-slate-600 hover:text-lmu-green hover:bg-lmu-green-50 rounded-lg transition-colors"
          title="Start a new conversation"
        >
          <RotateCcw className="w-3.5 h-3.5" />
          <span className="hidden sm:inline">New</span>
        </button>
      </div>

      {/* Context Warning Banner */}
      {contextWarning && (
        <div className="mx-4 mt-3 p-3 bg-amber-50 border border-amber-200 rounded-lg flex items-start gap-2">
          <div className="text-amber-600 text-xs flex-1">
            <strong>⚠️ Long Conversation:</strong> Consider starting a new conversation if responses become slow or errors occur.
          </div>
          <button
            onClick={handleNewConversation}
            className="text-xs text-amber-700 hover:text-amber-900 font-medium underline"
          >
            Reset Now
          </button>
        </div>
      )}

      {/* Messages */}
      <div className="flex-1 overflow-y-auto p-4 space-y-5 bg-slate-50/50">
        {messages.map((msg) => (
          <div
            key={msg.id}
            className={`flex gap-3 ${msg.role === 'user' ? 'flex-row-reverse' : 'flex-row'}`}
          >
            <div className={`w-8 h-8 rounded-full flex-shrink-0 flex items-center justify-center ${msg.role === 'user' ? 'bg-lmu-green text-white' : 'bg-white border border-slate-200 text-lmu-green'}`}>
              {msg.role === 'user' ? <User className="w-4 h-4" /> : <Bot className="w-5 h-5" />}
            </div>
            <div
              className={`max-w-[85%] p-3.5 rounded-2xl text-sm leading-relaxed shadow-sm ${
                msg.role === 'user'
                  ? 'bg-lmu-green text-white rounded-tr-none'
                  : msg.isError
                    ? 'bg-red-50 text-red-800 rounded-tl-none border border-red-200'
                    : 'bg-white text-slate-800 rounded-tl-none border border-slate-200'
              }`}
            >
              {msg.role === 'user' ? (
                msg.text
              ) : (() => {
                const { cleanText, sourceIds: inlineSourceIds } = parseInlineSourceCitations(msg.text);
                return (
                  <>
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        p: ({node, ...props}) => <p className="mb-2 last:mb-0 leading-relaxed" {...props} />,
                        ul: ({node, ...props}) => <ul className="list-disc pl-4 mb-2 space-y-1" {...props} />,
                        ol: ({node, ...props}) => <ol className="list-decimal pl-4 mb-2 space-y-1" {...props} />,
                        li: ({node, ...props}) => <li className="pl-1 marker:text-slate-400" {...props} />,
                        strong: ({node, ...props}) => <span className="font-semibold text-slate-900" {...props} />,
                        em: ({node, ...props}) => <em className="text-slate-600 not-italic" {...props} />,
                        h1: ({node, ...props}) => <h1 className="text-base font-bold text-slate-900 mt-3 mb-2" {...props} />,
                        h2: ({node, ...props}) => <h2 className="text-sm font-bold text-slate-900 mt-2 mb-1" {...props} />,
                        h3: ({node, ...props}) => <h3 className="text-sm font-semibold text-slate-900 mt-2 mb-1" {...props} />,
                        blockquote: ({node, ...props}) => <blockquote className="border-l-2 border-lmu-green-100 pl-3 italic text-slate-500 my-2 text-xs" {...props} />,
                        code: ({node, ...props}) => <code className="bg-slate-100 text-slate-600 px-1 py-0.5 rounded text-xs font-mono" {...props} />,
                        table: ({node, ...props}) => <div className="overflow-x-auto my-2"><table className="min-w-full text-sm border-collapse border border-slate-200 rounded" {...props} /></div>,
                        thead: ({node, ...props}) => <thead className="bg-slate-50" {...props} />,
                        tbody: ({node, ...props}) => <tbody className="divide-y divide-slate-200" {...props} />,
                        tr: ({node, ...props}) => <tr className="border-b border-slate-200" {...props} />,
                        th: ({node, ...props}) => <th className="px-3 py-1.5 text-left text-xs font-semibold text-slate-700 border border-slate-200" {...props} />,
                        td: ({node, ...props}) => <td className="px-3 py-1.5 text-xs text-slate-600 border border-slate-200" {...props} />,
                      }}
                    >
                      {cleanText}
                    </ReactMarkdown>
                    {renderSources(msg.sources ?? [], inlineSourceIds)}
                  </>
                );
              })()}
            </div>
          </div>
        ))}
        {isLoading && (
          <div className="flex gap-3">
            <div className="w-8 h-8 rounded-full bg-white border border-slate-200 flex items-center justify-center">
              <RefreshCw className="w-4 h-4 text-lmu-green animate-spin" />
            </div>
            <div className="bg-white border border-slate-200 px-4 py-3 rounded-2xl rounded-tl-none shadow-sm flex items-center gap-2">
              <span className="text-sm text-slate-500">{statusMessage || processingMessage || 'Thinking...'}</span>
            </div>
          </div>
        )}
        <div ref={messagesEndRef} />
      </div>

      {/* Suggestions */}
      {(messages.length < 3 || followUpQuestions.length > 0) && (
        <div className="px-4 pb-2 flex gap-2 overflow-x-auto hide-scrollbar">
          {suggestions.slice(0, 4).map((prompt, idx) => (
            <button
              key={idx}
              onClick={() => handleSend(prompt)}
              disabled={isLoading}
              className="whitespace-nowrap px-3 py-1.5 bg-white border border-lmu-green-100 text-lmu-green text-xs rounded-full hover:bg-lmu-green-50 transition-colors disabled:opacity-50"
            >
              {prompt}
            </button>
          ))}
        </div>
      )}

      {/* Input */}
      <div className="p-4 border-t border-slate-100 bg-white">
        {/* Guideline selector */}
        <div className="flex items-center mb-2" ref={guidelineDropdownRef}>
          <div className="relative">
            <button
              onClick={() => setShowGuidelineDropdown(v => !v)}
              className="flex items-center gap-1.5 px-2.5 py-1 text-[11px] bg-slate-100 hover:bg-slate-200 border border-slate-200 rounded-lg text-slate-600 transition-colors"
            >
              <span className="text-slate-400 font-medium">Guidelines:</span>
              <span className="font-semibold text-slate-700">
                {guidelineTypes.map(t => t.toUpperCase()).join(', ')}
              </span>
              <ChevronDown className={`w-3 h-3 text-slate-400 transition-transform ${showGuidelineDropdown ? 'rotate-180' : ''}`} />
            </button>
            {showGuidelineDropdown && (
              <div className="absolute bottom-full left-0 mb-1 bg-white border border-slate-200 rounded-lg shadow-lg z-20 py-1 min-w-[168px]">
                {GUIDELINE_OPTIONS.map(option => (
                  <label
                    key={option.value}
                    className="flex items-center gap-2.5 px-3 py-2 hover:bg-slate-50 cursor-pointer text-xs text-slate-700 select-none"
                  >
                    <input
                      type="checkbox"
                      checked={guidelineTypes.includes(option.value)}
                      onChange={() => toggleGuidelineType(option.value)}
                      className="rounded accent-lmu-green w-3.5 h-3.5"
                    />
                    {option.label}
                  </label>
                ))}
              </div>
            )}
          </div>
        </div>

        {/* Text input row */}
        <div className="relative flex items-center">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => e.key === 'Enter' && handleSend()}
            placeholder="Ask about patient data..."
            className="w-full pl-4 pr-12 py-3 bg-slate-50 border border-slate-200 rounded-xl text-sm focus:outline-none focus:ring-2 focus:ring-lmu-green/20 focus:border-lmu-green transition-all placeholder:text-slate-400"
            disabled={isLoading}
          />
          <button
            onClick={() => handleSend()}
            disabled={isLoading || !input.trim()}
            className="absolute right-2 p-2 bg-lmu-green text-white rounded-lg hover:bg-lmu-green-dark disabled:opacity-50 disabled:cursor-not-allowed transition-colors"
          >
            <Send className="w-4 h-4" />
          </button>
        </div>
      </div>
    </div>
  );
};

export default Chatbot;
