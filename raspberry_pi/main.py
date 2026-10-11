#!/usr/bin/env python3
"""
МАН — Raspberry Pi: двоетапна перевірка робіт.

Піни (BCM):
  SK6812 data  → GPIO 18 (PWM0 / DMA 10; потрібен dtparam=audio=off)
  Кнопка       → GPIO 17 (pull_up)
  Баззер       → GPIO 13
  LCD 16x2     → I2C 0x27

Стани LED: райдуга → фіолетовий (Pass 1 OK) → синій пульс (Pass 2) →
зелений / жовтий / червоний за %.
"""

from __future__ import annotations

import colorsys
import math
import os
import threading
import time
from enum import Enum, auto
from typing import Optional

import cv2
import requests
from gpiozero import Button, Buzzer
from RPLCD.i2c import CharLCD

LED_PIN = 18
BUTTON_PIN = 17
BUZZER_PIN = 13
LCD_I2C_ADDRESS = 0x27

LED_COUNT = 16
LED_FREQ_HZ = 800_000
LED_DMA = 10
LED_CHANNEL = 0
LED_BRIGHTNESS = 100
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


def show_idle_lcd(lcd):
    update_lcd(lcd, "Choose student", "web or scan doc")


class Beeper:
    def __init__(self, pin: int = BUZZER_PIN):
        self._buzzer = None
        try:
            self._buzzer = Buzzer(pin)
            self._buzzer.off()
        except Exception as exc:
            print(f"[WARN] Buzzer unavailable: {exc}", flush=True)

    def beep(self, duration: float = 0.06, count: int = 1, gap: float = 0.06):
        if self._buzzer is None:
            return

        def _run():
            try:
                for i in range(count):
                    self._buzzer.on()
                    time.sleep(duration)
                    self._buzzer.off()
                    if i + 1 < count:
                        time.sleep(gap)
            except Exception:
                pass

        threading.Thread(target=_run, daemon=True).start()

    def click(self):
        self.beep(0.04, 1)

    def result_ok(self):
        self.beep(0.08, 2, 0.07)

    def result_warn(self):
        self.beep(0.12, 1)

    def result_bad(self):
        self.beep(0.18, 3, 0.08)

    def error(self):
        self.beep(0.25, 1)

    def close(self):
        if self._buzzer is None:
            return
        try:
            self._buzzer.off()
            self._buzzer.close()
        except Exception:
            pass


class LedStrip:
    """SK6812 on GPIO 18 — hardware PWM channel 0, DMA 10.

    Animations (rainbow, blue pulse) run in a daemon thread so the button
    and camera loop stay responsive. stop() clears the strip.
    """

    READY_PURPLE = (160, 0, 255)

    def __init__(self, pin: int = LED_PIN, count: int = LED_COUNT):
        self.count = count
        self._pixels = None
        self._Color = None
        self._lock = threading.Lock()
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
            strip.begin()
            self._pixels = strip
            print(
                f"[SYSTEM] LED GPIO {pin} freq={LED_FREQ_HZ} dma={LED_DMA} "
                f"ch={LED_CHANNEL} brightness={LED_BRIGHTNESS} invert={LED_INVERT}",
                flush=True,
            )
        except Exception as exc:
            print(f"[ERROR] LED init failed: {exc}", flush=True)
            if hasattr(os, "geteuid") and os.geteuid() != 0:
                print("[ERROR] Run as root for LED PWM access", flush=True)
            self._pixels = None

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
                time.sleep(0.2)
                continue
            time.sleep(LED_FRAME_DELAY_S)
            self._phase += LED_FRAME_DELAY_S

    def _show(self):
        if self._pixels is not None:
            self._pixels.show()

    def _set_pixel(self, i: int, r: int, g: int, b: int):
        if self._pixels is None:
            return
        self._pixels.setPixelColor(i, self._Color(int(r), int(g), int(b)))

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
    global server_connected
    try:
        response = requests.get(f"{SERVER_URL}/api/pi/sync", timeout=2)
        server_connected = response.status_code == 200
        return server_connected
    except requests.RequestException:
        server_connected = False
        return False


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


