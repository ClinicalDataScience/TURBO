"""Pydantic request/response models for the MedGemma API."""
from typing import Optional
from pydantic import BaseModel, Field, model_validator


class SourceItem(BaseModel):
    source_id: str
    source_type: str
    resource_type: str
    fhir_id: Optional[str] = None
    milvus_document_id: Optional[str] = None
    title: Optional[str] = None
    date: Optional[str] = None
    preview: Optional[str] = None
    content_markdown: Optional[str] = None


class GetListResponse(BaseModel):
    items: list[SourceItem]
    total: int


class TimelineEventItem(BaseModel):
    id: str
    source_id: str
    type: str
    date: Optional[str] = None
    title: str
    key_insight: Optional[str] = None
    priority: int = 3
    sub_source_ids: list[str] = []


class TreatmentResponseItem(BaseModel):
    """Treatment response status for a CT imaging report."""
    cycle_id: str
    status: str
    status_label: str
    confidence: str = "medium"
    basis: Optional[str] = None
    imaging_source_ids: list[str] = []
    imaging_date: Optional[str] = None


class TimelineResponse(BaseModel):
    patient_id: str
    events: list[TimelineEventItem]
    treatment_responses: list[TreatmentResponseItem] = []
    filters_available: list[str] = ["all", "tumor_board", "imaging", "therapy"]


class SourceDetailResponse(BaseModel):
    source_id: str
    source_type: str
    resource_type: str
    title: Optional[str] = None
    date: Optional[str] = None
    content_markdown: str


class FieldWithSources(BaseModel):
    value: Optional[str] = None
    source_ids: list[str] = []


class ItemWithSources(BaseModel):
    """A single list item carrying its own source attribution."""
    text: str
    source_ids: list[str] = []


class ListFieldWithSources(BaseModel):
    items: list[ItemWithSources] = []
    source_ids: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_items(cls, data):
        if isinstance(data, dict) and "items" in data:
            data["items"] = [
                item if isinstance(item, dict) else {"text": item, "source_ids": []}
                for item in data["items"]
            ]
        return data


class TherapyItem(BaseModel):
    type: str
    name: Optional[str] = None
    description: Optional[str] = None
    cycles: Optional[int] = None
    last_cycle_date: Optional[str] = None
    start_date: Optional[str] = None
    end_date: Optional[str] = None
    efficacy: Optional[str] = None
    intolerance: Optional[str] = None
    source_ids: list[str] = []


class TherapiesField(BaseModel):
    chemo: list[TherapyItem] = []
    radiation: list[TherapyItem] = []


class ImagingItem(BaseModel):
    type: str
    modality: Optional[str] = None
    organ_system: Optional[str] = None
    date: Optional[str] = None
    key_findings: Optional[str] = None
    assessment: Optional[str] = None
    progression: Optional[str] = None
    metastatic_pattern: Optional[str] = None
    disease_evolution: Optional[str] = None
    comparison_to_prior_staging: Optional[str] = None
    ai_reasoning: Optional[str] = None
    tnm_from_imaging: Optional[str] = None
    source_ids: list[str] = []


class MissingInfoItem(BaseModel):
    field: str
    question: str
    priority: str = "medium"


class DemographicsField(BaseModel):
    name: Optional[str] = None
    age: Optional[int] = None
    gender: Optional[str] = None
    treating_physician: Optional[str] = None
    social_history: Optional[str] = None
    source_ids: list[str] = []


class PathologyField(BaseModel):
    cancer_type: Optional[str] = None
    key_findings: list[ItemWithSources] = []
    mutations: list[ItemWithSources] = []
    molecular_markers: list[ItemWithSources] = []
    sequencing_findings: Optional[str] = None
    sequencing_source_ids: list[str] = []
    source_ids: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_lists(cls, data):
        if isinstance(data, dict):
            for key in ("key_findings", "mutations", "molecular_markers"):
                if key in data and isinstance(data[key], list):
                    data[key] = [
                        item if isinstance(item, dict) else {"text": item, "source_ids": []}
                        for item in data[key]
                    ]
        return data


