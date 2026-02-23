import React from 'react';
import { X, Zap, Loader2, Check, Trash2, Clock } from 'lucide-react';
import type { CacheEntry, SummaryProgress } from '../../services/api';
import type { Patient } from '../../types';

export interface CacheModalProps {
  patient: Patient;
  entries: CacheEntry[];
  loading: boolean;
  generateStatus: 'idle' | 'loading' | 'success' | 'error';
  generateError: string;
  generateStatusMessage: string;
  generateProgress: SummaryProgress | null;
  newClinicalQuestion: string;
  onNewClinicalQuestionChange: (value: string) => void;
  onClose: () => void;
  onGenerate: (clinicalQuestion?: string) => void;
  onCancelGeneration: () => void;
  onDeleteEntry: (entryId: number) => void;
  onUseEntry: (entry: CacheEntry) => void;
}

export const CacheModal: React.FC<CacheModalProps> = ({
  patient,
  entries,
  loading,
  generateStatus,
  generateError,
  generateStatusMessage,
  generateProgress,
  newClinicalQuestion,
  onNewClinicalQuestionChange,
  onClose,
  onGenerate,
  onCancelGeneration,
  onDeleteEntry,
  onUseEntry,
}) => {
  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50" onClick={onClose}>
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 overflow-hidden max-h-[80vh] flex flex-col" onClick={(e) => e.stopPropagation()}>
        {/* Header */}
        <div className="p-5 border-b border-slate-100 flex items-center justify-between shrink-0">
          <div className="flex items-center gap-3">
            <div className="bg-amber-100 p-2 rounded-lg">
              <Zap className="w-5 h-5 text-amber-600" />
            </div>
            <div>
              <h3 className="font-semibold text-slate-900">Prepared Summaries</h3>
              <p className="text-sm text-slate-500">{patient.name}</p>
            </div>
          </div>
          <button onClick={onClose} className="p-1.5 hover:bg-slate-100 rounded-full transition-colors">
            <X className="w-4 h-4 text-slate-400" />
          </button>
        </div>

        {/* Content */}
        <div className="overflow-y-auto flex-1 p-5">
          {/* Existing entries */}
          {loading ? (
            <div className="flex items-center justify-center py-6">
              <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
              <span className="ml-2 text-sm text-slate-400">Loading...</span>
            </div>
          ) : entries.length > 0 ? (
            <div className="space-y-2 mb-5">
              <h4 className="text-xs font-medium text-slate-400 uppercase tracking-wide">Cached Summaries</h4>
              {entries.map((entry) => (
                <div
                  key={entry.id}
                  className="border border-slate-200 rounded-lg p-3 flex items-start justify-between gap-3 hover:border-slate-300 transition-colors"
                >
                  <div className="flex-1 min-w-0">
                    <p className="text-sm text-slate-800 font-medium truncate">
                      {entry.clinical_question || 'General review (no question)'}
                    </p>
                    <div className="flex items-center gap-1.5 mt-1 text-xs text-slate-400">
                      <Clock className="w-3 h-3" />
                      {new Date(entry.created_at).toLocaleString()}
                    </div>
                  </div>
                  <div className="flex items-center gap-1.5 shrink-0">
                    <button
                      onClick={() => onUseEntry(entry)}
                      className="px-3 py-1.5 text-xs bg-lmu-green text-white rounded-lg hover:bg-lmu-green-dark transition-colors"
                    >
                      Use
                    </button>
                    <button
                      onClick={() => onDeleteEntry(entry.id)}
                      className="p-1.5 text-slate-400 hover:text-red-600 hover:bg-red-50 rounded-lg transition-colors"
                      title="Delete this cached summary"
                    >
                      <Trash2 className="w-3.5 h-3.5" />
                    </button>
                  </div>
                </div>
              ))}
            </div>
          ) : (
            <div className="text-center py-4 text-sm text-slate-400 mb-4">
              No prepared summaries yet.
            </div>
          )}

          {/* Generate new */}
          <div className="border-t border-slate-100 pt-4">
            <h4 className="text-xs font-medium text-slate-400 uppercase tracking-wide mb-2">Generate New</h4>

            {generateStatus === 'success' ? (
              <div className="flex items-center gap-2 py-3 text-green-600">
                <Check className="w-5 h-5" />
                <span className="text-sm font-medium">Summary prepared successfully!</span>
              </div>
            ) : generateStatus === 'loading' ? (
              (() => {
                const pct = generateProgress
                  ? Math.round((generateProgress.current / generateProgress.total) * 100)
                  : 0;
                return (
                  <div className="flex flex-col items-center py-6 px-4">
                    <Loader2 className="w-6 h-6 animate-spin text-lmu-green mb-3" />
                    <div className="w-full max-w-sm">
                      <div className="flex items-center justify-between mb-1.5">
                        <span className="text-sm text-slate-600">{generateStatusMessage}</span>
                        {generateProgress && (
                          <span className="text-xs font-medium text-slate-500">{pct}%</span>
                        )}
                      </div>
                      <div className="w-full h-2 bg-slate-200 rounded-full overflow-hidden">
                        <div
                          className="h-full bg-lmu-green rounded-full transition-all duration-500 ease-out"
                          style={{ width: generateProgress ? `${pct}%` : '0%' }}
                        />
                      </div>
                      {generateProgress && (
                        <p className="text-xs text-slate-400 mt-1.5 text-center">
                          Step {generateProgress.current} of {generateProgress.total}
                        </p>
                      )}
                    </div>
                    <button
                      onClick={onCancelGeneration}
                      className="mt-3 flex items-center gap-1.5 px-3 py-1.5 text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors"
                      title="Cancel generation"
                    >
                      <X className="w-3.5 h-3.5" />
                      Cancel
                    </button>
                  </div>
                );
              })()
            ) : (
              <>
                {/* Base question generate button */}
                <p className="text-xs text-slate-500 mb-3">
                  The base summary addresses: next steps, additional diagnostics, therapy adequacy, and whether to switch therapy.
                </p>
                <button
                  onClick={() => onGenerate('')}
                  className="w-full px-4 py-2.5 text-sm bg-lmu-green text-white rounded-lg hover:bg-lmu-green-dark transition-colors flex items-center justify-center gap-2 mb-4"
                >
                  <Zap className="w-4 h-4" />
                  Generate Summary
                </button>

                {/* Optional additional question */}
                <div className="border-t border-slate-100 pt-3">
                  <label className="block text-xs text-slate-400 mb-1.5">Or add a specific question:</label>
                  <textarea
                    value={newClinicalQuestion}
                    onChange={(e) => onNewClinicalQuestionChange(e.target.value)}
                    placeholder="e.g. Recommendation for next-line therapy after progression..."
                    className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-lmu-green/20 focus:border-lmu-green resize-none"
                    rows={2}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' && !e.shiftKey) {
                        e.preventDefault();
                        if (newClinicalQuestion.trim()) onGenerate();
                      }
                    }}
                  />
                  {generateStatus === 'error' && (
                    <div className="mt-2 p-2 bg-red-50 border border-red-200 rounded-lg text-sm text-red-700">
                      {generateError}
                    </div>
                  )}
                  <div className="flex justify-end mt-2">
                    <button
                      onClick={() => onGenerate()}
                      disabled={!newClinicalQuestion.trim()}
                      className="px-4 py-2 text-sm bg-slate-700 text-white rounded-lg hover:bg-slate-800 transition-colors disabled:opacity-50 disabled:cursor-not-allowed flex items-center gap-2"
                    >
                      <Zap className="w-4 h-4" />
                      Generate with Question
                    </button>
                  </div>
                </div>
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
