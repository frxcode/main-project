#!/usr/bin/env python3
"""
МАН — Raspberry Pi: двоетапна перевірка робіт.

Піни (BCM):
  SK6812 data  → GPIO 18 (PWM0), також GPIO 12 (той самий PWM0)
                 і GPIO 13 (PWM1). Один кадр на всі три, бо дріт
                 переносили між цими пінами.
  Кнопка       → GPIO 17 (pull_up)
  LCD 16x2     → I2C 0x27
  PWM: dtparam=audio=off і force_pwm_open=0, інакше firmware
  тримає тактовий генератор PWM.

Стани LED: райдуга → фіолетовий (Pass 1 OK) → синій пульс (Pass 2) →
зелений / жовтий / червоний за %.
"""

from __future__ import annotations

import colorsys
import math
import os
import subprocess
import threading
import time
from enum import Enum, auto
from typing import Optional

import cv2
import requests
from gpiozero import Button, Device
from RPLCD.i2c import CharLCD

# GPIO 12 and GPIO 18 share PWM0, so both carry the same waveform.
# GPIO 13 is PWM1 and gets the same frame on the second channel.
LED_PIN = 18
LED_MIRROR_PIN = 12
LED_PIN_ALT = 13
BUTTON_PIN = 17
LCD_I2C_ADDRESS = 0x27

LED_COUNT = 16
LED_FREQ_HZ = 800_000
LED_DMA = 10
LED_CHANNEL = 0
LED_BRIGHTNESS = 180
LED_INVERT = False
LED_FRAME_DELAY_S = 0.04
LED_TRANSITION_ALPHA = 0.18
RAINBOW_SPEED = 0.05
PULSE_SPEED = 2.0

BUTTON_BOUNCE_S = 0.08
SYNC_INTERVAL_S = 2.0
RESULT_HOLD_S = 4.0
SERVER_URL = "http://192.168.1.90:4576"

last_line1, last_line2 = "", ""
server_connected = False


class LedMode(Enum):
    STANDBY = auto()
    READY = auto()
    PROCESSING = auto()
    RESULT = auto()


def update_lcd(lcd, line1, line2=""):
    global last_line1, last_line2
    l1, l2 = str(line1)[:16], str(line2)[:16]
    if l1 == last_line1 and l2 == last_line2:
        return
    last_line1, last_line2 = l1, l2
    print(f"[LCD] {l1} | {l2}", flush=True)
    if lcd is None:
        return
    try:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string(l1)
        lcd.cursor_pos = (1, 0)
        lcd.write_string(l2)
    except Exception as exc:
        print(f"[WARN] LCD update failed: {exc}", flush=True)


IDLE_LCD_MESSAGE = "Choose student in web or scan document"
_idle_phase = 0
_idle_tick = 0.0


def show_idle_lcd(lcd):
    """Scroll the full standby sentence across the 16x2 LCD."""
    global _idle_phase, _idle_tick
    now = time.time()
    padded = IDLE_LCD_MESSAGE + "    "
    if now - _idle_tick >= 0.4:
        _idle_phase = (_idle_phase + 1) % len(padded)
        _idle_tick = now
    window = (padded + padded)[_idle_phase : _idle_phase + 32]
    update_lcd(lcd, window[:16], window[16:32])


def use_software_gpio() -> None:
    """Read the button through lgpio, not hardware PWM.

    The PWM clock is reserved for the SK6812 strip. A hardware-PWM pin
    factory would retune that clock and the pixels would ignore the frame.
    """
    try:
        from gpiozero.pins.lgpio import LGPIOFactory

        Device.pin_factory = LGPIOFactory()
    except Exception as exc:
        print(f"[WARN] Software GPIO factory unavailable: {exc}", flush=True)


