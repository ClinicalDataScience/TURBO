import React, { useEffect, useRef, useState, useCallback } from 'react';
import { streamGetSummary, getGenerationProgress, cancelGeneration, regenerateField, type SummaryProgress } from '../../services/api';
import type { SummaryResponse, MissingInfoItem } from '../../types';
import { SourceBadges } from '../SourceBadge';
import {
  User,
  Stethoscope,
  Activity,
  Pill,
  AlertCircle,
  Heart,
  Brain,
  FileText,
  RefreshCw,
  MessageSquare,
  X,
  Dna,
} from 'lucide-react';
import { Section, resolveItem, InlineSourceList, InferredInfoIcon, LLMInferredBadge } from './SummarySection';
import MarkdownText from './MarkdownText';
import { SummaryProgress as SummaryProgressUI } from './SummaryProgress';

interface SummaryPanelProps {
  patientId: string;
  clinicalQuestion?: string;
  onAskQuestion?: (question: string) => void;
  onMissingInfo?: (items: MissingInfoItem[]) => void;
  onOpenMissingInfoModal?: (items: MissingInfoItem[], selectedItem?: MissingInfoItem) => void;
  onSummaryLoaded?: (demographics: { name?: string | null; age?: number | null; gender?: string | null }) => void;
  refreshKey?: number;
}

/**
 * Parse [SOURCE: uuid, uuid] citations from text.
 * Returns the cleaned text and a deduplicated list of source/fhir IDs.
 */
function parseSourceCitations(text: string): { cleanText: string; sourceIds: string[] } {
  const ids: string[] = [];
  const cleanText = text.replace(/\s*\[SOURCE:\s*([^\]]+)\]/g, (_match, group: string) => {
    group.split(',').forEach((id) => {
      const trimmed = id.trim();
      if (trimmed && !ids.includes(trimmed)) ids.push(trimmed);
    });
    return '';
  });
  return { cleanText, sourceIds: ids };
}

