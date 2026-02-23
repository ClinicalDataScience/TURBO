import React from 'react';
import { ChevronRight, Loader2, AlertCircle, Zap, Check } from 'lucide-react';
import type { Patient } from '../../types';

export interface PatientCardProps {
  patient: Patient;
  cacheCount: number;
  isGenerating: boolean;
  hasFailed: boolean;
  onNavigate: (patientId: string) => void;
  onOpenModal: (patient: Patient) => void;
}

export const PatientCard: React.FC<PatientCardProps> = ({
  patient,
  cacheCount,
  isGenerating,
  hasFailed,
  onNavigate,
  onOpenModal,
}) => {
  return (
    <div
      className="p-6 hover:bg-slate-50 transition-colors group flex items-center justify-between"
    >
      <div
        className="flex items-center gap-4 flex-1 cursor-pointer"
        onClick={() => onNavigate(patient.id)}
      >
        <div className="w-10 h-10 rounded-full bg-lmu-green-100 text-lmu-green-dark flex items-center justify-center font-semibold text-sm">
          {patient.name.split(' ').map(n => n[0]).join('').slice(0, 2)}
        </div>
        <div>
          <h3 className="font-medium text-slate-900 group-hover:text-lmu-green transition-colors">
            {patient.name}
          </h3>
          <div className="flex items-center gap-3 text-sm text-slate-500">
            <span className="font-mono text-xs">{patient.internalId || patient.id}</span>
            {patient.age > 0 && (
              <>
                <span className="w-1 h-1 rounded-full bg-slate-300"></span>
                <span>{patient.age} yrs</span>
              </>
            )}
            {patient.gender !== 'Unknown' && (
              <>
                <span className="w-1 h-1 rounded-full bg-slate-300"></span>
                <span>{patient.gender}</span>
              </>
            )}
          </div>
        </div>
      </div>

      <div className="flex items-center gap-4">
        {/* Prepare / cache / generating button */}
        {isGenerating ? (
          <button
            onClick={(e) => { e.stopPropagation(); onOpenModal(patient); }}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-blue-600 bg-blue-50 border border-blue-200 rounded-lg hover:bg-blue-100 transition-colors"
            title="Summary generation in progress — click to view"
          >
            <Loader2 className="w-3.5 h-3.5 animate-spin" />
            Generating...
          </button>
        ) : hasFailed && cacheCount === 0 ? (
          <button
            onClick={(e) => { e.stopPropagation(); onOpenModal(patient); }}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-red-600 bg-red-50 border border-red-200 rounded-lg hover:bg-red-100 transition-colors"
            title="Pregeneration failed - click to retry"
          >
            <AlertCircle className="w-3.5 h-3.5" />
            Failed
          </button>
        ) : cacheCount > 0 ? (
          <button
            onClick={(e) => { e.stopPropagation(); onOpenModal(patient); }}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-green-600 bg-green-50 border border-green-200 rounded-lg hover:bg-green-100 transition-colors"
            title={`${cacheCount} prepared ${cacheCount === 1 ? 'summary' : 'summaries'}`}
          >
            <Check className="w-3.5 h-3.5" />
            {cacheCount} Prepared
          </button>
        ) : (
          <button
            onClick={(e) => { e.stopPropagation(); onOpenModal(patient); }}
            className="flex items-center gap-1.5 px-2.5 py-1.5 text-xs text-amber-700 bg-amber-50 border border-amber-200 rounded-lg hover:bg-amber-100 transition-colors"
            title="Pregenerate clinical summary"
          >
            <Zap className="w-3.5 h-3.5" />
            Prepare
          </button>
        )}

        <div className="text-right">
          <div className="text-xs text-slate-400 uppercase tracking-wide font-medium">Last Updated</div>
          <div className="text-sm text-slate-700">
            {new Date(patient.lastUpdated).toLocaleDateString()}
          </div>
        </div>
        <ChevronRight
          className="w-5 h-5 text-slate-300 group-hover:text-lmu-green cursor-pointer"
          onClick={() => onNavigate(patient.id)}
        />
      </div>
    </div>
  );
};
