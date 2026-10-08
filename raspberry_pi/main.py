#!/usr/bin/env python3
import sys
import time
import requests
from datetime import datetime
from pathlib import Path

# --- ВАЖЛИВО: ВПИШИ ТУТ IP СВОГО НОУТБУКА ---
SERVER_URL = "http://192.168.X.X:8000/upload/"

def init_lcd():
    try:
        from RPLCD.i2c import CharLCD
        lcd = CharLCD("PCF8574", 0x27, port=1, cols=16, rows=2)
        lcd.clear()
        return lcd
    except Exception as e:
        print(f"[WARN] LCD недоступний: {e}")
        return None

def lcd_write(lcd, line1="", line2=""):
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
    try:
        from gpiozero import Button
        button = Button(17)
        return button
    except Exception as e:
        print(f"[WARN] Кнопка недоступна: {e}")
        return None

def take_photo_to_memory(cap, cv2_module):
    if cap is None or cv2_module is None:
        raise RuntimeError("Камера не ініціалізована")

    ret, frame = cap.read()
    if not ret or frame is None:
        raise RuntimeError("Не вдалося зчитати кадр з камери")

    # Кодуємо кадр у формат PNG
    success, buffer = cv2_module.imencode('.png', frame)
    if not success:
        raise RuntimeError("Не вдалося закодувати фото")
        
    return buffer.tobytes()

def cleanup(lcd, cap):
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
    print("[INFO] Режим: відправка фото (PNG) відразу на сервер")

    lcd = init_lcd()
    lcd_write(lcd, "Starting...", "Please wait")

    cap, cv2_module = init_camera()
    if cap is None:
        lcd_write(lcd, "Camera Error", "Check webcam")
        print("[ERROR] Камера не знайдена або недоступна")
    else:
        lcd_write(lcd, "Camera OK", "System Ready")
        print("[INFO] Камера OK")
        time.sleep(1.5)
        lcd_write(lcd, "System Ready", "Press button")

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
                time.sleep(0.5)
                continue

            print("\n[INFO] Кнопку натиснуто — зйомка...")
            lcd_write(lcd, "Taking photo...", "Please wait")

            try:
                time.sleep(0.2)
                
                photo_bytes = take_photo_to_memory(cap, cv2_module)
                
                lcd_write(lcd, "Sending...", "Please wait")
                print(f"[INFO] Відправляємо на сервер {SERVER_URL}...")
                
                try:
                    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                    # Генеруємо назву файлу з розширенням .png
                    filename = f"work_{timestamp}.png"
                    
                    # Відправляємо байти з MIME-типом image/png
                    response = requests.post(
                        SERVER_URL, 
                        files={'file': (filename, photo_bytes, 'image/png')}, 
                        timeout=10
                    )
                    
                    if response.status_code == 200:
                        lcd_write(lcd, "Sent OK!", "Success")
                        print(f"[INFO] Успішно відправлено! Відповідь: {response.json()}")
                    else:
                        lcd_write(lcd, "Server Error", f"Code {response.status_code}")
                        print(f"[ERROR] Помилка сервера: {response.status_code}")
                except requests.exceptions.RequestException as e:
                    lcd_write(lcd, "Server Error", "No Connection")
                    print(f"[ERROR] Помилка з'єднання: {e}")

            except Exception as e:
                print(f"[ERROR] Зйомка не вдалася: {e}")
                lcd_write(lcd, "Photo Error", "Try again")

            time.sleep(2.5)
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