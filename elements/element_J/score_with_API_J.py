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

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element J rubric consistently and professionally.

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
- Discuss the quality and relevance of the project’s external evaluation (J1).
- Explain how effectively the student analyzes and interprets evaluation feedback (J2).
- Evaluate the specificity, feasibility, and usefulness of proposed improvements or recommendations (J3).
- Discuss the quality and relevance of the project’s external evaluation (J1).
- Explain how effectively the student analyzes and synthesizes evaluation feedback (J2).
- Evaluate the specificity, detail, and usefulness of the synthesis and conclusions drawn from the external evaluation (J3).
- Identify strengths and weaknesses in the student’s evaluation and reflection process.
- Provide 2–4 specific, actionable recommendations for improving evaluation quality, analysis depth, reflection, or future planning.
- Be professional, readable, and student-facing.
- Avoid mentioning scoring mechanics, AI, calibration, OCR, or internal evaluation processes.
- Use natural paragraph breaks for readability.

The narrative should reward thoughtful reflection and meaningful evaluation while identifying vague, unsupported, or superficial analysis.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 170 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "MyDesign_Element_J_Scoring_Rubric.txt"

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
    elif blended_version == "v1.5":
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

        You are scoring a MyDesign Element J document using the Element J blended scoring model v1.0.

        Element J evaluates documentation of external project evaluation. Score only Element J evidence.

        The document may contain OCR errors, formatting corruption, incomplete tables, or PDF artifacts. Focus on recoverable meaning rather than formatting quality.

        FIRST:
        Identify documented evaluators, including stakeholders and field experts. Determine whether their roles are stated and whether field experts are demonstrably qualified.

        SECOND:
        Identify the external evaluation feedback. Determine whether it addresses aspects of the design or engineering design process, such as prototype performance, testing results, analysis, design decisions, iteration, usability, constraints, or improvement needs.

        THIRD:
        Evaluate whether the student summarizes and synthesizes the external evaluation in a specific, detailed, and thorough way.

        Score conservatively when:
        - evaluators are named but their stakeholder/expert role is unclear
        - field expert qualifications are not demonstrated
        - feedback is vague praise or general opinion
        - feedback does not address the design or engineering design process
        - evaluation evidence is copied or listed without synthesis
        - the document contains template headings but little completed content

        Strong scores require:
        - multiple documented evaluators
        - clear stakeholder roles or demonstrably qualified field experts
        - feedback tied to specific aspects of the design or engineering process
        - synthesis that identifies meaningful trends, takeaways, or patterns across evaluator comments
        - specific details from evaluator feedback

        Do not award points for:
        - headings alone
        - blank template sections
        - evaluator names without documented feedback
        - undocumented claims that feedback was received
        - generic praise disconnected from the project
        - summaries that merely repeat comments without synthesis

        J1 focuses on who provided evaluation and whether stakeholder/expert roles are documented.
        J2 focuses on whether the evaluation addresses one or more aspects of the design or engineering design process.
        J3 focuses on the specificity, detail, thoroughness, and synthesis of the external evaluation summary.

        J2:
        Scores of 4–5 require evaluation that clearly addresses identifiable aspects of the design or engineering design process. Generic praise or broad opinions should not score above 2.

        J3:
        Scores of 4–5 require synthesis that integrates multiple evaluator comments into clear conclusions, trends, or takeaways. Merely listing evaluator comments should not score above 2.

        If the document contains no meaningful evaluator documentation, external evaluation, or synthesis, scores of 0 across multiple subelements may be appropriate.

        Return only valid JSON in exactly this format:

        {{
        "evaluation_sources": [
            "short evaluator/source phrase",
            "short evaluator/source phrase",
            "short evaluator/source phrase"
        ],
        "J1": {{"score": X, "rationale": "..."}},
        "J2": {{"score": X, "rationale": "..."}},
        "J3": {{"score": X, "rationale": "..."}},
        "narrative_feedback": ""
        }}
        """
    elif blended_model == "v1.5":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring a MyDesign Element J document using Blended Model v1.5.

        Element J evaluates documentation of external project evaluation. Score only Element J evidence.

        Score strictly based on explicit documentation in the student submission.

        FIRST:
        Identify all documented stakeholders and field experts. Do not infer roles or qualifications unless the document provides enough context to support them.

        SECOND:
        Identify documented external evaluation feedback and determine whether it addresses aspects of the design or engineering design process.

        THIRD:
        Evaluate the student’s synthesis of the external evaluation, including whether the summary identifies meaningful trends, specific takeaways, or patterns across evaluator comments.

        Apply the following principles:

        J1:
        High scores require multiple stakeholders and/or demonstrably qualified field experts with documented evaluation. Stakeholders and field experts should not be double-counted unless separate roles are clearly justified.     
        Do not count teachers, classmates, friends, teammates, or family members as field experts unless demonstrable professional expertise relevant to the project is explicitly documented.
        Mentions of receiving comments or opinions do not alone qualify as stakeholder evaluation.

        J2:
        High scores require evaluator feedback addressing aspects of the design or engineering design process.
        Scores of 4–5 require evaluation that clearly addresses identifiable aspects of the design or engineering design process. Generic praise or broad opinions should not score above 2.

        J3:
        High scores require a specific, detailed, and thorough synthesis of the external evaluation. Strong synthesis identifies meaningful trends, takeaways, patterns, or conclusions across evaluator comments. Merely listing or copying comments is not strong synthesis.  Scores of 4–5 require synthesis that integrates multiple evaluator comments into clear conclusions, trends, or takeaways. Merely listing evaluator comments should not score above 2.
        Feedback may address the prototype, testing results, analysis of testing results, design decisions, iteration, usability, constraints, or other project-specific engineering issues. Vague praise or presentation comments do not count as strong design evaluation.
        Scores above 2 require synthesis that combines multiple evaluator comments into broader conclusions, trends, patterns, or actionable takeaways.
        Merely listing evaluator comments, paraphrasing comments individually, or providing general reflections should not score above 2.
        Scores above 2 require true synthesis of external evaluation.

        True synthesis means:
        - combining multiple evaluator comments into broader conclusions
        - identifying trends, patterns, agreements, disagreements, or recurring themes
        - explaining meaningful takeaways from the external evaluation

        The following should normally score no higher than 2:
        - merely listing evaluator comments
        - paraphrasing comments one-by-one
        - generic summaries
        - isolated reflections without integration
        - copied feedback with little analysis
        - brief statements that feedback was “helpful”

        Score conservatively when:
        - evaluator roles are unclear
        - field expert qualifications are not demonstrated
        - feedback is vague, generic, or disconnected from design work
        - evaluation addresses only presentation rather than the design or process
        - synthesis is missing, sparse, copied, or merely list-like

        Do not reward:
        - formatting alone
        - blank template sections
        - evaluator names without documented feedback
        - undocumented claims that evaluation occurred
        - generic praise without project-specific evaluation
        - repeated evaluator comments without student synthesis

        Verbose documents may contain many names, comments, or screenshots. Do not equate quantity of text with evaluation quality.
        Do not equate document length, number of comments, or quantity of evaluator discussion with quality of synthesis.
        
        Score using the rubric scale 0–5 for J1, J2, and J3.

        If the document contains no meaningful evaluator documentation, external evaluation, or synthesis, scores of 0 across multiple subelements may be appropriate.

        Return only valid JSON in exactly this format:

        {{
        "evaluation_sources": [
            "short evaluator/source phrase",
            "short evaluator/source phrase",
            "short evaluator/source phrase"
        ],
        "J1": {{"score": X, "rationale": "..."}},
        "J2": {{"score": X, "rationale": "..."}},
        "J3": {{"score": X, "rationale": "..."}},
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
            response_dict[f"J{i}"] = int(response_dict.get(f"J{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1,4):
            response_dict[f"J{i}_api"] = int(response_dict[f"J{i}"])

        scores = [response_dict[f"J{i}_api"] for i in range(1,4)]
        response_dict["element_score_api"] = sum(scores) / len(scores)

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 4):
            assert f"J{i}" in response_dict, f"Missing J{i}"
            assert f"J{i}_rationale" in response_dict, f"Missing J{i}_rationale"

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
                k not in response_dict for k in [f"J{i}" for i in range(1, 4)]
            )

            # Ensure J scores are integers
            for i in range(1, 4):
                row[f"J{i}"] = int(row.get(f"J{i}", 0))

            print(
                filename,
                [row[f"J{i}_api"] for i in range(1, 4)],
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

    scores = [f"J{i}" for i in range(1, 4)]
    api_scores = [f"J{i}_api" for i in range(1, 4)]  # 👈 

    flags = [f"J{i}_flag" for i in range(1, 4)]
    rationales = [f"J{i}_rationale" for i in range(1, 4)]

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
            print("J key value:", i, type(response_dict.get(f"J{i}")), response_dict.get(f"J{i}"))
            row[f"J{i}"] = int(response_dict.get(f"J{i}", 0))
            row[f"J{i}_rationale"] = response_dict.get(f"J{i}_rationale", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        # --- Attach API scores ---
        for i in range(1, 4):
            row[f"J{i}_api"] = int(
                response_dict.get(f"J{i}_api", response_dict.get(f"J{i}", 0))
            )

        # --- Attach element API score ---
        row["element_score_api"] = float(
            response_dict.get("element_score_api", 0)
        )

        print("Narrative in row:", row.get("narrative_feedback"))

        print(
                filename,
                [row[f"J{i}_api"] for i in range(1, 4)],
                row["element_score_api"]
            )

        results.append(row)

    # ✅ return is OUTSIDE the loop, INSIDE the function
    return pd.DataFrame(results)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.0", "v1.5"], default="v1.5",
                        help="Which blended model logic to apply (default: v1.5)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
