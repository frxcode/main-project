#!/usr/bin/env python3
import time
import requests
import cv2
from RPLCD.i2c import CharLCD
from gpiozero import Button

SERVER_URL = "http://192.168.1.90:8000"
last_line1, last_line2 = "", ""
server_connected = False


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


def sync_with_server():
    global server_connected
    try:
        response = requests.get(f"{SERVER_URL}/api/pi/sync", timeout=2)
        if response.status_code == 200:
            payload = response.json()
            student_name = payload.get("active_student_name", "None")
            server_connected = True
            print(f"[SYNC] active_student_name={student_name}", flush=True)
            return str(student_name)
        else:
            server_connected = False
            print(f"[WARN] Server responded with {response.status_code}", flush=True)
            return "None"
    except requests.RequestException as exc:
        server_connected = False
        print(f"[ERROR] Server disconnected: {exc}", flush=True)
        return "None"


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


def handle_button_press(lcd, current_student, cap):
    print("[ACTION] Button pressed", flush=True)

    if current_student == "None":
        print("[WARN] No student selected; button ignored", flush=True)
        update_lcd(lcd, "Choose student", "web")
        return

    if not server_connected:
        print("[WARN] Server disconnected; cannot analyze", flush=True)
        update_lcd(lcd, "System fail", "Code 301")
        return

    print("[ACTION] Checking AI...", flush=True)
    update_lcd(lcd, "Checking", "AI")
    time.sleep(0.5)

    photo_bytes = capture_photo(cap)
    if photo_bytes is None:
        update_lcd(lcd, "Analysis fail", "Code 201")
        return

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
            return

        payload = response.json()
        if payload.get("status") != "success":
            update_lcd(lcd, "Analysis fail", "Code 501")
            return

        pct = int(payload.get("ai_percent", 0))
        pct = max(0, min(100, pct))
        update_lcd(lcd, "Analysis done", f"{pct}% AI")
        print(f"[RESULT] ai_percent={pct}%", flush=True)
        time.sleep(2)
        update_lcd(lcd, current_student[:16], "Press button")
    except requests.RequestException as exc:
        print(f"[ERROR] Network failure during upload: {exc}", flush=True)
        update_lcd(lcd, "Analysis fail", "Code 500")


def main():
    print("[SYSTEM] Raspberry Pi started", flush=True)
    update_lcd(None, "Welcome", "")

    try:
        lcd = CharLCD("PCF8574", 0x27, port=1, cols=16, rows=2)
        print("[SYSTEM] LCD detected", flush=True)
    except Exception as exc:
        print(f"[WARN] LCD not found: {exc}", flush=True)
        lcd = None
        update_lcd(lcd, "System fail", "Code 101")
        return

    update_lcd(lcd, "Welcome", "")
    time.sleep(1.0)
    update_lcd(lcd, "Starting", "system")
    time.sleep(1.5)
    update_lcd(lcd, "System OK", "Ready")
    time.sleep(1.2)

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("[ERROR] Camera could not open", flush=True)
        update_lcd(lcd, "System fail", "Code 202")
        return

    cap.set(3, 640)
    cap.set(4, 480)

    button = Button(17, pull_up=True)
    current_student = "None"
    last_sync = 0
    button_pressed = False

    def on_button_pressed():
        nonlocal button_pressed
        button_pressed = True

    button.when_pressed = on_button_pressed

    while True:
        now = time.time()

        if now - last_sync >= 2:
            current_student = sync_with_server()
            last_sync = time.time()

        if current_student == "None":
            update_lcd(lcd, "Choose student", "web")
        elif not button_pressed:
            student_text = current_student[:16]
            update_lcd(lcd, student_text, "Press button")

        if button_pressed:
            button_pressed = False
            handle_button_press(lcd, current_student, cap)
            time.sleep(0.5)

        time.sleep(0.1)


if __name__ == "__main__":
    main()