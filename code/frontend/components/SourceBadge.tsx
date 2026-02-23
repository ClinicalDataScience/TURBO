import React, { useState, useRef, useEffect } from 'react';
import { createPortal } from 'react-dom';
import ReactMarkdown from 'react-markdown';
import remarkGfm from 'remark-gfm';
import { getSourceDetail } from '../services/api';
import type { SourceDetailResponse } from '../types';
import { FileText, Database, User, Loader2 } from 'lucide-react';

interface SourceBadgeProps {
  sourceId: string;
  label?: string;
  sourceType?: 'fhir' | 'milvus' | 'user_input';
  className?: string;
}

type NormalizedSourceType = 'fhir' | 'milvus' | 'user_input';

const SourceBadge: React.FC<SourceBadgeProps> = ({ sourceId, label, sourceType, className = '' }) => {
  const [showPopup, setShowPopup] = useState(false);
  const [loading, setLoading] = useState(false);
  const [content, setContent] = useState<SourceDetailResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [popupCoords, setPopupCoords] = useState({ top: 0, left: 0 });
  const [popupVertical, setPopupVertical] = useState<'top' | 'bottom'>('bottom');
  const [popupHorizontal, setPopupHorizontal] = useState<'left' | 'right'>('left');
  const badgeRef = useRef<HTMLSpanElement>(null);
  const popupRef = useRef<HTMLDivElement>(null);
  const closeTimeoutRef = useRef<number | null>(null);

  const normalizeSourceType = (type?: string): NormalizedSourceType | undefined => {
    if (!type) return undefined;
    const normalized = type.trim().toLowerCase().replace(/[\s-]+/g, '_');
    if (normalized === 'fhir' || normalized === 'milvus' || normalized === 'user_input') {
      return normalized;
    }
    return undefined;
  };

  const loadSourceDetail = async () => {
    if (content || loading) return;
    setLoading(true);
    setError(null);
    try {
      const detail = await getSourceDetail(sourceId);
      setContent(detail);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Failed to load source');
    } finally {
      setLoading(false);
    }
  };

  const handleMouseEnter = async () => {
    // Clear any pending close timeout
    if (closeTimeoutRef.current) {
      clearTimeout(closeTimeoutRef.current);
      closeTimeoutRef.current = null;
    }

    setShowPopup(true);

    // Calculate popup position relative to viewport
    if (badgeRef.current) {
      const rect = badgeRef.current.getBoundingClientRect();
      const spaceBelow = window.innerHeight - rect.bottom;
      const spaceAbove = rect.top;
      const spaceRight = window.innerWidth - rect.left;

      const verticalPos = spaceBelow < 300 && spaceAbove > 300 ? 'top' : 'bottom';
      const horizontalPos = spaceRight < 320 ? 'right' : 'left';

      setPopupVertical(verticalPos);
      setPopupHorizontal(horizontalPos);

      // Calculate fixed position coordinates
      const top = verticalPos === 'top' ? rect.top - 8 : rect.bottom + 8;
      const left = horizontalPos === 'right' ? rect.right : rect.left;

      setPopupCoords({ top, left });
    }

    // Fetch content if not already loaded
    await loadSourceDetail();
  };

  const handleMouseLeave = () => {
    // Delay closing to allow moving to popup
    closeTimeoutRef.current = window.setTimeout(() => {
      setShowPopup(false);
    }, 200);
  };

  const handlePopupMouseEnter = () => {
    // Cancel close if hovering over popup
    if (closeTimeoutRef.current) {
      clearTimeout(closeTimeoutRef.current);
      closeTimeoutRef.current = null;
    }
  };

  const handlePopupMouseLeave = () => {
    // Close when leaving popup
    setShowPopup(false);
  };

  // Update popup position on scroll/resize and close on outside click
  useEffect(() => {
    if (!showPopup) return;

    const updatePosition = () => {
      if (badgeRef.current) {
        const rect = badgeRef.current.getBoundingClientRect();
        const spaceBelow = window.innerHeight - rect.bottom;
        const spaceAbove = rect.top;
        const spaceRight = window.innerWidth - rect.left;

        const verticalPos = spaceBelow < 300 && spaceAbove > 300 ? 'top' : 'bottom';
        const horizontalPos = spaceRight < 320 ? 'right' : 'left';

        setPopupVertical(verticalPos);
        setPopupHorizontal(horizontalPos);

        const top = verticalPos === 'top' ? rect.top - 8 : rect.bottom + 8;
        const left = horizontalPos === 'right' ? rect.right : rect.left;

        setPopupCoords({ top, left });
      }
    };

    const handleClickOutside = (e: MouseEvent) => {
      if (
        badgeRef.current &&
        popupRef.current &&
        !badgeRef.current.contains(e.target as Node) &&
        !popupRef.current.contains(e.target as Node)
      ) {
        setShowPopup(false);
      }
    };

    window.addEventListener('scroll', updatePosition, true);
    window.addEventListener('resize', updatePosition);
    document.addEventListener('mousedown', handleClickOutside);

    return () => {
      window.removeEventListener('scroll', updatePosition, true);
      window.removeEventListener('resize', updatePosition);
      document.removeEventListener('mousedown', handleClickOutside);

      // Clear any pending timeout
      if (closeTimeoutRef.current) {
        clearTimeout(closeTimeoutRef.current);
        closeTimeoutRef.current = null;
      }
    };
  }, [showPopup]);

  // Resolve type without requiring hover when sourceType isn't provided.
  useEffect(() => {
    if (sourceType || content || loading) return;
    void loadSourceDetail();
  }, [sourceType, sourceId, content, loading]);

  // Resolve source type: prefer fetched content, fall back to prop
  const resolvedType = normalizeSourceType(content?.source_type) ?? normalizeSourceType(sourceType);

  // Get icon based on source type
  const getSourceIcon = () => {
    switch (resolvedType) {
      case 'fhir':
        return <Database className="w-3 h-3" />;
      case 'milvus':
        return <FileText className="w-3 h-3" />;
      case 'user_input':
        return <User className="w-3 h-3" />;
      default:
        return <FileText className="w-3 h-3" />;
    }
  };

  // Get badge color based on source type
  const getBadgeColor = () => {
    switch (resolvedType) {
      case 'fhir':
        return 'bg-lmu-green-100 text-lmu-green-dark hover:bg-lmu-green-light';
      case 'milvus':
        return 'bg-purple-100 text-purple-700 hover:bg-purple-200';
      case 'user_input':
        return 'bg-green-100 text-green-700 hover:bg-green-200';
      default:
        return 'bg-slate-100 text-slate-600 hover:bg-slate-200';
    }
  };

  return (
    <span
      ref={badgeRef}
      className={`inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-xs font-medium cursor-pointer transition-colors relative ${getBadgeColor()} ${className}`}
      onMouseEnter={handleMouseEnter}
      onMouseLeave={handleMouseLeave}
    >
      {getSourceIcon()}
      <span>{label || 'src'}</span>

      {/* Popup - rendered via portal to avoid clipping */}
      {showPopup && createPortal(
        <div
          ref={popupRef}
          className="fixed z-50 w-80 max-h-96 overflow-auto bg-white border border-slate-200 rounded-lg shadow-xl"
          style={{
            top: popupVertical === 'top' ? 'auto' : `${popupCoords.top}px`,
            bottom: popupVertical === 'top' ? `${window.innerHeight - popupCoords.top}px` : 'auto',
            left: popupHorizontal === 'right' ? 'auto' : `${popupCoords.left}px`,
            right: popupHorizontal === 'right' ? `${window.innerWidth - popupCoords.left}px` : 'auto',
          }}
          onMouseEnter={handlePopupMouseEnter}
          onMouseLeave={handlePopupMouseLeave}
          onClick={(e) => e.stopPropagation()}
        >
          {loading && (
            <div className="p-4 flex items-center justify-center">
              <Loader2 className="w-5 h-5 animate-spin text-slate-400" />
              <span className="ml-2 text-sm text-slate-500">Loading...</span>
            </div>
          )}

          {error && (
            <div className="p-4 text-sm text-red-600">
              {error}
            </div>
          )}

          {content && !loading && (
            <div className="p-4">
              {/* Header */}
              <div className="flex items-start justify-between mb-3 pb-2 border-b border-slate-100">
                <div>
                  <h4 className="font-semibold text-slate-900 text-sm">
                    {content.title || 'Source'}
                  </h4>
                  <div className="flex items-center gap-2 mt-1">
                    <span className={`inline-flex items-center px-1.5 py-0.5 rounded text-xs font-medium ${getBadgeColor()}`}>
                      {content.source_type}
                    </span>
                    <span className="text-xs text-slate-500">
                      {content.resource_type}
                    </span>
                  </div>
                  {content.date && (
                    <div className="text-xs text-slate-400 mt-1">
                      {new Date(content.date).toLocaleDateString()}
                    </div>
                  )}
                </div>
              </div>

              {/* Content */}
              <div className="prose prose-sm prose-slate max-w-none">
                <ReactMarkdown remarkPlugins={[remarkGfm]}>
                  {content.content_markdown || 'No content available'}
                </ReactMarkdown>
              </div>
            </div>
          )}
        </div>,
        document.body
      )}
    </span>
  );
};

