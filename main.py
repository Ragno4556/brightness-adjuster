import os

os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

import configparser
import ctypes
from ctypes import wintypes
import math
from pathlib import Path
import sys
import time

import cv2
from PySide6.QtCore import QTimer
from PySide6.QtGui import QIcon
from PySide6.QtWidgets import QApplication, QMenu, QSystemTrayIcon


# Win32 setup

gdi32 = ctypes.windll.gdi32
user32 = ctypes.WinDLL("user32")

gdi32.CreateDCW.argtypes = [
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    wintypes.LPCWSTR,
    ctypes.c_void_p,
]
gdi32.CreateDCW.restype = wintypes.HDC

gdi32.GetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.GetDeviceGammaRamp.restype = wintypes.BOOL

gdi32.SetDeviceGammaRamp.argtypes = [wintypes.HDC, ctypes.c_void_p]
gdi32.SetDeviceGammaRamp.restype = wintypes.BOOL

gdi32.DeleteDC.argtypes = [wintypes.HDC]
gdi32.DeleteDC.restype = wintypes.BOOL


# Monitor types

MonitorEnumProc = ctypes.WINFUNCTYPE(
    wintypes.BOOL,
    wintypes.HMONITOR,
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    wintypes.LPARAM,
)

user32.EnumDisplayMonitors.argtypes = [
    wintypes.HDC,
    ctypes.POINTER(wintypes.RECT),
    MonitorEnumProc,
    wintypes.LPARAM,
]
user32.EnumDisplayMonitors.restype = wintypes.BOOL


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
        ("szDevice", wintypes.WCHAR * 32),
    ]


user32.GetMonitorInfoW.argtypes = [
    wintypes.HMONITOR,
    ctypes.POINTER(MONITORINFOEXW),
]
user32.GetMonitorInfoW.restype = wintypes.BOOL

GammaRamp = (ctypes.c_ushort * 256) * 3


# Paths

# Bundled resources such as icon.ico live beside the source file when running
# normally, or in PyInstaller's temporary bundle directory when frozen.
if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
    RESOURCE_DIR = Path(sys._MEIPASS)
else:
    RESOURCE_DIR = Path(__file__).resolve().parent

# User-editable settings belong in the persistent roaming AppData folder.
appdata_value = os.environ.get("APPDATA")
if appdata_value:
    SETTINGS_DIR = Path(appdata_value) / "AutoBrightness"
else:
    SETTINGS_DIR = Path.home() / "AppData" / "Roaming" / "AutoBrightness"

SETTINGS_DIR.mkdir(parents=True, exist_ok=True)
config_path = SETTINGS_DIR / "settings.ini"


# Load settings

DEFAULT_SETTINGS = {
    "neutral_light_value": "105",
    "min_brightness": "0.35",
    "max_brightness": "1.25",
    "dead_zone": "0.01",
    "brighten_speed": "2.5",
    "darken_speed": "1.0",
    "brightness_offset": "0.08",
    "max_brightness_change_per_second": "0.4",
}

config = configparser.ConfigParser()
config.read(config_path, encoding="utf-8")

if not config.has_section("settings"):
    config.add_section("settings")

settings_changed = False
for key, value in DEFAULT_SETTINGS.items():
    if key not in config["settings"]:
        config["settings"][key] = value
        settings_changed = True

if not config_path.exists() or settings_changed:
    with config_path.open("w", encoding="utf-8") as config_file:
        config.write(config_file)

neutral_light = config.getfloat("settings", "neutral_light_value")
min_screen = config.getfloat("settings", "min_brightness")
max_screen = config.getfloat("settings", "max_brightness")
dead_zone = config.getfloat("settings", "dead_zone")
brighten_speed = config.getfloat("settings", "brighten_speed")
darken_speed = config.getfloat("settings", "darken_speed")
brightness_offset = config.getfloat("settings", "brightness_offset")
max_change_per_second = config.getfloat(
    "settings", "max_brightness_change_per_second"
)

current_brightness = 1.0
last_time = time.perf_counter()


