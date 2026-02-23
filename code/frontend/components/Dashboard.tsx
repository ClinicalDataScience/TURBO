import React, { useState, useEffect } from 'react';
import { useParams, useNavigate, useLocation } from 'react-router-dom';
import Timeline from './Timeline';
import SummaryPanel from './Summary/SummaryPanel';
import Chatbot from './Chatbot';
import MissingInfoModal from './MissingInfoModal';
import { ArrowLeft, UserCircle, FileText, Edit2 } from 'lucide-react';
import { getSourceList, checkSummaryCache } from '../services/api';
import type { MissingInfoItem } from '../types';

const Dashboard: React.FC = () => {
  const { patientId } = useParams();
  const navigate = useNavigate();
  const location = useLocation();
  const navState = location.state as { clinicalQuestion?: string; fragestellung?: string } | null;

  const [clinicalQuestion, setClinicalQuestion] = useState<string>(navState?.clinicalQuestion || navState?.fragestellung || '');
  const [editingClinicalQuestion, setEditingClinicalQuestion] = useState(false);
  const [tempClinicalQuestion, setTempClinicalQuestion] = useState('');
  const [chatQuestion, setChatQuestion] = useState<string | undefined>();
  const [patientName, setPatientName] = useState<string>('');
  const [patientAge, setPatientAge] = useState<number | null>(null);
  const [patientGender, setPatientGender] = useState<string>('');
  const [patientDataLoaded, setPatientDataLoaded] = useState(false);

  // Clinical question prompt state (shown when no cached summary exists and none was passed via nav)
  const [showClinicalQuestionPrompt, setShowClinicalQuestionPrompt] = useState(false);
  const [promptClinicalQuestion, setPromptClinicalQuestion] = useState('');

  // Missing info popup state
  const [missingInfo, setMissingInfo] = useState<MissingInfoItem[]>([]);
  const [showMissingInfoModal, setShowMissingInfoModal] = useState(false);
  const [summaryRefreshKey, setSummaryRefreshKey] = useState(0);

  // If no clinical question was explicitly selected (via "Use" button), prompt only
  // when there is no existing base summary cached for this patient.
  useEffect(() => {
    if (!patientId) return;
    if (navState?.clinicalQuestion || navState?.fragestellung) return;
    checkSummaryCache(patientId)
      .then((result) => {
        // Show prompt only if the patient has no cached summaries at all
        if (result.entries.length === 0) {
          setShowClinicalQuestionPrompt(true);
        }
      })
      .catch(() => {
        // If cache check fails, show prompt as fallback
        setShowClinicalQuestionPrompt(true);
      });
  }, [patientId]);

  const handleClinicalQuestionPromptSubmit = () => {
    if (promptClinicalQuestion.trim()) {
      setClinicalQuestion(promptClinicalQuestion.trim());
    }
    setShowClinicalQuestionPrompt(false);
  };

  const handleClinicalQuestionPromptSkip = () => {
    setShowClinicalQuestionPrompt(false);
  };

  // Fetch patient name quickly from source list (fast DB query, no LLM)
  useEffect(() => {
    if (!patientId) return;
    const controller = new AbortController();
    getSourceList({ patient_id: patientId, source_type: 'fhir', signal: controller.signal })
      .then((list) => {
        const patientResource = list.items.find(item => item.resource_type === 'Patient');
        setPatientName(patientResource?.title?.trim() || `Patient ${patientId}`);
        setPatientDataLoaded(true);
      })
      .catch((e) => {
        if (e.name === 'AbortError') return;
        setPatientName(`Patient ${patientId}`);
        setPatientDataLoaded(true);
      });
    return () => controller.abort();
  }, [patientId]);

  // Demographics are populated from SummaryPanel via handleSummaryLoaded callback.

  // Handle "Ask in chat" from summary panel
  const handleAskQuestion = (question: string) => {
    setChatQuestion(question);
    // Reset after a short delay to allow for multiple questions
    setTimeout(() => setChatQuestion(undefined), 100);
  };

  // Receive demographics from SummaryPanel once summary is loaded
  const handleSummaryLoaded = (demographics: { name?: string | null; age?: number | null; gender?: string | null }) => {
    if (demographics.name) setPatientName(demographics.name);
    setPatientAge(demographics.age || null);
    setPatientGender(demographics.gender || '');
  };

  // Handle missing info detected from summary (store only — modal opened explicitly by user click)
  const handleMissingInfo = (items: MissingInfoItem[]) => {
    setMissingInfo(items);
  };

  const handleCloseMissingInfoModal = () => {
    setShowMissingInfoModal(false);
  };

  const handleSummaryUpdated = () => {
    setSummaryRefreshKey(k => k + 1);
  };

  const handleOpenMissingInfoModal = (items: MissingInfoItem[], selectedItem?: MissingInfoItem) => {
    if (items.length === 0) return;

    if (selectedItem) {
      const prioritized = [
        selectedItem,
        ...items.filter(
          item => !(item.field === selectedItem.field && item.question === selectedItem.question),
        ),
      ];
      setMissingInfo(prioritized);
    } else {
      setMissingInfo(items);
    }

    setShowMissingInfoModal(true);
  };

  const handleClinicalQuestionEdit = () => {
    setTempClinicalQuestion(clinicalQuestion);
    setEditingClinicalQuestion(true);
  };

  const handleClinicalQuestionSave = () => {
    setClinicalQuestion(tempClinicalQuestion);
    setEditingClinicalQuestion(false);
  };

  if (!patientId) {
    return (
      <div className="min-h-screen flex items-center justify-center bg-lmu-gray">
        <div className="text-center">
          <h2 className="text-xl font-bold text-lmu-text">Patient ID Required</h2>
          <button onClick={() => navigate('/')} className="mt-4 text-lmu-green hover:underline">
            Back to List
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="h-screen bg-lmu-gray flex flex-col overflow-hidden">
      {/* Top Header */}
      <header className="bg-white border-b border-slate-200 z-30 px-4 py-2 flex items-center justify-between shadow-sm">
        <div className="flex items-center gap-4">
          <button
            onClick={() => navigate('/')}
            className="p-2 hover:bg-slate-100 rounded-full transition-colors text-slate-500"
          >
            <ArrowLeft className="w-5 h-5" />
          </button>

          <div className="flex items-center gap-3">
            <div className="w-10 h-10 rounded-full bg-slate-100 text-slate-600 flex items-center justify-center">
              <UserCircle className="w-6 h-6" />
            </div>
            <div>
              <h1 className="font-bold text-slate-900 leading-tight">{patientName || `Patient ${patientId}`}</h1>
              <div className="text-xs text-slate-500 flex gap-2">
                <span className="font-mono bg-slate-100 px-1 rounded">{patientId}</span>
                {patientAge && <><span>•</span><span>{patientAge} yrs</span></>}
                {patientGender && <><span>•</span><span>{patientGender}</span></>}
              </div>
            </div>
          </div>
        </div>

        {/* Clinical Question */}
        <div className="flex-1 max-w-xl mx-8">
          {editingClinicalQuestion ? (
            <div className="flex items-center gap-2">
              <input
                type="text"
                value={tempClinicalQuestion}
                onChange={(e) => setTempClinicalQuestion(e.target.value)}
                placeholder="Enter additional clinical question..."
                className="flex-1 px-3 py-1.5 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-lmu-green/20 focus:border-lmu-green"
                autoFocus
                onKeyDown={(e) => e.key === 'Enter' && handleClinicalQuestionSave()}
              />
              <button
                onClick={handleClinicalQuestionSave}
                className="px-3 py-1.5 bg-lmu-green text-white text-sm rounded-lg hover:bg-lmu-green-dark"
              >
                Save
              </button>
              <button
                onClick={() => setEditingClinicalQuestion(false)}
                className="px-3 py-1.5 text-slate-600 text-sm hover:bg-slate-100 rounded-lg"
              >
                Cancel
              </button>
            </div>
          ) : (
            <button
              onClick={handleClinicalQuestionEdit}
              className="w-full flex items-center gap-2 px-3 py-1.5 text-left text-sm text-slate-600 bg-slate-50 border border-slate-200 rounded-lg hover:bg-slate-100 transition-colors"
            >
              <FileText className="w-4 h-4 text-slate-400" />
              {clinicalQuestion || 'Click to add additional clinical question...'}
              <Edit2 className="w-3 h-3 ml-auto text-slate-400" />
            </button>
          )}
        </div>

        <div className="flex items-center gap-3">
          <div className="text-right hidden sm:block">
            <div className="text-[10px] uppercase text-slate-400 font-bold tracking-wider">Tumor Board Status</div>
            <div className="text-xs font-medium text-green-600 bg-green-50 px-2 py-0.5 rounded-full inline-block">Ready for Review</div>
          </div>
        </div>
      </header>

      {/* Main Content Grid */}
      <main className="flex-1 p-3 md:p-4 grid grid-cols-1 lg:grid-cols-12 grid-rows-[auto_1fr] gap-4 max-w-[1800px] mx-auto w-full min-h-0">

        {/* Timeline (row 1, full width) */}
        <div className="lg:col-span-12">
          <Timeline patientId={patientId} />
        </div>

        {/* Clinical Summary (row 2, cols 1-9) */}
        <div className="lg:col-span-9 bg-white rounded-xl shadow-sm border border-slate-200 p-4 overflow-y-auto min-h-0">
          <SummaryPanel
            patientId={patientId}
            clinicalQuestion={clinicalQuestion || undefined}
            onAskQuestion={handleAskQuestion}
            onMissingInfo={handleMissingInfo}
            onOpenMissingInfoModal={handleOpenMissingInfoModal}
            onSummaryLoaded={handleSummaryLoaded}
            refreshKey={summaryRefreshKey}
          />
        </div>

        {/* Chat (row 2, cols 10-12) */}
        <div className="lg:col-span-3 min-h-0 h-[calc(100vh-200px)] lg:h-full">
          {patientDataLoaded ? (
            <Chatbot
              patientId={patientId}
              patientName={patientName || `Patient ${patientId}`}
              initialQuestion={chatQuestion}
            />
          ) : (
            <div className="flex h-full items-center justify-center bg-white rounded-xl shadow-sm border border-slate-200">
              <div className="flex flex-col items-center gap-3 text-slate-400">
                <div className="w-6 h-6 border-2 border-slate-200 border-t-lmu-green rounded-full animate-spin" />
                <span className="text-sm">Loading patient data...</span>
              </div>
            </div>
          )}
        </div>

      </main>

      {/* Clinical Question Prompt Modal */}
      {showClinicalQuestionPrompt && (
        <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
          <div className="bg-white rounded-xl shadow-xl w-full max-w-md mx-4 overflow-hidden">
            <div className="p-6 border-b border-slate-100">
              <div className="flex items-center gap-3">
                <div className="bg-lmu-green-100 p-2 rounded-lg">
                  <FileText className="w-5 h-5 text-lmu-green" />
                </div>
                <div>
                  <h3 className="font-semibold text-slate-900">Additional Clinical Question</h3>
                  <p className="text-sm text-slate-500">Optionally add a specific question for this patient review</p>
                </div>
              </div>
            </div>

            <div className="p-6">
              <p className="text-xs text-slate-500 mb-3 bg-slate-50 rounded-lg p-2">
                The summary always addresses: <em>next steps, additional diagnostics needed, therapy adequacy, and whether to switch therapy.</em>
              </p>
              <label className="block text-sm font-medium text-slate-700 mb-2">
                Additional Question (optional)
              </label>
              <textarea
                value={promptClinicalQuestion}
                onChange={(e) => setPromptClinicalQuestion(e.target.value)}
                placeholder="e.g. Recommendation for next-line therapy after progression on first-line treatment..."
                className="w-full px-3 py-2 border border-slate-300 rounded-lg text-sm focus:outline-none focus:ring-2 focus:ring-lmu-green/20 focus:border-lmu-green resize-none"
                rows={3}
                autoFocus
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) {
                    e.preventDefault();
                    handleClinicalQuestionPromptSubmit();
                  }
                }}
              />
              <p className="text-xs text-slate-400 mt-2">
                This will be added alongside the base clinical question. You can change it later.
              </p>
              <div className="flex justify-end gap-2 mt-4">
                <button
                  onClick={handleClinicalQuestionPromptSkip}
                  className="px-4 py-2 text-sm text-slate-600 hover:bg-slate-100 rounded-lg transition-colors"
                >
                  Skip
                </button>
                <button
                  onClick={handleClinicalQuestionPromptSubmit}
                  className="px-4 py-2 text-sm bg-lmu-green text-white rounded-lg hover:bg-lmu-green-dark transition-colors"
                >
                  Continue
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Missing Info Popup Chatbot */}
      {showMissingInfoModal && missingInfo.length > 0 && (
        <MissingInfoModal
          patientId={patientId}
          patientName={patientName || `Patient ${patientId}`}
          clinicalQuestion={clinicalQuestion || undefined}
          missingInfo={missingInfo}
          onClose={handleCloseMissingInfoModal}
          onSummaryUpdated={handleSummaryUpdated}
        />
      )}
    </div>
  );
};

export default Dashboard;
