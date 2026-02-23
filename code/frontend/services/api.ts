/**
 * API Service Layer for TURBO Dashboard
 * Handles all communication with the FastAPI backend
 */

import type {
  GetListResponse,
  TimelineResponse,
  SourceDetailResponse,
  SummaryResponse,
  QueryRequest,
  QueryResponse,
  QuerySourceRef,
  AddKeypointsRequest,
  KeypointResult,
  Patient,
  PatientMetadata,
} from '../types';

// API base URL - configurable via environment variable
const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

// Cache for source details (to avoid repeated fetches for hover popups)
const sourceCache = new Map<string, SourceDetailResponse>();

/**
 * Generic fetch wrapper with error handling
 */
async function fetchApi<T>(
  endpoint: string,
  options: RequestInit = {}
): Promise<T> {
  const url = `${API_BASE_URL}${endpoint}`;

  const response = await fetch(url, {
    ...options,
    headers: {
      'Content-Type': 'application/json',
      ...options.headers,
    },
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`API Error ${response.status}: ${errorText}`);
  }

  return response.json();
}

/**
 * Health check
 */
export async function checkHealth(signal?: AbortSignal): Promise<{ status: string; version: string }> {
  return fetchApi('/health', { signal });
}

/**
 * Get list of all sources (FHIR resources and Milvus documents)
 */
export async function getSourceList(params?: {
  patient_id?: string;
  source_type?: 'fhir' | 'milvus';
  signal?: AbortSignal;
}): Promise<GetListResponse> {
  const searchParams = new URLSearchParams();

  if (params?.patient_id) {
    searchParams.set('patient_id', params.patient_id);
  }
  if (params?.source_type) {
    searchParams.set('source_type', params.source_type);
  }

  const queryString = searchParams.toString();
  const endpoint = `/get_list${queryString ? `?${queryString}` : ''}`;

  return fetchApi<GetListResponse>(endpoint, { signal: params?.signal });
}

/**
 * Get timeline events for a patient
 */
export async function getTimeline(patientId: string): Promise<TimelineResponse> {
  return fetchApi<TimelineResponse>(`/timeline/${encodeURIComponent(patientId)}`);
}

/**
 * Get source details (for hover popup)
 * Uses caching to avoid repeated fetches
 */
export async function getSourceDetail(sourceId: string): Promise<SourceDetailResponse> {
  // Check cache first
  if (sourceCache.has(sourceId)) {
    return sourceCache.get(sourceId)!;
  }

  const detail = await fetchApi<SourceDetailResponse>(
    `/source/${encodeURIComponent(sourceId)}`
  );

  // Cache the result
  sourceCache.set(sourceId, detail);

  return detail;
}

/**
 * Clear source cache (useful when data is updated)
 */
export function clearSourceCache(): void {
  sourceCache.clear();
}

/**
 * Get patient summary
 */
export async function getSummary(
  patientId: string,
  clinicalQuestion?: string,
  signal?: AbortSignal
): Promise<SummaryResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set('patient_id', patientId);

  if (clinicalQuestion) {
    searchParams.set('clinical_question', clinicalQuestion);
  }

  return fetchApi<SummaryResponse>(`/get_summary?${searchParams.toString()}`, { signal });
}

/**
 * Start background summary generation (fire-and-forget, survives page reload).
 */
export async function startGeneration(
  patientId: string,
  clinicalQuestion?: string,
  skipCache?: boolean,
): Promise<{ status: 'started' | 'running' | 'cached'; started_at?: string }> {
  return fetchApi(`/start_generation`, {
    method: 'POST',
    body: JSON.stringify({
      patient_id: patientId,
      clinical_question: clinicalQuestion || null,
      skip_cache: skipCache || false,
    }),
  });
}

/**
 * Cancel a running background generation.
 */
export async function cancelGeneration(
  patientId: string,
  clinicalQuestion?: string,
): Promise<{ status: 'cancelled' | 'not_found' }> {
  const searchParams = new URLSearchParams();
  searchParams.set('patient_id', patientId);
  if (clinicalQuestion) {
    searchParams.set('clinical_question', clinicalQuestion);
  }
  return fetchApi(`/cancel_generation?${searchParams.toString()}`, { method: 'POST' });
}

/**
 * Check active background generations for a patient.
 */
export async function getGenerationStatus(
  patientId: string,
): Promise<{ active: Array<{ clinical_question: string | null; started_at: string }> }> {
  const searchParams = new URLSearchParams();
  searchParams.set('patient_id', patientId);
  return fetchApi(`/generation_status?${searchParams.toString()}`);
}

