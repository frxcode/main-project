#!/usr/bin/env python3
"""
МАН — Raspberry Pi: перевірка робіт на ШІ
Фотографування робіт кнопкою з виводом статусу на LCD 16x2.
"""

import sys
import time
from datetime import datetime
from pathlib import Path

# Папка images/ — на рівень вище від raspberry_pi/
BASE_DIR = Path(__file__).resolve().parent.parent
IMAGES_DIR = BASE_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)


def init_lcd():
    """
    LCD 16x2 через I2C (PCF8574, адреса 0x27).

    Підключення:
      SDA -> GPIO 2 (Pin 3)
      SCL -> GPIO 3 (Pin 5)
      VCC -> 5V (Pin 4)
      GND -> GND (Pin 6)
    """
    try:
        from RPLCD.i2c import CharLCD

        lcd = CharLCD("PCF8574", 0x27, port=1, cols=16, rows=2)
        lcd.clear()
        return lcd
    except Exception as e:
        print(f"[WARN] LCD недоступний: {e}")
        return None


def lcd_write(lcd, line1="", line2=""):
    """Вивід на LCD (2 рядки по 16 символів). Якщо LCD немає — у термінал."""
    if lcd is None:
        print(f"[LCD] {line1} | {line2}")
        return
    try:
        lcd.clear()
        lcd.cursor_pos = (0, 0)
        lcd.write_string(str(line1)[:16])
        lcd.cursor_pos = (1, 0)
        lcd.write_string(str(line2)[:16])
    except Exception as e:
        print(f"[WARN] Помилка запису на LCD: {e}")


def init_camera():
    """Вебкамера через OpenCV: cv2.VideoCapture(0)."""
    try:
        import cv2

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None, None
        ret, _ = cap.read()
        if not ret:
            cap.release()
            return None, None
        return cap, cv2
    except Exception as e:
        print(f"[WARN] Камера недоступна: {e}")
        return None, None


def init_button():
    """
    Модульна кнопка (3 виводи: G, V, S) на BCM 17.

    Підключення:
      G (Ground) -> GND
      V (VCC)    -> 3.3V (Pin 1)
      S (Signal) -> GPIO 17 (Pin 11)
    """
    try:
        from gpiozero import Button

        # BCM 17 — сигнал кнопки (Pin 11)
        button = Button(17)
        return button
    except Exception as e:
        print(f"[WARN] Кнопка недоступна: {e}")
        return None


def take_photo(cap, cv2_module):
    """Знімок з камери → images/photo_YYYYMMDD_HHMMSS.jpg."""
    if cap is None or cv2_module is None:
        raise RuntimeError("Камера не ініціалізована")

    ret, frame = cap.read()
    if not ret or frame is None:
        raise RuntimeError("Не вдалося зчитати кадр з камери")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = IMAGES_DIR / f"photo_{timestamp}.jpg"
    if not cv2_module.imwrite(str(filename), frame):
        raise RuntimeError(f"Не вдалося зберегти файл: {filename}")
    return filename


def cleanup(lcd, cap):
    """Звільнення LCD та камери при виході."""
    print("\n[INFO] Завершення роботи...")
    if lcd is not None:
        try:
            lcd.clear()
            lcd.close()
        except Exception:
            pass
    if cap is not None:
        try:
            cap.release()
        except Exception:
            pass


def main():
    print("[INFO] Старт системи МАН — перевірка робіт на ШІ")
    print(f"[INFO] Фото зберігаються у: {IMAGES_DIR}")

    lcd = init_lcd()
    lcd_write(lcd, "Starting...", "Please wait")

    # --- Камера ---
    cap, cv2_module = init_camera()
    if cap is None:
        lcd_write(lcd, "Camera Error", "Check webcam")
        print("[ERROR] Камера не знайдена або недоступна")
    else:
        lcd_write(lcd, "Camera OK", "System Ready")
        print("[INFO] Камера OK")
        time.sleep(1.5)
        lcd_write(lcd, "System Ready", "Press button")

    # --- Кнопка ---
    button = init_button()
    if button is None:
        lcd_write(lcd, "Button Error", "Check wiring")
        print("[ERROR] Кнопка недоступна. Вихід.")
        cleanup(lcd, cap)
        sys.exit(1)

    print("[INFO] Очікування натискання кнопки (Ctrl+C для виходу)...")

    try:
        while True:
            try:
                button.wait_for_press()
            except Exception as e:
                print(f"[WARN] Помилка кнопки: {e}")
                time.sleep(0.5)
                continue

            print("[INFO] Кнопку натиснуто — зйомка...")
            lcd_write(lcd, "Taking photo...", "Please wait")

            try:
                time.sleep(0.2)  # стабілізація автоекспозиції
                path = take_photo(cap, cv2_module)
                print(f"[INFO] Фото збережено: {path}")
                lcd_write(lcd, "Photo saved!", path.name[:16])
            except Exception as e:
                print(f"[ERROR] Зйомка не вдалася: {e}")
                lcd_write(lcd, "Photo Error", "Try again")

            time.sleep(1.0)
            lcd_write(lcd, "System Ready", "Press button")

            try:
                button.wait_for_release(timeout=2)
            except Exception:
                pass
            time.sleep(0.3)

    except KeyboardInterrupt:
        pass
    finally:
        cleanup(lcd, cap)
        print("[INFO] Програму завершено.")


if __name__ == "__main__":
    main()
