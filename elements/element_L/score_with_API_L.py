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
# ELEMENT L RULE ENGINE
# =========================

def apply_element_l_rules(response_dict, blended_model):
    # Normalize API scores into temporary L1/L2
    for i in range(1, 3):
        response_dict[f"L{i}"] = int(
            response_dict.get(f"L{i}", response_dict.get(f"L{i}_api", 0))
        )

    # Always preserve API scores
    for i in range(1, 3):
        response_dict[f"L{i}_api"] = int(response_dict.get(f"L{i}_api", response_dict[f"L{i}"]))

    # Legacy: no rule engine, rule = API
    if blended_model == "v1.5":
        for i in range(1, 3):
            response_dict[f"L{i}_rule"] = int(response_dict[f"L{i}_api"])
        response_dict["L1_flag"] = ""
        response_dict["L2_flag"] = ""

    # Current: apply rule caps
    elif blended_model == "v1.6":
        valid_count_raw = response_dict.get("valid_project_recommendations_count", None)

        if valid_count_raw is None or str(valid_count_raw).strip() == "" or str(valid_count_raw).lower() == "nan":
            # CSV fallback: estimate from L1/L2 API scores when recommendation-count metadata is unavailable
            if response_dict["L1"] > 0 or response_dict["L2"] > 0:
                valid_count = 3
            else:
                valid_count = 0
        else:
            valid_count = int(float(valid_count_raw))

        # ---- L1/L2 caps based on valid recommendation count ----
        if valid_count == 0:
            response_dict["L1"] = 0
            response_dict["L2"] = 0
            response_dict["L1_flag"] = "cap: no valid project recommendations"
            response_dict["L2_flag"] = "cap: no valid project recommendations"

        elif valid_count == 1:
            response_dict["L1"] = min(response_dict["L1"], 2)
            response_dict["L2"] = min(response_dict["L2"], 1)
            response_dict["L1_flag"] = "cap: only one valid project recommendation"
            response_dict["L2_flag"] = "cap: limited implementation basis"

        elif valid_count == 2:
            response_dict["L1"] = min(response_dict["L1"], 3)
            response_dict["L1_flag"] = "cap: limited number of valid recommendations"
            response_dict["L2_flag"] = ""

        else:
            response_dict["L1_flag"] = ""
            response_dict["L2_flag"] = ""

        impl_text = str(response_dict.get("valid_project_recommendations", "")).lower()

        implementation_markers = [
            "material", "materials",
            "dimension", "dimensions",
            "measurement", "measurements",
            "sensor", "wire", "wires",
            "wall", "support", "bracket",
            "testing", "test",
            "prototype",
            "recalculate", "calculate",
            "construction", "construct",
            "fasten", "attach", "secure",
            "replace", "coating",
            "method", "procedure",
            "wood", "metal", "plastic",
            "tape", "hook", "hooks"
        ]

        impl_hits = sum(1 for m in implementation_markers if m in impl_text)

        if valid_count > 0:
            if response_dict["L2"] >= 5 and impl_hits < 2:
                response_dict["L2"] = 3
                response_dict["L2_flag"] = (
                    response_dict.get("L2_flag") or "cap: insufficient implementation detail"
                )

            if response_dict["L2"] >= 3 and impl_hits == 0:
                response_dict["L2"] = 2
                response_dict["L2_flag"] = (
                    response_dict.get("L2_flag") or "cap: vague implementation planning"
                )

        for i in range(1, 3):
            response_dict[f"L{i}_rule"] = int(response_dict[f"L{i}"])

    else:
        raise ValueError(f"Unsupported blended_model version: {blended_model}")

    response_dict["element_score_api"] = (
        response_dict["L1_api"] + response_dict["L2_api"]
    ) / 2

    response_dict["element_score_raw"] = response_dict["element_score_api"]

    response_dict["element_score_rule"] = (
        response_dict["L1_rule"] + response_dict["L2_rule"]
    ) / 2

    # Remove ambiguous bare scores
    for i in range(1, 3):
        response_dict.pop(f"L{i}", None)

    return response_dict

