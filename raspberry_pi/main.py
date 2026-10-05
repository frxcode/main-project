#!/usr/bin/env python3
"""
МАН — Raspberry Pi: перевірка робіт на ШІ
Фотографування робіт кнопкою з виводом статусу на LCD 16x2.
"""

import os
import sys
import time
from datetime import datetime
from pathlib import Path

# --- Шлях до папки images (на рівень вище від raspberry_pi/) ---
BASE_DIR = Path(__file__).resolve().parent.parent
IMAGES_DIR = BASE_DIR / "images"
IMAGES_DIR.mkdir(parents=True, exist_ok=True)

# --- Піни BCM ---
BUTTON_PIN = 17
LED_GREEN = 22
LED_YELLOW = 27
LED_RED = 18

# --- I2C адреса LCD (типово 0x27 або 0x3F) ---
LCD_I2C_ADDRESS = 0x27


def init_lcd():
    """Ініціалізація LCD 16x2 через I2C (RPLCD)."""
    try:
        from RPLCD.i2c import CharLCD

        lcd = CharLCD(
            i2c_expander="PCF8574",
            address=LCD_I2C_ADDRESS,
            port=1,
            cols=16,
            rows=2,
            charmap="A00",
            auto_linebreaks=True,
        )
        lcd.clear()
        return lcd
    except Exception as e:
        print(f"[WARN] LCD недоступний: {e}")
        return None


def lcd_write(lcd, line1="", line2=""):
    """Безпечний вивід на LCD (2 рядки по 16 символів)."""
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
    """Перевірка та відкриття вебкамери через OpenCV."""
    try:
        import cv2

        cap = cv2.VideoCapture(0)
        if not cap.isOpened():
            return None, None
        # Коротка перевірка кадру
        ret, _ = cap.read()
        if not ret:
            cap.release()
            return None, None
        return cap, cv2
    except Exception as e:
        print(f"[WARN] Камера недоступна: {e}")
        return None, None


def init_gpio():
    """Ініціалізація кнопки та світлодіодів через gpiozero."""
    try:
        from gpiozero import Button, LED

        button = Button(BUTTON_PIN, pull_up=True, bounce_time=0.1)
        led_green = LED(LED_GREEN)
        led_yellow = LED(LED_YELLOW)
        led_red = LED(LED_RED)
        return button, led_green, led_yellow, led_red
    except Exception as e:
        print(f"[WARN] GPIO недоступний: {e}")
        return None, None, None, None


def blink_leds(led_green, led_yellow, led_red, duration=0.3):
    """Коротке запалювання всіх світлодіодів для перевірки."""
    leds = [led for led in (led_green, led_yellow, led_red) if led is not None]
    if not leds:
        return
    try:
        for led in leds:
            led.on()
        time.sleep(duration)
        for led in leds:
            led.off()
        # Послідовне мигання: зелений → жовтий → червоний
        for led in leds:
            led.on()
            time.sleep(0.15)
            led.off()
    except Exception as e:
        print(f"[WARN] Помилка світлодіодів: {e}")


def take_photo(cap, cv2_module):
    """Зробити знімок і зберегти в images/ з timestamp-назвою."""
    if cap is None or cv2_module is None:
        raise RuntimeError("Камера не ініціалізована")

    ret, frame = cap.read()
    if not ret or frame is None:
        raise RuntimeError("Не вдалося зчитати кадр з камери")

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    filename = IMAGES_DIR / f"photo_{timestamp}.jpg"
    ok = cv2_module.imwrite(str(filename), frame)
    if not ok:
        raise RuntimeError(f"Не вдалося зберегти файл: {filename}")
    return filename


def cleanup(lcd, cap, leds):
    """Коректне звільнення ресурсів при виході."""
    print("\n[INFO] Завершення роботи...")
    if leds:
        for led in leds:
            if led is not None:
                try:
                    led.off()
                except Exception:
                    pass
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
        # Продовжуємо без камери — цикл все одно чекатиме кнопку,
        # але знімок видасть помилку на LCD
    else:
        lcd_write(lcd, "Camera OK", "System Ready")
        print("[INFO] Камера OK")
        time.sleep(1.5)
        lcd_write(lcd, "System Ready", "Press button")

    # --- GPIO ---
    button, led_green, led_yellow, led_red = init_gpio()
    leds = (led_green, led_yellow, led_red)

    if button is None:
        lcd_write(lcd, "GPIO Error", "Check wiring")
        print("[ERROR] GPIO (кнопка/LED) недоступний. Вихід.")
        cleanup(lcd, cap, leds)
        sys.exit(1)

    # Зелений — система готова
    try:
        if led_green is not None:
            led_green.on()
    except Exception:
        pass

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

            # Жовтий — процес зйомки
            try:
                if led_green is not None:
                    led_green.off()
                if led_yellow is not None:
                    led_yellow.on()
            except Exception:
                pass

            lcd_write(lcd, "Taking photo...", "Please wait")

            try:
                # Невелика пауза для стабілізації автоекспозиції
                time.sleep(0.2)
                path = take_photo(cap, cv2_module)
                print(f"[INFO] Фото збережено: {path}")

                lcd_write(lcd, "Photo saved!", path.name[:16])
                blink_leds(led_green, led_yellow, led_red)

            except Exception as e:
                print(f"[ERROR] Зйомка не вдалася: {e}")
                lcd_write(lcd, "Photo Error", "Try again")
                try:
                    if led_red is not None:
                        led_red.on()
                        time.sleep(1.0)
                        led_red.off()
                except Exception:
                    pass

            # Повернення в режим очікування
            time.sleep(1.0)
            lcd_write(lcd, "System Ready", "Press button")
            try:
                if led_yellow is not None:
                    led_yellow.off()
                if led_green is not None:
                    led_green.on()
            except Exception:
                pass

            # Антидребезг / уникнення повторного спрацювання
            try:
                button.wait_for_release(timeout=2)
            except Exception:
                pass
            time.sleep(0.3)

    except KeyboardInterrupt:
        pass
    finally:
        cleanup(lcd, cap, leds)
        print("[INFO] Програму завершено.")


if __name__ == "__main__":
    main()