# Find connected monitors

monitors = []


@MonitorEnumProc
def monitor_callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
    monitor_info = MONITORINFOEXW()
    monitor_info.cbSize = ctypes.sizeof(MONITORINFOEXW)

    if user32.GetMonitorInfoW(hMonitor, ctypes.byref(monitor_info)):
        monitors.append({"handle": hMonitor, "info": monitor_info})

    return True


if not user32.EnumDisplayMonitors(None, None, monitor_callback, 0):
    print("Failed to enumerate monitors")
    sys.exit(1)


# Initialize monitor HDCs and save original gamma ramps

for monitor in monitors:
    hdc = gdi32.CreateDCW("DISPLAY", monitor["info"].szDevice, None, None)

    if not hdc:
        print("Failed to create HDC for " + monitor["info"].szDevice)
        sys.exit(1)

    monitor["hdc"] = hdc
    gamma_ramp = GammaRamp()
    original_gamma_ramp = GammaRamp()

    if not gdi32.GetDeviceGammaRamp(hdc, original_gamma_ramp):
        print(
            "Populating original gamma ramp for "
            + monitor["info"].szDevice
            + " failed"
        )
        sys.exit(1)

    for channel in range(3):
        for i in range(256):
            gamma_ramp[channel][i] = original_gamma_ramp[channel][i]

    monitor["ogRamp"] = original_gamma_ramp
    monitor["ramp"] = gamma_ramp
    print("Found monitor:", monitor["info"].szDevice)


# Start webcam

vc = cv2.VideoCapture(0)

if vc.isOpened():
    rval, frame = vc.read()
else:
    rval = False
    frame = None


# Create system tray application

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

tray = QSystemTrayIcon()
tray.setIcon(QIcon(str(RESOURCE_DIR / "icon.ico")))

menu = QMenu()
exit_action = menu.addAction("Exit")

tray.setContextMenu(menu)
tray.show()


# Update brightness

def update_brightness():
    global frame, rval, last_time, current_brightness

    if not rval or frame is None:
        rval, frame = vc.read()
        return

    hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    now = time.perf_counter()
    dt = min(now - last_time, 0.05)
    last_time = now

    average = round(hsv_frame[:, :, 2].mean())
    normalized = average / neutral_light
    target_brightness = math.log1p(normalized) / math.log(2)
    target_brightness += brightness_offset
    target_brightness = max(min_screen, min(max_screen, target_brightness))

    difference = target_brightness - current_brightness

    if abs(difference) < dead_zone:
        difference = 0

    speed = brighten_speed if difference > 0 else darken_speed
    alpha = 1 - math.exp(-speed * dt)
    new_brightness = current_brightness + difference * alpha

    max_change = max_change_per_second * dt
    change = max(-max_change, min(max_change, new_brightness - current_brightness))
    current_brightness += change

    for monitor in monitors:
        for channel in range(3):
            for i in range(256):
                value = int(monitor["ogRamp"][channel][i] * current_brightness)
                monitor["ramp"][channel][i] = min(65535, max(0, value))

        if not gdi32.SetDeviceGammaRamp(monitor["hdc"], monitor["ramp"]):
            print("SetDeviceGammaRamp failed for " + monitor["info"].szDevice)

    rval, frame = vc.read()


# Program shutdown

cleaned_up = False


def quit_program():
    global cleaned_up

    if cleaned_up:
        return

    cleaned_up = True
    timer.stop()

    for monitor in monitors:
        if not gdi32.SetDeviceGammaRamp(monitor["hdc"], monitor["ogRamp"]):
            print("Failed to restore gamma ramp for " + monitor["info"].szDevice)

        gdi32.DeleteDC(monitor["hdc"])

    vc.release()
    tray.hide()


# Qt connections

timer = QTimer()
timer.timeout.connect(update_brightness)
timer.start(100)

exit_action.triggered.connect(app.quit)
app.aboutToQuit.connect(quit_program)


# Start Qt event loop

sys.exit(app.exec())