// Component for rendering multiple source badges
interface SourceBadgesProps {
  sourceIds: string[];
  maxVisible?: number;
  sourceType?: 'fhir' | 'milvus' | 'user_input';
  className?: string;
}

export const SourceBadges: React.FC<SourceBadgesProps> = ({
  sourceIds,
  maxVisible = 3,
  sourceType,
  className = '',
}) => {
  const [dropdownOpen, setDropdownOpen] = useState(false);
  const dropdownRef = useRef<HTMLSpanElement>(null);

  // Close dropdown on outside click
  useEffect(() => {
    if (!dropdownOpen) return;
    const handleClick = (e: MouseEvent) => {
      if (dropdownRef.current && !dropdownRef.current.contains(e.target as Node)) {
        setDropdownOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClick);
    return () => document.removeEventListener('mousedown', handleClick);
  }, [dropdownOpen]);

  if (!sourceIds || sourceIds.length === 0) {
    return null;
  }

  const visibleSources = sourceIds.slice(0, maxVisible);
  const hiddenSources = sourceIds.slice(maxVisible);

  return (
    <span className={`inline-flex items-center gap-1 flex-wrap ${className}`}>
      {visibleSources.map((sourceId, index) => (
        <SourceBadge
          key={sourceId}
          sourceId={sourceId}
          label={`${index + 1}`}
          sourceType={sourceType}
        />
      ))}
      {hiddenSources.length > 0 && (
        <span ref={dropdownRef} className="relative inline-flex">
          <button
            onClick={(e) => { e.stopPropagation(); setDropdownOpen(!dropdownOpen); }}
            className="text-xs text-slate-500 hover:text-slate-700 hover:bg-slate-100 px-1 py-0.5 rounded cursor-pointer transition-colors"
          >
            +{hiddenSources.length} more
          </button>
          {dropdownOpen && (
            <span className="absolute top-full left-0 mt-1 z-50 bg-white border border-slate-200 rounded-lg shadow-lg p-1.5 flex flex-col gap-1 min-w-max">
              {hiddenSources.map((sourceId, index) => (
                <SourceBadge
                  key={sourceId}
                  sourceId={sourceId}
                  label={`${maxVisible + index + 1}`}
                  sourceType={sourceType}
                />
              ))}
            </span>
          )}
        </span>
      )}
    </span>
  );
};

export default SourceBadge;
