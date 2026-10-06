"""Linux launcher for Claude Code preview_start.

Delegates to the matching ``scripts/<name>.sh`` shell script. This fork is
Linux-only, so the Windows ``.bat``/PowerShell branches have been removed.
Can be invoked with any Python 3.x.

Usage:
    python scripts/launch.py <script_name> [args...]

Examples:
    python scripts/launch.py run_desktop_app
    python scripts/launch.py run_desktop_app --voice-debug
    python scripts/launch.py run_evals
"""

import os
import subprocess
import sys


def main():
    if len(sys.argv) < 2:
        print("Usage: python scripts/launch.py <script_name> [args...]")
        sys.exit(1)

    script_name = sys.argv[1]
    extra_args = sys.argv[2:]

    project_root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    scripts_dir = os.path.join(project_root, "scripts")

    script_path = os.path.join(scripts_dir, f"{script_name}.sh")
    if not os.path.isfile(script_path):
        print(f"ERROR: {script_path} not found")
        sys.exit(1)
    result = subprocess.run(
        ["bash", script_path] + extra_args,
        cwd=project_root,
    )

    sys.exit(result.returncode)


if __name__ == "__main__":
    main()
