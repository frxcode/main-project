#!/usr/bin/env python3
"""
МАН — Raspberry Pi: перевірка робіт на ШІ.

Піни (BCM):
  SK6812 / WS2812 data  → GPIO 26  (живлення стрічки — зовнішні 5V, GND спільний)
  Фізична кнопка        → GPIO 17  (S→GPIO17, V→3.3V, G→GND; pull_up у коді)
  Активний баззер       → GPIO 13  (сигнали при кліках / результаті)
  LCD 16x2 I2C          → адреса 0x27 (SDA GPIO 2, SCL GPIO 3)

Керування однією кнопкою:
  1 клік  — фото + відправка на сервер (учень з LCD)
  2 кліки — наступний учень
"""

from __future__ import annotations

import colorsys
import math
import threading
import time
from enum import Enum, auto
from typing import Optional

import cv2
import requests
from gpiozero import Button, Buzzer
from RPLCD.i2c import CharLCD

# ---------------------------------------------------------------------------
# Жорстко закріплена конфігурація пінів / периферії
# ---------------------------------------------------------------------------
LED_PIN = 26          # SK6812 / WS2812 data (BCM)
LED_BITBANG_CLOCK_PIN = 16  # фіктивний CLK для bitbang SPI (не підключати)
BUTTON_PIN = 17       # фізична кнопка (BCM), active-LOW + pull_up
BUZZER_PIN = 13       # Active buzzer (BCM)
LCD_I2C_ADDRESS = 0x27

LED_COUNT = 16
LED_BRIGHTNESS = 48   # 0–255
# rpi_ws281x DMA підтримує лише GPIO 10/12/13/18/19/21 — для GPIO 26
# використовуємо Adafruit NeoPixel_SPI (bitbang).

DOUBLE_CLICK_WINDOW_S = 0.35  # вікно між кліками для double-click
SYNC_INTERVAL_S = 2.0
RESULT_HOLD_S = 4.0

SERVER_URL = "http://192.168.1.90:8000"

# ---------------------------------------------------------------------------
# Глобальний стан LCD (щоб не мигати зайвим clear)
# ---------------------------------------------------------------------------
last_line1, last_line2 = "", ""
server_connected = False


class LedMode(Enum):
    STANDBY = auto()       # райдужний перелив
    PROCESSING = auto()    # пульс синім (знімок / очікування ШІ)
    RESULT = auto()        # колір за вердиктом


# ---------------------------------------------------------------------------
# LCD
# ---------------------------------------------------------------------------
def update_lcd(lcd, line1, line2):
    global last_line1, last_line2
    l1 = str(line1)[:16]
    l2 = str(line2)[:16]

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


def show_student(lcd, student_name: str, line2: str = "1x photo 2x next"):
    name = student_name if student_name and student_name != "None" else "No student"
    update_lcd(lcd, name[:16], line2[:16])


# ---------------------------------------------------------------------------
# Баззер
# ---------------------------------------------------------------------------
class Beeper:
    def __init__(self, pin: int = BUZZER_PIN):
        self._buzzer = None
        try:
            self._buzzer = Buzzer(pin)
            self._buzzer.off()
            print(f"[SYSTEM] Buzzer on GPIO {pin}", flush=True)
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
            except Exception as exc:
                print(f"[WARN] Buzzer beep failed: {exc}", flush=True)

        threading.Thread(target=_run, daemon=True).start()

    def click(self):
        self.beep(0.04, 1)

    def next_student(self):
        self.beep(0.05, 2, 0.05)

    def photo_start(self):
        self.beep(0.1, 1)

    def result_ok(self):
        self.beep(0.08, 2, 0.07)

    def result_warn(self):
        self.beep(0.12, 1)

    def result_bad(self):
        self.beep(0.18, 3, 0.08)

    def error(self):
        self.beep(0.25, 1)

    def close(self):
        if self._buzzer is not None:
            try:
                self._buzzer.off()
                self._buzzer.close()
            except Exception:
                pass


