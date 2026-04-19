from __future__ import annotations

import argparse
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

DIRECT_IMPORT_ERRORS: dict[str, str] = {}

try:
    import Quartz as DIRECT_QUARTZ
except Exception as exc:
    DIRECT_QUARTZ = None
    DIRECT_IMPORT_ERRORS["Quartz"] = repr(exc)

try:
    import cv2 as DIRECT_CV2
except Exception as exc:
    DIRECT_CV2 = None
    DIRECT_IMPORT_ERRORS["cv2"] = repr(exc)

try:
    import numpy as DIRECT_NUMPY
except Exception as exc:
    DIRECT_NUMPY = None
    DIRECT_IMPORT_ERRORS["numpy"] = repr(exc)

try:
    from AppKit import NSWorkspace as DIRECT_NSWorkspace
except Exception as exc:
    DIRECT_NSWorkspace = None
    DIRECT_IMPORT_ERRORS["AppKit.NSWorkspace"] = repr(exc)

try:
    from ApplicationServices import (
        AXIsProcessTrusted as DIRECT_AXIsProcessTrusted,
        AXIsProcessTrustedWithOptions as DIRECT_AXIsProcessTrustedWithOptions,
        AXUIElementCopyAttributeValue as DIRECT_AXUIElementCopyAttributeValue,
        AXUIElementCopyElementAtPosition as DIRECT_AXUIElementCopyElementAtPosition,
        AXUIElementCreateApplication as DIRECT_AXUIElementCreateApplication,
        kAXTrustedCheckOptionPrompt as DIRECT_kAXTrustedCheckOptionPrompt,
    )
except Exception as exc:
    DIRECT_AXIsProcessTrusted = None
    DIRECT_AXIsProcessTrustedWithOptions = None
    DIRECT_AXUIElementCopyAttributeValue = None
    DIRECT_AXUIElementCopyElementAtPosition = None
    DIRECT_AXUIElementCreateApplication = None
    DIRECT_kAXTrustedCheckOptionPrompt = None
    DIRECT_IMPORT_ERRORS["ApplicationServices"] = repr(exc)


QUARTZ_PYTHON: str | None = None
ACCESSIBILITY_PYTHON: str | None = None
VISION_PYTHON: str | None = None
APP_ACTIVATION_DELAY_SECONDS = 0.35
COMPOSER_REFocus_DELAY_SECONDS = 0.12
COMPOSER_SETTLE_DELAY_SECONDS = 0.25
COMPOSER_PRIMARY_BOTTOM_OFFSET_PIXELS = 72
COMPOSER_SECONDARY_BOTTOM_OFFSET_PIXELS = 104
EDITABLE_AX_ROLES = ("AXTextArea", "AXTextField", "AXSearchField", "AXComboBox")
HELPER_TIMEOUT_SECONDS = 8
APPLE_SCRIPT_TIMEOUT_SECONDS = 10
KEYBOARD_SETTLE_DELAY_SECONDS = 0.06
KEY_CODE_A = 0
KEY_CODE_V = 9
KEY_CODE_RETURN = 36
KEY_CODE_ESCAPE = 53
ACCESSIBILITY_PROMPTED = False


