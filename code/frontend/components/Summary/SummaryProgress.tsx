import React from 'react';
import { Loader2 } from 'lucide-react';
import type { SummaryProgress as SummaryProgressType } from '../../services/api';

export interface SummaryProgressProps {
  loadingStatus: string;
  loadingProgress: SummaryProgressType | null;
}

export const SummaryProgress: React.FC<SummaryProgressProps> = ({
  loadingStatus,
  loadingProgress,
}) => {
  const pct = loadingProgress
    ? Math.round((loadingProgress.current / loadingProgress.total) * 100)
    : 0;

  return (
    <div className="flex flex-col items-center justify-center h-64 px-8">
      <Loader2 className="w-6 h-6 animate-spin text-lmu-green mb-4" />
      <div className="w-full max-w-md">
        <div className="flex items-center justify-between mb-1.5">
          <span className="text-sm text-slate-600">{loadingStatus}</span>
          {loadingProgress && (
            <span className="text-xs font-medium text-slate-500">
              {pct}%
            </span>
          )}
        </div>
        <div className="w-full h-2 bg-slate-200 rounded-full overflow-hidden">
          <div
            className="h-full bg-lmu-green rounded-full transition-all duration-500 ease-out"
            style={{ width: loadingProgress ? `${pct}%` : '0%' }}
          />
        </div>
        {loadingProgress && (
          <p className="text-xs text-slate-400 mt-1.5 text-center">
            Step {loadingProgress.current} of {loadingProgress.total}
          </p>
        )}
      </div>
    </div>
  );
};
