def interpret_diagnostics(api_drift, final_drift, golden_fail, production_drift):

    if not api_drift and not final_drift and not golden_fail:
        return "System stable."

    if api_drift and final_drift:
        return (
            "Upstream model behavior has shifted.\n"
            "Changes in GPT scoring are propagating through calibration.\n"
            "Check model version, prompt stability, or input document changes."
        )

    if api_drift and not final_drift:
        return "GPT model behavior appears to have changed, but calibration is compensating."

    if final_drift and not api_drift:
        return "Calibration layer may have changed or is misaligned with current scoring."

    if golden_fail and not api_drift and not final_drift:
        return "Expert scoring baseline may have changed."

    if production_drift and not api_drift and not final_drift and not golden_fail:
        return "Student documents appear to have changed."

    return "Multiple diagnostic signals detected. Investigate scoring pipeline or increase sample size."