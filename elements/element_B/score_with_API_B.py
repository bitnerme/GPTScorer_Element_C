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
import shutil
import pytesseract
from pdf2image import convert_from_path
import win32com.client
import pythoncom
from scripts.shared.utils import extract_text_with_fallback
import traceback

print("LOADED SCORE_WITH_API_B FROM:", __file__)

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

GPT_MODEL_LEGACY = "gpt-4.1-mini"
GPT_MODEL_CURRENT = "gpt-4.1-mini"   

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element B rubric consistently and professionally.

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
- Be written in 2–3 clear paragraphs (not a single block of text).
- Clearly explain overall strengths in identifying and analyzing prior solutions (B1–B2).
- Clearly explain weaknesses in the evaluation of advantages/disadvantages and justification of solutions.
- Reference criterion numbers when helpful (e.g., B1, B2).
- Provide 2–4 specific, actionable recommendations for improvement.
- Be professional, readable, and student-facing.
- Avoid mentioning scoring mechanics, AI, or calibration.
- Use natural paragraph breaks to improve readability.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 170 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "Current Element B Rubric.txt"

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
    if blended_version == "v1.2":
        return GPT_MODEL_LEGACY
    elif blended_version == "v1.4b":
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

    print("ENTERED ELEMENT B API SCORING FUNCTION")

    # Choose prompt based on model
    if blended_model == "v1.2":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Scoring Guidance (Legacy v1.2 Interpretation):

        Apply expert-scorer judgment when evaluating the document.

        - Accept reasonable implicit evidence when the context clearly supports the student's understanding.
        - Narrative explanation may substitute for formal structure when the design thinking is clear.
        - Partial completion of a design step should receive partial credit rather than the lowest score.

        ELEMENT B INTERPRETATION GUIDANCE:

        B1 – Design Concepts
        - Evaluate the extent to which the student generates and explores possible design ideas.
        - Multiple distinct ideas or alternatives support higher scores.
        - A single idea without exploration of alternatives should not score above level 2.

        B2 – Solution Justification
        - Evaluate how clearly the student explains why their chosen design is appropriate.
        - Comparisons between ideas strengthen justification.
        - Vague explanations without reasoning should receive lower scores.

        SCORING SCALE GUIDANCE:

        0 → No evidence of the criterion  
        1 → Minimal or very weak evidence  
        2 → Limited evidence; partially addressed  
        3 → Moderate evidence but incomplete  
        4 → Strong and well-developed evidence  
        5 → Exceptional evidence; explicit and comprehensive  

        B1 evaluates identification of prior solutions.
        B2 evaluates the quality of comparison and evaluation.

        These must be scored independently.
        A student can score high on B1 but low on B2 if analysis is weak.

        If only one prior solution is identified, B1 must be ≤ 2 regardless of description quality.

        A high score (4–5) for B2 requires:
        - explicit comparison between multiple solutions
        - clearly explained advantages AND disadvantages
        - reasoning about why one solution is better than others

         A score of 3 may be given if:
        - multiple solutions are discussed
        - some advantages/disadvantages are identified
        - but comparison or reasoning is incomplete

        Scores of 0–2 should be used when:
        - only one solution is discussed, OR
        - pros/cons are minimal, vague, or missing

        Use the full 0–5 scale.
        Do not default to middle scores unless evidence clearly supports it.

        Student Document:
        \"\"\"{content}\"\"\"

        IMPORTANT OUTPUT CONSTRAINT:
        
        Element B contains exactly four criteria: B1, B2.

        Do not generate any additional fields such as b3, b4, B5 or B6.
        Only return scores for B1, B2.

        If the submission references diagrams, sketches, tables, or other visual elements that are not fully visible in the extracted text, do not assume the work is missing.

        However, only assign credit when the surrounding text clearly describes what the visual shows, how it is used, or what conclusions are drawn from it.

        Do not award credit based solely on vague references such as "see diagram" without explanation.

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

        Return exactly one valid JSON object matching the required structure.

        Do not close the outer JSON object until after the scoring_hints array.

        Do not include markdown fences, commentary, trailing commas, or any text before or after the JSON object.

        Return only valid JSON in exactly this format:

        {{
        "B1": {{"score": X, "rationale": "."}},
        "B2": {{"score": X, "rationale": "."}},
        "narrative_feedback": "170–220 word narrative written in 2–3 paragraphs explaining strengths, weaknesses, and specific improvement suggestions.",
        "scoring_hints": [
            "...",
            "...",
            "..."
        ]}}
        """
    elif blended_model == "v1.4b":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are evaluating this submission using the Element B rubric.

        GENERAL SCORING PRINCIPLES:

        - Base scores strictly on explicit evidence in the student document.
        - Do not assume missing elements are present.
        - Do not infer design reasoning or testing plans unless clearly demonstrated.
        - When evidence is partial or underdeveloped, award partial credit rather than full credit.
        - Scores of 4 or 5 require clear, explicit, and well-developed evidence.

        CRITICAL DIFFERENTIATION REQUIREMENT:

        - Distinguish clearly between weak, adequate, strong, and exceptional submissions.
        - If a submission minimally satisfies the rubric, score at level 2.
        - If it adequately meets the rubric but lacks depth or completeness, score at level 3.
        - Reserve level 4 for clearly strong and well-developed work.
        - Reserve level 5 only for comprehensive, explicit, and clearly superior work.
        - Avoid clustering most submissions at level 3.

        ELEMENT B INTERPRETATION GUIDANCE:

        B1 – Design Concepts
        - Higher scores require evidence of multiple design ideas or alternative solutions.
        - A single design idea without exploration of alternatives should not score above level 2.

        B2 – Solution Justification
        - Strong scores require clear reasoning explaining why the chosen design is preferable.
        - Comparisons between alternative ideas strengthen justification.

        SCORING SCALE GUIDANCE:

        0 → No meaningful evidence  
        1 → Minimal evidence  
        2 → Limited evidence with major gaps  
        3 → Adequate but incomplete  
        4 → Strong and well-developed  
        5 → Exceptional and comprehensive  

        B1 evaluates identification of prior solutions.
        B2 evaluates the quality of comparison and evaluation.

        These must be scored independently.
        A student can score high on B1 but low on B2 if analysis is weak.

        If only one prior solution is identified, B1 must be ≤ 2 regardless of description quality.

        A high score (4–5) for B2 requires:
        - explicit comparison between multiple solutions
        - clearly explained advantages AND disadvantages
        - reasoning about why one solution is better than others

        A high score (4–5) for B2 requires:
        - clear comparison between two or more solutions
        - explicit explanation of advantages AND disadvantages
        - reasoning about which solution is better and why

        A score of 3 may be given if:
        - multiple solutions are discussed
        - some advantages/disadvantages are identified
        - but comparison or reasoning is incomplete

        Scores of 0–2 should be used when:
        - only one solution is discussed, OR
        - pros/cons are minimal, vague, or missing

        Use the full 0–5 scale.
        Do not default to middle scores unless evidence clearly supports it.

        IMPORTANT OUTPUT CONSTRAINT:

        Element B contains exactly four criteria: B1, B2.

        Do not generate any additional fields such as B3, B4, B5 or B6.
        Only return scores for B1, B2.

        If the submission references diagrams, sketches, tables, or other visual elements that are not fully visible in the extracted text, do not assume the work is missing.

        However, only assign credit when the surrounding text clearly describes what the visual shows, how it is used, or what conclusions are drawn from it.

        Do not award credit based solely on vague references such as "see diagram" without explanation.

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

        Return exactly one valid JSON object matching the required structure.

        Do not close the outer JSON object until after the scoring_hints array.

        Do not include markdown fences, commentary, trailing commas, or any text before or after the JSON object.


        Return only valid JSON in exactly this format:

        {{
        "B1": {{"score": X, "rationale": "."}},
        "B2": {{"score": X, "rationale": "."}},
        "narrative_feedback": "170–220 word narrative written in 2–3 paragraphs explaining strengths, weaknesses, and specific improvement suggestions.",
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

    try:
        response = openai.ChatCompletion.create(
            model=gpt_model,
            messages=messages,
            temperature=0,
            top_p=1,
            max_tokens=1500
        )
    except Exception:
        print("FAILED FILE:", filename)
        raise

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
    def repair_common_json_errors(text: str) -> str:
        return re.sub(
            r'("narrative_feedback"\s*:\s*"(?:\\.|[^"\\])*")\s*}\s*,\s*'
            r'("scoring_hints"\s*:)',
            r'\1,\n  \2',
            text,
            flags=re.DOTALL
        )

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
            cleaned = clean_json_string(cleaned)
            cleaned = repair_common_json_errors(cleaned)

            response_dict = json5.loads(cleaned)

            print("B RESPONSE DICT KEYS:", response_dict.keys())
            print("B RAW MODEL HINTS:", response_dict.get("scoring_hints"))

        except Exception as e:
            print(f"⚠️ First parse failed for {filename}: {e}")
            print("⚠️ Retrying once...")

            retry_messages = messages + [
                {
                    "role": "user",
                    "content": (
                        "Your previous response was not valid JSON and could not be parsed. "
                        "Return the complete response again as exactly one valid JSON object matching the required schema. "
                        "Do not include markdown, code fences, commentary, or any text outside the JSON object. "
                        "Do not close the outer JSON object until after the scoring_hints array. "
                    )
                }
            ]

            retry_response = openai.ChatCompletion.create(
                model=gpt_model,
                messages=retry_messages,
                temperature=0,
                top_p=1,
                max_tokens=1500
            )

            try:
                retry_str = retry_response.choices[0].message.content.strip()

                print("RETRY RAW RESPONSE:")
                print(retry_str)

                print("RETRY CLEANED RESPONSE:")
                print(clean_json_string(retry_str))


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

                cleaned = clean_json_string(retry_str)
                cleaned = repair_common_json_errors(cleaned)

                print("RETRY CLEANED RESPONSE:")
                print(cleaned)

                response_dict = json5.loads(cleaned)
                print("✅ Retry succeeded")

            except Exception as retry_error:
                print(f"❌ Retry failed for {filename}: {retry_error}")
                return {
                    "truncation_detected": 1,
                    "incomplete_response": True
                }

        required_paths = [
            ("B1", "score"),
            ("B1", "rationale"),
            ("B2", "score"),
            ("B2", "rationale"),
        ]

        for section, field in required_paths:
            if section not in response_dict or field not in response_dict[section]:
                raise ValueError(f"Response omitted {section}.{field}")

        if "narrative_feedback" not in response_dict:
            raise ValueError("Response omitted narrative_feedback")

        if "scoring_hints" not in response_dict:
            raise ValueError("Response omitted scoring_hints")

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

        for i in range(1,3):
            response_dict[f"B{i}"] = int(response_dict.get(f"B{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1,3):
            response_dict[f"B{i}_api"] = int(response_dict[f"B{i}"])

        scores = [response_dict[f"B{i}_api"] for i in range(1,3)]
        response_dict["element_score_api"] = sum(scores) / len(scores)

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 3):
            assert f"B{i}" in response_dict, f"Missing B{i}"
            assert f"B{i}_rationale" in response_dict, f"Missing B{i}_rationale"

        return response_dict

    except json.JSONDecodeError as e:
        print(f"❌ JSON parse failed for {filename}")
        print(response_str)
        return {
            "truncation_detected": 1
        }

    print("B RETURN VALUE TYPE:", type(result))
    print(
        "B RETURN KEYS:",
        result.keys() if isinstance(result, dict) else "NOT A DICT",
    )
    print(
        "B RETURN HINTS:",
        result.get("scoring_hints") if isinstance(result, dict) else None,
    )

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
        k not in response_dict for k in [f"B{i}" for i in range(1, 3)]
    )

    # Ensure integer scores
    for i in range(1, 3):
        row[f"B{i}"] = int(row.get(f"B{i}", 0))

    print("scoring_hints after storing row:",row.get("scoring_hints"))

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

            # Add API scores safely
            for i in range(1, 3):
                row[f"B{i}_api"] = int(
                    response_dict.get(f"B{i}_api", row.get(f"B{i}", 0))
                )

            row["element_score_api"] = float(
                response_dict.get("element_score_api", 0)
            )

            print(
                filename,
                [row[f"B{i}_api"] for i in range(1, 3)],
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

    scores = [f"B{i}" for i in range(1, 3)]
    api_scores = [f"B{i}_api" for i in range(1, 3)]  # 👈 NEW

    flags = [f"B{i}_flag" for i in range(1, 3)]
    rationales = [f"B{i}_rationale" for i in range(1, 3)]

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

        row = build_result_row(filename, text, response_dict, idx)

        for i in range(1, 3):
            row[f"B{i}_rationale"] = response_dict.get(f"B{i}_rationale", "")

        # NOTE: No rule processing currently implemented for Element B.
        # Placeholder for future blended model rule logic.
        # Rules (if added) must modify B{i}, not B{i}_api.

        # Ensure API scores are present in output
        for i in range(1, 3):
            row[f"B{i}_api"] = int(
                response_dict.get(f"B{i}_api", row.get(f"B{i}", 0))
            )

        row["element_score_api"] = float(
            response_dict.get("element_score_api", 0)
        )

        print("ROW KEYS:", row.keys())

        print(
            filename,
            [response_dict.get(f"B{i}_api", 0) for i in range(1, 3)],
            response_dict.get("element_score_api", 0)
        )

        # Preserve teacher-review scoring hints from the model response
        raw_hints = response_dict.get("scoring_hints", [])

        if isinstance(raw_hints, list):
            row["scoring_hints"] = [
                str(hint).strip()
                for hint in raw_hints
                if hint is not None and str(hint).strip()
            ]
        elif isinstance(raw_hints, str) and raw_hints.strip():
            row["scoring_hints"] = [raw_hints.strip()]
        else:
            row["scoring_hints"] = []

        results.append(row)

    # ✅ return is OUTSIDE the loop, INSIDE the function
    print("Afer normalizing and appending row to results and before return:",[r.get("scoring_hints") for r in results])
    return pd.DataFrame(results)



if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.2", "v1.4b"], default="v1.4b",
                        help="Which blended model logic to apply (default: v1.4b)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
