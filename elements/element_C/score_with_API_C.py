import os
import json5
import json
import re
import time
import argparse
import pandas as pd
from pathlib import Path
from scripts.shared.utils import extract_text_from_file, call_gpt_with_backoff
import openai
import pytesseract
from pdf2image import convert_from_path
import win32com.client
import pythoncom
from scripts.shared.utils import extract_text_with_fallback
import traceback
import re


# Resolve project root: c:\GPTScorer
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Set path to Tesseract executable
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# =========================
# GPT MODEL CONFIGURATION
# =========================

GPT_MODEL_LEGACY = "gpt-3.5-turbo"
GPT_MODEL_CURRENT = "gpt-4.1-mini"

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the assigned rubric consistently and professionally.

Document Scope Handling Policy:

- If a submitted document appears to be for a different element (e.g., Element B, C, or D), evaluate it strictly against the Element A rubric. Award credit only for content that satisfies Element A criteria. Do not assign automatic zeros solely because the document was uploaded under the wrong element.

- If a document contains content for multiple elements:
• If a clearly labeled Element A section is present, evaluate only that section.
• If no clearly labeled Element A section is present, evaluate the entire document against the Element A rubric.

- Do not refuse scoring due to element mismatch.
- Base scores solely on alignment with the Element A rubric.
- Avoid commentary about element mismatch unless directly relevant to rubric criteria.
"""

NARRATIVE_INSTRUCTION = """
After assigning scores and providing brief criterion rationales, write a 180–220 word narrative feedback summary.

The summary must:
- Be written in paragraph form.
- Clearly explain strengths and weaknesses.
- Reference criterion numbers when helpful (e.g., A1, A4).
- Provide specific, actionable recommendations.
- Be professional and student-facing.
- Avoid mentioning scoring mechanics, AI, flags, or calibration.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 180 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "Current Element C Rubric.txt"

print("RUBRIC PATH:", RUBRIC_PATH)

with open(RUBRIC_PATH, "r", encoding="utf-8") as f:
    RUBRIC_TEXT = f.read().strip()  

def detect_solution_like(text: str) -> bool:
    t = (text or "").lower()

    solution_markers = [
        "prototype", "we built", "we made", "we created", "we constructed", "we assembled",
        "how it works", "this works by", "our design", "our solution", "final design",
        "materials", "wiring", "arduino", "circuit", "3d printed", "printed", "glued"
    ]

    requirement_markers = [
        "must", "should", "need to", "needs to", "requirement", "design requirement",
        "criteria", "constraint"
    ]

    sol_hits = sum(t.count(k) for k in solution_markers)
    req_hits = sum(t.count(k) for k in requirement_markers)

    # “solution-like” if solution language dominates AND requirement language is scarce
    return (sol_hits >= 2 and sol_hits >= req_hits + 1) or (sol_hits >= 3 and req_hits == 0)


def clean_json_string(raw):
    # Remove trailing commas before } or ]
    return re.sub(r',\s*([}\]])', r'\1', raw)

def detect_solution_specification(text: str) -> bool:
    t = (text or "").lower()

    constraint_terms = [
        "must allow", "must accommodate", "must not exceed",
        "should be able", "needs to", "requirement is to"
    ]

    specification_terms = [
        "will be made of", "is made of", "built with",
        "includes", "uses", "consists of", "constructed from"
    ]

    constraint_hits = sum(t.count(k) for k in constraint_terms)
    spec_hits = sum(t.count(k) for k in specification_terms)

    # Specification-dominant → invalid Element C per experts
    return spec_hits >= max(1, constraint_hits)

def detect_post_solution_requirements(text: str) -> bool:
    t = (text or "").lower()

    solution_specific = [
        "2x4", "plywood", "steel", "aluminum",
        "arduino", "servo", "motor", "circuit",
        "3d printed", "bolted", "welded"
    ]

    testing_presuppose = [
        "test by placing",
        "verify by installing",
        "measure after building",
        "once constructed"
    ]

    exploratory_terms = [
        "explore", "consider", "investigate",
        "evaluate", "compare", "tradeoff",
        "alternative"
    ]

    sol_hits = sum(t.count(k) for k in solution_specific)
    test_hits = sum(t.count(k) for k in testing_presuppose)
    has_exploration = any(k in t for k in exploratory_terms)

    signals = 0
    if sol_hits >= 2:
        signals += 1
    if test_hits >= 1:
        signals += 1
    if not has_exploration:
        signals += 1

    return signals >= 2