# ---------------------------------------------------------------------------
# LED стрічка / кільце (SK6812 / WS2812) на GPIO 26
# ---------------------------------------------------------------------------
class LedStrip:
    """Анімації в окремому потоці: standby / processing / result.

    GPIO 26 не підтримується DMA-драйвером rpi_ws281x, тому використовуємо
    Adafruit NeoPixel_SPI через bitbang (MOSI = GPIO 26).
    """

    def __init__(self, pin: int = LED_PIN, count: int = LED_COUNT):
        self.count = count
        self._pixels = None
        self._backend = None
        self._lock = threading.Lock()
        self._mode = LedMode.STANDBY
        self._result_color = (0, 180, 0)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._phase = 0.0
        self._init_pixels(pin, count)

    def _init_pixels(self, pin: int, count: int):
        brightness = max(0.05, min(1.0, LED_BRIGHTNESS / 255.0))
        try:
            import board
            import bitbangio
            import neopixel_spi as neo_spi

            mosi = getattr(board, f"D{pin}")
            clock = getattr(board, f"D{LED_BITBANG_CLOCK_PIN}")
            spi = bitbangio.SPI(clock=clock, MOSI=mosi)
            pixels = neo_spi.NeoPixel_SPI(
                spi,
                count,
                pixel_order=neo_spi.GRB,
                auto_write=False,
                brightness=brightness,
            )
            self._pixels = pixels
            self._backend = "neopixel_spi"
            print(
                f"[SYSTEM] LED strip on GPIO {pin} via bitbang SPI, n={count}",
                flush=True,
            )
            return
        except Exception as exc:
            print(f"[WARN] NeoPixel bitbang init failed: {exc}", flush=True)

        # fallback: rpi_ws281x (лише для GPIO 10/12/13/18/19/21)
        try:
            from rpi_ws281x import PixelStrip, Color  # type: ignore

            self._Color = Color
            strip = PixelStrip(count, pin, 800_000, 10, False, LED_BRIGHTNESS, 0)
            strip.begin()
            self._pixels = strip
            self._backend = "rpi_ws281x"
            print(f"[SYSTEM] LED strip on GPIO {pin} via rpi_ws281x, n={count}", flush=True)
        except Exception as exc:
            print(f"[WARN] LED strip unavailable: {exc}", flush=True)
            self._pixels = None
            self._backend = None

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
        with self._lock:
            self._mode = LedMode.STANDBY

    def set_processing(self):
        with self._lock:
            self._mode = LedMode.PROCESSING

    def set_result(self, ai_percent: int):
        pct = max(0, min(100, int(ai_percent)))
        if pct < 25:
            color = (0, 220, 40)       # зелений — самостійна робота
        elif pct < 75:
            color = (255, 140, 0)      # жовтий / помаранчевий
        else:
            color = (255, 20, 20)      # червоний — висока ймовірність ШІ
        with self._lock:
            self._result_color = color
            self._mode = LedMode.RESULT

    def _loop(self):
        while not self._stop.is_set():
            with self._lock:
                mode = self._mode
                result_color = self._result_color
            try:
                if mode == LedMode.STANDBY:
                    self._draw_rainbow()
                    time.sleep(0.03)
                elif mode == LedMode.PROCESSING:
                    self._draw_pulse_blue()
                    time.sleep(0.03)
                else:
                    self._draw_solid(result_color)
                    time.sleep(0.05)
            except Exception as exc:
                print(f"[WARN] LED frame failed: {exc}", flush=True)
                time.sleep(0.2)
            self._phase += 0.03

    def _show(self):
        if self._pixels is None:
            return
        if self._backend in ("neopixel_spi", "rpi_ws281x"):
            self._pixels.show()

    def _set_pixel(self, i: int, r: int, g: int, b: int):
        if self._pixels is None:
            return
        r, g, b = int(r), int(g), int(b)
        if self._backend == "neopixel_spi":
            self._pixels[i] = (r, g, b)
        elif self._backend == "rpi_ws281x":
            self._pixels.setPixelColor(i, self._Color(r, g, b))

    def _clear(self):
        if self._pixels is None:
            return
        for i in range(self.count):
            self._set_pixel(i, 0, 0, 0)
        self._show()

    def _draw_rainbow(self):
        if self._pixels is None:
            return
        for i in range(self.count):
            hue = (self._phase * 0.35 + i / max(1, self.count)) % 1.0
            r, g, b = colorsys.hsv_to_rgb(hue, 1.0, 0.55)
            self._set_pixel(i, r * 255, g * 255, b * 255)
        self._show()

    def _draw_pulse_blue(self):
        if self._pixels is None:
            return
        level = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(self._phase * 4.0))
        b = int(255 * level)
        g = int(40 * level)
        for i in range(self.count):
            self._set_pixel(i, 0, g, b)
        self._show()

    def _draw_solid(self, color):
        if self._pixels is None:
            return
        r, g, b = color
        level = 0.65 + 0.35 * (0.5 + 0.5 * math.sin(self._phase * 2.0))
        for i in range(self.count):
            self._set_pixel(i, r * level, g * level, b * level)
        self._show()