class LedStrip:
    """SK6812 frames on GPIO 18, mirrored to GPIO 12 and GPIO 13.

    Channel 0 is PWM0 (GPIO 18, DMA 10). GPIO 12 is the other PWM0 pad, so
    it carries the same waveform. Channel 1 is PWM1 on GPIO 13. Rainbow and
    the blue pulse run in a daemon thread; purple and the result colors are
    written solid immediately.
    """

    READY_PURPLE = (160, 0, 255)

    def __init__(self, pin: int = LED_PIN, count: int = LED_COUNT):
        self.count = count
        self._pixels = None
        self._Color = None
        self._ws = None
        self._ch1 = None
        self._lock = threading.RLock()
        self._mode = LedMode.STANDBY
        self._result_color = (0, 180, 0)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._phase = 0.0
        self._current_colors = [(0, 0, 0) for _ in range(count)]
        self._init_pixels(pin, count)

    def _init_pixels(self, pin: int, count: int):
        try:
            from rpi_ws281x import PixelStrip, Color, ws  # type: ignore

            self._Color = Color
            self._ws = ws
            strip_type = getattr(ws, "WS2811_STRIP_GRB", None)
            args = (
                count,
                pin,
                LED_FREQ_HZ,
                LED_DMA,
                LED_INVERT,
                LED_BRIGHTNESS,
                LED_CHANNEL,
            )
            strip = (
                PixelStrip(*args, strip_type)
                if strip_type is not None
                else PixelStrip(*args)
            )
            if strip_type is not None:
                ch1 = ws.ws2811_channel_get(strip._leds, 1)
                ws.ws2811_channel_t_gamma_set(ch1, list(range(256)))
                ws.ws2811_channel_t_count_set(ch1, count)
                ws.ws2811_channel_t_gpionum_set(ch1, LED_PIN_ALT)
                ws.ws2811_channel_t_invert_set(ch1, 1 if LED_INVERT else 0)
                ws.ws2811_channel_t_brightness_set(ch1, LED_BRIGHTNESS)
                ws.ws2811_channel_t_strip_type_set(ch1, strip_type)
                self._ch1 = ch1
            strip.begin()
            self._pixels = strip
            self._reassert_pins()
            print(
                f"[SYSTEM] LED GPIO {pin}+{LED_MIRROR_PIN} PWM0 and "
                f"GPIO {LED_PIN_ALT} PWM1 freq={LED_FREQ_HZ} dma={LED_DMA} "
                f"brightness={LED_BRIGHTNESS} invert={LED_INVERT} type=GRB",
                flush=True,
            )
        except Exception as exc:
            print(f"[ERROR] LED init failed: {exc}", flush=True)
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                print("[ERROR] Run as root for LED PWM access", flush=True)
            self._pixels = None
            self._ch1 = None

    def _reassert_pins(self):
        """Keep the data pads on PWM after gpiozero opens the button."""
        for pin, func in (
            (str(LED_MIRROR_PIN), "a0"),  # GPIO 12 PWM0
            ("18", "a5"),                  # GPIO 18 PWM0
            (str(LED_PIN_ALT), "a0"),      # GPIO 13 PWM1
            ("19", "ip"),                  # old sensor pad, leave idle
        ):
            try:
                subprocess.run(
                    ["pinctrl", "set", pin, func],
                    check=False,
                    capture_output=True,
                )
            except Exception as exc:
                print(f"[WARN] pin {pin} mux failed: {exc}", flush=True)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=1.5)
        self._clear()

    def set_standby(self):
        """Waiting for scan — rainbow (background thread)."""
        with self._lock:
            self._mode = LedMode.STANDBY

    def set_ready(self):
        """Student matched — solid purple immediately."""
        with self._lock:
            self._mode = LedMode.READY
            self._paint_solid(self.READY_PURPLE)

    def set_processing(self):
        """Pass 2 waiting for the server — pulsing blue."""
        with self._lock:
            self._mode = LedMode.PROCESSING

    def set_result(self, ai_percent: int):
        """Solid result color: green / orange / red."""
        pct = max(0, min(100, int(ai_percent)))
        if pct < 25:
            color = (0, 255, 0)
        elif pct < 75:
            color = (255, 140, 0)
        else:
            color = (255, 0, 0)
        with self._lock:
            self._result_color = color
            self._mode = LedMode.RESULT
            self._paint_solid(color)

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                mode = self._mode
                result_color = self._result_color
                try:
                    if mode == LedMode.STANDBY:
                        self._draw_rainbow()
                    elif mode == LedMode.READY:
                        self._paint_solid(self.READY_PURPLE)
                    elif mode == LedMode.PROCESSING:
                        self._draw_pulse_blue()
                    else:
                        self._paint_solid(result_color)
                except Exception as exc:
                    print(f"[WARN] LED frame failed: {exc}", flush=True)
                else:
                    self._phase += LED_FRAME_DELAY_S
            time.sleep(LED_FRAME_DELAY_S)

    def _show(self):
        if self._pixels is not None:
            self._pixels.show()

    def _set_pixel(self, i: int, r: int, g: int, b: int):
        if self._pixels is None or self._Color is None:
            return
        color = self._Color(int(r), int(g), int(b))
        self._pixels.setPixelColor(i, color)
        if self._ch1 is not None and self._ws is not None:
            self._ws.ws2811_led_set(self._ch1, i, color)

    def _clear(self):
        if self._pixels is None:
            return
        self._current_colors = [(0, 0, 0) for _ in range(self.count)]
        for i in range(self.count):
            self._set_pixel(i, 0, 0, 0)
        self._show()

    def _draw_colors(self, target_colors):
        if self._pixels is None:
            return
        blended = []
        for i, target in enumerate(target_colors):
            old = self._current_colors[i]
            color = tuple(
                int(old[c] + (target[c] - old[c]) * LED_TRANSITION_ALPHA)
                for c in range(3)
            )
            blended.append(color)
            self._set_pixel(i, *color)
        self._current_colors = blended
        self._show()

    def _draw_rainbow(self):
        colors = []
        for i in range(self.count):
            hue = (self._phase * RAINBOW_SPEED + i / max(1, self.count)) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 0.55)
            colors.append((r * 255, g * 255, b * 255))
        self._draw_colors(colors)

    def _paint_solid(self, color):
        """Write a solid color immediately (no blend, no breathe)."""
        if self._pixels is None:
            return
        r, g, b = color
        colors = [(r, g, b) for _ in range(self.count)]
        for i, (rr, gg, bb) in enumerate(colors):
            self._set_pixel(i, rr, gg, bb)
        self._current_colors = colors
        self._show()

    def _draw_pulse_blue(self):
        level = 0.2 + 0.8 * (0.5 + 0.5 * math.sin(self._phase * PULSE_SPEED))
        self._draw_colors([(0, int(30 * level), int(255 * level)) for _ in range(self.count)])