def get_gpt_model(blended_version: str) -> str:
    """
    Selects the correct GPT model based on blended model version.
    """
    if blended_version == "v1.13":
        return GPT_MODEL_LEGACY
    elif blended_version in ("v1.14", "v1.15"):
        return GPT_MODEL_CURRENT
    else:
        raise ValueError(f"Unknown blended model version: {blended_version}")

def is_truncated_json(text: str) -> bool:
    """
    Detects likely truncated JSON responses.
    """
    if not text:
        return True

    text = text.strip()

    # Must start with {
    if not text.startswith("{"):
        return False

    open_braces = text.count("{")
    close_braces = text.count("}")

    # Mismatched braces → likely truncated
    if close_braces < open_braces:
        return True

    # Doesn't properly end
    if not text.endswith("}"):
        return True

    return False


def score_document(filename, content, blended_model):
    # Choose prompt based on model
    if blended_model == "v1.13":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Scoring Guidance:
        - Apply a careful and evidence-based interpretation.
        - Base scores on the strength, clarity, and completeness of the evidence presented.
        - When evidence is partially articulated or implied but reasonably supported by the document, award partial credit rather than full credit.
        - Avoid over-interpreting vague or general statements as fully developed requirements.

        Dimension-Specific Clarifications:

        C1 (Research Connection):
        - Higher scores require clear linkage between design requirements and prior research, data, testing, benchmarking, or cited information.
        - If research linkage is weak or implicit, award limited partial credit rather than full credit.

        C2 (Clear, Detailed, Prioritized Requirements):
        - Requirements should be clearly articulated and sufficiently detailed to guide design decisions.
        - Clear prioritization strengthens the score.
        - If prioritization is suggested but not explicitly structured, award partial rather than full credit.

        C3 (High Priority Requirements):
        - Higher scores require clear identification of high-priority requirements.
        - If importance is suggested but not clearly distinguished from other requirements, award limited credit rather than maximum credit.

        Scoring Scale Anchors (Apply to All C1–C6 Dimensions):

        Use the following general scale guidance when assigning 0–5 scores:

        0 = No evidence of the criterion in the document.
        1 = Minimal or very weak evidence; criterion is mentioned but not developed.
        2 = Limited evidence; partially addressed but lacking clarity, detail, or structure.
        3 = Moderate evidence; clearly present but incomplete, inconsistently applied, or lacking full development.
        4 = Strong evidence; clearly articulated, well-developed, and mostly complete.
        5 = Exceptional evidence; explicit, detailed, comprehensive, and clearly distinguished from weaker performance levels.

        When evidence falls between levels, select the score that best reflects overall strength and completeness rather than defaulting to a lower score.

        Student Document:
        \"\"\"{content}\"\"\"

        # Narrative feedback requirements (Version A)
        # - 150–220 words (practical teacher length)
        # - One paragraph
        # - Must reference each C score and why (briefly)
        # - Must include 2–4 concrete improvement recommendations
        # - No bullet lists, no headings, no “Overall Assessment:” label

        Return only valid JSON in exactly this format:

        {{
        "C1": {{"score": X, "rationale": "."}},
        "C2": {{"score": X, "rationale": "."}},
        "C3": {{"score": X, "rationale": "."}},
        "C4": {{"score": X, "rationale": "."}},
        "C5": {{"score": X, "rationale": "."}},
        "C6": {{"score": X, "rationale": "."}},
        "narrative_feedback": "150–220 word single-paragraph rationale for teachers, referencing the scores and giving specific improvement suggestions."
        }}
        """
    elif blended_model in ("v1.14", "v1.15"):
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are a rigorous engineering design evaluator applying this rubric consistently and conservatively.

        GENERAL SCORING PRINCIPLES:

        - Base scores strictly on explicit evidence in the student document.
        - Do not assume missing elements are present.
        - Do not infer validation, research linkage, or prioritization unless clearly demonstrated.
        - When evidence is partial or underdeveloped, award partial credit.
        - Scores of 4 or 5 require clear, explicit, and well-developed evidence aligned directly to rubric language.

        CRITICAL DIFFERENTIATION REQUIREMENT:

        - Distinguish clearly between weak, adequate, strong, and exceptional submissions.
        - If a submission minimally satisfies the rubric, score at level 2.
        - If it adequately meets the rubric but lacks depth or completeness, score at level 3.
        - Reserve level 4 for clearly strong and well-developed work.
        - Reserve level 5 only for comprehensive, explicit, and clearly superior work.
        - Do not cluster most submissions at level 3. Use the full scale when justified by evidence.

        RUBRIC ANCHOR ENFORCEMENT:

        - For each category (C1–C6), match the document to the specific rubric descriptor provided in the rubric text.
        - If the rubric specifies numeric or quantitative thresholds (e.g., percentage ranges, number of requirements, stakeholder groups), apply those thresholds literally.
        - If the document clearly meets a lower-level descriptor (including 0 or 1), assign that level.
        - If the document clearly meets the highest-level descriptor, assign 5.
        - Do not restrict scores to the 2–4 range when rubric criteria justify 0, 1, or 5.
        - Select the score whose rubric description most precisely matches the evidence.

        SPECIFIC INTERPRETATION GUIDANCE:

        C2 (Requirements List & Prioritization):
        - If requirements are present but prioritization is unclear, weak, or inconsistently applied, assign 1.
        - If requirements are listed but lack clear structure or specificity, do not score above 2.
        - Assign 0 only if requirements are absent or extremely vague.
        - Assign 4 or 5 only when requirements are clearly structured, explicitly prioritized, and sufficiently detailed.

        C4 (Objective & Measurable):
        - If requirements lack measurable criteria or rely primarily on subjective language, assign 0 or 1.
        - If measurability is inconsistent or vague across multiple requirements, do not score above 2.
        - Assign 4 or 5 only if most requirements are clearly testable and objectively defined.

        C5 (Leading to Solution):
        - If requirements are weakly connected to a tangible solution or lack feasibility grounding, assign 0 or 1.
        - If connection to solution is generic or loosely implied, limit score to 2.
        - Assign 4 or 5 only when requirements clearly and directly support a viable, implementable solution.

        SCORING SCALE GUIDANCE:

        0 → No meaningful evidence of the rubric requirement  
        1 → Minimal or very weak evidence; mentioned but not developed  
        2 → Limited or partial fulfillment; important gaps remain  
        3 → Adequate fulfillment; clearly present but incomplete or uneven  
        4 → Strong and well-developed; mostly complete and clearly articulated  
        5 → Exceptional; explicit, comprehensive, and clearly distinguished from lower levels  

        Use professional judgment, but prioritize rubric-aligned evidence over general impression.

        For each category (C1–C6), explicitly determine which rubric descriptor the evidence most closely matches, then assign that score.

        # Narrative feedback requirements (Version A)
        # - 150–220 words (practical teacher length)
        # - One paragraph
        # - Must reference each C score and why (briefly)
        # - Must include 2–4 concrete improvement recommendations
        # - No bullet lists, no headings, no “Overall Assessment:” label

        Return only valid JSON in exactly this format:

        {{
        "C1": {{"score": X, "rationale": "."}},
        "C2": {{"score": X, "rationale": "."}},
        "C3": {{"score": X, "rationale": "."}},
        "C4": {{"score": X, "rationale": "."}},
        "C5": {{"score": X, "rationale": "."}},
        "C6": {{"score": X, "rationale": "."}},
        "narrative_feedback": "150–220 word single-paragraph rationale for teachers, referencing the scores and giving specific improvement suggestions."
        }}
        """
    else:
        raise ValueError(f"Unsupported blended_model version: {blended_model}")

    messages = [
        {
            "role": "system",
            "content": "You are a rigorous evaluator scoring an engineering design portfolio."
        },
        {
                "role": "user",
        "content": prompt
        }
    ]

    # === Call the API ===
    
    gpt_model = get_gpt_model(blended_model)

    print("MODEL BEING USED:", gpt_model)

    response = openai.ChatCompletion.create(
        model=gpt_model,
        messages=messages,
        temperature=0,
        top_p=1,
        max_tokens=3500
    )

    # === Extract text from OpenAI response (legacy SDK) ===
    try:
        response_str = response.choices[0].message.content
    except Exception as e:
        print(f"❌ Could not extract message content for {filename}: {e}")
        return {}

    if response is None:
        print(f"❌ GPT call returned None for {filename}")
        return {}

   # === Continue with parsing logic ===
    try:
        # --- Normalize ---
        response_str = response_str.strip()

        # --- Remove ALL markdown code fences ---
        if response_str.startswith("```"):
            response_str = "\n".join(
                line for line in response_str.splitlines()
                if not line.strip().startswith("```")
            ).strip()

        # --- Remove anything before first { ---
        first_brace = response_str.find("{")
        if first_brace != -1:
            response_str = response_str[first_brace:]

        # --- Trim anything after last } ---
        last_brace = response_str.rfind("}")
        if last_brace != -1:
            response_str = response_str[: last_brace + 1]

        # --- Parse JSON ---
        cleaned = clean_json_string(response_str)

        try:
            response_dict = json5.loads(cleaned)

        except Exception as e:
            print(f"⚠️ First parse failed for {filename}: {e}")

            # Check for truncation
            if is_truncated_json(cleaned):
                print("⚠️ Detected truncated JSON. Retrying once...")

                retry_response = openai.ChatCompletion.create(
                    model=gpt_model,
                    messages=messages,
                    temperature=0,
                    top_p=1,
                    max_tokens=3500
                )

                try:
                    retry_str = retry_response.choices[0].message.content.strip()

                    # Clean retry response
                    if retry_str.startswith("```"):
                        retry_str = "\n".join(
                            line for line in retry_str.splitlines()
                            if not line.strip().startswith("```")
                        ).strip()

                    first_brace = retry_str.find("{")
                    if first_brace != -1:
                        retry_str = retry_str[first_brace:]

                    last_brace = retry_str.rfind("}")
                    if last_brace != -1:
                        retry_str = retry_str[: last_brace + 1]

                    retry_str = clean_json_string(retry_str)

                    response_dict = json5.loads(retry_str)
                    print("✅ Retry succeeded")

                except Exception as retry_error:
                    print(f"❌ Retry failed for {filename}: {retry_error}")
                    return {
                        "truncation_detected": 1
                    }
            else:
                print("❌ Not a truncation case. Skipping document.")
                return {
                    "truncation_detected": 1
                }

        # --- Flatten nested structure ---
        flattened = {}
        for key, value in response_dict.items():
            if isinstance(value, dict) and "score" in value and "rationale" in value:
                flattened[key] = value["score"]
                flattened[f"{key}_rationale"] = value["rationale"]
            else:
                flattened[key] = value

        response_dict = flattened
        
        response_dict["truncation_detected"] = 0

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1, 7):
            response_dict[f"C{i}_api"] = int(response_dict[f"C{i}"])

        response_dict["element_score_api"] = sum(
            response_dict[f"C{i}_api"] for i in range(1, 7)
        ) / 6.0

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 7):
            assert f"C{i}" in response_dict, f"Missing C{i}"
            assert f"C{i}_rationale" in response_dict, f"Missing C{i}_rationale"

        return response_dict

    except json.JSONDecodeError as e:
        print(f"❌ JSON parse failed for {filename}")
        print(response_str)
        return {
            "truncation_detected": 1
        }

    response_dict["truncation_detected"] = 0

