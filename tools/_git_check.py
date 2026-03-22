import subprocess
from pathlib import Path

root = Path(__file__).resolve().parents[1]
out = root / "_git_out.txt"
r = subprocess.run(
    ["git", "-C", str(root), "status", "--porcelain"],
    capture_output=True,
    text=True,
)
out.write_text(r.stdout + "\n---stderr---\n" + r.stderr + "\nexit=" + str(r.returncode))