# =========================
# GPT MODEL CONFIGURATION
# =========================

GPT_MODEL_LEGACY = "gpt-4.1-mini"
GPT_MODEL_CURRENT = "gpt-4.1-mini"   

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element L rubric consistently and professionally.

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
- Discuss the quality and specificity of the project improvement recommendations (L1).
- Explain how effectively the student justifies why the recommendations are needed.
- Evaluate the quality, detail, and practicality of the implementation plans for proposed improvements (L2).
- Identify strengths and weaknesses in the student’s recommendations and implementation planning.
- Provide 2–4 specific, actionable recommendations for improving recommendation quality, rationale, specificity, or implementation detail.
- Be professional, readable, and student-facing.
- Avoid mentioning scoring mechanics, AI, calibration, OCR, or internal evaluation processes.
- Use natural paragraph breaks for readability.

The narrative should reward thoughtful, project-specific recommendations and detailed implementation planning while identifying vague, unsupported, reflective-only, or overly general recommendations.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 170 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "MyDesign_Element_L_Scoring_Rubric.txt"

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
    if blended_version == "v1.5":
        return GPT_MODEL_LEGACY
    elif blended_version == "v1.6":
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
    if blended_model == "v1.5":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring a MyDesign Element L engineering recommendations document using Blended Model v1.5.

        Element L evaluates:
        - L1: quality and specificity of project improvement recommendations
        - L2: detailed implementation plans for those recommendations

        The document may contain OCR errors, screenshots, formatting corruption, incomplete tables, or PDF artifacts. Focus on recovering meaning rather than penalizing formatting issues.

        FIRST:
        Identify all explicit recommendations for improving the project.

        Recommendations may involve:
        - design changes
        - material changes
        - testing improvements
        - construction improvements
        - usability improvements
        - manufacturing improvements
        - process improvements directly tied to the project

        SECOND:
        Determine whether the recommendations are:
        - project-specific
        - detailed
        - supported by rationale
        - supported by testing data, stakeholder feedback, or new research

        THIRD:
        Evaluate whether the student explains HOW the recommendations could actually be implemented.

        Implementation detail may include:
        - materials
        - dimensions
        - placement
        - testing methods
        - construction changes
        - engineering strategies
        - procedural steps

        Apply the following scoring guidance:

        L1:
        - Specific, project-level recommendations with rationale score highest.
        - Vague or generic recommendations score low.
        - Recommendations focused primarily on teamwork or group behavior cannot score above 3.

        L2:
        - High scores require actionable implementation detail aligned to the recommendations.
        - General future ideas without implementation detail should score low.
        - If implementation lacks concrete project-level detail, L2 should normally score 0–1.

        Do not award points for:
        - endorsements of the project
        - statements like “I recommend this project”
        - reflective writing without future improvements
        - generic positivity
        - headings alone
        - template filler
        - recommendations unrelated to the actual project

        If no explicit project-improvement recommendations appear, L1=0 and L2=0.

        Strong scores require:
        - multiple project-specific recommendations
        - clear rationale for improvements
        - evidence-based or thoroughly justified recommendations
        - actionable implementation planning
        - engineering-level detail

        Return only valid JSON in exactly this format:

        {{
        "identified_recommendations": [
            "short recommendation phrase",
            "short recommendation phrase",
            "short recommendation phrase"
        ],
        "L1": {{"score": X, "rationale": "..."}},
        "L2": {{"score": X, "rationale": "..."}},
        "narrative_feedback": ""
        }}
        """
    elif blended_model == "v1.6":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring a MyDesign Element L engineering recommendations document using Blended Model v1.6.

        Element L evaluates:
        - L1: quality and specificity of project improvement recommendations
        - L2: implementation planning for those recommendations

        Score strictly based on explicit evidence in the document.

        FIRST:
        Identify explicit recommendations for improving the actual engineering project.

        SECOND:
        Determine whether the recommendations are:
        - project-specific
        - actionable
        - justified
        - supported by rationale, testing, stakeholder feedback, or engineering reasoning

        THIRD:
        Evaluate whether the student provides concrete implementation planning aligned to the recommendations.

        Apply the following guidance:

       L1:

        Only count explicit engineering-design improvement recommendations.

        A valid recommendation must:
        - propose a specific improvement to the actual project
        AND
        - describe what should be changed, added, removed, or improved

        The following DO NOT count as valid recommendations:
        - reflections about the project
        - statements about learning
        - comments about teamwork
        - general future hopes
        - vague statements that the project could improve
        - statements that the project was successful or unsuccessful
        - recommendations unrelated to the engineering design itself

        Examples that DO NOT count:
        - “we learned a lot”
        - “we would do better next time”
        - “the project could be improved”
        - “we should work harder”
        - “we would spend more time”
        - “the project was successful”
        - “we recommend this project”

        Examples that DO count:
        - “increase wheel diameter to improve stability”
        - “replace cardboard supports with aluminum brackets”
        - “add waterproof coating to reduce moisture damage”
        - “relocate the sensor closer to the intake for more accurate readings”

        Documents containing only vague or general future-oriented discussion should normally score 0–1 for L1.

        L2:
        Scores above 2 require clear implementation detail.

        Implementation detail should normally include:
        - specific materials
        - construction methods
        - measurements or dimensions
        - testing procedures
        - engineering processes
        - step-by-step actions
        - technical modification procedures

        General future intentions do NOT justify high L2 scores.

        Statements such as:
        - “we would redesign it”
        - “we would improve the project”
        - “we would use better materials”
        - “we would test more”
        without concrete implementation detail should normally score 0–2.
        
        Do not reward:
        - template language
        - headings alone
        - generic reflection
        - storytelling
        - unsupported claims
        - recommendations unrelated to the project itself

        Prioritize engineering specificity, justification, and actionable implementation detail over writing polish or document length.

        If no explicit project-improvement recommendations appear, score L1=0 and L2=0.

        Do not assume recommendations are present unless explicit project-improvement actions are clearly described.

        Before scoring, separate recommendations into:
        - valid_project_recommendations: explicit project/design improvements only
        - non_counting_recommendations: reflection, teamwork, vague hopes, endorsements, or generic statements

        Only valid_project_recommendations may support L1 or L2 scores.

        Return only valid JSON in exactly this format:

        {{
        "identified_recommendations": [
            "short recommendation phrase",
            "short recommendation phrase",
            "short recommendation phrase"
        ],
        "valid_project_recommendations": [
            "specific engineering-design improvement only",
            "specific engineering-design improvement only"
        ],
        "non_counting_recommendations": [
            "reflection/teamwork/generic statement",
            "reflection/teamwork/generic statement"
        ],
        "L1": {{"score": X, "rationale": "..."}},
        "L2": {{"score": X, "rationale": "..."}},
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



        if isinstance(response_dict.get("identified_recommendations"), list):
            response_dict["identified_recommendations"] = "; ".join(response_dict["identified_recommendations"])

        valid_recs_raw = response_dict.get("valid_project_recommendations", [])
        non_counting_raw = response_dict.get("non_counting_recommendations", [])

        print("DEBUG valid_recs_raw:", valid_recs_raw)
        print("DEBUG non_counting_raw:", non_counting_raw)
        print("DEBUG identified:", response_dict.get("identified_recommendations"))

        if isinstance(valid_recs_raw, list):
            response_dict["valid_project_recommendations_count"] = len(valid_recs_raw)
            response_dict["valid_project_recommendations"] = "; ".join(valid_recs_raw)
        else:
            response_dict["valid_project_recommendations_count"] = 0

        if isinstance(non_counting_raw, list):
            response_dict["non_counting_recommendations"] = "; ".join(non_counting_raw)

        response_dict["truncation_detected"] = 0

        for i in range(1,3):
            response_dict[f"L{i}"] = int(response_dict.get(f"L{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1,3):
            response_dict[f"L{i}_api"] = int(response_dict[f"L{i}"])

        # =====================================================
        # Current L rule engine: recommendation and implementation caps
        # =====================================================
        response_dict = apply_element_l_rules(response_dict, blended_model)

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 3):
            assert f"L{i}_api" in response_dict, f"Missing L{i}_api"
            assert f"L{i}_rule" in response_dict, f"Missing L{i}_rule"
            assert f"L{i}_rationale" in response_dict, f"Missing L{i}_rationale"

        print("DEBUG FINAL KEYS:", response_dict.keys())
        print("DEBUG FINAL valid_project_recommendations:", response_dict.get("valid_project_recommendations"))
        print("DEBUG FINAL valid_project_recommendations_count:", response_dict.get("valid_project_recommendations_count"))

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
                k not in response_dict for k in [f"L{i}_api" for i in range(1, 3)]
            ) or any(
                k not in response_dict for k in [f"L{i}_rule" for i in range(1, 3)]
            )

            # Ensure L scores are integers
            for i in range(1, 3):
                row[f"L{i}_api"] = int(row.get(f"L{i}_api", 0))
                row[f"L{i}_rule"] = int(row.get(f"L{i}_rule", 0))

            print(
                filename,
                [row[f"L{i}_api"] for i in range(1, 3)],
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

    api_scores = [f"L{i}_api" for i in range(1, 3)]
    rule_scores = [f"L{i}_rule" for i in range(1, 3)]

    flags = [f"L{i}_flag" for i in range(1, 3)]
    rationales = [f"L{i}_rationale" for i in range(1, 3)]

    extras = [
        "identified_recommendations",
        "valid_project_recommendations",
        "valid_project_recommendations_count",
        "non_counting_recommendations",
        "truncation_detected",
        "element_score_api",
        "element_score_rule",
        "element_score_raw",
        "element_score_final",
        "narrative_feedback"
    ]

    raw_aliases = [f"L{i}_raw" for i in range(1, 3)]
    final_scores = [f"L{i}_final" for i in range(1, 3)]

    print("COLUMNS BEFORE REORDER:", output_df.columns.tolist())

    ordered_columns = core + api_scores + rule_scores + raw_aliases + final_scores + flags + rationales + extras

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
        
        for i in range(1, 3):
            if not response_dict or response_dict.get("truncation_detected") == 1:
                raise ValueError(f"response_dict is None for {filename}")
            print("L key value:", i, type(response_dict.get(f"L{i}")), response_dict.get(f"L{i}"))
            row[f"L{i}_api"] = int(response_dict.get(f"L{i}_api", 0))
            row[f"L{i}_rule"] = int(response_dict.get(f"L{i}_rule", 0))
            row[f"L{i}_rationale"] = response_dict.get(f"L{i}_rationale", "")

        row["identified_recommendations"] = response_dict.get("identified_recommendations", "")
        row["valid_project_recommendations"] = response_dict.get("valid_project_recommendations", "")
        row["valid_project_recommendations_count"] = response_dict.get("valid_project_recommendations_count", "")
        row["non_counting_recommendations"] = response_dict.get("non_counting_recommendations", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        # --- Attach API scores ---
        for i in range(1, 3):
            row[f"L{i}_api"] = int(
                response_dict.get(f"L{i}_api", response_dict.get(f"L{i}", 0))
            )

        # --- Attach element API score ---
        row["element_score_api"] = float(
            response_dict.get("element_score_api", 0)
        )
        row["element_score_rule"] = float(response_dict.get("element_score_rule", 0))
        row["element_score_raw"] = row["element_score_api"]

        print("Narrative in row:", row.get("narrative_feedback"))

        print(
                filename,
                [row[f"L{i}_api"] for i in range(1, 3)],
                row["element_score_api"]
            )

        results.append(row)

    # ✅ return is OUTSIDE the loop, INSIDE the function
    return pd.DataFrame(results)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.5", "v1.6"], default="v1.6",
                        help="Which blended model logic to apply (default: v1.6)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