def postprocess_v113(row, filename):
    for i in range(1, 7):
        rationale = row.get(f"C{i}_rationale", "").strip().lower()
        score = row.get(f"C{i}", 0)
        flag = "CI-ok"
        if not rationale or len(rationale.split()) < 3:
            flag = "flag"
            if score >= 4:
                score -= 1
        row[f"C{i}"] = score
        row[f"C{i}_flag"] = flag
    return row


def postprocess_v114(row, filename):
    for i in range(1, 7):
        rationale = row.get(f"C{i}_rationale", "").strip().lower()
        score = row.get(f"C{i}", 0)
        flag = "CI-ok"
        if (not rationale or len(rationale.split()) < 5 or
                any(weak in rationale for weak in ["not clear", "vague", "uncertain"])):
            flag = "flag"
            if score >= 3:
                score -= 1
        row[f"C{i}"] = score
        row[f"C{i}_flag"] = flag
    return row

def classify_structural_class(row):
    text = row.get("text", "")

    if (
        detect_solution_specification(text)
        or detect_post_solution_requirements(text)
    ):
        return 0

    return 1



    """
    Returns structural_class ∈ {0, 1, 2}
    0 = fundamentally not Element C
    1 = partial / emerging
    2 = substantively valid Element C
    """
    solution_like = detect_solution_like(row.get("text", ""))

    scores = [row.get(f"C{i}", 0) for i in range(1, 7)]
    flags = [row.get(f"C{i}_flag", "") for i in range(1, 7)]
    rationales = " ".join(
        row.get(f"C{i}_rationale", "").lower()
        for i in range(1, 7)
    )

    # --- Strong negative signals ---
    low_score_count = sum(s <= 1 for s in scores)
    flagged_count = sum(f == "flag" for f in flags)

    negative_keywords = [
        "solution",
        "prototype",
        "built",
        "actual design",
        "final product",
        "wrong element",
        "not design requirements"
    ]

    has_negative_language = any(k in rationales for k in negative_keywords)

    # --- Strong positive signals ---
    sufficient_scores = sum(s >= 2 for s in scores)

    # ---- Classification rules ----

    # Structural Class 0: fundamentally not Element C
    if solution_like:
        print(filename, "solution_like =", solution_like)
        return 0

    # Structural Class 2: substantively valid Element C
    if (
        sufficient_scores >= 3 and
        row.get("C2", 0) >= 2 and
        row.get("C3", 0) >= 2 and
        not has_negative_language
    ):
        return 2

    # Structural Class 1: everything else
    return 1

