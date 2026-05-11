import os
import json5
import json
import re
import argparse
import pandas as pd
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT))

import openai
import pytesseract
from scripts.shared.utils import extract_text_with_fallback
import traceback
import shutil
import time
import openai

def call_openai_with_retry(model, messages, max_tokens=2500, attempts=4):
    for attempt in range(attempts):
        try:
            return openai.ChatCompletion.create(
                model=model,
                messages=messages,
                temperature=0,
                top_p=1,
                max_tokens=max_tokens
            )
        except (openai.error.APIError,
                openai.error.Timeout,
                openai.error.APIConnectionError,
                openai.error.RateLimitError) as e:
            if attempt == attempts - 1:
                raise
            wait = 2 ** attempt
            print(f"⚠️ API error, retrying in {wait}s: {e}")
            time.sleep(wait)

# =========================
# GPT MODEL CONFIGURATION
# =========================

GPT_MODEL_LEGACY = "gpt-4.1-mini"
GPT_MODEL_CURRENT = "gpt-4.1-mini"   

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element H rubric consistently and professionally.

Your role is to evaluate student engineering design documentation and assign scores for each rubric criterion.

SCORING PRINCIPLES:

- Base scores strictly on explicit evidence in the student document.
- Do not assume missing elements are present.
- Do not reward effort or intent unless supported by clear evidence.
- When evidence is incomplete, award partial credit rather than full credit.
- Distinguish clearly between weak, adequate, strong, and exceptional submissions.
- Use the full 0–5 scoring scale when justified.

Always return valid JSON exactly in the requested format."""

NARRATIVE_INSTRUCTION = """
After assigning scores and providing brief criterion rationales, write a 170–220 word narrative feedback summary.

The summary must:
- Be written in 2–3 clear paragraphs.
- Explain strengths or weaknesses in how the testing plan addresses important design requirements (H1).
- Discuss the clarity, detail, and replicability of testing procedures (H2).
- Address the adequacy of planned test quantity, trial counts, and requirement coverage (H3).
- Evaluate the logic of the testing plan, including expected results, pass/fail criteria, thresholds, goals, and measurable outcomes (H4).
- Discuss the role and quality of field expert feedback and whether revisions based on that feedback are evident (H5).
- Acknowledge when requirements were inferred from testing content rather than explicitly stated.
- Provide 2–4 specific, actionable recommendations for improving testing rigor, repeatability, measurement quality, requirement alignment, or expert-informed revision.
- Be professional, readable, and student-facing.
- Avoid mentioning scoring mechanics, AI, calibration, OCR, or internal evaluation processes.
- Use natural paragraph breaks for readability.

The narrative should reward clearly testable engineering thinking while identifying vague, unsupported, or insufficiently measurable testing plans.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 170 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "MyDesign_Element_H_Scoring_Rubric.txt"

print("RUBRIC PATH:", RUBRIC_PATH)

with open(RUBRIC_PATH, "r", encoding="utf-8") as f:
    RUBRIC_TEXT = f.read().strip()  

def sanitize_for_json(text: str) -> str:
    if text is None:
        return ""

    # Remove NULs
    text = text.replace("\x00", "")

    # Remove other problematic control chars except common whitespace
    text = re.sub(r"[\x01-\x08\x0B\x0C\x0E-\x1F\x7F]", "", text)

    # Drop unpaired surrogates / invalid unicode for JSON encoding
    text = text.encode("utf-8", "ignore").decode("utf-8", "ignore")

    return text

def clean_json_string(raw):
    # Remove trailing commas before } or ]
    return re.sub(r',\s*([}\]])', r'\1', raw)

