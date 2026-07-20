from uuid import uuid4
from typing import Dict, Any

jobs: Dict[str, Dict[str, Any]] = {}

def create_job(total: int, element: str, subelement_count: int):
    job_id = str(uuid4())

    jobs[job_id] = {
        "completed": 0,
        "total": total,
        "status": "running",
        "phase": "Scoring",          # New
        "element": element,
        "subelement_count": subelement_count,
        "results": None,
    }

    print("JOB CREATED:", job_id)

    return job_id

def update_progress(job_id: str, completed: int):
    jobs[job_id]["completed"] = completed

def complete_job(job_id: str, results):
    jobs[job_id]["status"] = "done"
    jobs[job_id]["results"] = results
    print("COMPLETE_JOB STATE:", jobs[job_id])

def get_job(job_id: str):
    return jobs.get(job_id)

def update_total(job_id: str, total: int):
    jobs[job_id]["total"] = total

def update_phase(
    job_id: str,
    phase: str,
    completed: int | None = None,
    total: int | None = None,
) -> None:
    if job_id not in jobs:
        return

    jobs[job_id]["phase"] = phase

    if completed is not None:
        jobs[job_id]["completed"] = completed

    if total is not None:
        jobs[job_id]["total"] = total