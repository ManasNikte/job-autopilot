"""Windows-only behavior, checked by simulating the platform."""
import importlib
import subprocess
import sys


def test_no_console_window_for_child_programs_under_pythonw(monkeypatch):
    """Under pythonw.exe (the Task Scheduler setup), claude.exe / git.exe must start without a terminal window."""
    seen = {}

    class FakePopen:
        def __init__(self, *args, **kwargs):
            seen.update(kwargs)

    import app
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "stdout", None)                         # pythonw.exe has no console
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)
    importlib.reload(app)
    try:
        subprocess.Popen(["claude", "-p", "/usage"])
        assert seen.get("creationflags") == 0x08000000
        seen.clear()
        subprocess.Popen(["chrome"], creationflags=0x00000010)        # an explicit choice is kept
        assert seen["creationflags"] == 0x00000010
    finally:
        monkeypatch.undo()
        importlib.reload(app)
    assert subprocess.Popen.__name__ == "Popen"                      # macOS/Linux untouched


def test_console_runs_are_left_alone(monkeypatch):
    """Started from a terminal (python.exe), children share that console: nothing to hide."""
    import app
    real = subprocess.Popen
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(sys, "executable", r"C:\job-autopilot\.venv\Scripts\python.exe")
    importlib.reload(app)
    try:
        assert subprocess.Popen is real
    finally:
        monkeypatch.undo()
        importlib.reload(app)
