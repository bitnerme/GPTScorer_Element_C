import os
import json5
import json
import re
import time
import argparse
import pandas as pd
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import openai
from pdf2image import convert_from_path
import win32com.client
import pythoncom
from scripts.shared.utils import extract_text_with_fallback
import traceback
import shutil
import pytesseract


# Resolve project root: c:\GPTScorer
PROJECT_ROOT = Path(__file__).resolve().parents[2]

def configure_tesseract():
    # 1. Check environment variable first (best for Mac/Linux)
    tesseract_path = os.environ.get("TESSERACT_PATH")

    if tesseract_path and os.path.exists(tesseract_path):
        pytesseract.pytesseract.tesseract_cmd = tesseract_path
        return

    # 2. Try auto-detect (works if installed via brew/apt)
    detected = shutil.which("tesseract")
    if detected:
        pytesseract.pytesseract.tesseract_cmd = detected
        return

    # 3. Fallback (Windows default)
    default_win = r"C:\Program Files\Tesseract-OCR\tesseract.exe"
    if os.path.exists(default_win):
        pytesseract.pytesseract.tesseract_cmd = default_win
        return

    print("⚠️ Tesseract not found — OCR may fail")

# =========================
# GPT MODEL CONFIGURATION
# =========================

LEGACY_GPT_MODEL = "gpt-4.1-mini"     #"gpt-3.5-turbo"
CURRENT_GPT_MODEL = "gpt-4.1-mini"    #"gpt-4-0613"

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element A rubric consistently and professionally.

GENERAL SCORING PRINCIPLES:

- Base scores strictly on explicit evidence in the student document.
- Do not assume missing elements are present.
- If evidence is weak or incomplete, assign a lower score rather than a higher score.
- Do not reward effort, intent, or future plans unless supported by concrete evidence.
- Use the full 0–5 scale when justified by evidence.

For each sub-element A1–A6, match the evidence in the document to the rubric description that best fits."""

NARRATIVE_INSTRUCTION = """
After assigning scores and providing brief criterion rationales, write a 180–220 word narrative feedback summary.

The summary must:
- Be written in paragraph form.
- Clearly explain strengths and weaknesses.
- Reference criterion numbers when helpful (e.g., A3, A6).
- Provide specific, actionable recommendations.
- Be professional and student-facing.
- Avoid mentioning scoring mechanics, AI, or calibration.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 180 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "Current Element A Rubric.txt"

print("RUBRIC PATH:", RUBRIC_PATH)

with open(RUBRIC_PATH, "r", encoding="utf-8") as f:
    RUBRIC_TEXT = f.read().strip()  

def clean_json_string(raw):
    # Remove trailing commas before } or ]
    return re.sub(r',\s*([}\]])', r'\1', raw)

