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

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element K rubric consistently and professionally.

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
- Discuss how clearly and comprehensively the student summarizes the engineering design process (K1).
- Explain how effectively the student provides value judgments about project steps, including what went well, what did not go well, and why (K2).
- Evaluate whether the lessons learned are specific, thoughtful, and useful to future engineers beyond this particular project (K3).
- Identify strengths and weaknesses in the student’s reflection.
- Provide 2–4 specific, actionable recommendations for improving process summary, evaluative reflection, or transferable lessons learned.
- Be professional, readable, and student-facing.
- Avoid mentioning scoring mechanics, AI, calibration, OCR, or internal evaluation processes.
- Use natural paragraph breaks for readability.

The narrative should reward thoughtful reflection, honest evaluation, and useful engineering-process lessons while identifying vague, descriptive, or project-specific reflection.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 170 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "MyDesign_Element_K_Scoring_Rubric.txt"

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
    content = sanitize_for_json(content)
    filename = sanitize_for_json(filename)

    # Choose prompt based on model
    if blended_model == "v1.0":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring a MyDesign Element K engineering reflection document using Blended Model v1.0.

        Element K evaluates student reflection on the engineering design process.

        The document may contain OCR errors, screenshots, formatting corruption, incomplete tables, or PDF artifacts. Focus on recovering meaning rather than penalizing formatting issues.

        FIRST:
        Identify which phases or steps of the engineering design process are summarized.

        Typical phases may include:
        - problem definition
        - research
        - ideation
        - prototype selection/design
        - construction
        - testing
        - evaluation

        Equivalent wording is acceptable.

        SECOND:
        Determine whether the student provides value judgments about project steps, including:
        - what went well
        - what did not go well
        - why decisions or outcomes were successful or unsuccessful

        THIRD:
        Determine whether the lessons learned are useful to future engineers and transferable beyond this specific project.

        Score conservatively when:
        - reflections are vague or generic
        - summaries are incomplete
        - process steps are only briefly mentioned
        - lessons learned apply only to this specific project
        - statements lack explanation or analysis
        - the document mostly describes events without reflection

        Strong scores require:
        - clear summaries of multiple engineering design phases
        - meaningful evaluation of successes and failures
        - explanation of why outcomes occurred
        - thoughtful lessons learned
        - insights useful to future engineering work
        - process-focused reflection rather than storytelling

        Do not award points for:
        - headings alone
        - generic statements without analysis
        - simple descriptions of activities
        - recommendations for future project versions (belongs in Element L)
        - vague positive statements such as “it went well” without explanation

        K1 evaluates completeness and clarity of project summary.
        K2 evaluates quality of value judgment and reflection.
        K3 evaluates lessons learned and usefulness to future engineers.

        If the document contains minimal reflection, vague narrative, or little discussion of the engineering design process, scores of 0 across multiple subelements may be appropriate.

        Return only valid JSON in exactly this format:

        {{
        "reflected_steps": [
            "short process step phrase",
            "short process step phrase",
            "short process step phrase"
        ],
        "K1": {{"score": X, "rationale": "..."}},
        "K2": {{"score": X, "rationale": "..."}},
        "K3": {{"score": X, "rationale": "..."}},
        "narrative_feedback": ""
        }}
        """
    elif blended_model == "v1.2":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring a MyDesign Element K engineering reflection document using Blended Model v1.2.

        Element K evaluates student reflection on the engineering design process.

        Score strictly based on explicit evidence in the student document.

        FIRST:
        Identify whether the student summarizes multiple phases of the engineering design process.

        SECOND:
        Evaluate whether the student provides value judgments regarding project steps, including discussion of:
        - successes
        - failures
        - challenges
        - effectiveness of decisions
        - reasons outcomes occurred

        THIRD:
        Evaluate whether the lessons learned are generalized and useful to future engineers rather than limited only to this project.

        IMPORTANT:

        Many student reflections are written directly inside portfolio templates.

        Do not assume a document is blank, incomplete, or only a template merely because template instructions, prompts, headings, or rubric text are present.

        If substantive student reflection appears anywhere in the document, score that reflection using the rubric.

        Only treat a document as blank when no meaningful student-generated reflection content is present.

        Apply the following guidance:

        K1:
        Scores of 3 or higher require clearly identifiable summaries of at least three distinct engineering design phases or project components.

        Do not award high K1 scores for:
        - generic storytelling
        - chronological narration without clear process phases
        - repeated discussion of a single phase
        - vague descriptions of “working on the project”
        - reflection that lacks identifiable engineering-process structure

        Length alone should not increase K1 scores.
        
        K2:
        High scores require explicit evaluation of project performance or decisions. Design justification alone is not sufficient. Statements such as “we chose this because…” are not strong value judgment unless the student evaluates whether the decision was successful or unsuccessful.

        K3:
        High scores require lessons learned that are transferable to future engineering or design work. Project-specific recommendations or future feature ideas belong in Element L and should not strongly increase K3 scores.

        CRITICAL DISTINCTION FOR ELEMENT K

        Do not award K1 simply because the student discusses the project, design decisions, prototypes, testing, or project history.

        K1 requires explicit summaries of major engineering design process phases and what the team actually did during those phases.

        Do not award K2 simply because the student explains or justifies a decision. A value judgment requires evaluation of effectiveness, success, failure, importance, quality, usefulness, or impact. Explanations and justifications alone are not value judgments.

        Do not award K3 simply because the student states what happened or what they learned during the project. Lessons learned must be generalized beyond the specific project and presented in a way that would be useful to future engineering design efforts. Project-specific observations alone do not qualify as lessons learned.

        Score conservatively when:
        - reflections are vague or repetitive
        - summaries lack distinct engineering phases
        - lessons learned apply only to this project
        - reflections are mostly descriptive rather than evaluative
        - the document contains generic positivity without analysis

        Do not reward:
        - formatting alone
        - blank template sections
        - generic storytelling
        - unsupported claims of success
        - recommendations for future project development
        - superficial statements without explanation

        Prioritize depth of reflection, process understanding, and transferable engineering insight over writing polish.

        Return only valid JSON in exactly this format:

        {{
        "reflected_steps": [
            "short process step phrase",
            "short process step phrase",
            "short process step phrase"
        ],
        "K1": {{"score": X, "rationale": "..."}},
        "K2": {{"score": X, "rationale": "..."}},
        "K3": {{"score": X, "rationale": "..."}},
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

        for i in range(1,4):
            response_dict[f"K{i}"] = int(response_dict.get(f"K{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1,4):
            response_dict[f"K{i}_api"] = int(response_dict[f"K{i}"])
            response_dict[f"K{i}_raw"] = int(response_dict[f"K{i}"])

        scores = [response_dict[f"K{i}_api"] for i in range(1,4)]
        response_dict["element_score_api"] = sum(scores) / len(scores)
        response_dict["element_score_raw"] = response_dict["element_score_api"]

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 4):
            assert f"K{i}" in response_dict, f"Missing K{i}"
            assert f"K{i}_rationale" in response_dict, f"Missing K{i}_rationale"

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
            text = extract_text_with_fallback(file_path)
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
                k not in response_dict for k in [f"K{i}" for i in range(1, 4)]
            )

            # Ensure J scores are integers
            for i in range(1, 4):
                row[f"K{i}"] = int(row.get(f"K{i}", 0))

            print(
                filename,
                [row[f"K{i}_api"] for i in range(1, 4)],
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

    scores = [f"K{i}" for i in range(1, 4)]
    api_scores = [f"K{i}_api" for i in range(1, 4)]  # 👈 

    flags = [f"K{i}_flag" for i in range(1, 4)]
    rationales = [f"K{i}_rationale" for i in range(1, 4)]

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
        if not response_dict or response_dict.get("truncation_detected") == 1:
            print(f"Skipping {filename} due to failure.")
            continue

        row = {
            "Case": idx,
            "filename": filename,
            "text": text,
        }
        
        for i in range(1, 4):
            if not response_dict or response_dict.get("truncation_detected") == 1:
                raise ValueError(f"response_dict is None for {filename}")
            print("J key value:", i, type(response_dict.get(f"K{i}")), response_dict.get(f"K{i}"))
            row[f"K{i}"] = int(response_dict.get(f"K{i}", 0))
            row[f"K{i}_rationale"] = response_dict.get(f"K{i}_rationale", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        # --- Attach API and Raw scores ---
        for i in range(1, 4):
            row[f"K{i}_api"] = int(
                response_dict.get(f"K{i}_api", response_dict.get(f"K{i}", 0))
            )
            row[f"K{i}_raw"] = int(
                response_dict.get(f"K{i}_raw", response_dict.get(f"K{i}", 0))
            )

        # --- Attach element API and Raw score ---
        row["element_score_api"] = float(
            response_dict.get("element_score_api", 0)
        )
        row["element_score_raw"] = float(
            response_dict.get("element_score_raw", 0)
        )

        print("Narrative in row:", row.get("narrative_feedback"))

        print(
                filename,
                [row[f"K{i}_api"] for i in range(1, 4)],
                row["element_score_api"]
            )

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