/**
 * Progress info attached to status events during per-field summary generation.
 */
export interface SummaryProgress {
  current: number;
  total: number;
  field: string;
}

/**
 * SSE event from /get_summary/stream
 */
export type SummaryStreamEvent =
  | { type: 'status'; message: string; progress?: SummaryProgress }
  | { type: 'complete'; summary: SummaryResponse }
  | { type: 'error'; detail: string };

/**
 * Streaming summary generation — sends per-field status events with progress
 * during LLM generation.
 */
export async function streamGetSummary(
  patientId: string,
  clinicalQuestion?: string,
  onStatus?: (message: string, progress?: SummaryProgress) => void,
  signal?: AbortSignal,
  skipCache?: boolean,
): Promise<SummaryResponse> {
  const searchParams = new URLSearchParams();
  searchParams.set('patient_id', patientId);
  if (clinicalQuestion) {
    searchParams.set('clinical_question', clinicalQuestion);
  }
  if (skipCache) {
    searchParams.set('skip_cache', 'true');
  }

  const response = await fetch(
    `${API_BASE_URL}/get_summary/stream?${searchParams.toString()}`,
    { signal },
  );

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`API Error ${response.status}: ${errorText}`);
  }

  const reader = response.body?.getReader();
  if (!reader) throw new Error('No response body');

  const decoder = new TextDecoder();
  let buffer = '';
  let result: SummaryResponse | null = null;

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const parts = buffer.split('\n\n');
    buffer = parts.pop() || '';

    for (const part of parts) {
      const line = part.trim();
      if (!line.startsWith('data: ')) continue;
      try {
        const event = JSON.parse(line.slice(6)) as SummaryStreamEvent;
        if (event.type === 'status' && onStatus) {
          onStatus(event.message, event.progress);
        } else if (event.type === 'complete') {
          result = event.summary;
        } else if (event.type === 'error') {
          throw new Error(event.detail);
        }
      } catch (e) {
        if (e instanceof Error && e.message !== 'Unexpected end of JSON input') throw e;
      }
    }
  }

  // Process remaining buffer
  if (buffer.trim().startsWith('data: ')) {
    try {
      const event = JSON.parse(buffer.trim().slice(6)) as SummaryStreamEvent;
      if (event.type === 'complete') result = event.summary;
      else if (event.type === 'error') throw new Error(event.detail);
    } catch (e) {
      if (e instanceof Error && e.message !== 'Unexpected end of JSON input') throw e;
    }
  }

  if (!result) throw new Error('Summary stream ended without a result');
  return result;
}

export interface CacheEntry {
  id: number;
  clinical_question: string | null;
  created_at: string;
}

/**
 * Get all cached summaries for a patient
 */
export async function checkSummaryCache(patientId: string): Promise<{
  entries: CacheEntry[];
}> {
  const searchParams = new URLSearchParams();
  searchParams.set('patient_id', patientId);
  const response = await fetchApi<{ entries: Array<{ id: number; fragestellung: string | null; clinical_question?: string | null; created_at: string }> }>(
    `/check_summary_cache?${searchParams.toString()}`
  );
  return {
    entries: response.entries.map((entry) => ({
      id: entry.id,
      clinical_question: entry.clinical_question ?? entry.fragestellung ?? null,
      created_at: entry.created_at,
    })),
  };
}

/**
 * Delete cached summaries for a patient. Pass entryId to delete a single entry.
 */
export async function deleteSummaryCache(patientId: string, entryId?: number): Promise<{ deleted: number }> {
  const searchParams = new URLSearchParams();
  searchParams.set('patient_id', patientId);
  if (entryId !== undefined) {
    searchParams.set('entry_id', String(entryId));
  }
  return fetchApi(`/delete_summary_cache?${searchParams.toString()}`, { method: 'DELETE' });
}

/**
 * Update a cached patient summary with user-provided information
 */
