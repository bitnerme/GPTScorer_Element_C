from uuid import uuid4
from typing import Dict, Any

jobs: Dict[str, Dict[str, Any]] = {}

def create_job(total: int):
    job_id = str(uuid4())
    jobs[job_id] = {
        "completed": 0,
        "total": total,
        "status": "running",
        "results": None,
    }
    return job_id

def update_progress(job_id: str, completed: int):
    jobs[job_id]["completed"] = completed

def complete_job(job_id: str, results):
    jobs[job_id]["status"] = "done"
    jobs[job_id]["results"] = results
    #print("COMPLETE_JOB STATE:", jobs[job_id])

def get_job(job_id: str):
    return jobs.get(job_id)