# ---------------------------------------------------------------------------
# Сервер
# ---------------------------------------------------------------------------
def sync_with_server():
    """Повертає (active_student_name, students_list)."""
    global server_connected
    try:
        response = requests.get(f"{SERVER_URL}/api/pi/sync", timeout=2)
        if response.status_code == 200:
            payload = response.json()
            student_name = str(payload.get("active_student_name", "None"))
            students = payload.get("students") or []
            server_connected = True
            print(f"[SYNC] active_student_name={student_name}", flush=True)
            return student_name, students
        server_connected = False
        print(f"[WARN] Server responded with {response.status_code}", flush=True)
        return "None", []
    except requests.RequestException as exc:
        server_connected = False
        print(f"[ERROR] Server disconnected: {exc}", flush=True)
        return "None", []


def next_student_on_server():
    """Подвійний клік: наступний учень, синхронізовано з бекендом."""
    global server_connected
    try:
        response = requests.post(f"{SERVER_URL}/api/pi/next_student", timeout=3)
        if response.status_code == 200:
            payload = response.json()
            server_connected = True
            name = str(payload.get("active_student_name", "None"))
            print(f"[SYNC] next student → {name}", flush=True)
            return name
        print(f"[WARN] next_student status={response.status_code}", flush=True)
        name, _ = sync_with_server()
        return name
    except requests.RequestException as exc:
        print(f"[ERROR] next_student failed: {exc}", flush=True)
        name, _ = sync_with_server()
        return name


def capture_photo(cap):
    for _ in range(5):
        cap.grab()

    ret, frame = cap.read()
    if not ret or frame is None:
        print("[ERROR] Failed to capture image from camera", flush=True)
        return None

    success, buffer = cv2.imencode(".png", frame)
    if not success:
        print("[ERROR] Failed to encode camera frame", flush=True)
        return None

    return buffer.tobytes()


def handle_photo(lcd, current_student, cap, leds: LedStrip, beeper: Beeper):
    """Один клік — знімок і відправка на сервер з ім'ям поточного учня."""
    print("[ACTION] Single click — capture & upload", flush=True)
    beeper.photo_start()

    if current_student == "None" or not current_student:
        print("[WARN] No student selected; photo ignored", flush=True)
        update_lcd(lcd, "Choose student", "2x click next")
        beeper.error()
        return current_student

    if not server_connected:
        print("[WARN] Server disconnected; cannot analyze", flush=True)
        update_lcd(lcd, "System fail", "Code 301")
        beeper.error()
        return current_student

    leds.set_processing()
    show_student(lcd, current_student, "Taking photo...")
    time.sleep(0.2)

    photo_bytes = capture_photo(cap)
    if photo_bytes is None:
        update_lcd(lcd, "Analysis fail", "Code 201")
        beeper.error()
        leds.set_standby()
        return current_student

    show_student(lcd, current_student, "Waiting AI...")

    try:
        response = requests.post(
            f"{SERVER_URL}/upload/",
            params={"student_name": current_student},
            files={"file": ("work.png", photo_bytes, "image/png")},
            timeout=35,
        )
        print(f"[HTTP] upload status={response.status_code}", flush=True)

        if response.status_code != 200:
            print(f"[ERROR] Upload failed: {response.text}", flush=True)
            update_lcd(lcd, "Analysis fail", f"Code {response.status_code}")
            beeper.error()
            leds.set_standby()
            return current_student

        payload = response.json()
        if payload.get("status") != "success":
            update_lcd(lcd, "Analysis fail", "Code 501")
            beeper.error()
            leds.set_standby()
            return current_student

        pct = int(payload.get("ai_percent", 0))
        pct = max(0, min(100, pct))
        leds.set_result(pct)

        if pct < 25:
            beeper.result_ok()
        elif pct < 75:
            beeper.result_warn()
        else:
            beeper.result_bad()

        update_lcd(lcd, current_student[:16], f"{pct}% AI")
        print(f"[RESULT] ai_percent={pct}%", flush=True)
        time.sleep(RESULT_HOLD_S)
        leds.set_standby()
        show_student(lcd, current_student)
    except requests.RequestException as exc:
        print(f"[ERROR] Network failure during upload: {exc}", flush=True)
        update_lcd(lcd, "Analysis fail", "Code 500")
        beeper.error()
        leds.set_standby()

    return current_student


