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

# =========================
# GPT MODEL CONFIGURATION
# =========================

GPT_MODEL_LEGACY = "gpt-4.1-mini"
GPT_MODEL_CURRENT = "gpt-4.1-mini"   

SYSTEM_PROMPT = """You are a rigorous engineering design evaluator applying the Element G rubric consistently and professionally.

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
- Explain strengths in prototype construction and explanation (G1).
- Explain strengths or gaps in testability and measurable data generation (G2 and G3).
- Address how well the design supports evaluation of attributes.
- Discuss any missing or weak justification for non-testable attributes (G4).
- Provide 2–4 specific, actionable recommendations for improving prototype quality, testability, or explanation.
- Be professional, readable, and student-facing.
- Avoid mentioning scoring mechanics, AI, or calibration.
- Use natural paragraph breaks for readability.

Include this in the JSON output as:
"narrative_feedback": string

The narrative_feedback must be between 170 and 220 words.
"""

RUBRIC_PATH = Path(__file__).resolve().parent / "MyDesign_Element_G_Scoring_Rubric.txt"

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
    if blended_version == "v1.41":
        return GPT_MODEL_LEGACY
    elif blended_version == "v1.42":
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
    if blended_model == "v1.41":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring MyDesign Element G.

        Element G evaluates how well the student:
        - Explains their prototype iteration
        - Constructs a prototype capable of generating objective, testable data
        - Enables testing or modeling of design attributes
        - Justifies attributes that cannot be directly tested

        GENERAL SCORING PRINCIPLES:

        - Base scores strictly on explicit evidence in the student document.
        - Do not assume missing elements are present.
        - Do not infer testing capability unless clearly supported by the description.
        - Award partial credit when work is present but incomplete.
        - Scores of 4–5 require clear, specific, and well-supported evidence.
        - Use the full 0–5 scale when justified.

        CRITICAL CONTEXT RULE:

        If design requirements are not explicitly listed, infer the likely intended requirements from the design description and context. Use these inferred requirements when evaluating whether the prototype can generate objective, measurable data.

        Educational Prototype Interpretation Guidance:

        Students may demonstrate prototype quality and testability through descriptions, images, construction details, diagrams, material choices, or partial demonstrations rather than formal engineering documentation.

        Reasonable inferences may be made when the document clearly suggests that a prototype can generate measurable or observable data, even if every testing procedure is not fully specified.

        Do not require professional-level engineering validation for scores of 4–5.

        Strong educational prototypes may still receive high scores when:
        - the construction is clearly explained,
        - the intended testing approach is understandable,
        - measurable attributes are identifiable,
        - and the prototype appears realistically capable of evaluating the design.

        ELEMENT G INTERPRETATION GUIDANCE:

        G1 – Final Prototype Iteration Explanation
        - Evaluate how clearly the final prototype is described.
        - Strong responses explain what was built, how it was built, and key components.
        - Vague or minimal descriptions should receive lower scores.

        G2 – Construction for Testability
        - Evaluate whether the prototype is constructed so objective data can be collected on design requirements.
        - Higher scores require clear evidence that the prototype can generate measurable results.
        - If testability is unclear or not supported, limit scores.

        G3 – Testing or Modeling of Attributes
        - Evaluate whether specific attributes of the design can be tested or modeled.
        - Higher scores require multiple clearly identifiable attributes with testable or measurable outcomes.
        - General statements without specific attributes should receive lower scores.

        G4 – Justification of Non-Testable Attributes
        - Evaluate whether the student justifies attributes that cannot be tested and explains why expert review is needed.
        - Strong responses provide clear reasoning tied to the design.
        - Missing or weak justification should receive low scores.

        SCORING SCALE GUIDANCE:

        0 → No meaningful evidence  
        1 → Minimal or very weak evidence  
        2 → Limited evidence with major gaps  
        3 → Adequate but incomplete  
        4 → Strong and well-developed  
        5 → Exceptional, explicit, and comprehensive
      
        CRITICAL EVIDENCE RULES:

        - Do not give credit for vague statements such as “this can be tested” without explanation.
        - Do not assume testability unless the mechanism is described.
        - Statements about future testing without detail should not score above 2.
        - References to diagrams or images only count if the surrounding text explains them.

        VISUAL / PROTOTYPE CONSTRAINTS:

        - If the prototype is only described abstractly (no construction detail), limit G1.
        - If no physical or buildable prototype is evident, limit G2 and G3.
        - If diagrams are referenced but not explained, do not award full credit.

        Prototype Evidence Interpretation:

        Substantial prototype construction evidence may support higher G2 and G3 scores even when formal testing procedures are not fully described.

        Evidence of prototype quality may include:
        - detailed construction descriptions
        - physical assembly steps
        - labeled diagrams or CAD connected to a build
        - material selection reasoning
        - measurements or dimensions
        - visible mechanisms or functional components
        - evidence that the prototype could realistically produce observable or measurable results

        Do not require formal engineering test protocols for high G2 or G3 scores when the prototype clearly appears capable of evaluating important design attributes.

        IMPORTANT OUTPUT CONSTRAINT:

        Return scores only for G1, G2, G3, and G4.

        Return only valid JSON in exactly this format:

        {{
        "G1": {{"score": X, "rationale": "..." }},
        "G2": {{"score": X, "rationale": "..." }},
        "G3": {{"score": X, "rationale": "..." }},
        "G4": {{"score": X, "rationale": "..." }},
        "narrative_feedback": ""
        }}
        """
    elif blended_model == "v1.42":
        prompt = f"""
        Rubric:
        {RUBRIC_TEXT}

        Student Document:
        \"\"\"{content}\"\"\"

        You are scoring MyDesign Element G.

        Element G evaluates how well the student:
        - Explains their prototype iteration
        - Constructs a prototype capable of generating objective, testable data
        - Enables testing or modeling of design attributes
        - Justifies attributes that cannot be directly tested

        GENERAL SCORING PRINCIPLES:

        - Base scores strictly on explicit evidence in the student document.
        - Do not assume missing elements are present.
        - Do not infer testing capability unless clearly supported by the description.
        - Award partial credit when work is present but incomplete.
        - Scores of 4–5 require clear, specific, and well-supported evidence.
        - Use the full 0–5 scale when justified.

        CRITICAL CONTEXT RULE:

        If design requirements are not explicitly listed, infer the likely intended requirements from the design description and context. Use these inferred requirements when evaluating whether the prototype can generate objective, measurable data.

        Educational Prototype Interpretation Guidance:

        Students may demonstrate prototype quality and testability through descriptions, images, construction details, diagrams, material choices, or partial demonstrations rather than formal engineering documentation.

        Reasonable inferences may be made when the document clearly suggests that a prototype can generate measurable or observable data, even if every testing procedure is not fully specified.

        Do not require professional-level engineering validation for scores of 4–5.

        Strong educational prototypes may still receive high scores when:
        - the construction is clearly explained,
        - the intended testing approach is understandable,
        - measurable attributes are identifiable,
        - and the prototype appears realistically capable of evaluating the design.

        ELEMENT G INTERPRETATION GUIDANCE:

        G1 – Final Prototype Iteration Explanation
        - Evaluate how clearly the final prototype is described.
        - Strong responses explain what was built, how it was built, and key components.
        - Vague or minimal descriptions should receive lower scores.

        G2 – Construction for Testability
        - Evaluate whether the prototype is constructed so objective data can be collected on design requirements.
        - Higher scores require clear evidence that the prototype can generate measurable results.
        - If testability is unclear or not supported, limit scores.

        G3 – Testing or Modeling of Attributes
        - Evaluate whether specific attributes of the design can be tested or modeled.
        - Higher scores require multiple clearly identifiable attributes with testable or measurable outcomes.
        - General statements without specific attributes should receive lower scores.

        G4 – Justification of Non-Testable Attributes
        - High G4 scores should only be assigned when the student explicitly identifies attributes that cannot be directly tested or modeled AND explains why expert review, judgment, or alternative evaluation is necessary.
        - General discussion of limitations, improvements, or future work does not alone justify high G4 scores.
        - Do not infer non-testable attribute justification unless it is clearly stated or directly explained in the document.

        SCORING SCALE GUIDANCE:

        0 → No meaningful evidence  
        1 → Minimal or very weak evidence  
        2 → Limited evidence with major gaps  
        3 → Adequate but incomplete  
        4 → Strong and well-developed  
        5 → Exceptional, explicit, and comprehensive  

        CRITICAL EVIDENCE RULES:

        - Do not give credit for vague statements such as “this can be tested” without explanation.
        - Do not assume testability unless the mechanism is described.
        - Statements about future testing without detail should not score above 2.
        - References to diagrams or images only count if the surrounding text explains them.

        VISUAL / PROTOTYPE CONSTRAINTS:

        - If the prototype is only described abstractly (no construction detail), limit G1.
        - If no physical or buildable prototype is evident, limit G2 and G3.
        - If diagrams are referenced but not explained, do not award full credit.

        Prototype Evidence Interpretation:

        Substantial prototype construction evidence may support higher G2 and G3 scores even when formal testing procedures are not fully described.

        Evidence of prototype quality may include:
        - detailed construction descriptions
        - physical assembly steps
        - labeled diagrams or CAD connected to a build
        - material selection reasoning
        - measurements or dimensions
        - visible mechanisms or functional components
        - evidence that the prototype could realistically produce observable or measurable results
                
        Educational portfolios may demonstrate testability through substantial prototype construction evidence when the document also explains how the prototype will intentionally generate measurable, observable, or comparable data related to specific design requirements or attributes.

        Prototype sophistication alone does not justify high G2 or G3 scores.

        High G2 and G3 scores require explicit explanation of:
        - what will be tested or evaluated,
        - how evaluation will occur,
        - what observations, comparisons, or measurements will be collected,
        - or how the prototype demonstrates performance relative to requirements.

        Potential testability or implied functionality alone is insufficient for high G2 or G3 scores.

        Do not require formal engineering test protocols for high G2 or G3 scores when the student clearly explains a realistic educational testing or evaluation approach.
        
        IMPORTANT OUTPUT CONSTRAINT:

        Return scores only for G1, G2, G3, and G4.

        Return only valid JSON in exactly this format:

        {{
        "G1": {{"score": X, "rationale": "..." }},
        "G2": {{"score": X, "rationale": "..." }},
        "G3": {{"score": X, "rationale": "..." }},
        "G4": {{"score": X, "rationale": "..." }},
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

        for i in range(1,5):
            response_dict[f"G{i}"] = int(response_dict.get(f"G{i}",0))

        # =====================================================
        # 🔎 Capture PURE API scores before rule engine logic
        # =====================================================

        # --- Preserve pure API subscores BEFORE rule engine ---
        for i in range(1,5):
            response_dict[f"G{i}_api"] = int(response_dict[f"G{i}"])

        scores = [response_dict[f"G{i}_api"] for i in range(1,5)]
        response_dict["element_score_api"] = sum(scores) / len(scores)

        # --- Validate expected fields (FAIL LOUDLY) ---
        for i in range(1, 5):
            assert f"G{i}" in response_dict, f"Missing G{i}"
            assert f"G{i}_rationale" in response_dict, f"Missing G{i}_rationale"

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
                k not in response_dict for k in [f"G{i}" for i in range(1, 5)]
            )

            # Ensure G scores are integers
            for i in range(1, 5):
                row[f"G{i}"] = int(row.get(f"G{i}", 0))

            print(
                filename,
                [row[f"G{i}_api"] for i in range(1, 5)],
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

    scores = [f"G{i}" for i in range(1, 5)]
    api_scores = [f"G{i}_api" for i in range(1, 5)]  # 👈 NEW

    flags = [f"G{i}_flag" for i in range(1, 5)]
    rationales = [f"G{i}_rationale" for i in range(1, 5)]

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
        
        for i in range(1, 5):
            if response_dict is None:
                raise ValueError(f"response_dict is None for {filename}")
            print("G key value:", i, type(response_dict.get(f"G{i}")), response_dict.get(f"G{i}"))
            row[f"G{i}"] = int(response_dict.get(f"G{i}", 0))
            row[f"G{i}_rationale"] = response_dict.get(f"G{i}_rationale", "")

        row["narrative_feedback"] = response_dict.get("narrative_feedback", "")

        # --- Attach API scores ---
        for i in range(1, 5):
            row[f"G{i}_api"] = int(
                response_dict.get(f"G{i}_api", response_dict.get(f"G{i}", 0))
            )

        # --- Attach element API score ---
        row["element_score_api"] = float(
            response_dict.get("element_score_api", 0)
        )

        print("Narrative in row:", row.get("narrative_feedback"))

        print(
                filename,
                [row[f"G{i}_api"] for i in range(1, 5)],
                row["element_score_api"]
            )

        results.append(row)

    # ✅ return is OUTSIDE the loop, INSIDE the function
    return pd.DataFrame(results)

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Score documents using GPT API and blended model logic.")
    parser.add_argument("--folder", required=True, help="Folder containing documents to score")
    parser.add_argument("--output", required=True, help="Path to output CSV file")
    parser.add_argument("--blended-model", choices=["v1.41", "v1.42"], default="v1.42",
                        help="Which blended model logic to apply (default: v1.42)")
    args = parser.parse_args()


    main(
        folder_path=args.folder,
        output_path=args.output,
        blended_version=args.blended_model
    )
