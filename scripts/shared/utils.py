import time
import openai
import os
import backoff
from datetime import datetime
import pythoncom
import win32com.client
from pdf2image import convert_from_path
import pytesseract
import docx
import subprocess
import traceback
import json
from pathlib import Path

from pathlib import Path
import json


def check_drift(current_metrics, baseline_file):

    baseline_path = Path(baseline_file)

    if not baseline_path.exists():
        return {
            "status": "NO BASELINE",
            "message": f"Baseline file not found: {baseline_file}"
        }

    with open(baseline_path, "r") as f:
        baseline = json.load(f)

    report = {}
    failures = []

    # -----------------------------
    # Compute metric differences
    # -----------------------------

    api_mean_diff = abs(current_metrics["api_mean"] - baseline["api_mean"])
    api_std_diff = abs(current_metrics["api_std"] - baseline["api_std"])

    final_mean_diff = abs(current_metrics["final_mean"] - baseline["final_mean"])
    final_std_diff = abs(current_metrics["final_std"] - baseline["final_std"])

    report["api_mean_diff"] = api_mean_diff
    report["api_std_diff"] = api_std_diff
    report["final_mean_diff"] = final_mean_diff
    report["final_std_diff"] = final_std_diff

    # -----------------------------
    # Thresholds
    # -----------------------------

    API_MEAN_THRESHOLD = 0.25
    API_STD_THRESHOLD = 0.20
    FINAL_MEAN_THRESHOLD = 0.25
    FINAL_STD_THRESHOLD = 0.20

    if api_mean_diff > API_MEAN_THRESHOLD:
        failures.append("api_mean_shift")

    if api_std_diff > API_STD_THRESHOLD:
        failures.append("api_std_shift")

    if final_mean_diff > FINAL_MEAN_THRESHOLD:
        failures.append("final_mean_shift")

    if final_std_diff > FINAL_STD_THRESHOLD:
        failures.append("final_std_shift")

    sample_warning = None

    baseline_n = baseline.get("sample_size")
    current_n = current_metrics.get("sample_size")

    if baseline_n and current_n:
        if current_n < baseline_n * 0.5:
            sample_warning = (
                f"WARNING: current sample size ({current_n}) is much smaller "
                f"than baseline ({baseline_n}). Drift metrics may be unstable."
            )

    # -----------------------------
    # Final result
    # -----------------------------

    if failures:
        status = "DRIFT DETECTED"
    else:
        status = "PASS"

    return {
        "status": status,
        "sample_size_current": current_n,
        "sample_size_baseline": baseline_n,
        "sample_warning": sample_warning,
        "failures": failures,
        "report": report
    }

# --- Extract text from .docx/doc or .pdf ---
def extract_text_from_file(filepath):
    print("\n=== ENTER extract_text_from_file ===")
    print("FILEPATH:", repr(filepath))
    print("CALL STACK:")
    traceback.print_stack(limit=5)

    print("UTILS FILE LOCATION:", __file__)

    ext = os.path.splitext(filepath)[1]

    ext = ext.lower()

    if ext in (".docx", ".doc"):
        print("→ DOCX BRANCH")
        return extract_text_from_docx(filepath)
    elif ext == ".pdf":
        print("→ PDF BRANCH")
        return extract_text_from_pdf(filepath)
    else:
        raise ValueError(f"Unsupported file format: {repr(ext)}")

def run_ocr(filepath):
    pythoncom.CoInitialize()

    try:
        extracted_text = ""

        ext = os.path.splitext(filepath)[1].lower()
        abs_path = os.path.abspath(filepath)
        base, _ = os.path.splitext(abs_path)
        pdf_path = base + "_temp_ocr.pdf"

        # Kill any orphaned Word processes (optional but stabilizing)
        subprocess.run(
            ["taskkill", "/f", "/im", "WINWORD.EXE"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL
        )

        word = win32com.client.DispatchEx("Word.Application")
        word.Visible = False
        word.DisplayAlerts = 0

        doc = word.Documents.Open(abs_path, ReadOnly=True)
        doc.SaveAs2(pdf_path, FileFormat=17)
        doc.Close(False)

        word.Quit()
        del word

        time.sleep(0.5)  # allow COM to release

        pages = convert_from_path(
            pdf_path,
            poppler_path=r"C:\poppler\poppler-25.12.0\Library\bin"
        )

        for page in pages:
            extracted_text += pytesseract.image_to_string(page) + "\n"

        if os.path.exists(pdf_path):
            os.remove(pdf_path)

        return extracted_text

    except Exception:
        print("\nFULL TRACEBACK inside run_ocr:")
        traceback.print_exc()
        raise

    finally:
        pythoncom.CoUninitialize()

def extract_text_with_fallback(filepath, min_length=50):
    text = extract_text_from_file(filepath)
    text = text.strip()

    # Trigger OCR if empty or suspiciously short
    if len(text) < min_length:
        print(f"OCR triggered for {filepath}")
        text = run_ocr(filepath).strip()

    return text

def extract_text_from_docx(filepath):
    abs_path = os.path.abspath(filepath)
    doc = docx.Document(abs_path)
    return "\n".join(
        [para.text for para in doc.paragraphs if para.text.strip()]
    )

def extract_text_from_pdf(filepath):
    try:
        from pdfminer.high_level import extract_text
    except ImportError:
        raise ImportError("pdfminer.six must be installed to extract text from PDFs")

    text = extract_text(filepath)
    return text

# --- GPT call with retries ---
@backoff.on_exception(backoff.expo, (openai.error.OpenAIError, Exception), max_tries=5)
def call_gpt_with_backoff(prompt, system="You are a helpful assistant.",
                          model_order=None, temperature=0.0, max_tokens=3500):

    if model_order is None:
        raise ValueError("model_order must be explicitly provided by caller")

    last_exception = None

    for current_model in model_order:
        try:
            print(f"🔄 Trying model: {current_model}")

            response = openai.ChatCompletion.create(
                model=current_model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": prompt}
                ],
                temperature=temperature,
                max_tokens=max_tokens,
            )

            content = response['choices'][0]['message']['content']
            return content

        except Exception as e:
            print(f"⚠️ Error with model {current_model}: {e}")
            last_exception = e
            time.sleep(1)

    print("❌ All GPT models failed.")
    raise last_exception


