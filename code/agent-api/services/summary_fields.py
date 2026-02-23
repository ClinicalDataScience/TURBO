"""Per-field summary generation constants: schemas, instructions, validators.

This module is pure data — no runtime logic. It defines the structure for
the per-field summary generation pipeline in services/summary.py.
"""
from models import (
    DemographicsField, FieldWithSources,
    StagingField, PathologyField, ComorbiditiesField, ListFieldWithSources,
    GeneralConditionField, PatientWishesField, CourseOfDiseaseField,
    TherapyItem, ImagingItem,
)

# Maps each summary field to the FHIR resource types that provide its data.
FIELD_RESOURCE_TYPES: dict[str, list[str]] = {
    "demographics": ["Patient", "Observation"],
    "tumor_board_question": [],
    "initial_diagnosis": ["Condition", "DiagnosticReport"],
    "staging": ["DiagnosticReport", "Condition"],
    "pathology": ["DiagnosticReport"],
    "chemo": ["MedicationStatement", "MedicationRequest", "CarePlan"],
    "radiation": ["Procedure"],
    "imaging": ["DiagnosticReport", "ImagingStudy"],
    "comorbidities": ["Condition", "Procedure"],
    "contraindications": ["AllergyIntolerance", "Condition"],
    "general_condition": ["Observation", "Condition"],
    "symptoms": ["Observation", "Condition"],
    "patient_wishes": ["CarePlan", "Observation"],
    "course_of_disease": ["Condition", "DiagnosticReport", "ImagingStudy", "Observation", "Procedure", "MedicationStatement"],
}

FIELD_GENERATION_ORDER: list[str] = [
    "demographics",
    "tumor_board_question",
    "initial_diagnosis",
    "course_of_disease",
    "staging",
    "pathology",
    "chemo",
    "radiation",
    "imaging",
    "comorbidities",
    "contraindications",
    "general_condition",
    "symptoms",
    "patient_wishes",
]

FIELD_SCHEMAS: dict[str, str] = {
    "demographics": '{"name": "...", "age": null, "gender": "...", "treating_physician": null, "social_history": "...", "source_ids": ["<id>"]}',
    "tumor_board_question": '{"value": "...", "source_ids": ["<id>"]}',
    "initial_diagnosis": '{"value": "...", "source_ids": ["<id>"]}',
    "course_of_disease": '{"assessment": "...", "reasoning": "...", "source_ids": ["<id>"]}',
    "staging": '{"tnm": "...", "uicc_stage": "...", "other_staging": null, "is_inferred": false, "inference_basis": null, "source_ids": ["<id>"]}',
    "pathology": '{"cancer_type": "...", "key_findings": [{"text": "...", "source_ids": ["<id>"]}], "mutations": [{"text": "...", "source_ids": ["<id>"]}], "molecular_markers": [{"text": "...", "source_ids": ["<id>"]}], "sequencing_findings": "... or null", "sequencing_source_ids": ["<id>"], "source_ids": ["<id>"]}',
    "chemo": '[{"type": "Chemotherapy|Immunotherapy", "name": "...", "description": "...", "cycles": null, "last_cycle_date": null, "start_date": "...", "end_date": null, "efficacy": null, "intolerance": null, "source_ids": ["<id>"]}]',
    "radiation": '[{"type": "Radiation", "name": "...", "description": "...", "cycles": null, "last_cycle_date": null, "start_date": "...", "end_date": null, "efficacy": null, "intolerance": null, "source_ids": ["<id>"]}]',
    "imaging": '[{"type": "...", "modality": "...", "organ_system": "...", "date": "...", "key_findings": "...", "assessment": null, "progression": null, "metastatic_pattern": null, "disease_evolution": null, "comparison_to_prior_staging": null, "tnm_from_imaging": null, "source_ids": ["<id>"]}]',
    "comorbidities": '{"conditions": [{"text": "disease — year", "source_ids": ["<id>"]}], "previous_surgeries": [{"text": "surgery — year", "source_ids": ["<id>"]}], "previous_oncologic_diseases": [{"text": "...", "source_ids": ["<id>"]}], "risk_factors": [{"text": "...", "source_ids": ["<id>"]}], "source_ids": ["<id>"]}',
    "contraindications": '{"items": [{"text": "...", "source_ids": ["<id>"]}], "source_ids": ["<id>"]}',
    "general_condition": '{"ecog": null, "barthel_index": null, "description": "...", "treatment_tolerance": "...", "nursing_dependency": "...", "source_ids": ["<id>"]}',
    "symptoms": '{"items": [{"text": "...", "source_ids": ["<id>"]}], "source_ids": ["<id>"]}',
    "patient_wishes": '{"text": null, "therapy_goal": null, "needs_clarification": true, "source_ids": ["<id>"]}',
}