def request_json(method: str, url: str, payload: dict | None = None, token: str | None = None) -> dict | None:
    body = None if payload is None else json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if token:
        headers["X-Session-Token"] = token
    request = urllib.request.Request(
        url,
        data=body,
        headers=headers,
        method=method,
    )

    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            raw = response.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{method} {url} failed: {exc.code} {body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{method} {url} failed: {exc.reason}") from exc

    if not raw:
        return None
    return json.loads(raw)


def _probe_python(modules: tuple[str, ...]) -> str:
    candidates = [
        sys.executable,
        "/opt/anaconda3/bin/python3",
        "/opt/homebrew/bin/python3",
        "/usr/local/bin/python3",
        "/usr/bin/python3",
    ]

    probe = "\n".join(f"import {module}" for module in modules)
    for candidate in candidates:
        if not candidate or not Path(candidate).exists():
            continue
        result = subprocess.run(
            [candidate, "-c", probe],
            text=True,
            capture_output=True,
            check=False,
        )
        if result.returncode == 0:
            return candidate

    raise RuntimeError(f"No Python interpreter with required modules is available: {', '.join(modules)}.")


def get_quartz_python() -> str:
    global QUARTZ_PYTHON
    if QUARTZ_PYTHON:
        return QUARTZ_PYTHON
    QUARTZ_PYTHON = _probe_python(("Quartz",))
    return QUARTZ_PYTHON


def get_accessibility_python() -> str:
    global ACCESSIBILITY_PYTHON
    if ACCESSIBILITY_PYTHON:
        return ACCESSIBILITY_PYTHON
    ACCESSIBILITY_PYTHON = _probe_python(("AppKit", "ApplicationServices"))
    return ACCESSIBILITY_PYTHON


def get_vision_python() -> str:
    global VISION_PYTHON
    if VISION_PYTHON:
        return VISION_PYTHON
    VISION_PYTHON = _probe_python(("Quartz", "cv2", "numpy", "AppKit", "ApplicationServices"))
    return VISION_PYTHON


def run_helper(python_executable: str, script: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [python_executable, "-c", script, *args],
        text=True,
        capture_output=True,
        check=False,
        timeout=HELPER_TIMEOUT_SECONDS,
    )


def parse_helper_json(result: subprocess.CompletedProcess[str], error_message: str) -> dict:
    if result.returncode != 0:
        detail = result.stderr.strip() or result.stdout.strip()
        if detail:
            raise RuntimeError(f"{error_message}: {detail}")
        raise RuntimeError(error_message)
    return json.loads(result.stdout)


def ensure_line_buffering() -> None:
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if callable(reconfigure):
            reconfigure(line_buffering=True)


def direct_get_attr(element: object, name: str) -> tuple[int, object | None]:
    if DIRECT_AXUIElementCopyAttributeValue is None:
        return (-1, None)
    err, value = DIRECT_AXUIElementCopyAttributeValue(element, name, None)
    return int(err), value


def has_accessibility_access(*, prompt: bool) -> bool:
    if DIRECT_AXIsProcessTrustedWithOptions is not None and DIRECT_kAXTrustedCheckOptionPrompt is not None:
        try:
            options = {DIRECT_kAXTrustedCheckOptionPrompt: bool(prompt)}
            return bool(DIRECT_AXIsProcessTrustedWithOptions(options))
        except Exception:
            pass
    if DIRECT_AXIsProcessTrusted is not None:
        try:
            return bool(DIRECT_AXIsProcessTrusted())
        except Exception:
            pass
    return True


def ensure_accessibility_access(*, prompt: bool) -> None:
    global ACCESSIBILITY_PROMPTED
    request_prompt = prompt and not ACCESSIBILITY_PROMPTED
    trusted = has_accessibility_access(prompt=request_prompt)
    if request_prompt:
        ACCESSIBILITY_PROMPTED = True
    if not trusted:
        raise RuntimeError(
            "PocketMac is missing macOS Accessibility permission. "
            "Enable it for the installed PocketMac app in System Settings > Privacy & Security > Accessibility, "
            "then relaunch the app."
        )


def find_direct_running_app(app_name: str) -> object | None:
    if DIRECT_NSWorkspace is None:
        return None
    apps = DIRECT_NSWorkspace.sharedWorkspace().runningApplications()
    return next((item for item in apps if item.localizedName() == app_name), None)


def direct_main_window_info(app_name: str) -> tuple[dict[str, float], int] | None:
    if DIRECT_QUARTZ is None:
        return None

    windows = DIRECT_QUARTZ.CGWindowListCopyWindowInfo(
        DIRECT_QUARTZ.kCGWindowListOptionOnScreenOnly,
        DIRECT_QUARTZ.kCGNullWindowID,
    )
    candidates: list[tuple[float, dict[str, float], int]] = []
    for window in windows:
        if window.get("kCGWindowOwnerName") != app_name:
            continue
        if window.get("kCGWindowLayer", 1) != 0:
            continue
        bounds = window.get("kCGWindowBounds", {})
        width = float(bounds.get("Width", 0))
        height = float(bounds.get("Height", 0))
        if width <= 0 or height <= 0:
            continue
        candidates.append(
            (
                width * height,
                {key: float(value) for key, value in dict(bounds).items()},
                int(window.get("kCGWindowNumber", 0)),
            )
        )

    if not candidates:
        return None

    _, bounds, window_id = max(candidates, key=lambda item: item[0])
    return bounds, window_id


def parse_axvalue_float_pair(value: object, first_marker: str, second_marker: str) -> tuple[float, float] | None:
    if value is None:
        return None
    text = str(value)
    try:
        first = float(text.split(first_marker)[1].split()[0])
        second = float(text.split(second_marker)[1].split()[0])
    except (IndexError, ValueError):
        return None
    return (first, second)


def find_accessibility_composer_point(app_name: str) -> tuple[float, float] | None:
    try:
        if (
            DIRECT_NSWorkspace is not None
            and DIRECT_AXUIElementCreateApplication is not None
            and DIRECT_AXUIElementCopyAttributeValue is not None
        ):
            app = find_direct_running_app(app_name)
            if app is not None:
                ax_app = DIRECT_AXUIElementCreateApplication(app.processIdentifier())
                _, window = direct_get_attr(ax_app, "AXMainWindow")
                if window is None:
                    _, window = direct_get_attr(ax_app, "AXFocusedWindow")

                if window is not None:
                    queue = [window]
                    seen: set[str] = set()
                    while queue and len(seen) < 400:
                        element = queue.pop(0)
                        key = repr(element)
                        if key in seen:
                            continue
                        seen.add(key)

                        role = direct_get_attr(element, "AXRole")[1]
                        if role in EDITABLE_AX_ROLES:
                            position = parse_axvalue_float_pair(direct_get_attr(element, "AXPosition")[1], "x:", "y:")
                            size = parse_axvalue_float_pair(direct_get_attr(element, "AXSize")[1], "w:", "h:")
                            if position is not None and size is not None:
                                return (position[0] + (size[0] / 2), position[1] + (size[1] / 2))

                        for attr_name in ("AXContents", "AXChildren", "AXChildrenInNavigationOrder"):
                            children = direct_get_attr(element, attr_name)[1] or []
                            for child in list(children)[:50]:
                                queue.append(child)
    except Exception:
        return None

    helper = rf"""
import json
import sys
from AppKit import NSWorkspace
from ApplicationServices import AXUIElementCreateApplication, AXUIElementCopyAttributeValue

app_name = sys.argv[1]
editable_roles = {EDITABLE_AX_ROLES!r}

apps = NSWorkspace.sharedWorkspace().runningApplications()
app = next((item for item in apps if item.localizedName() == app_name), None)
if app is None:
    raise SystemExit(2)

def get_attr(element, name):
    err, value = AXUIElementCopyAttributeValue(element, name, None)
    return err, value

ax_app = AXUIElementCreateApplication(app.processIdentifier())
_, window = get_attr(ax_app, "AXMainWindow")
if window is None:
    _, window = get_attr(ax_app, "AXFocusedWindow")
if window is None:
    raise SystemExit(3)

queue = [window]
seen = set()
while queue and len(seen) < 400:
    element = queue.pop(0)
    key = repr(element)
    if key in seen:
        continue
    seen.add(key)

    role = get_attr(element, "AXRole")[1]
    if role in editable_roles:
        position = get_attr(element, "AXPosition")[1]
        size = get_attr(element, "AXSize")[1]
        if position is not None and size is not None:
            position_value = str(position)
            size_value = str(size)
            pos_x = float(position_value.split("x:")[1].split()[0])
            pos_y = float(position_value.split("y:")[1].split()[0])
            size_w = float(size_value.split("w:")[1].split()[0])
            size_h = float(size_value.split("h:")[1].split()[0])
            print(json.dumps({{"x": pos_x + (size_w / 2), "y": pos_y + (size_h / 2)}}))
            raise SystemExit(0)

    for attr_name in ("AXContents", "AXChildren", "AXChildrenInNavigationOrder"):
        children = get_attr(element, attr_name)[1] or []
        for child in list(children)[:50]:
            queue.append(child)

raise SystemExit(4)
"""
    try:
        result = run_helper(get_accessibility_python(), helper, app_name)
    except (RuntimeError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    return (float(payload["x"]), float(payload["y"]))


def find_visual_composer_point(app_name: str) -> tuple[float, float] | None:
    try:
        if (
            DIRECT_QUARTZ is not None
            and DIRECT_CV2 is not None
            and DIRECT_NUMPY is not None
            and DIRECT_NSWorkspace is not None
            and DIRECT_AXUIElementCreateApplication is not None
            and DIRECT_AXUIElementCopyElementAtPosition is not None
        ):
            main_window = direct_main_window_info(app_name)
            if main_window is not None:
                bounds, window_id = main_window
                image_ref = DIRECT_QUARTZ.CGWindowListCreateImage(
                    DIRECT_QUARTZ.CGRectNull,
                    DIRECT_QUARTZ.kCGWindowListOptionIncludingWindow,
                    window_id,
                    DIRECT_QUARTZ.kCGWindowImageBoundsIgnoreFraming,
                )
                if image_ref is not None:
                    image_width = DIRECT_QUARTZ.CGImageGetWidth(image_ref)
                    image_height = DIRECT_QUARTZ.CGImageGetHeight(image_ref)
                    bytes_per_row = DIRECT_QUARTZ.CGImageGetBytesPerRow(image_ref)
                    provider = DIRECT_QUARTZ.CGImageGetDataProvider(image_ref)
                    raw = DIRECT_NUMPY.frombuffer(
                        DIRECT_QUARTZ.CGDataProviderCopyData(provider),
                        dtype=DIRECT_NUMPY.uint8,
                    )
                    rgba = raw.reshape((image_height, bytes_per_row // 4, 4))[:, :image_width, :]
                    gray = DIRECT_CV2.cvtColor(rgba[:, :, :3], DIRECT_CV2.COLOR_BGR2GRAY)
                    blur = DIRECT_CV2.GaussianBlur(gray, (5, 5), 0)
                    threshold = DIRECT_CV2.adaptiveThreshold(
                        blur,
                        255,
                        DIRECT_CV2.ADAPTIVE_THRESH_GAUSSIAN_C,
                        DIRECT_CV2.THRESH_BINARY,
                        31,
                        7,
                    )
                    contours, _ = DIRECT_CV2.findContours(
                        255 - threshold,
                        DIRECT_CV2.RETR_LIST,
                        DIRECT_CV2.CHAIN_APPROX_SIMPLE,
                    )

                    default_x = image_width * 0.5
                    default_y = (
                        min(bounds["Height"] * 0.9, bounds["Height"] - COMPOSER_PRIMARY_BOTTOM_OFFSET_PIXELS)
                        / bounds["Height"]
                    ) * image_height

                    def candidate_score(rect: tuple[int, int, int, int]) -> float:
                        x, y, width, height = rect
                        center_x = x + (width / 2)
                        center_y = y + (height / 2)
                        width_score = width / image_width
                        center_score = 1.0 - abs(center_x - (image_width * 0.5)) / (image_width * 0.5)
                        default_distance = abs(center_x - default_x) + abs(center_y - default_y)
                        default_score = 1.0 - min(default_distance / (image_width + image_height), 1.0)
                        lower_bias = center_y / image_height
                        return (width_score * 2.4) + (center_score * 1.5) + (default_score * 1.8) + lower_bias

                    candidates: list[tuple[int, int, int, int]] = []
                    containing_default: list[tuple[int, int, int, int]] = []
                    for contour in contours:
                        x, y, width, height = DIRECT_CV2.boundingRect(contour)
                        area = width * height
                        aspect_ratio = width / max(height, 1)
                        if area < image_width * image_height * 0.01:
                            continue
                        if aspect_ratio < 4 or aspect_ratio > 30:
                            continue
                        if width < image_width * 0.2 or width > image_width * 0.92:
                            continue
                        if height < image_height * 0.03 or height > image_height * 0.16:
                            continue
                        center_x = x + (width / 2)
                        center_y = y + (height / 2)
                        if not (image_width * 0.12 <= center_x <= image_width * 0.88):
                            continue
                        if center_y < image_height * 0.4:
                            continue

                        rect = (x, y, width, height)
                        candidates.append(rect)
                        if x <= default_x <= x + width and y <= default_y <= y + height:
                            containing_default.append(rect)

                    best: tuple[int, int, int, int] | None = None
                    if containing_default:
                        best = min(containing_default, key=lambda rect: rect[2] * rect[3])
                    elif candidates:
                        best = max(candidates, key=candidate_score)

                    if best is not None:
                        x, y, width, height = best
                        center_x = bounds["X"] + ((x + (width / 2)) / image_width) * bounds["Width"]
                        center_y = bounds["Y"] + ((y + (height / 2)) / image_height) * bounds["Height"]

                        app = find_direct_running_app(app_name)
                        if app is not None:
                            ax_app = DIRECT_AXUIElementCreateApplication(app.processIdentifier())
                            err, element = DIRECT_AXUIElementCopyElementAtPosition(ax_app, center_x, center_y, None)
                            if err == 0 and element is not None:
                                role = direct_get_attr(element, "AXRole")[1]
                                if role in ("AXScrollArea", "AXTextArea", "AXTextField", None):
                                    return (center_x, center_y)
    except Exception:
        return None

    helper = r"""
import json
import sys

from ApplicationServices import AXUIElementCopyElementAtPosition, AXUIElementCreateApplication, AXUIElementCopyAttributeValue
from AppKit import NSWorkspace
import Quartz
import cv2
import numpy as np

app_name = sys.argv[1]
primary_offset = float(sys.argv[2])

windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
main_window = None
for window in windows:
    if window.get("kCGWindowOwnerName") != app_name:
        continue
    if window.get("kCGWindowLayer", 1) != 0:
        continue
    bounds = window.get("kCGWindowBounds", {})
    if float(bounds.get("Height", 0)) <= 200:
        continue
    main_window = window
    break

if main_window is None:
    raise SystemExit(2)

bounds = main_window["kCGWindowBounds"]
window_id = int(main_window["kCGWindowNumber"])
image_ref = Quartz.CGWindowListCreateImage(
    Quartz.CGRectNull,
    Quartz.kCGWindowListOptionIncludingWindow,
    window_id,
    Quartz.kCGWindowImageBoundsIgnoreFraming,
)
if image_ref is None:
    raise SystemExit(3)

image_width = Quartz.CGImageGetWidth(image_ref)
image_height = Quartz.CGImageGetHeight(image_ref)
bytes_per_row = Quartz.CGImageGetBytesPerRow(image_ref)
provider = Quartz.CGImageGetDataProvider(image_ref)
raw = np.frombuffer(Quartz.CGDataProviderCopyData(provider), dtype=np.uint8)
rgba = raw.reshape((image_height, bytes_per_row // 4, 4))[:, :image_width, :]
gray = cv2.cvtColor(rgba[:, :, :3], cv2.COLOR_BGR2GRAY)
blur = cv2.GaussianBlur(gray, (5, 5), 0)
threshold = cv2.adaptiveThreshold(
    blur,
    255,
    cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
    cv2.THRESH_BINARY,
    31,
    7,
)
contours, _ = cv2.findContours(255 - threshold, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)

default_x = image_width * 0.5
default_y = (min(float(bounds["Height"]) * 0.9, float(bounds["Height"]) - primary_offset) / float(bounds["Height"])) * image_height

def candidate_score(rect):
    x, y, width, height = rect
    center_x = x + (width / 2)
    center_y = y + (height / 2)
    width_score = width / image_width
    center_score = 1.0 - abs(center_x - (image_width * 0.5)) / (image_width * 0.5)
    default_distance = abs(center_x - default_x) + abs(center_y - default_y)
    default_score = 1.0 - min(default_distance / (image_width + image_height), 1.0)
    lower_bias = center_y / image_height
    return (width_score * 2.4) + (center_score * 1.5) + (default_score * 1.8) + lower_bias

candidates = []
containing_default = []
for contour in contours:
    x, y, width, height = cv2.boundingRect(contour)
    area = width * height
    aspect_ratio = width / max(height, 1)
    if area < image_width * image_height * 0.01:
        continue
    if aspect_ratio < 4 or aspect_ratio > 30:
        continue
    if width < image_width * 0.2 or width > image_width * 0.92:
        continue
    if height < image_height * 0.03 or height > image_height * 0.16:
        continue
    center_x = x + (width / 2)
    center_y = y + (height / 2)
    if not (image_width * 0.12 <= center_x <= image_width * 0.88):
        continue
    if center_y < image_height * 0.4:
        continue

    rect = (x, y, width, height)
    candidates.append(rect)
    if x <= default_x <= x + width and y <= default_y <= y + height:
        containing_default.append(rect)

best = None
if containing_default:
    best = min(containing_default, key=lambda rect: rect[2] * rect[3])
elif candidates:
    best = max(candidates, key=candidate_score)

if best is None:
    raise SystemExit(4)

x, y, width, height = best
center_x = float(bounds["X"]) + ((x + (width / 2)) / image_width) * float(bounds["Width"])
center_y = float(bounds["Y"]) + ((y + (height / 2)) / image_height) * float(bounds["Height"])

role = None
apps = NSWorkspace.sharedWorkspace().runningApplications()
app = next((item for item in apps if item.localizedName() == app_name), None)
if app is not None:
    ax_app = AXUIElementCreateApplication(app.processIdentifier())
    err, element = AXUIElementCopyElementAtPosition(ax_app, center_x, center_y, None)
    if err == 0 and element is not None:
        role_err, role_value = AXUIElementCopyAttributeValue(element, "AXRole", None)
        if role_err == 0:
            role = role_value

print(json.dumps({"x": center_x, "y": center_y, "role": role}))
"""
    try:
        result = run_helper(get_vision_python(), helper, app_name, str(COMPOSER_PRIMARY_BOTTOM_OFFSET_PIXELS))
    except (RuntimeError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    payload = json.loads(result.stdout)
    return (float(payload["x"]), float(payload["y"]))


def get_app_window_bounds(app_name: str) -> tuple[float, float, float, float]:
    direct_window = direct_main_window_info(app_name)
    if direct_window is not None:
        bounds, _ = direct_window
        return (bounds["X"], bounds["Y"], bounds["Width"], bounds["Height"])

    helper = r"""
import json
import sys
import Quartz

app_name = sys.argv[1]
windows = Quartz.CGWindowListCopyWindowInfo(Quartz.kCGWindowListOptionOnScreenOnly, Quartz.kCGNullWindowID)
candidates = []

for window in windows:
    if window.get("kCGWindowOwnerName") != app_name:
        continue
    if window.get("kCGWindowLayer", 1) != 0:
        continue
    bounds = window.get("kCGWindowBounds", {})
    width = int(bounds.get("Width", 0))
    height = int(bounds.get("Height", 0))
    if width <= 0 or height <= 0:
        continue
    candidates.append((width * height, bounds))

if not candidates:
    raise SystemExit(2)

_, bounds = max(candidates, key=lambda item: item[0])
plain_bounds = {key: float(value) for key, value in dict(bounds).items()}
print(json.dumps(plain_bounds))
"""
    bounds = parse_helper_json(
        run_helper(get_quartz_python(), helper, app_name),
        f"Could not find an on-screen window for {app_name}.",
    )
    return (float(bounds["X"]), float(bounds["Y"]), float(bounds["Width"]), float(bounds["Height"]))


def click_point(x: float, y: float) -> None:
    ensure_accessibility_access(prompt=False)
    if DIRECT_QUARTZ is not None:
        source = DIRECT_QUARTZ.CGEventSourceCreate(DIRECT_QUARTZ.kCGEventSourceStateCombinedSessionState)
        move = DIRECT_QUARTZ.CGEventCreateMouseEvent(
            source,
            DIRECT_QUARTZ.kCGEventMouseMoved,
            (x, y),
            DIRECT_QUARTZ.kCGMouseButtonLeft,
        )
        down = DIRECT_QUARTZ.CGEventCreateMouseEvent(
            source,
            DIRECT_QUARTZ.kCGEventLeftMouseDown,
            (x, y),
            DIRECT_QUARTZ.kCGMouseButtonLeft,
        )
        up = DIRECT_QUARTZ.CGEventCreateMouseEvent(
            source,
            DIRECT_QUARTZ.kCGEventLeftMouseUp,
            (x, y),
            DIRECT_QUARTZ.kCGMouseButtonLeft,
        )
        DIRECT_QUARTZ.CGEventPost(DIRECT_QUARTZ.kCGHIDEventTap, move)
        time.sleep(0.04)
        DIRECT_QUARTZ.CGEventPost(DIRECT_QUARTZ.kCGHIDEventTap, down)
        time.sleep(0.02)
        DIRECT_QUARTZ.CGEventPost(DIRECT_QUARTZ.kCGHIDEventTap, up)
        return

    helper = r"""
import sys
import time
import Quartz

x = float(sys.argv[1])
y = float(sys.argv[2])
source = Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateCombinedSessionState)
move = Quartz.CGEventCreateMouseEvent(source, Quartz.kCGEventMouseMoved, (x, y), Quartz.kCGMouseButtonLeft)
down = Quartz.CGEventCreateMouseEvent(source, Quartz.kCGEventLeftMouseDown, (x, y), Quartz.kCGMouseButtonLeft)
up = Quartz.CGEventCreateMouseEvent(source, Quartz.kCGEventLeftMouseUp, (x, y), Quartz.kCGMouseButtonLeft)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, move)
time.sleep(0.04)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
time.sleep(0.02)
Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
"""
    result = run_helper(get_quartz_python(), helper, str(x), str(y))
    if result.returncode != 0:
        raise RuntimeError("Unable to click Codex composer region.")


def send_key_event(key_code: int, *, command: bool = False) -> None:
    ensure_accessibility_access(prompt=False)
    if DIRECT_QUARTZ is not None:
        source = DIRECT_QUARTZ.CGEventSourceCreate(DIRECT_QUARTZ.kCGEventSourceStateCombinedSessionState)
        down = DIRECT_QUARTZ.CGEventCreateKeyboardEvent(source, key_code, True)
        up = DIRECT_QUARTZ.CGEventCreateKeyboardEvent(source, key_code, False)
        if command:
            flags = DIRECT_QUARTZ.kCGEventFlagMaskCommand
            DIRECT_QUARTZ.CGEventSetFlags(down, flags)
            DIRECT_QUARTZ.CGEventSetFlags(up, flags)
        DIRECT_QUARTZ.CGEventPost(DIRECT_QUARTZ.kCGHIDEventTap, down)
        time.sleep(0.02)
        DIRECT_QUARTZ.CGEventPost(DIRECT_QUARTZ.kCGHIDEventTap, up)
        time.sleep(KEYBOARD_SETTLE_DELAY_SECONDS)
        return

    # Fallback for non-Quartz environments.
    script_lines = ['tell application "System Events"']
    if command:
        script_lines.append(f"key code {key_code} using command down")
    else:
        script_lines.append(f"key code {key_code}")
    script_lines.append("end tell")
    subprocess.run(["osascript", "-e", "\n".join(script_lines)], check=True, timeout=APPLE_SCRIPT_TIMEOUT_SECONDS)


def clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def composer_target_points(left: float, top: float, width: float, height: float) -> list[tuple[float, float]]:
    center_x = left + (width * 0.5)
    min_y = top + (height * 0.76)
    max_y = top + height - 36
    raw_y_positions = [
        top + min(height * 0.9, height - COMPOSER_PRIMARY_BOTTOM_OFFSET_PIXELS),
        top + min(height * 0.93, height - COMPOSER_SECONDARY_BOTTOM_OFFSET_PIXELS),
    ]

    points: list[tuple[float, float]] = []
    for raw_y in raw_y_positions:
        target_y = clamp(raw_y, min_y, max_y)
        point = (center_x, target_y)
        if point not in points:
            points.append(point)
    return points


def focus_codex_composer(app_name: str) -> None:
    left, top, width, height = get_app_window_bounds(app_name)
    target = find_accessibility_composer_point(app_name)
    if target is None:
        target = find_visual_composer_point(app_name)
    if target is None:
        # Final fallback when richer signals are unavailable.
        target = composer_target_points(left, top, width, height)[0]

    target_x, target_y = target
    print(f"Composer target for {app_name}: ({target_x:.1f}, {target_y:.1f})")
    click_point(target_x, target_y)
    # The first click after activation can simply front the window on macOS.
    time.sleep(COMPOSER_REFocus_DELAY_SECONDS)
    click_point(target_x, target_y)
    time.sleep(COMPOSER_SETTLE_DELAY_SECONDS)


def focus_codex(app_name: str) -> str:
    ensure_accessibility_access(prompt=True)
    activate_codex(app_name)
    time.sleep(APP_ACTIVATION_DELAY_SECONDS)
    focus_codex_composer(app_name)
    return f"Brought {app_name} to the front and focused the composer."


def paste_into_codex(text: str, submit: bool, app_name: str) -> str:
    subprocess.run(["pbcopy"], input=text, text=True, check=True)
    focus_codex(app_name)
    send_key_event(KEY_CODE_A, command=True)
    send_key_event(KEY_CODE_V, command=True)
    if submit:
        send_key_event(KEY_CODE_RETURN)
    return f"Replaced prompt draft in {app_name}" + (" and pressed Return." if submit else ".")


def activate_codex(app_name: str) -> str:
    subprocess.run(
        ["osascript", "-e", f'tell application "{app_name}" to activate'],
        check=True,
        timeout=APPLE_SCRIPT_TIMEOUT_SECONDS,
    )
    return f"Brought {app_name} to the front."


def interrupt_codex(app_name: str) -> str:
    activate_codex(app_name)
    time.sleep(0.15)
    send_key_event(KEY_CODE_ESCAPE)
    return f"Sent Escape to {app_name}."


def process_command(command: dict, dry_run: bool, app_name: str) -> tuple[bool, str]:
    payload = command["payload"]
    if dry_run:
        if command["kind"] == "prompt_to_codex":
            return True, f"Dry run: would paste {len(payload['text'])} characters into {app_name}."
        return True, f"Dry run: would run {command['kind']} on {app_name}."

    try:
        if command["kind"] == "prompt_to_codex":
            text = payload["text"]
            submit = bool(payload.get("submit", True))
            detail = paste_into_codex(text, submit, app_name)
        elif command["kind"] == "focus_codex":
            detail = focus_codex(app_name)
        elif command["kind"] == "interrupt_codex":
            detail = interrupt_codex(app_name)
        else:
            return False, f"Unsupported command kind: {command['kind']}"
    except subprocess.TimeoutExpired as exc:
        return False, f"Timed out while handling {command['kind']}: {exc}"
    except subprocess.CalledProcessError as exc:
        return False, f"AppleScript failed: {exc}"
    except Exception as exc:
        return False, f"Command failed: {exc}"

    return True, detail


def run_loop(
    base_url: str,
    session_id: str,
    token: str,
    poll_seconds: float,
    dry_run: bool,
    app_name: str,
) -> None:
    ensure_line_buffering()
    agent_name = socket.gethostname()
    claim_url = f"{base_url}/api/sessions/{session_id}/commands/claim-next"
    heartbeat_url = f"{base_url}/api/sessions/{session_id}/heartbeat"

    print(f"Mac agent watching session {session_id}")
    print(f"Target app: {app_name}")
    print(
        "Direct imports:"
        f" Quartz={'yes' if DIRECT_QUARTZ is not None else 'no'}"
        f" cv2={'yes' if DIRECT_CV2 is not None else 'no'}"
        f" numpy={'yes' if DIRECT_NUMPY is not None else 'no'}"
        f" NSWorkspace={'yes' if DIRECT_NSWorkspace is not None else 'no'}"
        f" AX={'yes' if DIRECT_AXUIElementCreateApplication is not None else 'no'}"
    )
    print(f"Accessibility trusted at startup: {'yes' if has_accessibility_access(prompt=False) else 'no'}")
    if DIRECT_IMPORT_ERRORS:
        print(f"Direct import errors: {json.dumps(DIRECT_IMPORT_ERRORS, sort_keys=True)}")
    if dry_run:
        print("Dry-run mode enabled")

    while True:
        try:
            request_json("POST", heartbeat_url, {"role": "agent"}, token=token)
            command = request_json("POST", claim_url, {"agent_name": agent_name}, token=token)
            if command:
                ok, detail = process_command(command, dry_run=dry_run, app_name=app_name)
                complete_url = f"{base_url}/api/sessions/{session_id}/commands/{command['id']}/complete"
                request_json("POST", complete_url, {"ok": ok, "detail": detail}, token=token)
                print(f"Completed command {command['id']}: {detail}")
            else:
                time.sleep(poll_seconds)
        except KeyboardInterrupt:
            print("Mac agent stopped.")
            break
        except Exception as exc:
            print(f"Mac agent error: {exc}")
            time.sleep(poll_seconds)


def main() -> None:
    ensure_line_buffering()
    parser = argparse.ArgumentParser(description="Pocket Mac agent")
    parser.add_argument("--session", required=True, help="Session id to watch")
    parser.add_argument("--token", required=True, help="Session access token")
    parser.add_argument("--base-url", default="http://127.0.0.1:8000", help="FastAPI base URL")
    parser.add_argument("--poll-seconds", type=float, default=2.0, help="Polling interval")
    parser.add_argument("--app-name", default="Codex", help="Application name to activate")
    parser.add_argument("--dry-run", action="store_true", help="Do not send real keystrokes")
    args = parser.parse_args()

    run_loop(
        base_url=args.base_url.rstrip("/"),
        session_id=args.session,
        token=args.token,
        poll_seconds=args.poll_seconds,
        dry_run=args.dry_run,
        app_name=args.app_name,
    )


if __name__ == "__main__":
    main()
