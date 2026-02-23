#!/usr/bin/env python3
"""
Load sample oncology patient data into HAPI FHIR server.

This script creates a realistic lung cancer patient with:
- Patient demographics
- Initial diagnosis (NSCLC)
- Imaging reports (CT scans)
- Lab results (tumor markers)
- Pathology findings
- Procedures (biopsy, surgery)
- Medications (chemotherapy)
- Care plans (tumor board decisions)
"""

import requests
import json
from datetime import datetime, timedelta

# FHIR server configuration
FHIR_BASE_URL = "http://localhost:8080/fhir"

def create_patient():
    """Create a patient with lung cancer."""
    patient = {
        "resourceType": "Patient",
        "id": "patient-1",
        "identifier": [{
            "system": "http://hospital.example.org/patients",
            "value": "MRN-12345"
        }],
        "name": [{
            "use": "official",
            "family": "Schmidt",
            "given": ["Hans", "Wilhelm"]
        }],
        "gender": "male",
        "birthDate": "1955-03-15",
        "address": [{
            "use": "home",
            "line": ["Hauptstraße 123"],
            "city": "München",
            "postalCode": "80331",
            "country": "DE"
        }],
        "telecom": [{
            "system": "phone",
            "value": "+49-89-12345678",
            "use": "home"
        }]
    }
    return patient


def create_condition_nsclc(patient_id):
    """Create initial NSCLC diagnosis."""
    condition = {
        "resourceType": "Condition",
        "id": "condition-nsclc-1",
        "clinicalStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-clinical",
                "code": "active",
                "display": "Active"
            }]
        },
        "verificationStatus": {
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-ver-status",
                "code": "confirmed",
                "display": "Confirmed"
            }]
        },
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/condition-category",
                "code": "encounter-diagnosis",
                "display": "Encounter Diagnosis"
            }]
        }],
        "code": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": "254637007",
                "display": "Non-small cell lung cancer"
            }],
            "text": "Non-small cell lung cancer (NSCLC), adenocarcinoma"
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "onsetDateTime": "2024-09-15",
        "recordedDate": "2024-09-20T10:30:00Z",
        "stage": [{
            "summary": {
                "coding": [{
                    "system": "http://snomed.info/sct",
                    "code": "1222806006",
                    "display": "Stage IIIB"
                }],
                "text": "cT3 N2 M0 (Stage IIIB)"
            },
            "assessment": [{
                "display": "TNM staging based on imaging and biopsy"
            }]
        }],
        "note": [{
            "text": "Right upper lobe adenocarcinoma with mediastinal lymph node involvement. Patient presents with persistent cough and weight loss."
        }]
    }
    return condition


def create_diagnostic_report_ct_initial(patient_id):
    """Create initial CT scan diagnostic report."""
    report = {
        "resourceType": "DiagnosticReport",
        "id": "diag-ct-initial-1",
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                "code": "RAD",
                "display": "Radiology"
            }]
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "24627-2",
                "display": "CT Chest with contrast"
            }],
            "text": "CT Thorax mit Kontrastmittel"
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "effectiveDateTime": "2024-09-10T14:30:00Z",
        "issued": "2024-09-11T09:00:00Z",
        "conclusion": """CT Thorax Befund:

TECHNIK: CT Thorax mit intravenösem Kontrastmittel

BEFUND:
- Irreguläre, spiculierte Raumforderung im rechten Oberlappen, 4.5 x 3.8 cm
- Multiple mediastinale Lymphknoten vergrößert (Station 4R und 7), größter Durchmesser 2.1 cm
- Keine Pleuraergüsse oder Perikarderguss
- Keine Fernmetastasen in Leber oder Nebennieren erkennbar
- Emphysematöse Veränderungen beidseits

BEURTEILUNG:
- V.a. primäres Bronchialkarzinom rechter Oberlappen (cT3)
- Mediastinale Lymphadenopathie, V.a. N2-Stadium
- Staging: cT3 N2 M0 (Stadium IIIB)

EMPFEHLUNG:
- Histologische Sicherung mittels Bronchoskopie
- PET-CT zum Ausschluss okkulter Metastasen
- Vorstellung im Tumorboard"""
    }
    return report


