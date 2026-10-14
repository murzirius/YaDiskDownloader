import os
import sys
from pathlib import Path


def frozen():
    return bool(getattr(sys, "frozen", False))


def state_folder():
    return Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local")) / "YaDiskDownloader"


def program_folder():
    return Path(sys.executable).resolve().parent if frozen() else Path(__file__).resolve().parents[1]


def destination_folder():
    if frozen():
        return Path.home() / "Downloads" / "YaDiskDownloader"
    return Path(__file__).resolve().parents[1] / "downloads"