def ping_server() -> bool:
    connected, _active_id, _display = pull_server_selection()
    return connected


def pull_server_selection() -> tuple[bool, Optional[int], str]:
    """Read the student the teacher selected in the web dashboard."""
    global server_connected
    try:
        response = requests.get(f"{SERVER_URL}/api/pi/sync", timeout=2)
    except requests.RequestException:
        server_connected = False
        return False, None, ""
    server_connected = response.status_code == 200
    if not server_connected:
        return False, None, ""
    data = response.json()
    raw_id = data.get("active_student_id")
    if raw_id is None:
        return True, None, ""
    try:
        active_id = int(raw_id)
    except (TypeError, ValueError):
        return True, None, ""
    display = str(data.get("display_name") or data.get("active_student_name") or "").strip()
    if display == "None":
        display = ""
    return True, active_id, display[:16]


def clear_session_on_server() -> None:
    global server_connected
    try:
        response = requests.post(f"{SERVER_URL}/api/pi/clear_session", timeout=3)
        if response.status_code == 200:
            server_connected = True
    except requests.RequestException:
        pass


def reset_session(lcd, leds: LedStrip) -> tuple[Optional[int], str]:
    """Idle: rainbow + default LCD, no cached student."""
    leds.set_standby()
    show_idle_lcd(lcd)
    return None, ""


def capture_photo(cap) -> Optional[bytes]:
    for _ in range(5):
        cap.grab()
    ret, frame = cap.read()
    if not ret or frame is None:
        return None
    ok, buffer = cv2.imencode(".png", frame)
    return buffer.tobytes() if ok else None


def pass1_recognize(lcd, cap, leds: LedStrip) -> tuple[Optional[int], str]:
    """Photo → AI name → DB match. Returns (student_id, display_name)."""
    print("[ACTION] Pass 1: recognize", flush=True)
    if not server_connected:
        update_lcd(lcd, "System fail", "Code 301")
        return None, ""

    update_lcd(lcd, "Reading name", "AI")
    photo = capture_photo(cap)
    if photo is None:
        update_lcd(lcd, "Camera fail", "Code 201")
        return None, ""

    try:
        response = requests.post(
            f"{SERVER_URL}/api/pi/recognize_student",
            files={"file": ("work.png", photo, "image/png")},
            timeout=35,
        )
    except requests.RequestException:
        update_lcd(lcd, "Network fail", "Code 500")
        return None, ""

    if response.status_code != 200:
        update_lcd(lcd, "Name fail", f"Code {response.status_code}")
        return None, ""

    payload = response.json()
    if payload.get("status") != "success":
        recognized = payload.get("recognized") or {}
        hint = (
            recognized.get("display_name")
            or recognized.get("full_name")
            or ""
        ).strip()
        update_lcd(lcd, "Not in DB", hint[:16] if hint else "try again")
        time.sleep(1.8)
        return None, ""

    try:
        student_id = int(payload["student_id"])
    except (KeyError, TypeError, ValueError):
        update_lcd(lcd, "Name fail", "Code 501")
        return None, ""

    display = str(payload.get("display_name") or payload.get("student_name") or "").strip()
    if not display:
        update_lcd(lcd, "Name fail", "Code 501")
        return None, ""

    leds.set_ready()
    update_lcd(lcd, display[:16], "Press button")
    print(f"[READY] id={student_id} name={display}", flush=True)
    return student_id, display


