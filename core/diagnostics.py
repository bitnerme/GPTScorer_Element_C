def interpret_diagnostics(api_drift, final_drift, golden_fail, production_drift):

    if not api_drift and not final_drift and not golden_fail:
        return "System stable."

    if api_drift and not final_drift:
        return "GPT model behavior appears to have changed."

    if final_drift and not api_drift:
        return "Calibration layer may have changed."

    if golden_fail and not api_drift and not final_drift:
        return "Expert scoring baseline may have changed."

    if production_drift and not api_drift and not final_drift and not golden_fail:
        return "Student response distribution appears to have changed."

    return "Multiple diagnostic signals detected. Investigate scoring pipeline or increase sample size."
