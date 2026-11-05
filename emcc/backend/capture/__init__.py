"""Per-device capture: everything a device says, in hourly files per device.

`DeviceCapture` (writer.py) is the only entry point the rest of EMCC uses.
`levels.py` decides what each log level keeps; `files.py` is the crash-safe
file machinery ported from PyGUI.
"""

from .files import HAVE_XLSX
from .levels import DEFAULT_LEVEL, ERROR, INFO, WARNING, LogLevel
from .writer import DeviceCapture

__all__ = [
    "DEFAULT_LEVEL",
    "DeviceCapture",
    "ERROR",
    "HAVE_XLSX",
    "INFO",
    "LogLevel",
    "WARNING",
]