def pass2_analyze(
    lcd, student_id: int, display_name: str, cap, leds: LedStrip
) -> bool:
    """Full AI check linked to student_id. True = reset session afterwards."""
    print("[ACTION] Pass 2: analyze", flush=True)
    if not server_connected:
        update_lcd(lcd, "System fail", "Code 301")
        return True

    leds.set_processing()
    update_lcd(lcd, "Checking", "AI")
    photo = capture_photo(cap)
    if photo is None:
        update_lcd(lcd, "Camera fail", "Code 201")
        leds.set_ready()
        update_lcd(lcd, display_name[:16], "Press button")
        return False

    try:
        response = requests.post(
            f"{SERVER_URL}/upload/",
            data={"student_id": str(student_id)},
            files={"file": ("work.png", photo, "image/png")},
            timeout=60,
        )
    except requests.RequestException:
        update_lcd(lcd, "Network fail", "Code 500")
        return True

    if response.status_code != 200 or response.json().get("status") != "success":
        update_lcd(lcd, "Analysis fail", "Code 501")
        time.sleep(1.5)
        return True

    pct = max(0, min(100, int(response.json().get("ai_percent", 0))))
    leds.set_result(pct)
    update_lcd(lcd, "Analysis done", f"{pct}% AI")
    print(f"[RESULT] {pct}% AI", flush=True)
    time.sleep(RESULT_HOLD_S)
    return True


def main():
    print("[SYSTEM] Starting", flush=True)
    use_software_gpio()

    try:
        lcd = CharLCD("PCF8574", LCD_I2C_ADDRESS, port=1, cols=16, rows=2)
    except Exception as exc:
        print(f"[ERROR] LCD: {exc}", flush=True)
        return

    leds = LedStrip()
    leds.start()
    leds.set_standby()
    update_lcd(lcd, "Starting", "system")
    time.sleep(1.0)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        update_lcd(lcd, "System fail", "Code 202")
        leds.stop()
        return
    cap.set(3, 640)
    cap.set(4, 480)

    try:
        button = Button(BUTTON_PIN, pull_up=True, bounce_time=BUTTON_BOUNCE_S)
        leds._reassert_pins()
    except Exception as exc:
        print(f"[ERROR] Button: {exc}", flush=True)
        update_lcd(lcd, "System fail", "Code 102")
        leds.stop()
        cap.release()
        return

    clear_session_on_server()
    student_id, display_name = reset_session(lcd, leds)
    ready = False
    last_sync = time.time()
    print("[SYSTEM] Idle", flush=True)

    try:
        while True:
            now = time.time()
            if now - last_sync >= SYNC_INTERVAL_S:
                connected, active_id, active_name = pull_server_selection()
                last_sync = now
                if connected and active_id != student_id:
                    if active_id is None or not active_name:
                        student_id, display_name = reset_session(lcd, leds)
                        ready = False
                    else:
                        student_id, display_name, ready = active_id, active_name, True
                        leds.set_ready()
                        update_lcd(lcd, display_name[:16], "Press button")

            if not server_connected:
                if ready:
                    student_id, display_name = reset_session(lcd, leds)
                    ready = False
                update_lcd(lcd, "System fail", "Code 301")
            elif ready and student_id is not None:
                if not button.is_pressed:
                    update_lcd(lcd, display_name[:16], "Press button")
            elif not button.is_pressed:
                show_idle_lcd(lcd)

            if not button.is_pressed:
                time.sleep(0.05)
                continue

            button.wait_for_release(timeout=3.0)
            time.sleep(0.05)  # settle after bounce

            if ready and student_id is not None:
                if pass2_analyze(lcd, student_id, display_name, cap, leds):
                    clear_session_on_server()
                    student_id, display_name = reset_session(lcd, leds)
                    ready = False
            else:
                sid, name = pass1_recognize(lcd, cap, leds)
                if sid is not None:
                    student_id, display_name, ready = sid, name, True
                else:
                    clear_session_on_server()
                    student_id, display_name = reset_session(lcd, leds)
                    ready = False
            time.sleep(0.3)
    except KeyboardInterrupt:
        print("[SYSTEM] Stopped", flush=True)
    finally:
        clear_session_on_server()
        leds.stop()
        try:
            button.close()
        except Exception:
            pass
        cap.release()
        update_lcd(lcd, "Stopped", "")


if __name__ == "__main__":
    main()