def get_gpt_model(blended_version: str) -> str:
    """
    Selects the correct GPT model based on blended model version.
    """
    if blended_version == "v1.0":
        return LEGACY_GPT_MODEL
    elif blended_version == "v1.2":
        return CURRENT_GPT_MODEL
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
    
    document_length = len(content)

    # Choose prompt based on model
    if blended_model == "v1.0":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are a rigorous engineering design evaluator applying this rubric consistently and conservatively.

        GENERAL SCORING PRINCIPLES:

        - Base scores strictly on explicit evidence in the student document.
        - Do not assume missing elements are present.
        - Do not infer research, analysis, or validation unless clearly demonstrated.
        - When evidence is partial or underdeveloped, award partial credit rather than full credit.
        - Scores of 4 or 5 require clear, explicit, and well-developed evidence aligned directly to rubric language.

        CRITICAL DIFFERENTIATION REQUIREMENT:

        - Distinguish clearly between weak, adequate, strong, and exceptional submissions.
        - If a submission minimally satisfies the rubric, score at level 2.
        - If it adequately meets the rubric but lacks depth or completeness, score at level 3.
        - Reserve level 4 for clearly strong and well-developed work.
        - Reserve level 5 only for comprehensive, explicit, and clearly superior work.
        - Do not cluster most submissions at level 3. Use the full scale when justified by evidence.

        RUBRIC ANCHOR ENFORCEMENT:

        - For each category (A1–A6), match the document to the specific rubric descriptor provided in the rubric text.
        - If the rubric specifies numeric or quantitative thresholds, apply those thresholds literally.
        - If the document clearly meets a lower-level descriptor, assign that level.
        - If the document clearly meets the highest-level descriptor, assign 5.
        - Select the score whose rubric description most precisely matches the evidence.

        SPECIFIC INTERPRETATION GUIDANCE:

        A4 (Stakeholder Groups):
        - Stakeholders must represent distinct groups affected by the problem.
        - Listing only one or two groups (e.g., students, teachers) should not score above level 2.
        - Higher scores require several clearly differentiated stakeholder groups.
        - Avoid counting repeated variations of the same group as separate stakeholders.
        A5 (Sources and Evidence):
        - Credible sources should generally include research studies, government publications, academic articles, or reputable organizations.
        - Personal opinions, blogs, or uncited statements should not be considered credible sources.
        - A single credible source should not score above level 2.
        - Higher scores require multiple credible and varied sources.

        SCORING SCALE GUIDANCE:

        0 → No meaningful evidence of the rubric requirement  
        1 → Minimal or very weak evidence; mentioned but not developed  
        2 → Limited or partial fulfillment; important gaps remain  
        3 → Adequate fulfillment; clearly present but incomplete or uneven  
        4 → Strong and well-developed; mostly complete and clearly articulated  
        5 → Exceptional; explicit, comprehensive, and clearly distinguished from lower levels  

        Use professional judgment, but prioritize rubric-aligned evidence over general impression.

        For each category (A1–A6), explicitly determine which rubric descriptor the evidence most closely matches, then assign that score.

        POINTS FOR TEACHER REVIEW
        -------------------------

        Before returning the scores, identify exactly five scoring decisions that an experienced human scorer would most likely want to verify independently.

        For each scoring decision, write one concise review prompt for the teacher.

        - Three prompts must be document-specific and based on features, omissions, or ambiguities observed in this submission.
        - Two prompts must be broader rubric reminders for criteria that are especially important to verify in this submission, even if those concerns are not unusual.

        Blend the five prompts naturally. Do not label them as document-specific or general.

        Each prompt must:

        - direct the teacher to inspect or verify evidence before accepting the score;
        - identify a genuine scoring judgment rather than a routine checklist item;
        - avoid stating the scoring conclusion;
        - avoid recommending a score;
        - avoid merely summarizing a strength or weakness;
        - remain relevant to this submission;
        - avoid rubric shorthand such as A1, A2, A5 or similar subelement labels;
        - avoid explicit score references such as "zero score", "full marks", or "higher score";
        - read naturally as if written by an experienced moderator leaving review notes for another scorer.

        Good review prompts are document-specific.

        Prefer review prompts that identify genuine scoring judgments rather than routine rubric reminders.

        Poor:
        "Verify whether the document includes credible sources."

        Better:
        "Determine whether the Declaration itself should be treated as sufficient supporting evidence or whether independent sources are expected."

        Poor:
        "Check stakeholder groups."

        Better:
        "Consider whether references to British citizens, Parliament, the Crown, and Indigenous peoples represent distinct stakeholder groups or merely passing mentions."

        Poor:
        "Look for a problem statement."

        Better:
        "Decide whether the colonies' grievances collectively function as an explicit problem definition despite the absence of a labeled problem statement."

        Return the review prompts as a JSON array named "scoring_hints".

        Return only valid JSON in exactly this format:

        {{
        "A1": {{"score": X, "rationale": "."}},
        "A2": {{"score": X, "rationale": "."}},
        "A3": {{"score": X, "rationale": "."}},
        "A4": {{"score": X, "rationale": "."}},
        "A5": {{"score": X, "rationale": "."}},
        "A6": {{"score": X, "rationale": "."}},
        "narrative_feedback": "180–220 word single-paragraph explanation referencing strengths, weaknesses, and improvement suggestions.",
        "scoring_hints": [
            "...",
            "...",
            "..."
        ]}}
        """
    elif blended_model == "v1.2":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are a rigorous engineering design evaluator applying this rubric consistently and conservatively.

        GENERAL SCORING PRINCIPLES:

        - Base scores strictly on explicit evidence in the student document.
        - Do not assume missing elements are present.
        - Do not infer research, analysis, or validation unless clearly demonstrated.
        - When evidence is partial or underdeveloped, award partial credit rather than full credit.
        - Scores of 4 or 5 require clear, explicit, and well-developed evidence aligned directly to rubric language.

        CRITICAL DIFFERENTIATION REQUIREMENT:

        - Distinguish clearly between weak, adequate, strong, and exceptional submissions.
        - If a submission minimally satisfies the rubric, score at level 2.
        - If it adequately meets the rubric but lacks depth or completeness, score at level 3.
        - Reserve level 4 for clearly strong and well-developed work.
        - Reserve level 5 only for comprehensive, explicit, and clearly superior work.
        - Do not cluster most submissions at level 3. Use the full scale when justified by evidence.

        RUBRIC ANCHOR ENFORCEMENT:

        - For each category (A1–A6), match the document to the specific rubric descriptor provided in the rubric text.
        - If the rubric specifies numeric or quantitative thresholds, apply those thresholds literally.
        - If the document clearly meets a lower-level descriptor, assign that level.
        - If the document clearly meets the highest-level descriptor, assign 5.
        - Select the score whose rubric description most precisely matches the evidence.

        SPECIFIC INTERPRETATION GUIDANCE:

        A4 (Stakeholder Groups):
        - Stakeholders must represent distinct groups affected by the problem.
        - Listing only one or two groups (e.g., students, teachers) should not score above level 2.
        - Higher scores require several clearly differentiated stakeholder groups.
        - Avoid counting repeated variations of the same group as separate stakeholders.
        A5 (Sources and Evidence):
        - Credible sources should generally include research studies, government publications, academic articles, or reputable organizations.
        - Personal opinions, blogs, or uncited statements should not be considered credible sources.
        - A single credible source should not score above level 2.
        - Higher scores require multiple credible and varied sources.

        SCORING SCALE GUIDANCE:

        0 → No meaningful evidence of the rubric requirement  
        1 → Minimal or very weak evidence; mentioned but not developed  
        2 → Limited or partial fulfillment; important gaps remain  
        3 → Adequate fulfillment; clearly present but incomplete or uneven  
        4 → Strong and well-developed; mostly complete and clearly articulated  
        5 → Exceptional; explicit, comprehensive, and clearly distinguished from lower levels  

        Use professional judgment, but prioritize rubric-aligned evidence over general impression.

        For each category (A1–A6), explicitly determine which rubric descriptor the evidence most closely matches, then assign that score.

        POINTS FOR TEACHER REVIEW

        Before returning the scores, identify exactly five places where the student's submission contains evidence, omissions, or ambiguities that could reasonably lead experienced scorers to disagree.

        For each potential scoring disagreement, write one concise review prompt for the teacher.

        Every review prompt must be specific to this submission and must explicitly reference at least one concrete feature, claim, source, stakeholder, measurement, omission, ambiguity, person, organization, or design detail found in the student's work.

        A prompt is not sufficiently document-specific if it could be copied unchanged into another student's portfolio.

        Each prompt must:

        direct the teacher to inspect or verify evidence before accepting the score;
        identify a genuine scoring judgment rather than a routine checklist item;
        avoid stating the scoring conclusion;
        avoid recommending a score;
        avoid merely summarizing a strength or weakness;
        avoid rubric shorthand such as A1, A2, A5, or similar subelement labels;
        avoid explicit score references such as "zero score", "full marks", or "higher score";
        read naturally as if written by an experienced moderator leaving review notes for another scorer.

        Every review prompt must be specific to this submission and must explicitly reference at least one concrete feature, claim, source, stakeholder, measurement, omission, ambiguity, constraint, design requirement, user, person, organization, or design detail found in the student's work.

        Before returning the prompts, check all five. If any could apply unchanged to another student's portfolio, rewrite it so that it depends on details unique to this submission.

        Prefer review prompts that identify genuine scoring judgments rather than routine rubric reminders.

        Poor:
        "Verify whether the document includes credible sources."

        Better:
        "Determine whether the Declaration itself should be treated as sufficient supporting evidence or whether independent sources are expected."

        Poor:
        "Check stakeholder groups."

        Better:
        "Consider whether references to British citizens, Parliament, the Crown, and Indigenous peoples represent distinct stakeholder groups or merely passing mentions."

        Poor:
        "Look for a problem statement."

        Better:
        "Decide whether the colonies' grievances collectively function as an explicit problem definition despite the absence of a labeled problem statement."

        Poor:
        "Verify whether the problem statement clearly defines the problem."

        Better:
        "Determine whether the cited L1/L2 spinal injury and Mr. Kiser's height provide sufficient evidence that bending while pushing the cart is the primary engineering problem rather than simply one contributing factor."

        Return the review prompts as a JSON array named "scoring_hints".

        Return only valid JSON in exactly this format:

        {{
        "A1": {{"score": X, "rationale": "."}},
        "A2": {{"score": X, "rationale": "."}},
        "A3": {{"score": X, "rationale": "."}},
        "A4": {{"score": X, "rationale": "."}},
        "A5": {{"score": X, "rationale": "."}},
        "A6": {{"score": X, "rationale": "."}},
        "narrative_feedback": "180–220 word single-paragraph explanation referencing strengths, weaknesses, and improvement suggestions.",
        "scoring_hints": [
            "...",
            "...",
            "..."
        ]}}
        """
    else:
        raise ValueError(f"Unsupported blended_model version: {blended_model}")

    prompt = prompt + "\n\n" + NARRATIVE_INSTRUCTION

    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
        },
        {
            "role": "user",
            "content": prompt
        }
    ]

    # === Call the API ===
    
    gpt_model = get_gpt_model(blended_model)

    response = openai.ChatCompletion.create(
        model=gpt_model,
        messages=messages,
        temperature=0,
        top_p=1,
        max_tokens=1500
    )

    usage = response["usage"]

    input_tokens = usage["prompt_tokens"]
    output_tokens = usage["completion_tokens"]
    cached_tokens = usage["prompt_tokens_details"]["cached_tokens"]
    total_tokens = usage["total_tokens"]

    print(
        f"{filename}\t"
        f"{document_length}\t"
        f"{input_tokens}\t"
        f"{output_tokens}\t"
        f"{cached_tokens}\t"
        f"{total_tokens}"
    )

    print("MODEL BEING USED:", gpt_model)

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
            print("⚠️ Retrying once...")

            retry_response = openai.ChatCompletion.create(
                model=gpt_model,
                messages=messages,
                temperature=0,
                top_p=1,
                max_tokens=1500
            )

            try:
                retry_str = retry_response.choices[0].message.content.strip()

                # Remove markdown fences
                if retry_str.startswith("```"):
                    retry_str = "\n".join(
                        line for line in retry_str.splitlines()
                        if not line.strip().startswith("```")
                    ).strip()

                # Keep only JSON object
                first_brace = retry_str.find("{")
                if first_brace != -1:
                    retry_str = retry_str[first_brace:]

                last_brace = retry_str.rfind("}")
                if last_brace != -1:
                    retry_str = retry_str[:last_brace + 1]

                retry_str = clean_json_string(retry_str)

                response_dict = json5.loads(retry_str)
                print("✅ Retry succeeded")

            except Exception as retry_error:
                print(f"❌ Retry failed for {filename}: {retry_error}")
                return {
                    "truncation_detected": 1,
                    "incomplete_response": True
                }
        # --- Extract and normalize scoring hints ---
        raw_hints = response_dict.get("scoring_hints", [])

        #print("1. RAW MODEL HINTS:", response_dict.get("scoring_hints"))

        if isinstance(raw_hints, list):
            scoring_hints = [
                str(hint).strip()
                for hint in raw_hints
                if hint is not None and str(hint).strip()
            ]
        elif isinstance(raw_hints, str) and raw_hints.strip():
            # Defensive fallback in case the model returns one string.
            scoring_hints = [raw_hints.strip()]
        else:
            scoring_hints = []

        # --- Flatten nested structure ---
        flattened = {}
        for key, value in response_dict.items():
            if isinstance(value, dict) and "score" in value and "rationale" in value:
                flattened[key] = value["score"]
                flattened[f"{key}_rationale"] = value["rationale"]
            else:
                flattened[key] = value

        response_dict = flattened

        #print("2. PARSED HINTS:", scoring_hints)

        response_dict["truncation_detected"] = 0

        for i in range(1,7):
            response_dict[f"A{i}"] = int(response_dict.get(f"A{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1, 7):
            response_dict[f"A{i}_api"] = int(response_dict[f"A{i}"])

        response_dict["element_score_api"] = sum(
            response_dict[f"A{i}_api"] for i in range(1, 7)
        ) / 6.0

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 7):
            assert f"A{i}" in response_dict, f"Missing A{i}"
            assert f"A{i}_rationale" in response_dict, f"Missing A{i}_rationale"

        return response_dict

    except json.JSONDecodeError as e:
        print(f"❌ JSON parse failed for {filename}")
        #print(response_str)
        return {
            "truncation_detected": 1
        }

def natural_sort_key(s):
    return [int(text) if text.isdigit() else text.lower() for text in re.split(r'(\d+)', s)]

def build_result_row(filename, text, response_dict, idx):
    """
    Shared row construction for BOTH CLI and controller paths.

    Ensures identical schema regardless of entry point.
    """

    row = response_dict.copy()

    # Metadata
    row["Case"] = idx
    row["filename"] = filename
    row["text"] = text

    # Truncation flag
    row["truncation_detected"] = response_dict.get("truncation_detected", 0)

    # Incomplete response detection
    row["incomplete_response"] = any(
        k not in response_dict for k in [f"A{i}" for i in range(1, 7)]
    )

    # Ensure integer scores
    for i in range(1, 7):
        row[f"A{i}"] = int(row.get(f"A{i}", 0))

    return row

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
            row = build_result_row(filename, text, response_dict, idx)

            # Ensure A scores are integers
            for i in range(1, 7):
                row[f"A{i}"] = int(row.get(f"A{i}", 0))

            results.append(row)

        except Exception:
            print(f"\nFULL TRACEBACK for {filename}:")
            traceback.print_exc()
            print("⚠️ Skipping this document and continuing...")
            continue    

    output_df = pd.DataFrame(results)
    # Reorder columns
    core = ["Case", "filename", "text", "incomplete_response"]

    scores = [f"A{i}" for i in range(1, 7)]
    api_scores = [f"A{i}_api" for i in range(1, 7)]  # 👈 NEW

    flags = [f"A{i}_flag" for i in range(1, 7)]
    rationales = [f"A{i}_rationale" for i in range(1, 7)]

    extras = [
        "truncation_detected",
        "element_score_api",
        "narrative_feedback"
    ]

    #print("COLUMNS BEFORE REORDER:", output_df.columns.tolist())

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
        #print("EXTRACTED SAMPLE:", text[:300])

        response_dict = score_document(filename, text, blended_version)
        if response_dict is None:
            print(f"Skipping {filename} due to failure.")
            continue

        row = build_result_row(filename, text, response_dict, idx)

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        #print("Narrative in row:", row.get("narrative_feedback"))

        # NOTE: No rule processing currently implemented for Element A.
        # Placeholder for future blended model rule logic (e.g., v1.x adjustments).
        # If rules are added later, they should modify A{i} (not A{i}_api).

        # Ensure API scores are present in output
        for i in range(1, 7):
            row[f"A{i}_api"] = response_dict.get(f"A{i}_api", row.get(f"A{i}", 0))

        #print("3. ROW HINTS:", row.get("scoring_hints"))

        results.append(row)

    #print(
    #    "4. ALL RESULT HINTS:",
    #    [r.get("scoring_hints") for r in results]
    #)

    # ✅ return is OUTSIDE the loop, INSIDE the function
    return pd.DataFrame(results)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.0", "v1.2"], default="v1.2",
                        help="Which blended model logic to apply (default: v1.2)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
