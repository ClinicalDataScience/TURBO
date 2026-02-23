import React, { useState, useEffect, useRef, useCallback } from 'react';
import { useNavigate } from 'react-router-dom';
import { Search, Users, Plus, Loader2, RefreshCw, Database, AlertCircle } from 'lucide-react';
import turboLogo from '../../turbo-logo.svg';
import { getPatientList, checkHealth, deleteSummaryCache, checkSummaryCache, startGeneration, getGenerationProgress, getGenerationStatus, cancelGeneration } from '../../services/api';
import type { CacheEntry, SummaryProgress } from '../../services/api';
import type { Patient } from '../../types';
import { PatientCard } from './PatientCard';
import { CacheModal } from './CacheModal';


const PatientList: React.FC = () => {
  const navigate = useNavigate();
  const [searchTerm, setSearchTerm] = useState('');
  const [patients, setPatients] = useState<Patient[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [apiConnected, setApiConnected] = useState(false);
  const [showManualEntry, setShowManualEntry] = useState(false);
  const [manualPatientId, setManualPatientId] = useState('');

  // Cache state per patient (patient_id -> cache entries)
  const [patientCaches, setPatientCaches] = useState<Map<string, CacheEntry[]>>(new Map());

  // Modal state
  const [modalPatient, setModalPatient] = useState<Patient | null>(null);
  const [modalEntries, setModalEntries] = useState<CacheEntry[]>([]);
  const [modalLoading, setModalLoading] = useState(false);
  // Generation state
  const [newClinicalQuestion, setNewClinicalQuestion] = useState('');
  const [generateStatus, setGenerateStatus] = useState<'idle' | 'loading' | 'success' | 'error'>('idle');
  const [generateError, setGenerateError] = useState('');
  const [generateStatusMessage, setGenerateStatusMessage] = useState('');
  const [generateProgress, setGenerateProgress] = useState<SummaryProgress | null>(null);
  // Track failed patients
  const [failedPatients, setFailedPatients] = useState<Set<string>>(new Set());
  // Track patients with active generation (for row badges)
  const [generatingPatients, setGeneratingPatients] = useState<Set<string>>(new Set());

  const abortRef = React.useRef<AbortController | null>(null);
  const pollRef = useRef<ReturnType<typeof setInterval> | null>(null);
  const bgPollRef = useRef<ReturnType<typeof setInterval> | null>(null);

  // Poll for generation progress (modal-focused, updates progress bar)
  const startPolling = useCallback((patientId: string, clinicalQuestion?: string) => {
    if (pollRef.current) clearInterval(pollRef.current);
    pollRef.current = setInterval(async () => {
      try {
        const data = await getGenerationProgress(patientId, clinicalQuestion);

        if (data.status === 'in_progress') {
          setGenerateStatusMessage(data.message || 'Generating patient summary...');
          if (data.current_step && data.total_steps) {
            setGenerateProgress({
              current: data.current_step,
              total: data.total_steps,
              field: data.current_field || '',
            });
          }
        } else if (data.status === 'completed') {
          // Generation finished — stop polling, refresh cache
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setGenerateStatus('success');
          setGeneratingPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
          setFailedPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
          const result = await checkSummaryCache(patientId);
          setModalEntries(result.entries);
          setPatientCaches(prev => {
            const next = new Map(prev);
            next.set(patientId, result.entries);
            return next;
          });
          setNewClinicalQuestion('');
          setTimeout(() => setGenerateStatus('idle'), 1500);
        } else if (data.status === 'stale') {
          // Progress table says stale — double-check in-memory task before declaring failure
          try {
            const { active } = await getGenerationStatus(patientId);
            if (active.length > 0) {
              // Task is still running, just slow on current field — keep polling
              return;
            }
          } catch {
            // Can't reach status endpoint — assume stale
          }
          // Actually stale
          if (pollRef.current) clearInterval(pollRef.current);
          pollRef.current = null;
          setGenerateStatus('error');
          setGenerateError(data.message || 'Generation appears to have stalled. Please retry.');
          setGeneratingPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
          setFailedPatients(prev => new Set(prev).add(patientId));
        }
      } catch {
        // Polling error — keep trying
      }
    }, 3000);
  }, []);

  // Clean up polls on unmount
  useEffect(() => {
    return () => {
      if (pollRef.current) clearInterval(pollRef.current);
      if (bgPollRef.current) clearInterval(bgPollRef.current);
    };
  }, []);

  // Background polling for generating patients (row badge updates when modal is closed)
  useEffect(() => {
    if (generatingPatients.size === 0) {
      if (bgPollRef.current) { clearInterval(bgPollRef.current); bgPollRef.current = null; }
      return;
    }

    if (bgPollRef.current) clearInterval(bgPollRef.current);
    bgPollRef.current = setInterval(async () => {
      for (const patientId of generatingPatients) {
        try {
          const data = await getGenerationProgress(patientId);
          if (data.status === 'completed' || data.status === 'not_started') {
            setGeneratingPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
            // Refresh cache for this patient
            const result = await checkSummaryCache(patientId);
            setPatientCaches(prev => {
              const next = new Map(prev);
              if (result.entries.length > 0) {
                next.set(patientId, result.entries);
              }
              return next;
            });
            setFailedPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
          } else if (data.status === 'stale') {
            // Double-check in-memory task before declaring failure
            try {
              const { active } = await getGenerationStatus(patientId);
              if (active.length > 0) continue; // still running, just slow
            } catch { /* assume stale */ }
            setGeneratingPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
            setFailedPatients(prev => new Set(prev).add(patientId));
          }
        } catch {
          // skip
        }
      }
    }, 5000);

    return () => { if (bgPollRef.current) { clearInterval(bgPollRef.current); bgPollRef.current = null; } };
  }, [generatingPatients]);

  const loadPatients = async (signal?: AbortSignal) => {
    setLoading(true);
    setError(null);

    try {
      await checkHealth(signal);
      setApiConnected(true);

      const patientList = await getPatientList(signal);

      if (signal?.aborted) return;

      setPatients(patientList);
      // Load cache status and check for active generations for all patients
      for (const p of patientList) {
        checkSummaryCache(p.id)
          .then((result) => {
            if (signal?.aborted) return;
            if (result.entries.length > 0) {
              setPatientCaches(prev => {
                const next = new Map(prev);
                next.set(p.id, result.entries);
                return next;
              });
            }
          })
          .catch(() => {});
        // Check for in-progress generation (for row badge on page reload)
        getGenerationProgress(p.id)
          .then((progress) => {
            if (signal?.aborted) return;
            if (progress.status === 'in_progress') {
              setGeneratingPatients(prev => new Set(prev).add(p.id));
            }
          })
          .catch(() => {});
      }
    } catch (e) {
      if (e instanceof DOMException && e.name === 'AbortError') return;
      console.error('Failed to connect to API:', e);
      setApiConnected(false);
      setPatients([]);
    } finally {
      if (!signal?.aborted) setLoading(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    abortRef.current = controller;
    loadPatients(controller.signal);
    return () => controller.abort();
  }, []);

  const handleManualEntry = () => {
    if (manualPatientId.trim()) {
      navigate(`/dashboard/${encodeURIComponent(manualPatientId.trim())}`);
    }
  };

  const handleNavigate = (patientId: string) => {
    navigate(`/dashboard/${patientId}`);
  };

  const openModal = async (patient: Patient) => {
    setModalPatient(patient);
    setModalEntries(patientCaches.get(patient.id) || []);
    setNewClinicalQuestion('');
    setGenerateStatus('idle');
    setGenerateError('');
    setGenerateProgress(null);
    setGenerateStatusMessage('');
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    // Refresh entries from server + check for active generations
    setModalLoading(true);
    try {
      const [cacheResult, progressResult] = await Promise.all([
        checkSummaryCache(patient.id),
        getGenerationProgress(patient.id),
      ]);
      setModalEntries(cacheResult.entries);
      setPatientCaches(prev => {
        const next = new Map(prev);
        if (cacheResult.entries.length > 0) {
          next.set(patient.id, cacheResult.entries);
        } else {
          next.delete(patient.id);
        }
        return next;
      });
      // Resume loading state if generation is in progress
      if (progressResult.status === 'in_progress') {
        setGenerateStatus('loading');
        setGenerateStatusMessage(progressResult.message || 'Generating patient summary...');
        if (progressResult.current_step && progressResult.total_steps) {
          setGenerateProgress({
            current: progressResult.current_step,
            total: progressResult.total_steps,
            field: progressResult.current_field || '',
          });
        }
        startPolling(patient.id);
      }
    } catch {
      // Retain existing state on error
    } finally {
      setModalLoading(false);
    }
  };

  const closeModal = () => {
    setModalPatient(null);
    setGenerateStatus('idle');
    setGenerateProgress(null);
    setGenerateStatusMessage('');
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    // Do NOT cancel backend generation — it continues in the background
  };

  const handleGenerate = async (clinicalQuestion?: string) => {
    if (!modalPatient) return;
    const question = clinicalQuestion ?? newClinicalQuestion.trim();
    setGenerateStatus('loading');
    setGenerateError('');
    setGenerateStatusMessage('Starting generation...');
    setGenerateProgress(null);
    const patientId = modalPatient.id;

    try {
      const result = await startGeneration(patientId, question || undefined, true); // skip_cache — always regenerate
      if (result.status === 'cached') {
        // Cached result returned; display immediately
        setGenerateStatus('success');
        const cacheResult = await checkSummaryCache(patientId);
        setModalEntries(cacheResult.entries);
        setPatientCaches(prev => { const next = new Map(prev); next.set(patientId, cacheResult.entries); return next; });
        setNewClinicalQuestion('');
        setTimeout(() => setGenerateStatus('idle'), 1500);
        return;
      }
      // Generation started or already running — track and start polling
      setGeneratingPatients(prev => new Set(prev).add(patientId));
      setFailedPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
      startPolling(patientId, question || undefined);
    } catch (e) {
      setGenerateStatus('error');
      setGenerateError(e instanceof Error ? e.message : 'Generation failed');
      setFailedPatients(prev => new Set(prev).add(patientId));
    }
  };

  const handleCancelGeneration = async () => {
    if (!modalPatient) return;
    const patientId = modalPatient.id;
    // Reset UI immediately so the old progress bar doesn't linger
    if (pollRef.current) { clearInterval(pollRef.current); pollRef.current = null; }
    setGenerateStatus('idle');
    setGenerateProgress(null);
    setGenerateStatusMessage('');
    setGeneratingPatients(prev => { const next = new Set(prev); next.delete(patientId); return next; });
    // Fire-and-forget backend cancel
    cancelGeneration(patientId).catch(() => {});
  };

  const handleDeleteEntry = async (entryId: number) => {
    if (!modalPatient) return;
    const patientId = modalPatient.id;
    try {
      await deleteSummaryCache(patientId, entryId);
      const updated = modalEntries.filter(e => e.id !== entryId);
      setModalEntries(updated);
      setPatientCaches(prev => {
        const next = new Map(prev);
        if (updated.length > 0) {
          next.set(patientId, updated);
        } else {
          next.delete(patientId);
        }
        return next;
      });
    } catch {}
  };

  const handleUseEntry = (entry: CacheEntry) => {
    if (!modalPatient) return;
    navigate(`/dashboard/${modalPatient.id}`, {
      state: { clinicalQuestion: entry.clinical_question },
    });
  };

  const filteredPatients = patients.filter(p =>
    p.name.toLowerCase().includes(searchTerm.toLowerCase()) ||
    p.internalId.toLowerCase().includes(searchTerm.toLowerCase()) ||
    p.id.toLowerCase().includes(searchTerm.toLowerCase())
  );

  const getCacheCount = (patientId: string) => {
    return patientCaches.get(patientId)?.length || 0;
  };

  return (
    <div className="min-h-screen bg-lmu-gray p-8">
      <div className="max-w-5xl mx-auto">
        <header className="mb-10">
          <div className="flex items-center gap-3 mb-2">
            <img src={turboLogo} alt="Tumor Board Assistant" className="h-12 w-auto -mt-3" />
            <h1 className="text-2xl font-bold text-slate-900 tracking-tight">Tumor Board Assistant</h1>
          </div>
          <p className="text-slate-500 mt-1">Select a patient to review extracted clinical insights.</p>

          {/* API Status */}
          <div className="mt-4 flex items-center gap-2">
            {apiConnected ? (
              <span className="flex items-center gap-1.5 text-xs text-green-600 bg-green-50 px-2 py-1 rounded-full">
                <Database className="w-3 h-3" />
                Connected to API
              </span>
            ) : (
              <span className="flex items-center gap-1.5 text-xs text-amber-600 bg-amber-50 px-2 py-1 rounded-full">
                <AlertCircle className="w-3 h-3" />
                API offline - using demo data
              </span>
            )}
          </div>
        </header>

        <div className="bg-white rounded-xl shadow-sm border border-slate-200 overflow-hidden">
          {/* Toolbar */}
          <div className="p-6 border-b border-slate-100 flex flex-wrap items-center justify-between gap-4">
            <div className="flex items-center gap-4">
              <h2 className="text-lg font-medium flex items-center gap-2">
                <Users className="w-5 h-5 text-slate-400" />
                Active Cases
              </h2>
              <button
                onClick={() => {
                  abortRef.current?.abort();
                  const controller = new AbortController();
                  abortRef.current = controller;
                  loadPatients(controller.signal);
                }}
                className="p-2 text-slate-400 hover:text-slate-600 hover:bg-slate-100 rounded-full transition-colors"
                title="Refresh patient list"
              >
                <RefreshCw className="w-4 h-4" />
              </button>
            </div>

            <div className="flex items-center gap-3">
              <div className="relative w-72">
                <Search className="absolute left-3 top-1/2 -translate-y-1/2 w-4 h-4 text-slate-400" />
                <input
                  type="text"
                  placeholder="Search by name or ID..."
                  className="w-full pl-9 pr-4 py-2 border border-slate-200 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-lmu-green/20 focus:border-lmu-green transition-all"
                  value={searchTerm}
                  onChange={(e) => setSearchTerm(e.target.value)}
                />
              </div>

              <button
                onClick={() => setShowManualEntry(!showManualEntry)}
                className="flex items-center gap-2 px-4 py-2 bg-lmu-green text-white text-sm rounded-lg hover:bg-lmu-green-dark transition-colors"
              >
                <Plus className="w-4 h-4" />
                Enter Patient ID
              </button>
            </div>
          </div>

          {/* Manual Entry */}
          {showManualEntry && (
            <div className="p-6 bg-lmu-green-50 border-b border-lmu-green-100">
              <div className="flex items-center gap-3">
                <input
                  type="text"
                  placeholder="Enter FHIR Patient ID..."
                  className="flex-1 max-w-md px-4 py-2 border border-lmu-green-100 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-lmu-green/20 focus:border-lmu-green"
                  value={manualPatientId}
                  onChange={(e) => setManualPatientId(e.target.value)}
                  onKeyDown={(e) => e.key === 'Enter' && handleManualEntry()}
                />
                <button
                  onClick={handleManualEntry}
                  disabled={!manualPatientId.trim()}
                  className="px-4 py-2 bg-lmu-green text-white text-sm rounded-lg hover:bg-lmu-green-dark transition-colors disabled:opacity-50"
                >
                  Open Dashboard
                </button>
                <button
                  onClick={() => setShowManualEntry(false)}
                  className="px-4 py-2 text-slate-600 text-sm hover:bg-slate-100 rounded-lg transition-colors"
                >
                  Cancel
                </button>
              </div>
              <p className="text-xs text-lmu-green mt-2">
                Enter the FHIR Patient ID to view their medical dashboard. Data will be fetched from the FHIR server.
              </p>
            </div>
          )}

          {/* Loading State */}
          {loading && (
            <div className="p-12 flex items-center justify-center">
              <Loader2 className="w-6 h-6 animate-spin text-lmu-green" />
              <span className="ml-3 text-slate-500">Loading patients...</span>
            </div>
          )}

          {/* Error State */}
          {error && !loading && (
            <div className="p-6 bg-red-50 border-b border-red-100">
              <div className="flex items-center gap-2 text-red-700">
                <AlertCircle className="w-5 h-5" />
                <span>{error}</span>
              </div>
            </div>
          )}

          {/* Patient List */}
          {!loading && (
            <div className="divide-y divide-slate-100">
              {filteredPatients.map(patient => (
                <PatientCard
                  key={patient.id}
                  patient={patient}
                  cacheCount={getCacheCount(patient.id)}
                  isGenerating={generatingPatients.has(patient.id)}
                  hasFailed={failedPatients.has(patient.id)}
                  onNavigate={handleNavigate}
                  onOpenModal={openModal}
                />
              ))}

              {filteredPatients.length === 0 && !loading && (
                <div className="p-12 text-center text-slate-400">
                  {searchTerm ? (
                    <>No patients found matching &quot;{searchTerm}&quot;</>
                  ) : (
                    <>No patients available. Use &quot;Enter Patient ID&quot; to access a specific patient.</>
                  )}
                </div>
              )}
            </div>
          )}
        </div>

        {/* Footer info */}
        <div className="mt-6 text-center text-sm text-slate-400">
          <p>TURBO | Powered by HAI-DEF Medical AI Platform</p>
          <p className="text-xs mt-1">Patient data sourced from FHIR server with Milvus guideline database</p>
        </div>
      </div>

      {/* Prepare / Manage Cache Modal */}
      {modalPatient && (
        <CacheModal
          patient={modalPatient}
          entries={modalEntries}
          loading={modalLoading}
          generateStatus={generateStatus}
          generateError={generateError}
          generateStatusMessage={generateStatusMessage}
          generateProgress={generateProgress}
          newClinicalQuestion={newClinicalQuestion}
          onNewClinicalQuestionChange={setNewClinicalQuestion}
          onClose={closeModal}
          onGenerate={handleGenerate}
          onCancelGeneration={handleCancelGeneration}
          onDeleteEntry={handleDeleteEntry}
          onUseEntry={handleUseEntry}
        />
      )}
    </div>
  );
};

export default PatientList;