def apply_structural_gating(element_raw, structural_class):
    """
    Applies conservative bounds based on structural class.
    """

    # Class 0: fundamentally not Element C
    if structural_class == 0:
        return min(element_raw, 1.2)

    # Class 2: substantively valid Element C
    if structural_class == 2:
        return min(
            max(element_raw, 2.2),  # floor
            3.8                     # ceiling
        )

    # Class 1: no structural adjustment
    return element_raw


def postprocess_v115(row, filename):
    row = postprocess_v114(row, filename)

    text = row.get("text", "") or ""

    solution_like = (
        detect_solution_like(text)
        or detect_post_solution_requirements(text)
        or detect_solution_specification(text)
    )

    row["v115_solution_like"] = int(solution_like)

    if solution_like:
        for i in (2, 3, 5):
            key = f"C{i}"
            row[key] = min(int(row.get(key, 0)), 2)
            row[f"{key}_flag"] = "red flag"
            row[f"{key}_rationale"] += (
                " Content primarily describes a solution or implementation rather than design requirements."
            )

    c2 = int(row.get("C2", 0))
    c4 = int(row.get("C4", 0))
    weak_trigger = (c2 <= 1 and c4 <= 1)

    row["v115_weak_trigger_c2c4"] = int(weak_trigger)

    element_raw = sum(int(row.get(f"C{i}", 0)) for i in range(1, 7)) / 6.0
    element_adjust = 0.0

    if solution_like:
        element_adjust -= 1.0
    if weak_trigger:
        element_adjust -= 0.5

    row["element_score_raw"] = round(element_raw, 2)
    row["element_adjustment_total"] = round(element_adjust, 2)
    row["element_penalty_pvg"] = int(solution_like)
    row["element_penalty_weak_c2c4"] = int(weak_trigger)
    row["element_score_adjusted"] = round(max(0.0, element_raw + element_adjust), 2)

    ("DEBUG v115 keys:", sorted(row.keys()))

    return row


