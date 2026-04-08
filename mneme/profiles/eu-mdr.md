---
name: EU MDR
description: European Medical Device Regulation (EU 2017/745) documentation profile
version: 2.0
tone: formal
voice: passive-for-procedures
citation_style: section-reference
placeholder_for_missing_refs: "[TO ADD REF]"
trace_types:
  - derived-from
  - implemented-by
  - detailed-in
  - mitigated-by
  - verified-by
  - validated-by
  - referenced-in
  - supersedes
requirement_levels:
  shall: mandatory requirement - must be fulfilled
  should: recommended - expected unless justified otherwise
  may: permitted - optional, at discretion
vocabulary:
  - use: medical device
    reject: [product, unit, item, widget, gadget]
  - use: intended purpose
    reject: [intended use, use case, purpose]
  - use: manufacturer
    reject: [company, vendor, maker, supplier]
  - use: clinical evaluation
    reject: [clinical review, clinical assessment]
  - use: risk management
    reject: [risk analysis, risk review]
  - use: design and development
    reject: [R&D, engineering]
  - use: verification
    reject: [testing, checking]
  - use: validation
    reject: [user testing, acceptance testing]
  - use: notified body
    reject: [certification body, audit body]
  - use: technical documentation
    reject: [tech docs, technical file]
  - use: post-market surveillance
    reject: [PMS, market monitoring]
  - use: unique device identification
    reject: [UDI, device ID]
  - use: economic operator
    reject: [distributor, reseller]
  - use: conformity assessment
    reject: [compliance check, certification]
  - use: essential requirements
    reject: [basic requirements, core requirements]
---

# Principles

- Reproducibility: every section must contain enough detail that an independent reviewer with no prior knowledge of the project could in principle reproduce the work. If a reader has to guess, infer, or look elsewhere for critical information, the section is incomplete.
- Technical, not clinical: validation documents describe technical measurements (e.g. a kinematic signal captured by an accelerometer). They do not describe clinical outcomes, patient experiences, or functional burden. Language must reflect this distinction.
- Construct validity, not clinical efficacy: when a clinical rating scale (e.g. UPDRS) is used as a reference standard, the algorithm output is a technical proxy that correlates with that scale. Correlation demonstrates construct validity of the technical measurement, not clinical efficacy.

# General Rules

- Be specific, never vague. Replace "adequate statistical power" with the actual power calculation. Replace "diverse populations" with the exact demographic breakdown.
- Define before you use. Every abbreviation, metric, method, and technical term must be defined at first use.
- Reference everything. Every dataset, document, standard, or external claim must have an ID, version, and/or citation.
- If a specific reference cannot be identified at the time of writing, insert the placeholder [TO ADD REF] at the exact point where the citation is needed. Do not omit the claim or leave the gap unmarked.
- Separate observation from interpretation. State the result first; then in separate sentences state what it means technically.
- Avoid marketing language. Words like "excellent", "strong", "robust", "high accuracy" are editorial. Let the numbers speak.
- Use consistent terminology throughout. Pick one term for each concept (e.g. always "algorithm output" or always "tremor metric", not both interchangeably).
- Never leave a section blank. If a section heading exists, it must contain content or be removed.

# Terminology

| Use | Instead of | Why |
|---|---|---|
| Kinematic tremor features | Tremor symptoms | The algorithm detects kinematic signals from accelerometer data, not clinical symptoms. |
| Accelerometer-derived oscillatory signal | Tremor burden experienced by the patient | Distinguishes the technical signal from the patient experience. |
| The algorithm output correlates with the reference standard | The algorithm captures the severity of tremor | Correlation framing avoids claiming the algorithm measures clinical severity directly. |
| Technical proxy for clinician-rated scores | Clinically meaningful measurement | Construct validity language, not clinical efficacy language. |
| The output shows a monotonic association with [scale] | The measurement reflects the functional burden | Statistical relationship, not clinical interpretation. |
| Rhythmic oscillations within the target frequency band | Tremor as experienced during daily living | Frequency-domain technical description, not lived experience. |
| The algorithm quantifies the proportion of time during which target kinematic features are detected | The algorithm measures how tremor affects the patient's activities of daily living | Reports a measurement, not an outcome. |

# Framing: Describing correlation results

**Wrong:**

> The results showcase how tremor affects a patient's activities of daily living. This confirms the algorithm measures a symptom that is directly relevant to the patient's experience.

**Correct:**

> The algorithm's output demonstrates a statistically significant monotonic correlation (Spearman r = 0.399, p < 0.001) with clinician-rated UPDRS Part II Item 16 scores. This supports the construct validity of the accelerometer-derived tremor metric as a technical proxy for the reference standard.

**Why:** the wrong version makes a clinical claim. The correct version reports a statistical relationship with a reference standard and frames it as construct validity of a technical measurement.

