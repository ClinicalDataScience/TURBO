import React from 'react';
import { X, AlertCircle } from 'lucide-react';
import Chatbot from './Chatbot';
import { updateSummary } from '../services/api';
import type { MissingInfoItem } from '../types';

interface MissingInfoModalProps {
  patientId: string;
  patientName: string;
  clinicalQuestion?: string;
  missingInfo: MissingInfoItem[];
  onClose: () => void;
  onSummaryUpdated?: () => void;
}

const MissingInfoModal: React.FC<MissingInfoModalProps> = ({
  patientId,
  patientName,
  clinicalQuestion,
  missingInfo,
  onClose,
  onSummaryUpdated,
}) => {
  const highPriority = missingInfo.filter(i => i.priority === 'high');
  const mediumPriority = missingInfo.filter(i => i.priority === 'medium');
  const lowPriority = missingInfo.filter(i => i.priority === 'low');

  const formatItems = (items: MissingInfoItem[]) =>
    items.map(i => `- **${i.field}**: ${i.question}`).join('\n');

  const welcomeLines: string[] = [
    `The summary for **${patientName}** is missing some information that may be important for the tumor board discussion.\n`,
  ];

  if (highPriority.length > 0) {
    welcomeLines.push(`**High Priority:**\n${formatItems(highPriority)}\n`);
  }
  if (mediumPriority.length > 0) {
    welcomeLines.push(`**Medium Priority:**\n${formatItems(mediumPriority)}\n`);
  }
  if (lowPriority.length > 0) {
    welcomeLines.push(`**Low Priority:**\n${formatItems(lowPriority)}\n`);
  }

  welcomeLines.push('Please provide any information you have, or ask me to look it up.');

  const welcomeMessage = welcomeLines.join('\n');

  const suggestions = missingInfo
    .sort((a, b) => {
      const order: Record<string, number> = { high: 0, medium: 1, low: 2 };
      return (order[a.priority] ?? 2) - (order[b.priority] ?? 2);
    })
    .slice(0, 4)
    .map(i => i.question);

  const missingFieldNames = missingInfo.map(i => i.field);

  // Context prepended to every query so the agent understands the data collection role
  const queryContext = `[CONTEXT: You are helping a clinician provide missing information for a patient's tumor board summary. The clinician is providing NEW information that is NOT yet in the patient's medical records. Do NOT search existing records to verify this information — accept it as provided by the clinician and acknowledge it. After acknowledging, ask about the next missing field if any remain.
Missing fields: ${missingFieldNames.join(', ')}]`;

  // After each bot response, send user's input to the update endpoint to patch the summary
  const handleBotResponse = async (userText: string, _botText: string) => {
    try {
      await updateSummary({
        patient_id: patientId,
        clinical_question: clinicalQuestion || undefined,
        user_input: userText,
        missing_fields: missingFieldNames,
      });
      if (onSummaryUpdated) {
        onSummaryUpdated();
      }
    } catch (err) {
      // Non-critical: summary update failed but chat continues
      console.warn('Failed to update summary with user input:', err);
    }
  };

  return (
    <div className="fixed inset-0 bg-black/40 flex items-center justify-center z-50">
      <div className="bg-white rounded-xl shadow-xl w-full max-w-lg mx-4 h-[70vh] flex flex-col overflow-hidden">
        {/* Modal header */}
        <div className="px-4 py-3 border-b border-slate-200 flex items-center justify-between bg-amber-50 flex-shrink-0">
          <div className="flex items-center gap-2">
            <AlertCircle className="w-4 h-4 text-amber-600" />
            <h3 className="font-semibold text-sm text-amber-900">Missing Information</h3>
          </div>
          <button
            onClick={onClose}
            className="p-1 hover:bg-amber-100 rounded-full transition-colors text-amber-600"
          >
            <X className="w-4 h-4" />
          </button>
        </div>

        {/* Chatbot fills the rest of the modal */}
        <div className="flex-1 min-h-0">
          <Chatbot
            patientId={patientId}
            patientName={patientName}
            welcomeMessage={welcomeMessage}
            suggestions={suggestions}
            queryContext={queryContext}
            processingMessage="Processing the information you provided..."
            onBotResponse={handleBotResponse}
          />
        </div>
      </div>
    </div>
  );
};

export default MissingInfoModal;