def is_solution_description(text):
    """
    Detects whether content primarily describes solution behavior
    rather than design requirements.
    """
    solution_keywords = [
        "the device will",
        "we built",
        "we designed",
        "we implemented",
        "this prototype",
        "how it works",
        "steps",
        "procedure",
        "strategy",
        "process"
    ]

    text = text.lower()
    hits = sum(1 for k in solution_keywords if k in text)
    return hits >= 2

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]



def main(folder_path, output_path, blended_version):

    all_files = sorted(
        [
            f for f in os.listdir(folder_path)
            if f.lower().endswith((".pdf", ".docx"))
            and not f.startswith("~$")
        ],
        key=natural_sort_key
    )

    results = []

    for idx, filename in enumerate(all_files, start=1):
        full_path = os.path.join(folder_path, filename)
        print(f"Scoring {filename}...")
        try:
            text = extract_text_with_fallback(full_path)
            print("EXTRACTED LENGTH:", len(text))
            print("EXTRACTED SAMPLE:", text[:300])
            response_dict = score_document(filename, text,blended_version)

            if response_dict is None:
                print(f"❌ score_document returned None for {filename}")
                continue

            # Start from full response_dict so nothing is lost
            row = response_dict.copy()

            row["truncation_detected"] = response_dict.get("truncation_detected", 0)

            # Add metadata fields
            row["Case"] = idx
            row["filename"] = filename
            row["text"] = text

            row["incomplete_response"] = any(
                k not in response_dict for k in [f"C{i}" for i in range(1, 7)]
            )

            # Ensure C scores are integers
            for i in range(1, 7):
                row[f"C{i}"] = int(row.get(f"C{i}", 0))

            # --- Version-specific postprocessing ---
            if blended_version == "v1.13":
                row = postprocess_v113(row, filename)
            elif blended_version == "v1.14":
                row = postprocess_v114(row, filename)
            elif blended_version == "v1.15":
                # No rule engine for Current
                pass

            # --- Structural classification & gating (v1.14 only, for now) ---
            if blended_version == "v1.14":
                # Structural classification
                row["structural_class"] = classify_structural_class(row)

                # Element aggregation
                row["element_raw"] = sum(row[f"C{i}"] for i in range(1, 7)) / 6.0

                # Structural gating
                row["element_structured"] = apply_structural_gating(
                    row["element_raw"],
                    row["structural_class"]
                )

            results.append(row)

        except Exception:
            print(f"\nFULL TRACEBACK for {filename}:")
            traceback.print_exc()
            print("⚠️ Skipping this document and continuing...")
            continue    

    output_df = pd.DataFrame(results)
    # Reorder columns
    core = ["Case", "filename", "text", "incomplete_response"]

    scores = [f"C{i}" for i in range(1, 7)]
    api_scores = [f"C{i}_api" for i in range(1, 7)]  # 👈 NEW

    for i in range(1, 7):
        if row[f"C{i}"] != row[f"C{i}_api"]:
            print(f"{filename} C{i} changed by rule engine")

    flags = [f"C{i}_flag" for i in range(1, 7)]
    rationales = [f"C{i}_rationale" for i in range(1, 7)]

    extras = [
        "truncation_detected",
        "element_score_api",
        "element_score_raw",
        "element_adjustment_total",
        "element_penalty_pvg",
        "element_penalty_weak_c2c4",
        "element_score_adjusted",
        "v115_solution_like",
        "v115_weak_trigger_c2c4",
        "narrative_feedback"   # 👈 ADD THIS
    ]

    print("COLUMNS BEFORE REORDER:", output_df.columns.tolist())

    ordered_columns = core + scores + api_scores + flags + rationales + extras

    ordered_columns = [c for c in ordered_columns if c in output_df.columns]
    output_df = output_df[ordered_columns]

    output_df.to_csv(output_path, index=False)
    
    print("\n✅ Scoring complete. Output saved to:", output_path)