const SummaryPanel: React.FC<SummaryPanelProps> = ({
  patientId,
  clinicalQuestion,
  onAskQuestion,
  onMissingInfo,
  onOpenMissingInfoModal,
  onSummaryLoaded,
  refreshKey,
}) => {
  const [summary, setSummary] = useState<SummaryResponse | null>(null);
  const [loading, setLoading] = useState(true);
  const [loadingStatus, setLoadingStatus] = useState('Generating patient summary...');
  const [loadingProgress, setLoadingProgress] = useState<SummaryProgress | null>(null);
  const [error, setError] = useState<string | null>(null);
  const prevRefreshKey = useRef(refreshKey);
  const abortRef = useRef<AbortController | null>(null);
  const [showCourseReasoning, setShowCourseReasoning] = useState(false);
  const [showStagingReasoning, setShowStagingReasoning] = useState(false);
  const [regeneratingField, setRegeneratingField] = useState<string | null>(null);
  const [regenError, setRegenError] = useState<string | null>(null);

  const handleRegenerate = useCallback(async (fieldName: string, feedback: string) => {
    setRegeneratingField(fieldName);
    setRegenError(null);
    try {
      const updated = await regenerateField({
        patient_id: patientId,
        field_name: fieldName,
        feedback,
        clinical_question: clinicalQuestion,
      });
      setSummary(updated);
    } catch (e) {
      setRegenError(e instanceof Error ? e.message : 'Regeneration failed');
    } finally {
      setRegeneratingField(null);
    }
  }, [patientId, clinicalQuestion]);

  const loadSummary = async (skipCache = false) => {
    // Cancel any in-flight request before starting a new one
    if (abortRef.current) {
      abortRef.current.abort();
    }
    const controller = new AbortController();
    abortRef.current = controller;
    const { signal } = controller;

    // Also cancel any backend background generation
    cancelGeneration(patientId, clinicalQuestion).catch(() => {});

    setLoading(true);
    setLoadingStatus('Checking generation status...');
    setLoadingProgress(null);
    setError(null);

    try {
      // Check if a background generation is already in progress (e.g. started from PatientList)
      if (!skipCache) {
        console.log('Checking for in-progress generation...');
        const progressData = await getGenerationProgress(patientId, clinicalQuestion);
        if (signal.aborted) return;
        console.log('Progress data:', progressData);

        if (progressData.status === 'in_progress') {
          // Background generation is running — poll until it finishes instead of calling
          // streamGetSummary (which would return the old stale cached summary).
          console.log('Resuming in-progress generation from step', progressData.current_step);
          setLoadingStatus(progressData.message || 'Generating patient summary...');
          if (progressData.current_step && progressData.total_steps) {
            setLoadingProgress({
              current: progressData.current_step,
              total: progressData.total_steps,
              field: progressData.current_field || '',
            });
          }

          // Poll until background generation completes
          await new Promise<void>((resolve, reject) => {
            const onAbort = () => { clearInterval(poll); reject(new DOMException('Aborted', 'AbortError')); };
            signal.addEventListener('abort', onAbort, { once: true });
            const poll = setInterval(async () => {
              if (signal.aborted) { clearInterval(poll); return; }
              try {
                const data = await getGenerationProgress(patientId, clinicalQuestion);
                if (signal.aborted) { clearInterval(poll); return; }
                if (data.status === 'in_progress') {
                  setLoadingStatus(data.message || 'Generating patient summary...');
                  if (data.current_step && data.total_steps) {
                    setLoadingProgress({
                      current: data.current_step,
                      total: data.total_steps,
                      field: data.current_field || '',
                    });
                  }
                } else {
                  // completed, stale, or not_started — stop polling
                  clearInterval(poll);
                  signal.removeEventListener('abort', onAbort);
                  resolve();
                }
              } catch {
                // Keep polling on transient errors
              }
            }, 2000);
          });

          if (signal.aborted) return;

          // Background generation done — load the fresh cached summary
          console.log('Background generation completed, loading fresh summary');
          setLoadingStatus('Loading summary...');
          const data = await streamGetSummary(
            patientId,
            clinicalQuestion,
            (status, progress) => {
              setLoadingStatus(status);
              if (progress) setLoadingProgress(progress);
            },
            signal,
            false,
          );
          if (signal.aborted) return;
          setSummary(data);
          if (onSummaryLoaded) {
            onSummaryLoaded(data.demographics);
          }
          if (data.missing_info.length > 0 && onMissingInfo) {
            onMissingInfo(data.missing_info);
          }
          return; // Skip the normal streamGetSummary below
        } else if (progressData.status === 'completed') {
          // Generation completed, load from cache
          console.log('Generation already completed, loading from cache');
          setLoadingStatus('Loading cached summary...');
        } else {
          console.log('No in-progress generation found, starting fresh');
        }
      }

      const data = await streamGetSummary(
        patientId,
        clinicalQuestion,
        (status, progress) => {
          setLoadingStatus(status);
          if (progress) setLoadingProgress(progress);
        },
        signal,
        skipCache,
      );
      if (signal.aborted) return;
      setSummary(data);
      if (onSummaryLoaded) {
        onSummaryLoaded(data.demographics);
      }
      if (data.missing_info.length > 0 && onMissingInfo) {
        onMissingInfo(data.missing_info);
      }
    } catch (e) {
      // Silently ignore aborted requests — a newer load is taking over
      if (e instanceof DOMException && e.name === 'AbortError') return;
      if (signal.aborted) return;
      setError(e instanceof Error ? e.message : 'Failed to load summary');
    } finally {
      // Only clear loading state if this request wasn't aborted
      if (!signal.aborted) {
        setLoading(false);
      }
    }
  };

  useEffect(() => {
    prevRefreshKey.current = refreshKey;
    loadSummary(false);

    // Abort in-flight request when deps change or component unmounts
    return () => {
      if (abortRef.current) {
        abortRef.current.abort();
      }
    };
  }, [patientId, clinicalQuestion, refreshKey]);

  // Poll for progress updates while loading (as backup to SSE)
  useEffect(() => {
    if (!loading) return;

    const pollInterval = setInterval(async () => {
      try {
        const data = await getGenerationProgress(patientId, clinicalQuestion);

        if (data.status === 'in_progress') {
          // Update progress from polling data (in case SSE disconnected)
          setLoadingStatus(data.message || 'Generating patient summary...');
          if (data.current_step && data.total_steps) {
            setLoadingProgress({
              current: data.current_step,
              total: data.total_steps,
              field: data.current_field || '',
            });
          }
        } else if (data.status === 'completed') {
          // Generation already completed; polling can stop
          console.log('Generation completed, detected via polling');
        } else if (data.status === 'not_started') {
          // No generation in progress
          console.log('No generation in progress');
        }
      } catch (e) {
        console.error('Failed to poll progress:', e);
      }
    }, 3000); // Poll every 3 seconds

    return () => clearInterval(pollInterval);
  }, [loading, patientId, clinicalQuestion]);

  if (loading) {
    return (
      <SummaryProgressUI
        loadingStatus={loadingStatus}
        loadingProgress={loadingProgress}
      />
    );
  }

  if (error) {
    return (
      <div className="bg-red-50 border border-red-200 rounded-lg p-4">
        <div className="flex items-center gap-2 text-red-700">
          <AlertCircle className="w-5 h-5" />
          <span className="font-medium">Error loading summary</span>
        </div>
        <p className="text-sm text-red-600 mt-1">{error}</p>
        <button
          onClick={() => loadSummary(true)}
          className="mt-3 text-sm text-red-700 hover:text-red-800 flex items-center gap-1"
        >
          <RefreshCw className="w-4 h-4" />
          Retry
        </button>
      </div>
    );
  }

  if (!summary) {
    return null;
  }

  const hasStaging = summary.staging?.tnm || summary.staging?.uicc_stage || summary.staging?.other_staging;
  const hasComorbidities = (summary.comorbidities?.conditions?.length > 0 ||
    summary.comorbidities?.previous_surgeries?.length > 0 ||
    summary.comorbidities?.previous_oncologic_diseases?.length > 0 ||
    summary.comorbidities?.risk_factors?.length > 0);
  const hasPathology = (summary.pathology?.cancer_type ||
    summary.pathology?.key_findings?.length > 0 ||
    summary.pathology?.mutations?.length > 0 ||
    summary.pathology?.molecular_markers?.length > 0 ||
    summary.pathology?.sequencing_findings);

  return (
    <div className="space-y-4">
      {/* Header with refresh */}
      <div className="flex items-center justify-between mb-4">
        <h3 className="text-sm font-semibold text-slate-700 uppercase tracking-wide flex items-center gap-2">
          <FileText className="w-4 h-4" />
          Clinical Summary
        </h3>
        <button
          onClick={() => loadSummary(true)}
          className="p-1 text-slate-400 hover:text-slate-600 rounded transition-colors"
          title="Refresh summary"
        >
          <RefreshCw className="w-3 h-3" />
        </button>
      </div>

      {/* Two-column masonry layout for sections */}
      <div className="columns-1 xl:columns-2 gap-4 space-y-4">
        {/* Demographics */}
        <Section title="Demographics" icon={<User className="w-4 h-4" />} sourceIds={summary.demographics.source_ids} expandable>
          <div className="grid grid-cols-2 gap-2">
            <div><span className="text-slate-500">Name:</span> {summary.demographics.name || 'Unknown'}</div>
            <div><span className="text-slate-500">Age:</span> {summary.demographics.age || 'Unknown'}</div>
            <div><span className="text-slate-500">Gender:</span> {summary.demographics.gender || 'Unknown'}</div>
            {summary.demographics.treating_physician && (
              <div><span className="text-slate-500">Physician:</span> {summary.demographics.treating_physician}</div>
            )}
            {summary.demographics.social_history && (
              <div className="col-span-2"><span className="text-slate-500">Social History:</span> <MarkdownText>{summary.demographics.social_history}</MarkdownText></div>
            )}
          </div>
        </Section>

        {/* Tumor Board Question */}
        {summary.tumor_board_question.value && (
          <Section title="Tumor Board Question" icon={<MessageSquare className="w-4 h-4" />} sourceIds={summary.tumor_board_question.source_ids} expandable>
            <p className="font-medium text-lmu-green"><MarkdownText>{summary.tumor_board_question.value}</MarkdownText></p>
          </Section>
        )}

        {/* Initial Diagnosis */}
        <Section title="Initial Diagnosis" icon={<Stethoscope className="w-4 h-4" />} sourceIds={summary.initial_diagnosis.source_ids} expandable fieldName="initial_diagnosis" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'initial_diagnosis'}>
          <MarkdownText inline={false}>{summary.initial_diagnosis.value || 'No diagnosis recorded'}</MarkdownText>
        </Section>

        {/* Course of Disease (LLM inferred) */}
        {summary.course_of_disease.assessment && (() => {
          const { cleanText, sourceIds: citationSourceIds } = parseSourceCitations(summary.course_of_disease.assessment);
          return (
            <Section
              title="Course of Disease"
              icon={<Brain className="w-4 h-4" />}
              sourceIds={summary.course_of_disease.source_ids}
              expandable
              fieldName="course_of_disease"
              onRegenerate={handleRegenerate}
              isRegenerating={regeneratingField === 'course_of_disease'}
              variant="inferred"
              badge={<><LLMInferredBadge /><InferredInfoIcon /></>}
              footerAction={summary.course_of_disease.reasoning ? (
                <button
                  onClick={() => setShowCourseReasoning(true)}
                  className="mt-2 text-sm text-red-700 hover:text-red-800 hover:underline"
                >
                  Show reasoning
                </button>
              ) : undefined}
            >
              <MarkdownText inline={false}>{cleanText}</MarkdownText>
              {citationSourceIds.length > 0 && (
                <div className="mt-2">
                  <SourceBadges sourceIds={citationSourceIds} maxVisible={5} sourceType="fhir" />
                </div>
              )}
            </Section>
          );
        })()}

        {/* Staging */}
        {hasStaging && (
          <Section
            title="Staging"
            icon={<Activity className="w-4 h-4" />}
            sourceIds={summary.staging.source_ids}
            expandable
            fieldName="staging"
            onRegenerate={handleRegenerate}
            isRegenerating={regeneratingField === 'staging'}
            variant={summary.staging.is_inferred ? 'inferred' : 'default'}
            badge={summary.staging.is_inferred ? <><LLMInferredBadge /><InferredInfoIcon /></> : undefined}
            footerAction={summary.staging.is_inferred && summary.staging.inference_basis ? (
              <button
                onClick={() => setShowStagingReasoning(true)}
                className="mt-2 text-sm text-red-700 hover:text-red-800 hover:underline"
              >
                Show reasoning
              </button>
            ) : undefined}
          >
            {summary.staging.tnm && <p><span className="font-medium">TNM:</span> <MarkdownText>{summary.staging.tnm}</MarkdownText></p>}
            {summary.staging.uicc_stage && <p><span className="font-medium">UICC:</span> <MarkdownText>{summary.staging.uicc_stage}</MarkdownText></p>}
            {summary.staging.other_staging && <p><MarkdownText>{summary.staging.other_staging}</MarkdownText></p>}
          </Section>
        )}

        {/* Pathology */}
        {hasPathology && (
          <Section title="Pathology" icon={<Dna className="w-4 h-4" />} expandable fieldName="pathology" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'pathology'}>
            {summary.pathology.cancer_type && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Cancer Type:</span>
                <p className="font-medium mt-0.5"><MarkdownText>{summary.pathology.cancer_type}</MarkdownText></p>
              </div>
            )}
            {summary.pathology.key_findings.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Key Findings:</span>
                <InlineSourceList items={summary.pathology.key_findings} className="mt-1" />
              </div>
            )}
            {summary.pathology.mutations.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Mutations:</span>
                <div className="flex flex-wrap gap-1 mt-1">
                  {summary.pathology.mutations.map((raw, i) => {
                    const item = resolveItem(raw);
                    return (
                      <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 bg-purple-100 text-purple-700 rounded text-xs font-medium">
                        {item.text}
                        {item.source_ids.length > 0 && (
                          <SourceBadges sourceIds={item.source_ids} maxVisible={1} sourceType="fhir" />
                        )}
                      </span>
                    );
                  })}
                </div>
              </div>
            )}
            {summary.pathology.molecular_markers?.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Molecular Markers:</span>
                <div className="flex flex-wrap gap-1 mt-1">
                  {summary.pathology.molecular_markers.map((raw, i) => {
                    const item = resolveItem(raw);
                    return (
                      <span key={i} className="inline-flex items-center gap-1 px-2 py-0.5 bg-blue-100 text-blue-700 rounded text-xs font-medium">
                        {item.text}
                        {item.source_ids.length > 0 && (
                          <SourceBadges sourceIds={item.source_ids} maxVisible={1} sourceType="fhir" />
                        )}
                      </span>
                    );
                  })}
                </div>
              </div>
            )}
            {summary.pathology.sequencing_findings && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Sequencing:</span>
                <p className="mt-0.5"><MarkdownText>{summary.pathology.sequencing_findings}</MarkdownText></p>
                {summary.pathology.sequencing_source_ids?.length > 0 && (
                  <SourceBadges sourceIds={summary.pathology.sequencing_source_ids} maxVisible={1} sourceType="fhir" className="mt-1" />
                )}
              </div>
            )}
          </Section>
        )}

        {/* Therapies — Chemo & Radiation subsections */}
        {(summary.therapies?.chemo?.length > 0 || summary.therapies?.radiation?.length > 0) && (
          <Section title="Therapies" icon={<Pill className="w-4 h-4" />} expandable defaultExpanded={true} fieldName="therapies" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'therapies'}>
            {summary.therapies.chemo?.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Chemotherapy:</span>
                <ul className="list-disc list-inside space-y-2 mt-1">
                  {summary.therapies.chemo.map((therapy, i) => (
                    <li key={i} className="leading-relaxed">
                      <span className="font-medium">{therapy.name || therapy.type}</span>
                      {therapy.description && <span>: <MarkdownText>{therapy.description}</MarkdownText></span>}
                      {!therapy.description && (
                        <span className="text-slate-500 text-xs">
                          {therapy.start_date && ` (${new Date(therapy.start_date).toLocaleDateString()}`}
                          {therapy.end_date && ` - ${new Date(therapy.end_date).toLocaleDateString()}`}
                          {therapy.start_date && ')'}
                          {therapy.cycles && `, ${therapy.cycles} cycles`}
                        </span>
                      )}
                      {therapy.efficacy && <span className="text-xs text-slate-500 ml-1">[<MarkdownText>{therapy.efficacy}</MarkdownText>]</span>}
                      {therapy.intolerance && <span className="text-xs text-amber-600 ml-1">[Intolerance: <MarkdownText>{therapy.intolerance}</MarkdownText>]</span>}
                      <SourceBadges sourceIds={therapy.source_ids} maxVisible={1} sourceType="fhir" className="ml-1 inline-flex" />
                    </li>
                  ))}
                </ul>
              </div>
            )}
            {summary.therapies.radiation?.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Radiation:</span>
                <ul className="list-disc list-inside space-y-2 mt-1">
                  {summary.therapies.radiation.map((therapy, i) => (
                    <li key={i} className="leading-relaxed">
                      <span className="font-medium">{therapy.name || therapy.type}</span>
                      {therapy.description && <span>: <MarkdownText>{therapy.description}</MarkdownText></span>}
                      {!therapy.description && (
                        <span className="text-slate-500 text-xs">
                          {therapy.start_date && ` (${new Date(therapy.start_date).toLocaleDateString()}`}
                          {therapy.end_date && ` - ${new Date(therapy.end_date).toLocaleDateString()}`}
                          {therapy.start_date && ')'}
                          {therapy.cycles && `, ${therapy.cycles} cycles`}
                        </span>
                      )}
                      {therapy.efficacy && <span className="text-xs text-slate-500 ml-1">[<MarkdownText>{therapy.efficacy}</MarkdownText>]</span>}
                      {therapy.intolerance && <span className="text-xs text-amber-600 ml-1">[Intolerance: <MarkdownText>{therapy.intolerance}</MarkdownText>]</span>}
                      <SourceBadges sourceIds={therapy.source_ids} maxVisible={1} sourceType="fhir" className="ml-1 inline-flex" />
                    </li>
                  ))}
                </ul>
              </div>
            )}
          </Section>
        )}

        {/* Imaging */}
        {summary.imaging.length > 0 && (
          <Section title="Imaging Findings" icon={<Activity className="w-4 h-4" />} expandable defaultExpanded={true} fieldName="imaging" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'imaging'}>
            <div className="space-y-3">
              {summary.imaging.map((img, i) => (
                <div key={i} className="border-l-2 border-green-300 pl-3">
                  <div className="flex items-center justify-between">
                    <span className="font-medium">{img.type}</span>
                    {img.date && <span className="text-xs text-slate-500">{new Date(img.date).toLocaleDateString()}</span>}
                  </div>
                  {(img.modality || img.organ_system) && (
                    <p className="text-xs text-slate-500 mt-1">
                      {[img.modality, img.organ_system].filter(Boolean).join(' | ')}
                    </p>
                  )}
                  {img.key_findings && <div className="text-sm mt-1"><MarkdownText inline={false}>{img.key_findings}</MarkdownText></div>}
                  {img.assessment && <p className="text-sm text-slate-700 mt-1"><span className="font-medium">Assessment:</span> <MarkdownText>{img.assessment}</MarkdownText></p>}
                  {img.progression && <p className="text-sm text-slate-700"><span className="font-medium">Progression:</span> <MarkdownText>{img.progression}</MarkdownText></p>}
                  {img.comparison_to_prior_staging && (
                    <p className="text-sm text-slate-700"><span className="font-medium">Vs prior staging:</span> <MarkdownText>{img.comparison_to_prior_staging}</MarkdownText></p>
                  )}
                  {img.metastatic_pattern && (
                    <p className="text-sm text-slate-700"><span className="font-medium">Metastatic pattern:</span> <MarkdownText>{img.metastatic_pattern}</MarkdownText></p>
                  )}
                  {img.disease_evolution && (
                    <p className="text-sm text-slate-700"><span className="font-medium">Disease evolution:</span> <MarkdownText>{img.disease_evolution}</MarkdownText></p>
                  )}
                  {img.ai_reasoning && (
                    <div className="mt-2 p-2 bg-slate-50 rounded text-xs">
                      <span className="font-medium text-slate-600">AI Reasoning:</span>
                      <p className="mt-1"><MarkdownText>{img.ai_reasoning}</MarkdownText></p>
                    </div>
                  )}
                  {img.tnm_from_imaging && (
                    <div className="mt-1 text-xs text-purple-600 font-mono">TNM: {img.tnm_from_imaging}</div>
                  )}
                  <SourceBadges sourceIds={img.source_ids} maxVisible={1} sourceType="fhir" className="mt-1" />
                </div>
              ))}
            </div>
          </Section>
        )}

        {/* Comorbidities & Risk Factors (with inline sources) */}
        {hasComorbidities && (
          <Section title="Comorbidities & Risk Factors" icon={<Heart className="w-4 h-4" />} expandable fieldName="comorbidities" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'comorbidities'}>
            {summary.comorbidities.conditions.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Conditions:</span>
                <InlineSourceList items={summary.comorbidities.conditions} className="mt-1" />
              </div>
            )}
            {summary.comorbidities.previous_surgeries?.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Previous Surgeries:</span>
                <InlineSourceList items={summary.comorbidities.previous_surgeries} className="mt-1" />
              </div>
            )}
            {summary.comorbidities.previous_oncologic_diseases?.length > 0 && (
              <div className="mb-2">
                <span className="text-slate-500 text-xs uppercase tracking-wide">Previous Oncologic Diseases:</span>
                <InlineSourceList items={summary.comorbidities.previous_oncologic_diseases} className="mt-1" />
              </div>
            )}
            {summary.comorbidities.risk_factors.length > 0 && (
              <div>
                <span className="text-slate-500 text-xs uppercase tracking-wide">Risk Factors:</span>
                <InlineSourceList items={summary.comorbidities.risk_factors} className="mt-1" />
              </div>
            )}
          </Section>
        )}

        {/* Contraindications (with inline sources) */}
        {summary.contraindications.items.length > 0 && (
          <Section title="Contraindications" icon={<AlertCircle className="w-4 h-4" />} expandable fieldName="contraindications" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'contraindications'}>
            <ul className="list-disc list-inside space-y-1 text-black font-bold">
              {summary.contraindications.items.map((raw, i) => {
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
          </Section>
        )}

        {/* General Condition */}
        <Section title="General Condition" icon={<Activity className="w-4 h-4" />} sourceIds={summary.general_condition.source_ids} expandable fieldName="general_condition" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'general_condition'}>
          {(summary.general_condition.ecog !== null && summary.general_condition.ecog !== undefined) && (
            <div className="mb-2">
              <span className="text-slate-500 text-xs uppercase tracking-wide">ECOG Performance Status:</span>
              <p className="font-bold text-lg mt-0.5">{summary.general_condition.ecog}</p>
            </div>
          )}
          {(summary.general_condition.barthel_index !== null && summary.general_condition.barthel_index !== undefined) && (
            <div className="mb-2">
              <span className="text-slate-500 text-xs uppercase tracking-wide">Barthel Index:</span>
              <p className="font-bold mt-0.5">{summary.general_condition.barthel_index}</p>
            </div>
          )}
          {summary.general_condition.treatment_tolerance && (
            <div className="mb-2">
              <span className="text-slate-500 text-xs uppercase tracking-wide">Treatment Tolerance:</span>
              <p className="mt-0.5"><MarkdownText>{summary.general_condition.treatment_tolerance}</MarkdownText></p>
            </div>
          )}
          {summary.general_condition.nursing_dependency && (
            <div className="mb-2">
              <span className="text-slate-500 text-xs uppercase tracking-wide">Nursing Dependency:</span>
              <p className="mt-0.5"><MarkdownText>{summary.general_condition.nursing_dependency}</MarkdownText></p>
            </div>
          )}
          {summary.general_condition.description && (
            <div>
              <span className="text-slate-500 text-xs uppercase tracking-wide">Description:</span>
              <div className="mt-0.5"><MarkdownText inline={false}>{summary.general_condition.description}</MarkdownText></div>
            </div>
          )}
        </Section>

        {/* Symptoms (with inline sources) */}
        {summary.symptoms.items.length > 0 && (
          <Section title="Symptoms" icon={<AlertCircle className="w-4 h-4" />} expandable fieldName="symptoms" onRegenerate={handleRegenerate} isRegenerating={regeneratingField === 'symptoms'}>
            <InlineSourceList items={summary.symptoms.items} />
          </Section>
        )}

        {/* Patient Wishes */}
        {(summary.patient_wishes.text || summary.patient_wishes.therapy_goal) && (
          <Section
            title="Patient Wishes / Therapy Goal"
            icon={<Heart className="w-4 h-4" />}
            sourceIds={summary.patient_wishes.source_ids}
            sourceType={undefined}
            highlight={summary.patient_wishes.needs_clarification}
            expandable
            fieldName="patient_wishes"
            onRegenerate={handleRegenerate}
            isRegenerating={regeneratingField === 'patient_wishes'}
          >
            {summary.patient_wishes.text && (
              <MarkdownText inline={false}>{summary.patient_wishes.text}</MarkdownText>
            )}
            {summary.patient_wishes.therapy_goal && (
              <p className="mt-1 text-slate-500">Goal: <MarkdownText>{summary.patient_wishes.therapy_goal}</MarkdownText></p>
            )}
            {summary.patient_wishes.needs_clarification && onAskQuestion && (
              <button
                onClick={() => onAskQuestion("What are the patient's wishes regarding treatment?")}
                className="mt-2 text-sm text-amber-700 hover:text-amber-800 flex items-center gap-1"
              >
                <MessageSquare className="w-4 h-4" />
                Ask in chat
              </button>
            )}
          </Section>
        )}

      </div>

      {/* Regeneration error */}
      {regenError && (
        <div className="bg-red-50 border border-red-200 rounded-lg px-4 py-3 flex items-center justify-between">
          <div className="flex items-center gap-2 text-red-700 text-sm">
            <AlertCircle className="w-4 h-4" />
            <span>Regeneration failed: {regenError}</span>
          </div>
          <button onClick={() => setRegenError(null)} className="text-red-400 hover:text-red-600">
            <X className="w-4 h-4" />
          </button>
        </div>
      )}

      {/* Missing Information */}
      {summary.missing_info.length > 0 && (
        <div className="bg-amber-50 border border-amber-200 rounded-lg p-4">
          <h3 className="font-semibold text-amber-900 flex items-center gap-2 mb-3">
            <AlertCircle className="w-4 h-4" />
            Missing Information
          </h3>
          <div className="space-y-2">
            {summary.missing_info.map((item: MissingInfoItem, i) => (
              <div key={i} className="flex items-start gap-2">
                <span className={`px-1.5 py-0.5 rounded text-xs font-medium ${
                  item.priority === 'high' ? 'bg-red-100 text-red-700' :
                  item.priority === 'medium' ? 'bg-amber-100 text-amber-700' :
                  'bg-slate-100 text-slate-600'
                }`}>
                  {item.priority}
                </span>
                <div className="flex-1">
                  <span className="text-sm text-slate-700">{item.field}:</span>
                  {onOpenMissingInfoModal ? (
                    <button
                      onClick={() => onOpenMissingInfoModal(summary.missing_info, item)}
                      className="ml-2 text-sm text-lmu-green hover:text-lmu-green-dark hover:underline"
                    >
                      {item.question}
                    </button>
                  ) : (
                    <span className="ml-2 text-sm text-slate-500">{item.question}</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* Generated timestamp */}
      <div className="text-xs text-slate-400 text-center pt-2">
        Generated: {new Date(summary.generated_at).toLocaleString()}
      </div>

      {/* Staging reasoning modal */}
      {showStagingReasoning && summary.staging.inference_basis && (() => {
        const { cleanText, sourceIds } = parseSourceCitations(summary.staging.inference_basis);
        return (
          <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
            <div className="bg-white rounded-xl shadow-xl w-full max-w-xl mx-4 max-h-[80vh] overflow-hidden">
              <div className="px-4 py-3 border-b border-red-200 flex items-center justify-between bg-red-50">
                <div className="flex items-center gap-2">
                  <Activity className="w-4 h-4 text-red-600" />
                  <h3 className="font-semibold text-sm text-red-900">Staging Reasoning</h3>
                </div>
                <button
                  onClick={() => setShowStagingReasoning(false)}
                  className="p-1 hover:bg-red-100 rounded-full transition-colors text-red-600"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="p-4 text-sm text-slate-700 overflow-y-auto max-h-[60vh] prose prose-sm prose-slate max-w-none">
                <MarkdownText inline={false}>{cleanText}</MarkdownText>
                {sourceIds.length > 0 && (
                  <div className="mt-3 pt-3 border-t border-slate-200 not-prose">
                    <span className="text-xs text-slate-500 mr-2">Sources:</span>
                    <SourceBadges sourceIds={sourceIds} maxVisible={5} sourceType="fhir" />
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })()}

      {/* Course of disease reasoning modal */}
      {showCourseReasoning && summary.course_of_disease.reasoning && (() => {
        const { cleanText, sourceIds } = parseSourceCitations(summary.course_of_disease.reasoning);
        return (
          <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
            <div className="bg-white rounded-xl shadow-xl w-full max-w-xl mx-4 max-h-[80vh] overflow-hidden">
              <div className="px-4 py-3 border-b border-red-200 flex items-center justify-between bg-red-50">
                <div className="flex items-center gap-2">
                  <Brain className="w-4 h-4 text-red-600" />
                  <h3 className="font-semibold text-sm text-red-900">Course of Disease Reasoning</h3>
                </div>
                <button
                  onClick={() => setShowCourseReasoning(false)}
                  className="p-1 hover:bg-red-100 rounded-full transition-colors text-red-600"
                >
                  <X className="w-4 h-4" />
                </button>
              </div>
              <div className="p-4 text-sm text-slate-700 overflow-y-auto max-h-[60vh] prose prose-sm prose-slate max-w-none">
                <MarkdownText inline={false}>{cleanText}</MarkdownText>
                {sourceIds.length > 0 && (
                  <div className="mt-3 pt-3 border-t border-slate-200 not-prose">
                    <span className="text-xs text-slate-500 mr-2">Sources:</span>
                    <SourceBadges sourceIds={sourceIds} maxVisible={5} sourceType="fhir" />
                  </div>
                )}
              </div>
            </div>
          </div>
        );
      })()}
    </div>
  );
};

export default SummaryPanel;
