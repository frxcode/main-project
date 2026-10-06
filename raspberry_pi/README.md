# МАН — Raspberry Pi: перевірка робіт на ШІ

Проєкт для зйомки учнівських робіт кнопкою на Raspberry Pi з виводом статусу на LCD 16x2. Фото зберігаються у папку `images/` для подальшої перевірки на ознаки ШІ.

## Структура проєкту

```
main-project/
├── images/              # Збережені фото (.jpg)
└── raspberry_pi/
    ├── main.py          # Основна програма
    └── README.md        # Ця інструкція
```

## Необхідне обладнання

| Компонент                    | Примітка                            |
|------------------------------|-------------------------------------|
| Raspberry Pi 3/4/5           | Raspberry Pi OS (Bookworm/Bullseye) |
| LCD 1602 + I2C модуль        | PCF8574, адреса `0x27`              |
| Модульна кнопка (G, V, S)    | 3 виводи                            |
| USB веб-камера               | Через OpenCV                        |
| Дроти Dupont                 | Для підключення                     |

---

## Схема підключення (BCM нумерація)

> У коді використовується **BCM** (Broadcom GPIO numbers), не Physical/Board.

### Зведена таблиця

| Пристрій              | Сигнал | BCM пін | Physical пін | Примітка        |
|-----------------------|--------|---------|--------------|-----------------|
| Кнопка                | S      | **17**  | 11           | Signal          |
| Кнопка                | V      | —       | 1            | 3.3V            |
| Кнопка                | G      | —       | GND          | Земля           |
| LCD I2C               | SDA    | **2**   | 3            | Шина I2C1       |
| LCD I2C               | SCL    | **3**   | 5            | Шина I2C1       |
| LCD I2C               | VCC    | —       | 4            | 5V              |
| LCD I2C               | GND    | —       | 6            | Земля           |
| Веб-камера            | USB    | —       | USB-порт     | `VideoCapture(0)` |

### 1. Модульна кнопка (G, V, S) — BCM 17

```
Raspberry Pi              Кнопка (модуль)
─────────────             ───────────────
GND                 ────► G (Ground)
3.3V (Pin 1)        ────► V (VCC)
GPIO 17 (Pin 11)    ────► S (Signal)
```

У коді: `button = Button(17)`.

### 2. LCD 16x2 через I2C (PCF8574)

```
Raspberry Pi              LCD I2C модуль
─────────────             ──────────────
GPIO 2 / SDA (Pin 3) ───► SDA
GPIO 3 / SCL (Pin 5) ───► SCL
5V (Pin 4)           ───► VCC
GND (Pin 6)          ───► GND
```

У коді: `lcd = CharLCD('PCF8574', 0x27, port=1, cols=16, rows=2)`.

Перевірка адреси I2C:

```bash
sudo apt install -y i2c-tools
sudo raspi-config   # Interface Options → I2C → Enable
sudo i2cdetect -y 1
```

### 3. Веб-камера

Підключіть USB-камеру до будь-якого USB-порту. OpenCV: `cv2.VideoCapture(0)`.

```bash
ls /dev/video*
```

---

## Встановлення залежностей

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3-pip python3-venv i2c-tools libatlas-base-dev
sudo raspi-config nonint do_i2c 0

cd ~/Desktop/main-project   # або ваш шлях
python3 -m venv .venv
source .venv/bin/activate

pip install --upgrade pip
pip install gpiozero RPLCD smbus2 opencv-python
```

### Короткий список pip

```bash
pip install gpiozero RPLCD smbus2 opencv-python
```

| Пакет            | Призначення                   |
|------------------|-------------------------------|
| `gpiozero`       | Кнопка                        |
| `RPLCD`          | LCD 16x2 через I2C            |
| `smbus2`         | I2C-шина для RPLCD            |
| `opencv-python`  | Веб-камера та збереження .jpg |

> На деяких образах зручніше: `sudo apt install -y python3-opencv`  
> тоді: `python3 -m venv --system-site-packages .venv`

---

## Запуск

```bash
cd ~/Desktop/main-project/raspberry_pi
source ../.venv/bin/activate   # якщо використовували venv
python3 main.py
```

### Логіка роботи

1. Старт → ініціалізація LCD, перевірка камери.
2. Якщо камери немає → на LCD: `Camera Error`.
3. Якщо все добре → `Camera OK` / `System Ready`.
4. Очікування натискання кнопки.
5. Після натискання:
   - LCD: `Taking photo...`
   - Знімок → `images/photo_YYYYMMDD_HHMMSS.jpg`
   - LCD: `Photo saved!`
   - Повернення в `System Ready`
6. Вихід: `Ctrl+C`

---

## Обробка помилок

- LCD недоступний → повідомлення в термінал
- Камера відсутня → `Camera Error` на LCD; при зйомці — `Photo Error`
- Кнопка / GPIO недоступні → коректний вихід з повідомленням

---

## Автор / призначення

Проєкт підготовлено для **МАН** (Мала академія наук) — апаратна частина стенду для фіксації учнівських робіт перед подальшим аналізом на ознаки використання ШІ.