def score_documents_with_api(documents, blended_version):
    results = []

    for idx, doc in enumerate(documents, start=1):
     
        filename = doc["filename"]
        file_path = doc["path"]

        # --- Extraction ---
        text = extract_text_with_fallback(file_path)     
        print("EXTRACTED LENGTH:", len(text))
        print("EXTRACTED SAMPLE:", text[:300])

        response_dict = score_document(filename, text, blended_version)
        if response_dict is None:
            print(f"Skipping {filename} due to failure.")
            continue

        row = {
            "Case": idx,
            "filename": filename,
            "text": text,
        }
        
        for i in range(1, 7):
            if response_dict is None:
                raise ValueError(f"response_dict is None for {filename}")
            print("C key value:", i, type(response_dict.get(f"C{i}")), response_dict.get(f"C{i}"))
            row[f"C{i}"] = int(response_dict.get(f"C{i}", 0))
            row[f"C{i}_rationale"] = response_dict.get(f"C{i}_rationale", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        print("Narrative in row:", row.get("narrative_feedback"))

        if blended_version == "v1.13":
            row = postprocess_v113(row, filename)

        elif blended_version == "v1.14":
            row = postprocess_v114(row, filename)

        elif blended_version == "v1.15":
            # No rule engine for Current mode
            pass

        results.append(row)

    # ✅ return is OUTSIDE the loop, INSIDE the function
    return pd.DataFrame(results)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.13", "v1.14", "v1.15", "v1.15b"], default="v1.14",
                        help="Which blended model logic to apply (default: v1.14)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