# Framing: Describing figures

**Wrong:**

> This correlation validates that the algorithm's measurement is clinically meaningful and accurately reflects the functional burden experienced by the subject.

**Correct:**

> The progressive increase in algorithm-derived tremor percentage across higher reference standard scores supports a consistent monotonic relationship between the technical output and the clinician-rated scale.

**Why:** the wrong version uses "clinically meaningful" and "functional burden" (clinical claims). The correct version describes the relationship between two numerical signals.

# Document Type: risk-management

ISO 14971 compliant risk management file.

# Document Type: clinical-evaluation

MEDDEV 2.7/1 rev 4 clinical evaluation report.

# Document Type: design-history-file

Design and development documentation per Annex II.

# Document Type: software-documentation

IEC 62304 software lifecycle documentation.

# Document Type: post-market-surveillance

Post-market surveillance per Article 83-86.

# Document Type: technical-documentation

Technical documentation per Annex II and III.

# Document Type: design-validation-report

Design Validation Report under the EU MDR CE Marking process. Demonstrates the design output meets the design inputs and the device performs its intended technical function against pre-defined reference standards.

## Section: purpose-and-scope

State the intended use in technical terms (e.g. detection and quantification of kinematic features from triaxial accelerometer data). Be explicit that this report covers design validation against pre-defined reference standards. Avoid clinical claims about patient outcomes.

## Section: context

Write as a technical literature review. Cite peer-reviewed precedent for the chosen modality and the rationale for treating it as a valid technical proxy. This section must never be left blank - if a Context heading exists, it must contain real content.

## Section: referenced-documents

Every referenced document must include document ID, title, version number, and the date of the referenced version. No bare references.

## Section: execution-metadata

State explicitly who executed the validation (name and role), when (date), and any prerequisites (preliminary testing, training, prior knowledge required).

## Section: dataset-descriptions

Describe each dataset with reproducibility in mind. The reader should be able to identify the dataset's source institution, ethics approval reference, GDPR compliance posture, demographic composition (N, sex split, age range, severity distribution, inclusion/exclusion criteria), recording environment, exact device specifications with datasheet links, data characteristics, independence from training data (with split percentage and method when applicable), and storage location within the QMS.

## Section: methodology-explanations

Every method or technique mentioned must be explained in enough detail for an independent reviewer to reproduce it. Define every technical term at first use or remove it. Examples of methods that always need a full explanation: cross-validation schemes (LOSO, k-fold), aggregate metrics (e.g. Mean Daily Tremor %) including bin size, wear-time determination, aggregation, thresholds, filters.

## Section: test-equipment

Specify both software environment (language version, library versions with numbers, algorithm version, git commit hash) and hardware environment (OS, CPU, RAM).

## Section: sample-size-justification

Be specific and quantitative. Give exact sample sizes per dataset and subgroup, the statistical power calculation (target power, expected effect size, alpha, formula or tool used), demographic breakdowns, and inclusion/exclusion criteria. Do not appeal vaguely to "established standards" - cite any standard by name and number.

## Section: acceptance-criteria

Every threshold must have a documented rationale. Cite where the cutoff comes from (literature, regulatory guidance, predicate device comparison). Explain why the specific value was chosen as the minimum acceptable level - not why the achieved result happens to be good.

## Section: test-results

Report numbers, statistical tests, and pass/fail per acceptance criterion. Avoid editorial language ("excellent", "highly effective", "clinically meaningful"). When perfect scores appear (e.g. Precision = 1.00), acknowledge the implication and discuss possible contributing factors such as small sample size or class separability. Keep figure captions descriptive, not interpretive - interpretation belongs in body text.

## Section: conclusion

State pass/fail per metric. Use "validated" only in the technical sense (output meets pre-defined acceptance criteria against specified reference standards). Do not claim "clinically validated" unless a separate clinical validation has been performed.

# Submission Checklist

- Context section is populated with literature references
- All referenced documents have ID, version, and date
- Execution metadata (who, when, prerequisites) is stated
- Each dataset has full documentation (source, ethics, demographics, device specs, independence from training data, QMS location)
- Hardware and software environments are fully specified
- All methods explained in reproducible detail
- Sample size justification includes actual numbers, power calculations, and demographic breakdowns
- All acceptance criteria thresholds have documented, citable justifications
- No clinical claims - all language is technical (kinematic, proxy, correlation with reference standard)
- Perfect scores (1.00) are acknowledged and discussed
- No undefined terms
- No vague appeals to "established standards" - specific standards cited by name and number
- All placeholder values (e.g. ICC = 0.XX) are replaced with actual results
- Figure captions are descriptive, not interpretive
- Conclusion says "design validated" not "clinically validated"