FIELD_INSTRUCTIONS: dict[str, str] = {
    "demographics": (
        "Extract patient name, age, gender, treating physician, and social history "
        "(smoking status, alcohol, occupation, living situation) from Patient resource "
        "and Observation (social-history)."
    ),

    "initial_diagnosis": (
        "Extract the primary/initial diagnosis. Be specific about cancer type, location, "
        "and histology if available.\n"
        "- First bullet: **Change summary** — state whether the diagnosis has been updated "
        "or refined in recent entries (e.g., reclassification, new biopsy results).\n"
        "- Maximum 5 bullet points. Only include details relevant to the clinical question.\n"
        "- Omit generic background information that doesn't help answer the clinical question."
    ),

    "staging": (
        "First look for explicit TNM in DiagnosticReport. If absent, look for "
        "UICC/FIGO/other staging. If NO explicit staging exists, INFER from pathology "
        "and radiology reports, set is_inferred=true, and explain in inference_basis.\n"
        "- In other_staging (if used): state whether staging has been updated or restaged "
        "recently. Highlight any upstaging or downstaging.\n"
        "- Contextualize staging relative to the clinical question."
    ),

    "pathology": (
        "Extract ONLY from DiagnosticReport resources that are ACTUAL PATHOLOGY reports "
        "(histopathology, cytology, molecular pathology, biopsy results).\n"
        "DO NOT include imaging findings, radiology reports, or imaging-derived observations.\n"
        "How to identify pathology reports: they mention histology, biopsy, resection specimen, "
        "cytology, immunohistochemistry, molecular testing, or sequencing.\n"
        "How to identify NON-pathology reports (EXCLUDE these): they mention CT, MRI, PET, "
        "X-ray, ultrasound, imaging findings, or radiological assessment.\n\n"
        "Include cancer type/histology, molecular markers (PD-L1, HER2, ER/PR, etc.), "
        "mutations, and sequencing/NGS results.\n"
        "- First item in key_findings: **Change summary** — state whether pathology results "
        "have changed since initial diagnosis (e.g., new biopsy, marker changes).\n"
        "- Maximum 5 items per list (key_findings, mutations, molecular_markers).\n"
        "- Prioritize pathology findings that bear on the clinical question "
        "(e.g., markers relevant to proposed treatment decisions)."
    ),

    "chemo": (
        "Concise overview of systemic therapies as a list. Include chemotherapy regimens, "
        "immunotherapy, targeted therapy. For each: regimen name, number of cycles, last "
        "cycle start date, efficacy, intolerance. Connect MedicationStatement and "
        "MedicationRequest entities. Group related treatments.\n"
        "- Maximum 5 therapy items. Only include therapies actually administered.\n"
        "- For the description field of each item: keep it to one concise sentence.\n"
        "- In the efficacy field, note response/progression if documented.\n"
        "- Highlight therapies relevant to the clinical question (e.g., current line, response)."
    ),

    "radiation": (
        "Concise overview of radiation therapy as a list. For each: type of radiation, "
        "target area/location, start and end dates, dose if available, efficacy. "
        "Extract from Procedure resources related to radiation therapy.\n"
        "- Maximum 3 therapy items.\n"
        "- For the description field: one concise sentence per entry.\n"
        "- Focus on radiation relevant to the clinical question."
    ),

    "imaging": (
        "Structure imaging entries for tumor board decision-making. Be CONCISE — one short "
        "sentence per finding. Only include clinically relevant findings.\n\n"
        "STAGING CT: Any CT covering both thorax and abdomen. Create a SEPARATE entry with "
        "type='Staging CT' for each. Populate:\n"
        "- key_findings: Use markdown bullet list. One bullet per category:\n"
        "  * Primary tumor: size/change vs prior\n"
        "  * Lymph nodes: changed/new/resolved\n"
        "  * Pulmonary nodules: count, size, change\n"
        "  * Each OTHER organ finding (liver, bone, pleura, etc.) gets its OWN bullet.\n"
        "    Do NOT combine multiple organs into one bullet point.\n"
        "    Example:\n"
        "    - Liver: Progression of bilobar metastases\n"
        "    - Bone: New osteolysis L2 spinous process\n"
        "    - Pleura: Small right-sided pleural effusion\n"
        "- progression: 'Progressive' | 'Stable' | 'Partial response' | 'Complete response' | 'Mixed'\n"
        "- comparison_to_prior_staging: One sentence — what changed vs previous staging CT\n"
        "- metastatic_pattern: Hematogenous/lymphogenic/local + affected organs (one sentence)\n"
        "- disease_evolution: Trend in one sentence\n\n"
        "NON-STAGING IMAGING: Separate entries per modality/organ. Include only if clinically "
        "relevant (new findings, changes, treatment impact). Omit stable old findings.\n\n"
        "RULES:\n"
        "- Sort by date (most recent first). Always populate 'modality'.\n"
        "- Maximum 3 imaging entries total.\n"
        "- Keep ALL text fields concise — one sentence each for progression, "
        "comparison_to_prior_staging, metastatic_pattern, disease_evolution."
    ),

    "comorbidities": (
        "Include diseases NOT related to the current oncologic disease. "
        "Format as 'condition — year of diagnosis'. "
        "Separate into conditions, previous_surgeries, previous_oncologic_diseases, "
        "and risk_factors (smoking, family cancer history, occupational exposures).\n"
        "- Maximum 5 items per category.\n"
        "- Focus on comorbidities relevant to the clinical question "
        "(e.g., conditions that affect treatment options or prognosis)."
    ),

    "contraindications": (
        "List known drug allergies, drug intolerances, and organ dysfunctions that would "
        "restrict specific treatment options for this patient (e.g. renal insufficiency "
        "limiting platinum-based chemotherapy, documented allergy to taxanes, severe "
        "neuropathy contraindicating vincristine). Do NOT list general oncological "
        "diagnoses or symptoms here. If no relevant contraindications are documented, "
        "say so explicitly.\n"
        "- First item: **Change summary** — state whether any new contraindications "
        "or allergies have been recorded recently.\n"
        "- Maximum 5 items.\n"
        "- Highlight contraindications relevant to treatments discussed in the clinical question."
    ),

    "general_condition": (
        "Include ECOG if documented, Barthel index if available, treatment tolerance, "
        "and nursing dependency level.\n"
        "- In the description field: **Start with change summary** — has the patient's "
        "general condition changed recently (improved/deteriorated)? Then provide current status.\n"
        "- Maximum 5 bullet points in description.\n"
        "- Focus on aspects relevant to the clinical question (e.g., fitness for proposed treatment)."
    ),

    "symptoms": (
        "List current symptoms the patient is experiencing.\n"
        "- First item: **Change summary** — state whether symptoms have changed recently "
        "(new symptoms, resolved symptoms, worsening/improving symptoms).\n"
        "- Maximum 5 items. Focus on active, current symptoms.\n"
        "- Do not list symptoms that have fully resolved unless resolution is recent and noteworthy.\n"
        "- Prioritize symptoms relevant to the clinical question."
    ),

    "patient_wishes": (
        "Document patient wishes regarding treatment, therapy goals, and whether "
        "clarification is needed.\n"
        "- In the text field, use bullet points (max 5). First bullet: **Change summary** — "
        "have the patient's wishes or therapy goals changed or been updated recently?\n"
        "- Note wishes relevant to treatment options in the clinical question."
    ),

    "course_of_disease": (
        "Infer the disease course from therapy timeline, treatment efficacy, and imaging.\n\n"
        "In the 'assessment' field, use markdown bullet points (max 5) covering:\n"
        "1. **Overall trajectory**: Is disease progressing, stable, or responding? "
        "One sentence framed in context of the clinical question.\n"
        "2. **Primary tumor trend**: Size/configuration changes over time from imaging.\n"
        "3. **Nodule tracking**: For EACH tracked nodule or nodule group from imaging:\n"
        "   - Location (e.g., right lower lobe, segment 6)\n"
        "   - Count if multiple\n"
        "   - Size changes over time with dates (e.g., '12mm (Jan) → 8mm (Mar) → 5mm (Jun)')\n"
        "4. **Metastatic status**: Are metastases appearing, stable, or resolving? Which organs?\n"
        "5. **Treatment correlation**: Which therapy phases correlate with response or progression?\n\n"
        "Use the already-generated imaging, staging, pathology, chemo, and radiation fields "
        "as reference. Focus ONLY on current disease — ignore old/unrelated oncologic diseases.\n"
        "Use bold markdown (**text**) to highlight key changes and trends.\n"
        "Frame the entire assessment around what it means for the clinical question."
    ),
}

FIELD_VALIDATORS: dict[str, type | list] = {
    "demographics": DemographicsField,
    "tumor_board_question": FieldWithSources,
    "initial_diagnosis": FieldWithSources,
    "staging": StagingField,
    "pathology": PathologyField,
    "comorbidities": ComorbiditiesField,
    "contraindications": ListFieldWithSources,
    "general_condition": GeneralConditionField,
    "symptoms": ListFieldWithSources,
    "patient_wishes": PatientWishesField,
    "course_of_disease": CourseOfDiseaseField,
    "chemo": [TherapyItem],
    "radiation": [TherapyItem],
    "imaging": [ImagingItem],
}
