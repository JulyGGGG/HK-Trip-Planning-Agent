"""Record native Slurm evidence before entering the course PRoot runtime."""
import json
import os
import re
import subprocess
from pathlib import Path

job_id = os.environ["SLURM_JOB_ID"]
config = json.loads(Path("model-config.json").read_text(encoding="utf-8"))
ids = [job_id, str(config.get("service_job_id", ""))]
ids = [x for x in ids if re.fullmatch(r"\d+", x)]
result = {"workflow_job_id": job_id, "workflow_step_id": os.environ.get("SLURM_STEP_ID"), "service_job_id": config.get("service_job_id"), "scope": "current workflow and configured model service only"}
try:
    proc = subprocess.run(["sacct", "-n", "-P", "-j", ",".join(ids), "--format=JobIDRaw,State,ExitCode,Start,End"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
    result["sacct"] = {"exit_code": proc.returncode, "stdout": proc.stdout[:12000], "stderr": proc.stderr[:2000]}
except (OSError, subprocess.TimeoutExpired) as exc:
    result["sacct"] = {"available": False, "reason": str(exc)}
Path("native-diagnostics.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