def get_gpt_model(blended_version: str) -> str:
    """
    Selects the correct GPT model based on blended model version.
    """
    if blended_version == "v1.0":
        return GPT_MODEL_LEGACY
    elif blended_version == "v2.0":
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
    content = sanitize_for_json(content)
    filename = sanitize_for_json(filename)

    # Choose prompt based on model
    if blended_model == "v1.0":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring a MyDesign Element H engineering document using the Element H blended scoring model.

        The document may contain OCR errors, screenshots, formatting corruption, incomplete tables, or PDF artifacts. Focus on recovering meaning rather than penalizing formatting problems.

        FIRST:
        Infer the likely high-priority design requirements from the testing content itself. Requirements may be explicit or implied through pass/fail criteria, measurements, goals, thresholds, expected results, or repeated testing themes.

        SECOND:
        Evaluate whether the testing plan meaningfully tests those inferred requirements.

        Do not require perfect labeling or formatting.

        Score conservatively when:
        - tests are vague
        - procedures are not replicable
        - If trial counts are absent, unclear, or unrealistic, H3 should normally score 0–2.
        - pass/fail criteria are absent
        - requirements are only mentioned but not actually tested

        Strong scores require:
        - clear testing procedures
        - measurable outcomes
        - multiple trials
        - logical evaluation criteria
        - alignment between tests and inferred requirements
        - evidence that field expert feedback influenced revisions

        Do not award points for:
        - headings alone
        - templates left blank
        - vague statements
        - implied quality without evidence

        Accept functionally equivalent wording and partially damaged OCR text when meaning is still recoverable.

        Expected results without measurable criteria should not receive high scores for H4.

        H5 should score 0 unless the document clearly identifies field expert involvement.
        Mentions of teachers, classmates, teammates, or unspecified reviewers do not count.
        Scores above 2 require clear evidence that feedback changed the testing plan.

        If the document contains only headings, template placeholders, extremely vague statements, or minimal disconnected content, scores of 0 across multiple subelements may be appropriate.

        Return only valid JSON in exactly this format:

        {{
          "inferred_requirements": [
            "short requirement phrase",
            "short requirement phrase",
            "short requirement phrase"
        ],
        "H1": {{"score": X, "rationale": "..." }},
        "H2": {{"score": X, "rationale": "..." }},
        "H3": {{"score": X, "rationale": "..." }},
        "H4": {{"score": X, "rationale": "..." }},
        "H5": {{"score": X, "rationale": "..." }},
        "narrative_feedback": ""
        }}
        """
    elif blended_model == "v2.0":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring a MyDesign Element H engineering document using Blended Model v2.0.

        Score strictly based on explicit, testable evidence.

        FIRST:
        Identify or infer the project’s major design requirements from the testing plan content.

        SECOND:
        Evaluate whether the document provides structured, logical, and measurable testing aligned to those requirements.

        Apply the following principles:

        H1:
        Requirements must connect to actual test procedures. Mentions alone do not count.

        H2:
        Testing procedures must be sufficiently detailed for replication, including setup, tools, variables, measurements, and sequence of actions.

        H3:
        Testing quantity must be feasible and sufficient. Explicit trial counts and broad requirement coverage support higher H3 scores, but partially developed testing quantity planning may still support moderate scores.

        H4:
        Strong H4 scores require measurable outcomes or clear evaluation logic tied to requirements. Explicit thresholds are preferred, but partially measurable or logically implied evaluation criteria may still support moderate scores. Expected results without measurable criteria should not receive high scores.

        H5:
        Should score 0 unless the document clearly identifies field expert involvement.
        Mentions of teachers, classmates, teammates, or unspecified reviewers do not count.
        Scores above 2 require clear evidence that feedback changed the testing plan.

        Accept:
        - implied but logically coherent testing
        - semantically equivalent requirement wording
        - paraphrased expert feedback

        Do not reward:
        - formatting alone
        - empty structure
        - vague references
        - unsupported claims

        Score using the rubric scale 0-5 for each subelement.


        If the document contains only headings, template placeholders, extremely vague statements, or minimal disconnected content, scores of 0 across multiple subelements may be appropriate.

        Return only valid JSON in exactly this format:

        {{
         "inferred_requirements": [
            "short requirement phrase",
            "short requirement phrase",
            "short requirement phrase"
        ],
        "H1": {{"score": X, "rationale": "..." }},
        "H2": {{"score": X, "rationale": "..." }},
        "H3": {{"score": X, "rationale": "..." }},
        "H4": {{"score": X, "rationale": "..." }},
        "H5": {{"score": X, "rationale": "..." }},
        "narrative_feedback": ""
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

    try:
        response = call_openai_with_retry(gpt_model, messages, max_tokens=2500)
    except Exception:
        print("FAILED FILE:", filename)
        raise

    print("MODEL BEING USED:", gpt_model)

    # === Extract text from OpenAI response (legacy SDK) ===
    try:
        if response is None:
            print(f"❌ GPT call returned None for {filename}")
            return {}
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

                retry_response = call_openai_with_retry(gpt_model, messages, max_tokens=2500)

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

        if isinstance(response_dict.get("inferred_requirements"), list):
            response_dict["inferred_requirements"] = "; ".join(response_dict["inferred_requirements"])

        response_dict["truncation_detected"] = 0

        for i in range(1,6):
            response_dict[f"H{i}"] = int(response_dict.get(f"H{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1,6):
            response_dict[f"H{i}_api"] = int(response_dict[f"H{i}"])

        scores = [response_dict[f"H{i}_api"] for i in range(1,6)]
        response_dict["element_score_api"] = sum(scores) / len(scores)

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 6):
            assert f"H{i}" in response_dict, f"Missing H{i}"
            assert f"H{i}_rationale" in response_dict, f"Missing H{i}_rationale"

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

            if not response_dict or response_dict.get("truncation_detected") == 1:
                print(f"❌ Invalid or incomplete response for {filename}")
                continue

            # Start from full response_dict so nothing is lost
            row = response_dict.copy()

            row["truncation_detected"] = response_dict.get("truncation_detected", 0)

            # Add metadata fields
            row["Case"] = idx
            row["filename"] = filename
            row["text"] = text

            row["incomplete_response"] = any(
                k not in response_dict for k in [f"H{i}" for i in range(1, 6)]
            )

            # Ensure h scores are integers
            for i in range(1, 6):
                row[f"H{i}"] = int(row.get(f"H{i}", 0))

            print(
                filename,
                [row[f"H{i}_api"] for i in range(1, 6)],
                row["element_score_api"]
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

    scores = [f"H{i}" for i in range(1, 6)]
    api_scores = [f"H{i}_api" for i in range(1, 6)]  # 👈 NEW

    flags = [f"H{i}_flag" for i in range(1, 6)]
    rationales = [f"H{i}_rationale" for i in range(1, 6)]

    extras = [
        "inferred_requirements",
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
        
        for i in range(1, 6):
            if response_dict is None:
                raise ValueError(f"response_dict is None for {filename}")
            print("H key value:", i, type(response_dict.get(f"H{i}")), response_dict.get(f"H{i}"))
            row[f"H{i}"] = int(response_dict.get(f"H{i}", 0))
            row[f"H{i}_rationale"] = response_dict.get(f"H{i}_rationale", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        # --- Attach API scores ---
        for i in range(1, 6):
            row[f"H{i}_api"] = int(
                response_dict.get(f"H{i}_api", response_dict.get(f"H{i}", 0))
            )

        # --- Attach element API score ---
        row["element_score_api"] = float(
            response_dict.get("element_score_api", 0)
        )

        print("Narrative in row:", row.get("narrative_feedback"))

        print(
                filename,
                [row[f"H{i}_api"] for i in range(1, 6)],
                row["element_score_api"]
            )

        results.append(row)

    # ✅ return is OUTSIDE the loop, INSIDE the function
    return pd.DataFrame(results)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.0", "v2.0"], default="v2.0",
                        help="Which blended model logic to apply (default: v2.0)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