export async function updateSummary(params: {
  patient_id: string;
  clinical_question?: string;
  user_input: string;
  missing_fields?: string[];
}): Promise<SummaryResponse> {
  return fetchApi<SummaryResponse>('/update_summary', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

/**
 * Regenerate a single summary field with clinician feedback
 */
export async function regenerateField(params: {
  patient_id: string;
  field_name: string;
  feedback: string;
  clinical_question?: string;
}): Promise<SummaryResponse> {
  return fetchApi<SummaryResponse>('/regenerate_field', {
    method: 'POST',
    body: JSON.stringify(params),
  });
}

/**
 * Send a query to the LLM chat
 */
export async function sendQuery(request: QueryRequest): Promise<QueryResponse> {
  return fetchApi<QueryResponse>('/query', {
    method: 'POST',
    body: JSON.stringify(request),
  });
}

/**
 * Extract keypoints from sources
 */
export async function addKeypoints(
  request: AddKeypointsRequest
): Promise<KeypointResult[]> {
  const payload = {
    ...request,
    fragestellung: request.clinical_question,
  };
  return fetchApi<KeypointResult[]>('/add_keypoints', {
    method: 'POST',
    body: JSON.stringify(payload),
  });
}

/**
 * Get list of Milvus collections
 */
export async function getCollections(): Promise<{ collections: string[] }> {
  return fetchApi('/collections');
}

/**
 * Fetch the patient list from registered FHIR Patient resources.
 */
export async function getPatientList(signal?: AbortSignal): Promise<Patient[]> {
  try {
    const response = await getSourceList({ source_type: 'fhir', signal });
    const patients: Patient[] = [];
    const seen = new Set<string>();

    for (const item of response.items) {
      if (item.resource_type === 'Patient' && item.fhir_id && !seen.has(item.fhir_id)) {
        seen.add(item.fhir_id);
        patients.push({
          id: item.fhir_id,
          internalId: item.fhir_id,
          name: item.title || 'Unknown Patient',
          age: 0, // Would need to calculate from birthDate
          gender: 'Unknown',
          lastUpdated: item.date || new Date().toISOString(),
        });
      }
    }

    return patients;
  } catch {
    // Return empty list if API not available
    return [];
  }
}

/**
 * SSE event from /query/stream
 */
export type StreamEvent =
  | { type: 'status'; message: string }
  | { type: 'complete'; answer: string; sources: QuerySourceRef[]; conversation_id: string; follow_up_questions: string[] }
  | { type: 'error'; detail: string };

/**
 * Streaming query that yields parsed SSE events from the agent
 */
export async function* streamQuery(
  request: QueryRequest,
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent, void, unknown> {
  const response = await fetch(`${API_BASE_URL}/query/stream`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
    signal,
  });

  if (!response.ok) {
    const errorText = await response.text();
    throw new Error(`API Error ${response.status}: ${errorText}`);
  }

  const reader = response.body?.getReader();
  if (!reader) {
    throw new Error('No response body');
  }

  const decoder = new TextDecoder();
  let buffer = '';

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });

    // Parse complete SSE lines (each ends with \n\n)
    const parts = buffer.split('\n\n');
    buffer = parts.pop() || ''; // keep incomplete last part in buffer

    for (const part of parts) {
      const line = part.trim();
      if (line.startsWith('data: ')) {
        try {
          const payload = JSON.parse(line.slice(6)) as StreamEvent;
          yield payload;
        } catch {
          // skip malformed lines
        }
      }
    }
  }

  // Process any remaining buffer
  if (buffer.trim().startsWith('data: ')) {
    try {
      const payload = JSON.parse(buffer.trim().slice(6)) as StreamEvent;
      yield payload;
    } catch {
      // skip
    }
  }
}

/**
 * Get progress of in-flight summary generation (for resume on page reload)
 */
export async function getGenerationProgress(
  patientId: string,
  clinicalQuestion?: string
): Promise<{
  status: 'not_started' | 'in_progress' | 'completed' | 'stale';
  current_field?: string;
  current_step?: number;
  total_steps?: number;
  message?: string;
  started_at?: string;
  updated_at?: string;
  completed_at?: string;
  last_update?: string;
}> {
  const params = new URLSearchParams({ });
  if (clinicalQuestion) {
    params.append('clinical_question', clinicalQuestion);
  }
  const query = params.toString() ? `?${params.toString()}` : '';
  return fetchApi<any>(`/generation_progress/${encodeURIComponent(patientId)}${query}`);
}

/**
 * Fetch cancer-type classification and guideline preference for a patient
 */
export async function getPatientMetadata(patientId: string): Promise<PatientMetadata> {
  try {
    return await fetchApi<PatientMetadata>(`/patient/${encodeURIComponent(patientId)}/metadata`);
  } catch {
    return { patient_id: patientId, guideline_cancer_types: ['nsclc'] };
  }
}

// Export API base URL for debugging
export { API_BASE_URL };
