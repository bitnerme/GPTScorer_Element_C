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
import pytesseract
from pdf2image import convert_from_path
import win32com.client
import pythoncom
from scripts.shared.utils import extract_text_with_fallback
import traceback


# Resolve project root: c:\GPTScorer
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Set path to Tesseract executable
pytesseract.pytesseract.tesseract_cmd = r"C:\Program Files\Tesseract-OCR\tesseract.exe"

# =========================
# GPT MODEL CONFIGURATION
# =========================

GPT_MODEL_LEGACY = "gpt-3.5-turbo"
GPT_MODEL_CURRENT = "gpt-4.1-mini"    #"gpt-4-0613"

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
        return GPT_MODEL_LEGACY
    elif blended_version == "v1.2":
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
    if blended_model == "v1.0":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Scoring Guidance:

        - Apply an expert-scorer interpretation of the rubric.
        - Accept implicit evidence when the context clearly supports understanding.
        - Allow narrative framing if it helps explain the design situation.
        - Reward partial completion when the student's explanation suggests meaningful understanding.

        Evidence Interpretation:

        - Students may communicate ideas through narrative explanation rather than formal structure.
        - When evidence is implied but reasonably supported by the document, award partial credit rather than assigning the lowest score.
        - Minor gaps or missing details should not automatically reduce scores if the core idea is clear.

        Specific Interpretation Guidance:

        A4 (Concern of Primary Stakeholder Groups):

        Stakeholder groups must represent clearly distinct populations affected
        by the problem.

        Closely related groups should not be counted as separate stakeholders.
        For example:
        - students, high school students, teenagers → count as one group
        - teachers, instructors, educators → count as one group

        Listing only one or two stakeholder groups should not receive a score
        above level 2.

        Scores of 4 or 5 require several clearly differentiated stakeholder
        groups representing different roles or interests (e.g., students,
        teachers, parents, administrators, community members).

        Scoring Scale Anchors (Apply to All A1–A6 Dimensions):

        Use the following general scale guidance when assigning 0–5 scores:

        0 = No evidence of the criterion in the document.
        1 = Minimal or very weak evidence; criterion is mentioned but not developed.
        2 = Limited evidence; partially addressed but lacking clarity or detail.
        3 = Moderate evidence; clearly present but incomplete or uneven.
        4 = Strong evidence; clearly articulated and mostly complete.
        5 = Exceptional evidence; explicit, detailed, comprehensive, and clearly distinguished from weaker performance levels.

        When evidence falls between levels, select the score that best reflects overall strength rather than defaulting to the lowest possible score.

        Student Document:
        \"\"\"{content}\"\"\"

        Return only valid JSON in exactly this format:

        {{
        "A1": {{"score": X, "rationale": "."}},
        "A2": {{"score": X, "rationale": "."}},
        "A3": {{"score": X, "rationale": "."}},
        "A4": {{"score": X, "rationale": "."}},
        "A5": {{"score": X, "rationale": "."}},
        "A6": {{"score": X, "rationale": "."}},
        "narrative_feedback": "180–220 word single-paragraph explanation referencing strengths, weaknesses, and improvement suggestions."
        }}
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

        Return only valid JSON in exactly this format:

        {{
        "A1": {{"score": X, "rationale": "."}},
        "A2": {{"score": X, "rationale": "."}},
        "A3": {{"score": X, "rationale": "."}},
        "A4": {{"score": X, "rationale": "."}},
        "A5": {{"score": X, "rationale": "."}},
        "A6": {{"score": X, "rationale": "."}},
        "narrative_feedback": "180–220 word single-paragraph explanation referencing strengths, weaknesses, and improvement suggestions."
        }}
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

            # Check for truncation
            if is_truncated_json(cleaned):
                print("⚠️ Detected truncated JSON. Retrying once...")

                retry_response = openai.ChatCompletion.create(
                    model=gpt_model,
                    messages=messages,
                    temperature=0,
                    top_p=1,
                    max_tokens=1500
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
        print(response_str)
        return {
            "truncation_detected": 1
        }

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
                k not in response_dict for k in [f"A{i}" for i in range(1, 7)]
            )

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
            print("A key value:", i, type(response_dict.get(f"A{i}")), response_dict.get(f"A{i}"))
            row[f"A{i}"] = int(response_dict.get(f"A{i}", 0))
            row[f"A{i}_rationale"] = response_dict.get(f"A{i}_rationale", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        print("Narrative in row:", row.get("narrative_feedback"))

        results.append(row)

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