def create_diagnostic_report_ct_followup(patient_id):
    """Create follow-up CT scan after chemotherapy."""
    report = {
        "resourceType": "DiagnosticReport",
        "id": "diag-ct-followup-1",
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                "code": "RAD",
                "display": "Radiology"
            }]
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "24627-2",
                "display": "CT Chest with contrast"
            }],
            "text": "CT Thorax Verlaufskontrolle"
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "effectiveDateTime": "2024-12-05T11:15:00Z",
        "issued": "2024-12-06T08:30:00Z",
        "conclusion": """CT Thorax Verlaufskontrolle nach 3 Zyklen Chemotherapie:

BEFUND:
- Raumforderung rechter Oberlappen jetzt 3.2 x 2.4 cm (vorher 4.5 x 3.8 cm)
- Deutliche Größenreduktion der mediastinalen Lymphknoten (Station 4R: 1.2 cm, Station 7: 0.9 cm)
- Kein Pleuraerguss
- Keine neuen Läsionen

BEURTEILUNG:
- Partielles Ansprechen auf Chemotherapie (RECIST 1.1: -37% Größenreduktion)
- Downstaging zu ypT2 ypN1

EMPFEHLUNG:
- Fortsetzung Chemotherapie wie geplant
- Re-Evaluation für chirurgische Resektion nach abgeschlossener neoadjuvanter Therapie"""
    }
    return report


def create_observation_lab(patient_id, test_code, test_name, value, unit, date, obs_id):
    """Create a lab observation."""
    observation = {
        "resourceType": "Observation",
        "id": obs_id,
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "laboratory",
                "display": "Laboratory"
            }]
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": test_code,
                "display": test_name
            }],
            "text": test_name
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "effectiveDateTime": date,
        "valueQuantity": {
            "value": value,
            "unit": unit,
            "system": "http://unitsofmeasure.org"
        }
    }
    return observation


def create_procedure_biopsy(patient_id):
    """Create bronchoscopy with biopsy procedure."""
    procedure = {
        "resourceType": "Procedure",
        "id": "proc-biopsy-1",
        "status": "completed",
        "category": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": "103693007",
                "display": "Diagnostic procedure"
            }]
        },
        "code": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": "173160006",
                "display": "Bronchoscopy with biopsy"
            }],
            "text": "Bronchoskopie mit transbronchialer Biopsie"
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "performedDateTime": "2024-09-18T09:00:00Z",
        "outcome": {
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": "385669000",
                "display": "Successful"
            }]
        },
        "note": [{
            "text": """Bronchoskopie durchgeführt.

BEFUND:
- Endobronchiale Läsion im rechten Oberlappen-Bronchus
- Transbronchiale Biopsie entnommen (5 Proben)
- Keine Komplikationen

HISTOLOGIE (vorläufig):
- Adenokarzinom, mäßig differenziert (G2)
- TTF-1 positiv, Napsin A positiv
- Molekularpathologie angefordert (EGFR, ALK, PD-L1)"""
        }]
    }
    return procedure


def create_diagnostic_report_pathology(patient_id):
    """Create pathology report with molecular findings."""
    report = {
        "resourceType": "DiagnosticReport",
        "id": "diag-pathology-1",
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/v2-0074",
                "code": "PAT",
                "display": "Pathology"
            }]
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "60568-3",
                "display": "Pathology Synoptic report"
            }],
            "text": "Histopathologischer Befund mit Molekularpathologie"
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "effectiveDateTime": "2024-09-18T09:00:00Z",
        "issued": "2024-09-23T14:00:00Z",
        "conclusion": """HISTOPATHOLOGISCHER BEFUND

MAKROSKOPIE:
5 Gewebeproben aus transbronchialer Biopsie

MIKROSKOPIE:
Adenokarzinom der Lunge, mäßig differenziert (G2)
Azinäres Wachstumsmuster vorherrschend
Keine lymphovaskuläre Invasion in den vorliegenden Proben

IMMUNHISTOCHEMIE:
- TTF-1: positiv (nukleär)
- Napsin A: positiv
- p40: negativ
- CK7: positiv

MOLEKULARPATHOLOGIE:
- EGFR: Wildtyp (keine aktivierende Mutation)
- ALK: negativ (FISH)
- ROS1: negativ (IHC)
- PD-L1 TPS: 55% (22C3 PharmDx)
- KRAS: G12C Mutation nachgewiesen

DIAGNOSE:
Adenokarzinom der Lunge, pulmonal-acinar, G2
KRAS G12C mutiert, EGFR/ALK/ROS1 negativ
PD-L1 high expression (TPS 55%)

EMPFEHLUNG:
- Chemotherapie +/- Immuntherapie erwägen (PD-L1 >50%)
- KRAS G12C-Inhibitoren in späteren Linien möglich"""
    }
    return report


