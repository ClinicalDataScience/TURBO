// ============ Source Types ============

export interface SourceItem {
  source_id: string;
  source_type: 'fhir' | 'milvus' | 'user_input';
  resource_type: string;
  fhir_id?: string;
  milvus_document_id?: string;
  title?: string;
  date?: string;
  preview?: string;
  content_markdown?: string;
}

export interface GetListResponse {
  items: SourceItem[];
  total: number;
}

// ============ Timeline Types ============

export interface TimelineEvent {
  id: string;
  source_id: string;
  type: 'Initial Diagnosis' | 'Imaging' | 'Biopsy' | 'Surgery' | 'Chemotherapy' | 'Radiation' | 'Tumor Board' | 'Lab' | string;
  date?: string;
  title: string;
  key_insight?: string;
  priority: number;
  sub_source_ids?: string[];  // For grouped events (e.g., chemo cycles)
}

export interface TreatmentResponseItem {
  cycle_id: string;
  status: 'PD' | 'SD' | 'PR' | 'CR';
  status_label: string;
  confidence: 'high' | 'medium' | 'low';
  basis?: string;
  imaging_source_ids: string[];
  imaging_date?: string;
}

export interface TimelineResponse {
  patient_id: string;
  events: TimelineEvent[];
  treatment_responses: TreatmentResponseItem[];
  filters_available: string[];
}

// ============ Source Detail Types ============

export interface SourceDetailResponse {
  source_id: string;
  source_type: string;
  resource_type: string;
  title?: string;
  date?: string;
  content_markdown: string;
}

// ============ Summary Types ============

export interface FieldWithSources {
  value?: string;
  source_ids: string[];
}

export interface ItemWithSources {
  text: string;
  source_ids: string[];
}

export interface ListFieldWithSources {
  items: ItemWithSources[];
  source_ids: string[];
}

export interface DemographicsField {
  name?: string;
  age?: number;
  gender?: string;
  treating_physician?: string;
  social_history?: string;
  source_ids: string[];
}

export interface PathologyField {
  cancer_type?: string;
  key_findings: ItemWithSources[];
  mutations: ItemWithSources[];
  molecular_markers: ItemWithSources[];
  sequencing_findings?: string;
  sequencing_source_ids: string[];
  source_ids: string[];
}

export interface ComorbiditiesField {
  conditions: ItemWithSources[];
  previous_surgeries: ItemWithSources[];
  previous_oncologic_diseases: ItemWithSources[];
  risk_factors: ItemWithSources[];
  source_ids: string[];
}

export interface StagingField {
  tnm?: string;
  uicc_stage?: string;
  other_staging?: string;
  is_inferred: boolean;
  inference_basis?: string;
  source_ids: string[];
}

export interface GeneralConditionField {
  ecog?: number;
  barthel_index?: number;
  description?: string;
  treatment_tolerance?: string;
  nursing_dependency?: string;
  source_ids: string[];
}

export interface PatientWishesField {
  text?: string;
  therapy_goal?: string;
  source_ids: string[];
  needs_clarification: boolean;
}

export interface CourseOfDiseaseField {
  assessment?: string;
  reasoning?: string;
  source_ids: string[];
}

export interface TherapyItem {
  type: string;
  name?: string;
  description?: string;
  cycles?: number;
  last_cycle_date?: string;
  start_date?: string;
  end_date?: string;
  efficacy?: string;
  intolerance?: string;
  source_ids: string[];
}

export interface TherapiesField {
  chemo: TherapyItem[];
  radiation: TherapyItem[];
}

export interface ImagingItem {
  type: string;
  modality?: string;
  organ_system?: string;
  date?: string;
  key_findings?: string;
  assessment?: string;
  progression?: string;
  metastatic_pattern?: string;
  disease_evolution?: string;
  comparison_to_prior_staging?: string;
  ai_reasoning?: string;
  tnm_from_imaging?: string;
  source_ids: string[];
}

export interface MissingInfoItem {
  field: string;
  question: string;
  priority: 'high' | 'medium' | 'low';
}

export interface SummaryResponse {
  patient_id: string;
  generated_at: string;
  demographics: DemographicsField;
  tumor_board_question: FieldWithSources;
  initial_diagnosis: FieldWithSources;
  staging: StagingField;
  pathology: PathologyField;
  therapies: TherapiesField;
  imaging: ImagingItem[];
  comorbidities: ComorbiditiesField;
  contraindications: ListFieldWithSources;
  general_condition: GeneralConditionField;
  symptoms: ListFieldWithSources;
  patient_wishes: PatientWishesField;
  course_of_disease: CourseOfDiseaseField;
  missing_info: MissingInfoItem[];
}

// ============ Query/Chat Types ============

export interface QueryRequest {
  query: string;
  patient_id?: string;
  conversation_id?: string;
  guideline_cancer_types?: string[];
}

export interface PatientMetadata {
  patient_id: string;
  cancer_type_raw?: string;
  guideline_cancer_types: string[];
}

export interface QuerySourceRef {
  source_id: string;
  source_type: string;
  resource_type: string;
  title?: string;
  excerpt?: string;
  content_markdown?: string;
}

export interface QueryResponse {
  answer: string;
  sources: QuerySourceRef[];
  conversation_id: string;
  follow_up_questions: string[];
}

// ============ Chat Message Types ============

export interface ChatMessage {
  id: string;
  role: 'user' | 'assistant';
  text: string;
  sources?: QuerySourceRef[];
  isError?: boolean;
}

// ============ Keypoint Types ============

export interface KeypointItem {
  text: string;
  priority: number;
}

export interface KeypointResult {
  source_id: string;
  keypoints: KeypointItem[];
}

export interface AddKeypointsRequest {
  source_ids?: string[];
  all?: boolean;
  clinical_question: string;
}

// ============ Patient Types (for patient list) ============

export interface Patient {
  id: string;
  internalId: string;
  name: string;
  age: number;
  gender: string;
  lastUpdated: string;
}

// ============ Legacy Types (for backwards compatibility) ============

export interface SourceMetadata {
  document: string;
  page: number;
  date: string;
}

export interface ProfileEntry<T> {
  value: T;
  source: SourceMetadata;
}

export interface MedicalProfile {
  clinicalQuestion: ProfileEntry<string>;
  initialDiagnosis: ProfileEntry<string>;
  imagingFindings: ProfileEntry<string>;
  pathology: ProfileEntry<string>;
  therapies: ProfileEntry<string[]>;
  comorbidities: ProfileEntry<string[]>;
  medications: ProfileEntry<string[]>;
  generalCondition: ProfileEntry<string>;
  contraindications: ProfileEntry<string>;
  preferences: ProfileEntry<string>;
}

export interface LegacyPatient {
  id: string;
  internalId: string;
  name: string;
  age: number;
  gender: string;
  lastUpdated: string;
  profile: MedicalProfile;
  timeline: TimelineEvent[];
}