def pass1_recognize(lcd, cap, leds: LedStrip, beeper: Beeper) -> tuple[Optional[int], str]:
    """Photo → AI name → DB match. Returns (student_id, display_name)."""
    print("[ACTION] Pass 1: recognize", flush=True)
    if not server_connected:
        update_lcd(lcd, "System fail", "Code 301")
        beeper.error()
        return None, ""

    update_lcd(lcd, "Reading name", "AI")
    photo = capture_photo(cap)
    if photo is None:
        update_lcd(lcd, "Camera fail", "Code 201")
        beeper.error()
        return None, ""

    try:
        response = requests.post(
            f"{SERVER_URL}/api/pi/recognize_student",
            files={"file": ("work.png", photo, "image/png")},
            timeout=35,
        )
    except requests.RequestException:
        update_lcd(lcd, "Network fail", "Code 500")
        beeper.error()
        return None, ""

    if response.status_code != 200:
        update_lcd(lcd, "Name fail", f"Code {response.status_code}")
        beeper.error()
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
        beeper.error()
        time.sleep(1.8)
        return None, ""

    try:
        student_id = int(payload["student_id"])
    except (KeyError, TypeError, ValueError):
        update_lcd(lcd, "Name fail", "Code 501")
        beeper.error()
        return None, ""

    display = str(payload.get("display_name") or payload.get("student_name") or "").strip()
    if not display:
        update_lcd(lcd, "Name fail", "Code 501")
        beeper.error()
        return None, ""

    leds.set_ready()
    update_lcd(lcd, display[:16], "Press button")
    beeper.click()
    print(f"[READY] id={student_id} name={display}", flush=True)
    return student_id, display


def pass2_analyze(
    lcd, student_id: int, display_name: str, cap, leds: LedStrip, beeper: Beeper
) -> bool:
    """Full AI check linked to student_id. True = reset session afterwards."""
    print("[ACTION] Pass 2: analyze", flush=True)
    if not server_connected:
        update_lcd(lcd, "System fail", "Code 301")
        beeper.error()
        return True

    leds.set_processing()
    update_lcd(lcd, "Checking", "AI")
    photo = capture_photo(cap)
    if photo is None:
        update_lcd(lcd, "Camera fail", "Code 201")
        beeper.error()
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
        beeper.error()
        return True

    if response.status_code != 200 or response.json().get("status") != "success":
        update_lcd(lcd, "Analysis fail", "Code 501")
        beeper.error()
        time.sleep(1.5)
        return True

    pct = max(0, min(100, int(response.json().get("ai_percent", 0))))
    leds.set_result(pct)
    if pct < 25:
        beeper.result_ok()
    elif pct < 75:
        beeper.result_warn()
    else:
        beeper.result_bad()
    update_lcd(lcd, "Analysis done", f"{pct}% AI")
    print(f"[RESULT] {pct}% AI", flush=True)
    time.sleep(RESULT_HOLD_S)
    return True


def main():
    print("[SYSTEM] Starting", flush=True)

    try:
        lcd = CharLCD("PCF8574", LCD_I2C_ADDRESS, port=1, cols=16, rows=2)
    except Exception as exc:
        print(f"[ERROR] LCD: {exc}", flush=True)
        return

    beeper = Beeper()
    leds = LedStrip()
    leds.start()
    leds.set_standby()
    update_lcd(lcd, "Starting", "system")
    time.sleep(1.0)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        update_lcd(lcd, "System fail", "Code 202")
        beeper.error()
        leds.stop()
        beeper.close()
        return
    cap.set(3, 640)
    cap.set(4, 480)

    try:
        button = Button(BUTTON_PIN, pull_up=True, bounce_time=BUTTON_BOUNCE_S)
    except Exception as exc:
        print(f"[ERROR] Button: {exc}", flush=True)
        update_lcd(lcd, "System fail", "Code 102")
        beeper.error()
        leds.stop()
        beeper.close()
        cap.release()
        return

    clear_session_on_server()
    ping_server()
    student_id, display_name = reset_session(lcd, leds)
    ready = False
    last_sync = 0.0
    print("[SYSTEM] Idle", flush=True)

    try:
        while True:
            now = time.time()
            if now - last_sync >= SYNC_INTERVAL_S:
                ping_server()
                last_sync = now

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

            beeper.click()
            button.wait_for_release(timeout=3.0)
            time.sleep(0.05)  # settle after bounce

            if ready and student_id is not None:
                if pass2_analyze(lcd, student_id, display_name, cap, leds, beeper):
                    clear_session_on_server()
                    student_id, display_name = reset_session(lcd, leds)
                    ready = False
            else:
                sid, name = pass1_recognize(lcd, cap, leds, beeper)
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
        beeper.close()
        try:
            button.close()
        except Exception:
            pass
        cap.release()
        update_lcd(lcd, "Stopped", "")


if __name__ == "__main__":
    main()
