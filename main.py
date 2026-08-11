import os

os.environ["OPENCV_VIDEOIO_MSMF_ENABLE_HW_TRANSFORMS"] = "0"

import cv2
import ctypes
from ctypes import wintypes
import math
import time
import configparser
from pathlib import Path
import sys

from PySide6.QtWidgets import QApplication, QSystemTrayIcon, QMenu
from PySide6.QtGui import QIcon
from PySide6.QtCore import QTimer


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


user32.GetMonitorInfoW.argtypes = [wintypes.HMONITOR, ctypes.POINTER(MONITORINFOEXW)]
user32.GetMonitorInfoW.restype = wintypes.BOOL

GammaRamp = (ctypes.c_ushort * 256) * 3


# Load settings

BASE_DIR = Path(__file__).resolve().parent
config_path = BASE_DIR / "settings.ini"

config = configparser.ConfigParser()
success = config.read(config_path)

if not success:
    print("Error reading settings.ini file. Make sure it isn't deleted or modified!")
    exit()

neutral_light = float(config["settings"]["neutral_light_value"])
min_screen = float(config["settings"]["min_brightness"])
max_screen = float(config["settings"]["max_brightness"])
dead_zone = float(config["settings"]["dead_zone"])
brighten_speed = float(config["settings"]["brighten_speed"])
darken_speed = float(config["settings"]["darken_speed"])
brightness_offset = float(config["settings"]["brightness_offset"])

max_change_per_second = float(config["settings"]["max_brightness_change_per_second"])

current_brightness = 1
last_time = time.perf_counter()


# Find connected monitors

monitors = []


@MonitorEnumProc
def monitor_callback(hMonitor, hdcMonitor, lprcMonitor, dwData):
    monitor_info = MONITORINFOEXW()
    monitor_info.cbSize = ctypes.sizeof(MONITORINFOEXW)

    success = user32.GetMonitorInfoW(hMonitor, ctypes.byref(monitor_info))

    if success:
        monitors.append({"handle": hMonitor, "info": monitor_info})

    return True


success = user32.EnumDisplayMonitors(None, None, monitor_callback, 0)

if not success:
    print("Failed to enumerate monitors")
    exit()


# Initialize monitor HDCs and gamma ramps

for monitor in monitors:
    hdc = gdi32.CreateDCW("DISPLAY", monitor["info"].szDevice, None, None)

    if not hdc:
        print("Failed to create HDC for " + monitor["info"].szDevice)
        exit()

    monitor["hdc"] = hdc

    gamma_ramp = GammaRamp()
    og_gamma_ramp = GammaRamp()

    success = gdi32.GetDeviceGammaRamp(monitor["hdc"], og_gamma_ramp)

    if not success:
        print(
            "Populating original gamma ramp for " + monitor["info"].szDevice + " failed"
        )
        exit()

    for channel in range(3):
        for i in range(256):
            gamma_ramp[channel][i] = og_gamma_ramp[channel][i]

    monitor["ogRamp"] = og_gamma_ramp
    monitor["ramp"] = gamma_ramp

    print("Found monitor:", monitor["info"].szDevice)


# Start webcam

vc = cv2.VideoCapture(0)

if vc.isOpened():
    rval, frame = vc.read()
else:
    rval = False


# Create system tray application

app = QApplication(sys.argv)
app.setQuitOnLastWindowClosed(False)

tray = QSystemTrayIcon()
tray.setIcon(QIcon(str(BASE_DIR / "icon.ico")))

menu = QMenu()

auto_brightness_enabled = menu.addAction("Enabled")
auto_brightness_enabled.setCheckable(True)
auto_brightness_enabled.setChecked(True)

exit_action = menu.addAction("Exit")

tray.setContextMenu(menu)
tray.show()


# Update brightness


def update_brightness():
    global frame
    global rval
    global last_time
    global current_brightness

    if not rval:
        return

    hsv_frame = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)

    now = time.perf_counter()
    dt = now - last_time
    last_time = now

    dt = min(dt, 0.05)

    average = round(hsv_frame[:, :, 2].mean())

    normalized = average / neutral_light

    target_brightness = math.log1p(normalized) / math.log(2)

    target_brightness += brightness_offset

    target_brightness = max(min_screen, min(max_screen, target_brightness))

    difference = target_brightness - current_brightness

    if abs(difference) < dead_zone:
        target_brightness = current_brightness
        difference = 0

    if difference > 0:
        speed = brighten_speed
    else:
        speed = darken_speed

    alpha = 1 - math.exp(-speed * dt)

    new_brightness = current_brightness + difference * alpha

    max_change = max_change_per_second * dt

    change = new_brightness - current_brightness

    if change > max_change:
        change = max_change
    elif change < -max_change:
        change = -max_change

    current_brightness += change

    for monitor in monitors:
        for channel in range(3):
            for i in range(256):
                value = int(monitor["ogRamp"][channel][i] * current_brightness)

                value = min(65535, max(0, value))

                monitor["ramp"][channel][i] = value

        success = gdi32.SetDeviceGammaRamp(monitor["hdc"], monitor["ramp"])

        if not success:
            print("SetDeviceGammaRamp failed for " + monitor["info"].szDevice)

    rval, frame = vc.read()


# Program shutdown


def quit_program():
    timer.stop()

    identity_ramp = GammaRamp()

    for channel in range(3):
        for i in range(256):
            identity_ramp[channel][i] = i * 257

    for monitor in monitors:
        gdi32.SetDeviceGammaRamp(monitor["hdc"], identity_ramp)

        gdi32.DeleteDC(monitor["hdc"])

    vc.release()

    tray.hide()
    app.quit()


# Qt connections

timer = QTimer()
timer.timeout.connect(update_brightness)
timer.start(100)

exit_action.triggered.connect(quit_program)


# Start Qt event loop

sys.exit(app.exec())