class ComorbiditiesField(BaseModel):
    conditions: list[ItemWithSources] = []
    previous_surgeries: list[ItemWithSources] = []
    previous_oncologic_diseases: list[ItemWithSources] = []
    risk_factors: list[ItemWithSources] = []
    source_ids: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_lists(cls, data):
        if isinstance(data, dict):
            for key in ("conditions", "previous_surgeries", "previous_oncologic_diseases", "risk_factors"):
                if key in data and isinstance(data[key], list):
                    data[key] = [
                        item if isinstance(item, dict) else {"text": item, "source_ids": []}
                        for item in data[key]
                    ]
        return data


class StagingField(BaseModel):
    tnm: Optional[str] = None
    uicc_stage: Optional[str] = None
    other_staging: Optional[str] = None
    is_inferred: bool = False
    inference_basis: Optional[str] = None
    source_ids: list[str] = []

    @model_validator(mode="before")
    @classmethod
    def _migrate_legacy_value(cls, data):
        if isinstance(data, dict) and "value" in data and "tnm" not in data:
            val = data.pop("value")
            if val:
                data["other_staging"] = val
        return data


class GeneralConditionField(BaseModel):
    ecog: Optional[int] = None
    barthel_index: Optional[int] = None
    description: Optional[str] = None
    treatment_tolerance: Optional[str] = None
    nursing_dependency: Optional[str] = None
    source_ids: list[str] = []


class PatientWishesField(BaseModel):
    text: Optional[str] = None
    therapy_goal: Optional[str] = None
    source_ids: list[str] = []
    needs_clarification: bool = False


class CourseOfDiseaseField(BaseModel):
    assessment: Optional[str] = None
    reasoning: Optional[str] = None
    source_ids: list[str] = []



class SummaryResponse(BaseModel):
    patient_id: str
    generated_at: str
    demographics: DemographicsField
    tumor_board_question: FieldWithSources
    initial_diagnosis: FieldWithSources
    staging: StagingField = StagingField()
    pathology: PathologyField
    therapies: TherapiesField = TherapiesField()
    imaging: list[ImagingItem] = []
    comorbidities: ComorbiditiesField
    contraindications: ListFieldWithSources
    general_condition: GeneralConditionField
    symptoms: ListFieldWithSources
    patient_wishes: PatientWishesField
    course_of_disease: CourseOfDiseaseField
    missing_info: list[MissingInfoItem] = []


class UpdateSummaryRequest(BaseModel):
    patient_id: str
    clinical_question: Optional[str] = None
    fragestellung: Optional[str] = None
    user_input: str
    missing_fields: list[str] = []


class RegenerateFieldRequest(BaseModel):
    patient_id: str
    field_name: str
    feedback: str
    clinical_question: Optional[str] = None


class QueryRequest(BaseModel):
    query: str
    patient_id: Optional[str] = None
    conversation_id: Optional[str] = None
    guideline_cancer_types: Optional[list[str]] = None


class QuerySourceRef(BaseModel):
    source_id: str
    source_type: str
    resource_type: str
    title: Optional[str] = None
    excerpt: Optional[str] = None
    content_markdown: Optional[str] = None


class QueryResponse(BaseModel):
    answer: str
    sources: list[QuerySourceRef] = []
    conversation_id: str
    follow_up_questions: list[str] = []


class KeypointItem(BaseModel):
    text: str
    priority: int = 3


class KeypointResult(BaseModel):
    source_id: str
    keypoints: list[KeypointItem]


class AddKeypointsRequest(BaseModel):
    source_ids: Optional[list[str]] = None
    all: bool = False
    clinical_question: Optional[str] = None
    fragestellung: Optional[str] = None


class StartGenerationRequest(BaseModel):
    patient_id: str
    clinical_question: Optional[str] = None
    skip_cache: bool = False


# Legacy models
class DocumentItem(BaseModel):
    document_id: str
    document_name: str
    text: str
    metadata: dict
    keypoints: Optional[list[str]] = None
    fragestellung: Optional[str] = None


class KeypointRequest(BaseModel):
    documents: list[DocumentItem]
    fragestellung: str
