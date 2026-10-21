"""Job Autopilot."""
import os
import subprocess
import sys

# Windows: when the dashboard runs without a console (pythonw.exe, the Task Scheduler setup), every console
# program it starts (claude.exe, git.exe) would flash a new terminal window. Start them without one.
if sys.platform == "win32" and (sys.stdout is None or os.path.basename(sys.executable).lower() == "pythonw.exe"):
    _Popen = subprocess.Popen

    class _NoConsolePopen(_Popen):
        def __init__(self, *args, **kwargs):
            if not kwargs.get("creationflags"):
                kwargs["creationflags"] = subprocess.CREATE_NO_WINDOW
            super().__init__(*args, **kwargs)

    subprocess.Popen = _NoConsolePopen