# ---------------------------------------------------------------------------
# Одна фізична кнопка: single click / double click
# ---------------------------------------------------------------------------
def wait_button_gesture(button: Button, already_pressed: bool = False) -> str:
    """
    Повертає 'single' або 'double'.
    already_pressed=True — перший клік уже зафіксовано в головному циклі.
    """
    if not already_pressed:
        button.wait_for_press()

    # дочекатися відпускання першого кліку
    if button.is_pressed:
        button.wait_for_release(timeout=3.0)

    # вікно очікування другого кліку
    deadline = time.monotonic() + DOUBLE_CLICK_WINDOW_S
    while time.monotonic() < deadline:
        if button.is_pressed:
            button.wait_for_release(timeout=3.0)
            return "double"
        time.sleep(0.01)

    return "single"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------
def main():
    print("[SYSTEM] Raspberry Pi started", flush=True)
    update_lcd(None, "Welcome", "")

    try:
        # LCD I2C PCF8574 @ 0x27 — SDA GPIO2, SCL GPIO3, VCC 5V, GND
        lcd = CharLCD("PCF8574", LCD_I2C_ADDRESS, port=1, cols=16, rows=2)
        print("[SYSTEM] LCD detected", flush=True)
    except Exception as exc:
        print(f"[WARN] LCD not found: {exc}", flush=True)
        lcd = None
        update_lcd(lcd, "System fail", "Code 101")
        return

    beeper = Beeper(BUZZER_PIN)
    leds = LedStrip(LED_PIN, LED_COUNT)
    leds.start()
    leds.set_standby()
    beeper.beep(0.05, 2, 0.05)

    update_lcd(lcd, "Welcome", "")
    time.sleep(0.8)
    update_lcd(lcd, "Starting", "system")
    time.sleep(1.0)
    update_lcd(lcd, "System OK", "Ready")
    time.sleep(0.8)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[ERROR] Camera could not open", flush=True)
        update_lcd(lcd, "System fail", "Code 202")
        beeper.error()
        leds.stop()
        beeper.close()
        return

    cap.set(3, 640)
    cap.set(4, 480)

    # Фізична кнопка: S→GPIO 17, V→3.3V, G→GND; внутрішній pull_up (active-LOW)
    try:
        button = Button(
            BUTTON_PIN,
            pull_up=True,
            bounce_time=0.05,
        )
        print(f"[SYSTEM] Button on GPIO {BUTTON_PIN} (pull_up)", flush=True)
    except Exception as exc:
        print(f"[ERROR] Button failed: {exc}", flush=True)
        update_lcd(lcd, "System fail", "Code 102")
        beeper.error()
        leds.stop()
        beeper.close()
        cap.release()
        return

    current_student = "None"
    last_sync = 0.0

    try:
        while True:
            now = time.time()
            if now - last_sync >= SYNC_INTERVAL_S:
                current_student, _ = sync_with_server()
                last_sync = time.time()
                show_student(lcd, current_student)

            if not button.is_pressed:
                time.sleep(0.05)
                continue

            beeper.click()
            gesture = wait_button_gesture(button, already_pressed=True)

            if gesture == "double":
                print("[ACTION] Double click — next student", flush=True)
                beeper.next_student()
                current_student = next_student_on_server()
                last_sync = time.time()
                show_student(lcd, current_student, "Selected")
                time.sleep(0.25)
                show_student(lcd, current_student)
            else:
                current_student = handle_photo(
                    lcd, current_student, cap, leds, beeper
                )
                last_sync = time.time()
                show_student(lcd, current_student)

            while button.is_pressed:
                time.sleep(0.05)
            time.sleep(0.15)

    except KeyboardInterrupt:
        print("[SYSTEM] Interrupted", flush=True)
    finally:
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