def create_medication_chemotherapy(patient_id):
    """Create chemotherapy medication statement."""
    medication = {
        "resourceType": "MedicationStatement",
        "id": "med-chemo-1",
        "status": "active",
        "medicationCodeableConcept": {
            "coding": [{
                "system": "http://www.nlm.nih.gov/research/umls/rxnorm",
                "code": "56946",
                "display": "Carboplatin"
            }],
            "text": "Carboplatin/Pemetrexed + Pembrolizumab"
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "effectiveDateTime": "2024-10-01T00:00:00Z",
        "dateAsserted": "2024-10-01T10:00:00Z",
        "dosage": [{
            "text": "Carboplatin AUC 5 + Pemetrexed 500mg/m² + Pembrolizumab 200mg q3w",
            "timing": {
                "repeat": {
                    "frequency": 1,
                    "period": 3,
                    "periodUnit": "wk"
                }
            },
            "route": {
                "coding": [{
                    "system": "http://snomed.info/sct",
                    "code": "47625008",
                    "display": "Intravenous"
                }]
            }
        }],
        "note": [{
            "text": "Neoadjuvante Chemotherapie + Immuntherapie geplant für 3-4 Zyklen vor geplanter chirurgischer Resektion. Patient toleriert Therapie gut, leichte Fatigue und Übelkeit (CTCAE Grad 1)."
        }]
    }
    return medication


def create_care_plan_tumor_board(patient_id):
    """Create tumor board care plan."""
    care_plan = {
        "resourceType": "CarePlan",
        "id": "careplan-tb-1",
        "status": "active",
        "intent": "plan",
        "title": "Tumorboard-Empfehlung NSCLC Stadium IIIB",
        "description": "Multidisziplinäre Tumorboard-Entscheidung für neoadjuvante Therapie",
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "period": {
            "start": "2024-09-25T00:00:00Z"
        },
        "created": "2024-09-25T14:00:00Z",
        "author": {
            "display": "Tumorboard Thoraxonkologie"
        },
        "category": [{
            "coding": [{
                "system": "http://snomed.info/sct",
                "code": "734163000",
                "display": "Care plan"
            }]
        }],
        "activity": [{
            "detail": {
                "status": "in-progress",
                "description": "Neoadjuvante Chemotherapie (Carboplatin/Pemetrexed) + Immuntherapie (Pembrolizumab) für 3-4 Zyklen",
                "scheduledPeriod": {
                    "start": "2024-10-01",
                    "end": "2024-12-31"
                }
            }
        }, {
            "detail": {
                "status": "scheduled",
                "description": "Re-Staging mittels CT Thorax nach 3 Zyklen",
                "scheduledPeriod": {
                    "start": "2024-12-01",
                    "end": "2024-12-15"
                }
            }
        }, {
            "detail": {
                "status": "scheduled",
                "description": "Evaluation für chirurgische Resektion (Lobektomie rechts) bei Ansprechen",
                "scheduledPeriod": {
                    "start": "2025-01-01"
                }
            }
        }],
        "note": [{
            "text": """TUMORBOARD-EMPFEHLUNG vom 25.09.2024:

DIAGNOSE:
- Adenokarzinom rechter Oberlappen, cT3 N2 M0 (Stadium IIIB)
- KRAS G12C mutiert, PD-L1 TPS 55%
- ECOG 1, guter Allgemeinzustand

EMPFEHLUNG:
1. Neoadjuvante Chemotherapie + Immuntherapie:
   - Carboplatin AUC 5 + Pemetrexed 500mg/m² + Pembrolizumab 200mg
   - 3-4 Zyklen q3w

2. Re-Staging nach 3 Zyklen (CT Thorax)

3. Bei Ansprechen: Chirurgische Resektion
   - Lobektomie rechts mit systematischer Lymphadenektomie
   - Ziel: R0-Resektion

4. Adjuvante Therapie nach OP-Histologie

BEGRÜNDUNG:
- Stadium IIIB potenziell resektabel
- PD-L1 >50% spricht für Immuntherapie-Kombination
- Guter Allgemeinzustand erlaubt multimodale Therapie

TEILNEHMER: Dr. Müller (Onkologie), Prof. Schmidt (Thoraxchirurgie),
Dr. Weber (Radiologie), Dr. Klein (Pathologie), Dr. Fischer (Strahlentherapie)"""
        }]
    }
    return care_plan


def create_observation_ecog(patient_id):
    """Create ECOG performance status observation."""
    observation = {
        "resourceType": "Observation",
        "id": "obs-ecog-1",
        "status": "final",
        "category": [{
            "coding": [{
                "system": "http://terminology.hl7.org/CodeSystem/observation-category",
                "code": "survey",
                "display": "Survey"
            }]
        }],
        "code": {
            "coding": [{
                "system": "http://loinc.org",
                "code": "89262-0",
                "display": "ECOG Performance Status"
            }],
            "text": "ECOG Performance Status"
        },
        "subject": {
            "reference": f"Patient/{patient_id}"
        },
        "effectiveDateTime": "2024-09-20T10:00:00Z",
        "valueInteger": 1,
        "note": [{
            "text": "Patient ambulant, leichte Einschränkung bei schwerer körperlicher Arbeit. Selbstständig bei Alltagsaktivitäten."
        }]
    }
    return observation


def upload_resource(resource):
    """Upload a FHIR resource to the server."""
    resource_type = resource["resourceType"]
    resource_id = resource.get("id", "")

    if resource_id:
        url = f"{FHIR_BASE_URL}/{resource_type}/{resource_id}"
        response = requests.put(url, json=resource, headers={
            "Content-Type": "application/fhir+json"
        })
    else:
        url = f"{FHIR_BASE_URL}/{resource_type}"
        response = requests.post(url, json=resource, headers={
            "Content-Type": "application/fhir+json"
        })

    if response.status_code in [200, 201]:
        print(f"✓ Created {resource_type}/{resource_id}")
        return True
    else:
        print(f"✗ Failed to create {resource_type}/{resource_id}: {response.status_code}")
        print(f"  Response: {response.text[:200]}")
        return False


def main():
    """Load all sample data into FHIR server."""
    print("=" * 80)
    print("Loading Sample Oncology Patient Data into FHIR Server")
    print("=" * 80)
    print()

    patient_id = "patient-1"

    # Create resources
    resources = [
        # Patient
        create_patient(),

        # Diagnosis
        create_condition_nsclc(patient_id),

        # Imaging
        create_diagnostic_report_ct_initial(patient_id),
        create_diagnostic_report_ct_followup(patient_id),

        # Procedures
        create_procedure_biopsy(patient_id),

        # Pathology
        create_diagnostic_report_pathology(patient_id),

        # Lab results
        create_observation_lab(patient_id, "2857-1", "CEA", 8.2, "ng/mL", "2024-09-15T08:00:00Z", "obs-cea-1"),
        create_observation_lab(patient_id, "2857-1", "CEA", 5.1, "ng/mL", "2024-12-01T08:00:00Z", "obs-cea-2"),
        create_observation_lab(patient_id, "6690-2", "WBC", 6.5, "10*3/uL", "2024-09-15T08:00:00Z", "obs-wbc-1"),
        create_observation_lab(patient_id, "718-7", "Hemoglobin", 13.2, "g/dL", "2024-09-15T08:00:00Z", "obs-hgb-1"),

        # Performance status
        create_observation_ecog(patient_id),

        # Medications
        create_medication_chemotherapy(patient_id),

        # Care plan
        create_care_plan_tumor_board(patient_id),
    ]

    # Upload all resources
    success_count = 0
    for resource in resources:
        if upload_resource(resource):
            success_count += 1

    print()
    print("=" * 80)
    print(f"✓ Successfully loaded {success_count}/{len(resources)} resources")
    print(f"✓ Patient ID: {patient_id}")
    print()
    print("You can now access the patient summary in the dashboard!")
    print("=" * 80)


if __name__ == "__main__":
    main()
