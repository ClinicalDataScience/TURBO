import React, { useState, useEffect, useRef, useMemo } from 'react';
import type { TimelineEvent, TimelineResponse, TreatmentResponseItem } from '../types';
import { getTimeline } from '../services/api';
import SourceBadge from './SourceBadge';
import {
  Calendar,
  Stethoscope,
  Pill,
  Scissors,
  FileText,
  X,
  Filter,
  Loader2,
  RefreshCw,
  Activity,
  Radio,
  Users,
  AlertCircle,
  Star,
  TrendingUp,
  TrendingDown,
  Minus,
  CheckCircle,
} from 'lucide-react';

interface TimelineProps {
  patientId?: string;
  events?: TimelineEvent[]; // For backward compatibility
}

type EffectiveResponseMarker = {
  response: TreatmentResponseItem;
  source: 'direct' | 'carried';
  anchorEventId: string;
};

const Timeline: React.FC<TimelineProps> = ({ patientId, events: propEvents }) => {
  const [selectedFilters, setSelectedFilters] = useState<Set<string>>(new Set());
  const [filterOpen, setFilterOpen] = useState(false);
  const filterRef = useRef<HTMLDivElement>(null);
  const [selectedEvent, setSelectedEvent] = useState<TimelineEvent | null>(null);
  const [events, setEvents] = useState<TimelineEvent[]>(propEvents || []);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filtersAvailable, setFiltersAvailable] = useState<string[]>([]);
  const [showResponseLayer, setShowResponseLayer] = useState(false);
  const [treatmentResponses, setTreatmentResponses] = useState<TreatmentResponseItem[]>([]);

  const hasUsableDate = (event: TimelineEvent) =>
    !!event.date && !Number.isNaN(new Date(event.date).getTime());

  const buildFilterOptions = (timelineEvents: TimelineEvent[]) => {
    const types = new Set<string>();
    timelineEvents.filter(hasUsableDate).forEach((event) => {
      if (event.type?.trim()) {
        types.add(event.type);
      }
    });
    return Array.from(types);
  };

  const loadTimeline = async () => {
    if (!patientId) return;

    setLoading(true);
    setError(null);
    try {
      const response: TimelineResponse = await getTimeline(patientId);
      setEvents(response.events);
      setTreatmentResponses(response.treatment_responses || []);
      setFiltersAvailable(buildFilterOptions(response.events));
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load timeline');
    } finally {
      setLoading(false);
    }
  };

  useEffect(() => {
    if (patientId) {
      loadTimeline();
    } else if (propEvents) {
      setEvents(propEvents);
      setFiltersAvailable(buildFilterOptions(propEvents));
    }
  }, [patientId]);

  useEffect(() => {
    if (propEvents && !patientId) {
      setEvents(propEvents);
      setFiltersAvailable(buildFilterOptions(propEvents));
    }
  }, [propEvents]);

  const eventTypeMap: Record<string, string> = {
    'all': 'All',
    'tumor_board': 'Tumor Board',
    'imaging': 'Imaging',
    'therapy': 'Therapy',
    'Initial Diagnosis': 'Diagnosis',
    'Imaging': 'Imaging',
    'Biopsy': 'Biopsy',
    'Surgery': 'Surgery',
    'Chemotherapy': 'Chemo',
    'Medication': 'Medication',
    'Radiation': 'Radiation',
    'Tumor Board': 'Board',
  };

  const getIcon = (type: string) => {
    const normalizedType = type.toLowerCase();
    if (normalizedType.includes('imaging') || normalizedType.includes('ct') || normalizedType.includes('mri')) {
      return <FileText className="w-4 h-4" />;
    }
    if (normalizedType.includes('surgery') || normalizedType.includes('biopsy')) {
      return <Scissors className="w-4 h-4" />;
    }
    if (normalizedType.includes('chemo') || normalizedType.includes('medication')) {
      return <Pill className="w-4 h-4" />;
    }
    if (normalizedType.includes('radiation')) {
      return <Radio className="w-4 h-4" />;
    }
    if (normalizedType.includes('diagnosis') || normalizedType.includes('condition')) {
      return <Stethoscope className="w-4 h-4" />;
    }
    if (normalizedType.includes('board') || normalizedType.includes('consult')) {
      return <Users className="w-4 h-4" />;
    }
    return <Activity className="w-4 h-4" />;
  };

  const getColor = (type: string) => {
    const normalizedType = type.toLowerCase();
    if (normalizedType.includes('imaging') || normalizedType.includes('ct') || normalizedType.includes('mri')) {
      return 'bg-blue-100 text-blue-700 border-blue-200';
    }
    if (normalizedType.includes('surgery') || normalizedType.includes('biopsy')) {
      return 'bg-red-100 text-red-700 border-red-200';
    }
    if (normalizedType.includes('chemo') || normalizedType.includes('medication')) {
      return 'bg-purple-100 text-purple-700 border-purple-200';
    }
    if (normalizedType.includes('radiation')) {
      return 'bg-orange-100 text-orange-700 border-orange-200';
    }
    if (normalizedType.includes('diagnosis') || normalizedType.includes('condition')) {
      return 'bg-amber-100 text-amber-700 border-amber-200';
    }
    if (normalizedType.includes('board') || normalizedType.includes('consult')) {
      return 'bg-indigo-100 text-indigo-700 border-indigo-200';
    }
    return 'bg-slate-100 text-slate-700 border-slate-200';
  };

  const getResponseColor = (status: string) => {
    switch (status) {
      case 'PD': return 'bg-red-100 text-red-700 border-red-300';
      case 'SD': return 'bg-yellow-100 text-yellow-700 border-yellow-300';
      case 'PR': return 'bg-emerald-100 text-emerald-700 border-emerald-300';
      case 'CR': return 'bg-green-100 text-green-700 border-green-300';
      default:   return 'bg-slate-100 text-slate-500 border-slate-300';
    }
  };

  const getResponseIcon = (status: string) => {
    switch (status) {
      case 'PD': return <TrendingUp className="w-3 h-3" />;
      case 'SD': return <Minus className="w-3 h-3" />;
      case 'PR': return <TrendingDown className="w-3 h-3" />;
      case 'CR': return <CheckCircle className="w-3 h-3" />;
      default:   return null;
    }
  };

  const isImagingTimelineEvent = (event: TimelineEvent): boolean => {
    const normalizedType = (event.type || '').toLowerCase();
    return normalizedType === 'imaging' || normalizedType === 'diagnosticreport';
  };

  const getResponseForImagingEvent = useMemo(() => {
    const bySourceId = new Map<string, TreatmentResponseItem>();
    for (const response of treatmentResponses) {
      for (const sourceId of response.imaging_source_ids) {
        if (!bySourceId.has(sourceId)) {
          bySourceId.set(sourceId, response);
        }
      }
    }
    return (event: TimelineEvent): TreatmentResponseItem | undefined => {
      if (!event.source_id || !isImagingTimelineEvent(event)) return undefined;
      return bySourceId.get(event.source_id);
    };
  }, [treatmentResponses]);

  const effectiveResponseByEventId = useMemo(() => {
    const datedEvents = [...events]
      .filter(hasUsableDate)
      .sort((a, b) => new Date(a.date as string).getTime() - new Date(b.date as string).getTime());

    const anchors = datedEvents
      .map((event) => ({
        event,
        response: getResponseForImagingEvent(event),
      }))
      .filter((item): item is { event: TimelineEvent; response: TreatmentResponseItem } => !!item.response);

    const markerByEventId = new Map<string, EffectiveResponseMarker>();

    for (const anchor of anchors) {
      markerByEventId.set(anchor.event.id, {
        response: anchor.response,
        source: 'direct',
        anchorEventId: anchor.event.id,
      });
    }

    if (anchors.length > 0) {
      const firstAnchor = anchors[0];
      const firstTs = new Date(firstAnchor.event.date as string).getTime();
      for (const event of datedEvents) {
        const eventTs = new Date(event.date as string).getTime();
        if (eventTs >= firstTs) continue;
        if (markerByEventId.has(event.id)) continue;
        markerByEventId.set(event.id, {
          response: firstAnchor.response,
          source: 'carried',
          anchorEventId: firstAnchor.event.id,
        });
      }
    }

    for (let i = 0; i < anchors.length - 1; i += 1) {
      const startAnchor = anchors[i];
      const endAnchor = anchors[i + 1];
      const startTs = new Date(startAnchor.event.date as string).getTime();
      const endTs = new Date(endAnchor.event.date as string).getTime();
      for (const event of datedEvents) {
        const eventTs = new Date(event.date as string).getTime();
        if (eventTs <= startTs || eventTs >= endTs) continue;
        if (markerByEventId.has(event.id)) continue;
        markerByEventId.set(event.id, {
          response: endAnchor.response,
          source: 'carried',
          anchorEventId: endAnchor.event.id,
        });
      }
    }

    return markerByEventId;
  }, [events, getResponseForImagingEvent]);

  const getEffectiveResponseForEvent = (event: TimelineEvent): EffectiveResponseMarker | undefined =>
    effectiveResponseByEventId.get(event.id);

  const getResponseRailColor = (status?: string): string => {
    switch (status) {
      case 'PD': return '#ef4444';
      case 'SD': return '#eab308';
      case 'PR': return '#10b981';
      case 'CR': return '#16a34a';
      case 'ND': return '#06b6d4';
      default: return '#cbd5e1';
    }
  };

  const formatDate = (dateStr?: string) => {
    if (!dateStr) return 'Unknown date';
    try {
      return new Date(dateStr).toLocaleDateString('en-US', {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
      });
    } catch {
      return dateStr;
    }
  };

  // Close dropdown on click outside
  useEffect(() => {
    const handleClickOutside = (e: MouseEvent) => {
      if (filterRef.current && !filterRef.current.contains(e.target as Node)) {
        setFilterOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, []);

  const toggleFilter = (type: string) => {
    setSelectedFilters(prev => {
      const next = new Set(prev);
      if (next.has(type)) {
        next.delete(type);
      } else {
        next.add(type);
      }
      return next;
    });
  };

  // Filter and sort events
  let filteredEvents = [...events].filter(hasUsableDate);

  // Apply type filter (empty selection = show all)
  if (selectedFilters.size > 0) {
    filteredEvents = filteredEvents.filter(e => {
      const eventType = e.type.toLowerCase();
      return Array.from(selectedFilters).some(f => {
        const filterType = f.toLowerCase();
        if (filterType === 'therapy') {
          return eventType.includes('chemo') || eventType.includes('radiation') ||
                 eventType.includes('surgery') || eventType.includes('medication');
        }
        return eventType.includes(filterType);
      });
    });
  }

  // Sort by date (newest first)
  filteredEvents.sort((a, b) => {
    const dateA = a.date ? new Date(a.date).getTime() : 0;
    const dateB = b.date ? new Date(b.date).getTime() : 0;
    return dateB - dateA;
  });

  const lastDirectResponseTsInView = filteredEvents.reduce<number | null>((acc, event) => {
    if (!event.date) return acc;
    const marker = getEffectiveResponseForEvent(event);
    if (!marker || marker.source !== 'direct') return acc;
    const ts = new Date(event.date).getTime();
    if (Number.isNaN(ts)) return acc;
    return acc === null || ts > acc ? ts : acc;
  }, null);

  const getResponseRailStatusForEvent = (event: TimelineEvent): string | undefined => {
    const marker = getEffectiveResponseForEvent(event);
    if (marker?.response?.status) {
      return marker.response.status;
    }
    if (lastDirectResponseTsInView !== null && event.date) {
      const ts = new Date(event.date).getTime();
      if (!Number.isNaN(ts) && ts > lastDirectResponseTsInView) {
        return 'ND';
      }
    }
    return undefined;
  };

  const hasNoDiseaseSegment =
    lastDirectResponseTsInView !== null &&
    filteredEvents.some((event) => getResponseRailStatusForEvent(event) === 'ND');

  if (loading) {
    return (
      <div className="w-full bg-white rounded-xl shadow-sm border border-slate-200 p-8">
        <div className="flex flex-col items-center justify-center gap-2">
          <div className="flex items-center">
            <Loader2 className="w-6 h-6 animate-spin text-lmu-green" />
            <span className="ml-2 text-slate-500">Loading timeline...</span>
          </div>
          <span className="text-xs text-slate-400">Fetching live FHIR data from server</span>
        </div>
      </div>
    );
  }

  if (error) {
    return (
      <div className="w-full bg-white rounded-xl shadow-sm border border-red-200 p-4">
        <div className="flex items-center gap-2 text-red-700">
          <AlertCircle className="w-5 h-5" />
          <span>{error}</span>
        </div>
        <button
          onClick={loadTimeline}
          className="mt-2 text-sm text-red-600 hover:text-red-700 flex items-center gap-1"
        >
          <RefreshCw className="w-4 h-4" />
          Retry
        </button>
      </div>
    );
  }

  return (
    <div className="w-full bg-white rounded-xl shadow-sm border border-slate-200 p-4 relative">
      <div className="flex items-center justify-between mb-4 flex-wrap gap-2">
        <h3 className="text-sm font-semibold text-slate-700 uppercase tracking-wide flex items-center gap-2">
          <Calendar className="w-4 h-4" />
          Clinical Timeline
          {patientId && (
            <button
              onClick={loadTimeline}
              className="p-1 text-slate-400 hover:text-slate-600 rounded transition-colors"
              title="Refresh timeline"
            >
              <RefreshCw className="w-3 h-3" />
            </button>
          )}
        </h3>

        <div className="flex items-center gap-2">

          {/* Treatment Response Layer Toggle */}
          <label className="flex items-center gap-1.5 text-xs text-slate-600 cursor-pointer">
            <input
              type="checkbox"
              checked={showResponseLayer}
              onChange={(e) => setShowResponseLayer(e.target.checked)}
              className="rounded border-slate-300 text-lmu-green focus:ring-lmu-green"
            />
            Treatment response intervals
          </label>

          {/* Type Filter (multi-select) */}
          <div className="relative" ref={filterRef}>
            <button
              onClick={() => setFilterOpen(prev => !prev)}
              className="flex items-center gap-1.5 pl-2.5 pr-3 py-1.5 text-sm font-medium text-slate-600 bg-slate-50 border border-slate-200 rounded-lg focus:outline-none focus:ring-2 focus:ring-lmu-green/20 focus:border-lmu-green transition-all cursor-pointer hover:bg-slate-100"
            >
              <Filter className="w-3.5 h-3.5 text-slate-400" />
              {selectedFilters.size === 0
                ? 'All types'
                : selectedFilters.size === 1
                  ? (eventTypeMap[Array.from(selectedFilters)[0]] || Array.from(selectedFilters)[0])
                  : `${selectedFilters.size} types`}
              <svg className={`w-4 h-4 text-slate-400 transition-transform ${filterOpen ? 'rotate-180' : ''}`} viewBox="0 0 20 20" fill="none"><path stroke="currentColor" strokeLinecap="round" strokeLinejoin="round" strokeWidth="1.5" d="M6 8l4 4 4-4" /></svg>
            </button>

            {filterOpen && filtersAvailable.length > 0 && (
              <div className="absolute right-0 top-full mt-1 z-30 bg-white border border-slate-200 rounded-lg shadow-lg py-1 min-w-[10rem]">
                {filtersAvailable.map((t) => (
                  <label
                    key={t}
                    className="flex items-center gap-2 px-3 py-1.5 text-sm text-slate-700 hover:bg-slate-50 cursor-pointer"
                  >
                    <input
                      type="checkbox"
                      checked={selectedFilters.has(t)}
                      onChange={() => toggleFilter(t)}
                      className="rounded border-slate-300 text-lmu-green focus:ring-lmu-green"
                    />
                    {eventTypeMap[t] || t}
                  </label>
                ))}
                {selectedFilters.size > 0 && (
                  <>
                    <div className="border-t border-slate-100 my-1" />
                    <button
                      onClick={() => setSelectedFilters(new Set())}
                      className="w-full text-left px-3 py-1.5 text-xs text-slate-500 hover:text-slate-700 hover:bg-slate-50"
                    >
                      Clear all
                    </button>
                  </>
                )}
              </div>
            )}
          </div>
        </div>
      </div>

      {filteredEvents.length === 0 ? (
        <div className="text-center py-8 text-slate-500">
          <Calendar className="w-8 h-8 mx-auto mb-2 opacity-50" />
          <p>No events to display</p>
        </div>
      ) : (
        <div className="relative">
          {/* Connection Line — shift down when response layer is active to avoid overlapping badges */}
          <div className={`absolute left-0 right-0 h-0.5 bg-slate-100 -z-0 ${showResponseLayer ? 'top-14' : 'top-8'}`}></div>

          <div className="overflow-x-auto px-1 py-2 pb-6 snap-x">
            {showResponseLayer && (
              <>
              <div className="flex mb-2">
                {filteredEvents.map((event, index) => {
                  const currentStatus = getResponseRailStatusForEvent(event);
                  const current = currentStatus ? { status: currentStatus } : undefined;
                  const next = index < filteredEvents.length - 1
                    ? (() => {
                        const nextStatus = getResponseRailStatusForEvent(filteredEvents[index + 1]);
                        return nextStatus ? { status: nextStatus } : undefined;
                      })()
                    : current;
                  const fromColor = getResponseRailColor(current?.status);
                  const toColor = getResponseRailColor(next?.status);
                  return (
                    <div
                      key={`${event.id}-response-rail`}
                      className="flex-shrink-0"
                      style={{ width: index === filteredEvents.length - 1 ? '13rem' : '14rem' }}
                    >
                      <div
                        className="h-1 rounded-full"
                        style={{ background: `linear-gradient(90deg, ${fromColor} 0%, ${toColor} 100%)` }}
                      />
                    </div>
                  );
                })}
              </div>
              </>
            )}

            <div className="flex gap-4">
              {filteredEvents.map((event) => {
                const responseMarker = showResponseLayer ? getEffectiveResponseForEvent(event) : undefined;
                const response = responseMarker?.source === 'direct' ? responseMarker.response : undefined;

                return (
                  <div
                    key={event.id}
                    onClick={() => setSelectedEvent(event)}
                    className="flex-shrink-0 w-52 snap-start cursor-pointer group"
                  >
                    {/* Treatment Response Pill — direct response reports only */}
                    {showResponseLayer && (
                      <div className="h-6 mb-1 flex items-end">
                        {response && (
                          <div
                            className={`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-semibold border ${getResponseColor(response.status)}`}
                            title={response.basis || response.status_label}
                          >
                            {getResponseIcon(response.status)}
                            {response.status_label}
                            {response.confidence === 'low' && (
                              <span className="opacity-60">?</span>
                            )}
                          </div>
                        )}
                      </div>
                    )}

                    <div
                      className={`relative z-10 w-8 h-8 rounded-full flex items-center justify-center border-2 mb-3 bg-white ${
                        getColor(event.type).split(' ')[1]
                      } ${getColor(event.type).split(' ')[2]}`}
                    >
                      {getIcon(event.type)}
                    </div>
                    <div
                      className={`p-3 rounded-lg border transition-all hover:shadow-md ${
                        selectedEvent?.id === event.id
                          ? 'ring-2 ring-lmu-green border-transparent'
                          : 'border-slate-200 bg-white'
                      }`}
                    >
                      <div className="text-xs text-slate-500 mb-1">{formatDate(event.date)}</div>
                      <div className="font-medium text-slate-900 text-sm truncate">{event.title}</div>
                      {event.key_insight && (
                        <div className="text-xs text-slate-600 mt-1 line-clamp-3">
                          {event.key_insight}
                        </div>
                      )}
                      <div className="flex items-center justify-between mt-1">
                        <span className={`text-xs px-1.5 py-0.5 rounded ${getColor(event.type)}`}>
                          {eventTypeMap[event.type] || event.type}
                        </span>
                        {event.priority && event.priority <= 2 && (
                          <span className="text-xs text-red-600 font-medium">High Priority</span>
                        )}
                      </div>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
        </div>
      )}

      {/* Detail Popover/Modal Overlay */}
      {selectedEvent && (
        <div className="absolute top-16 left-0 right-0 z-20 mx-4">
          <div className="bg-white text-slate-900 p-4 rounded-lg shadow-xl animate-in fade-in slide-in-from-top-2 duration-200 border border-slate-200">
            <div className="flex justify-between items-start mb-3">
              <div className="flex flex-col gap-1">
                <div className="flex items-center gap-2">
                  <span className="text-xs font-bold px-2 py-0.5 rounded bg-slate-100 text-slate-700">
                    {eventTypeMap[selectedEvent.type] || selectedEvent.type}
                  </span>
                  <span className="text-sm text-slate-500">{formatDate(selectedEvent.date)}</span>
                </div>
              </div>
              <div className="flex items-start gap-4">
                {selectedEvent.source_id && (
                  <div className="text-right hidden sm:block">
                    <div className="text-[10px] uppercase tracking-wider text-slate-500 font-medium">Source</div>
                    <SourceBadge sourceId={selectedEvent.source_id} label="View" sourceType="fhir" className="mt-1" />
                  </div>
                )}
                <button onClick={(e) => { e.stopPropagation(); setSelectedEvent(null); }}>
                  <X className="w-5 h-5 text-slate-400 hover:text-slate-700 transition-colors" />
                </button>
              </div>
            </div>

            {selectedEvent.source_id && (
              <div className="sm:hidden mb-2 text-xs text-slate-500 flex items-center gap-2">
                <span className="uppercase tracking-wider font-medium">Source:</span>
                <SourceBadge sourceId={selectedEvent.source_id} label="View" sourceType="fhir" />
              </div>
            )}

            <h4 className="font-semibold text-lg mb-1">{selectedEvent.title}</h4>

            {selectedEvent.key_insight && (
              <div className="bg-emerald-50 border border-emerald-200 p-3 rounded text-sm text-emerald-900 mt-3">
                <div className="font-semibold text-xs uppercase tracking-wider mb-1 text-emerald-700">
                  Key Insight
                </div>
                {selectedEvent.key_insight}
              </div>
            )}

            {/* Treatment Response Detail (direct report marker only) */}
            {(() => {
              const marker = getEffectiveResponseForEvent(selectedEvent);
              const resp = marker?.source === 'direct' ? marker.response : undefined;
              return resp ? (
                <div className={`mt-3 p-3 rounded border ${getResponseColor(resp.status)}`}>
                  <div className="font-semibold text-xs uppercase tracking-wider mb-1">
                    Treatment Response
                  </div>
                  <div className="flex items-center gap-2 text-sm font-medium">
                    {getResponseIcon(resp.status)}
                    <span>{resp.status_label}</span>
                    <span className="text-xs opacity-70">({resp.confidence} confidence)</span>
                  </div>
                  {resp.basis && (
                    <div className="text-xs mt-1 opacity-80">{resp.basis}</div>
                  )}
                  {resp.imaging_source_ids.length > 0 && (
                    <div className="mt-2 flex gap-1">
                      {resp.imaging_source_ids.map(sid => (
                        <SourceBadge key={sid} sourceId={sid} label="Imaging" sourceType="fhir" />
                      ))}
                    </div>
                  )}
                </div>
              ) : null;
            })()}

            {selectedEvent.priority && (
              <div className="mt-3 text-xs text-slate-500">
                Priority: {selectedEvent.priority}/5
              </div>
            )}
          </div>
        </div>
      )}
    </div>
  );
};

export default Timeline;
