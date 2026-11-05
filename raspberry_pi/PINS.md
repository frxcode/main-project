# Raspberry Pi pinout (BCM)

Numbers in code are **BCM** (Broadcom), not physical board numbers.  
Source of truth: `main.py` (`LED_PIN`, `BUTTON_PIN`, `LCD_I2C_ADDRESS`).

## Use these pins

| Device | Signal | BCM | Physical | Notes |
|--------|--------|-----|----------|-------|
| Button | S | **17** | 11 | `pull_up=True` |
| Button | V | — | 1 | 3.3V |
| Button | G | — | 6 / 9 / 14… | GND |
| LCD I2C | SDA | **2** | 3 | I2C1 |
| LCD I2C | SCL | **3** | 5 | I2C1 |
| LCD I2C | VCC | — | 4 | 5V |
| LCD I2C | GND | — | 6 | GND |
| SK6812 data | DIN | **18** | 12 | PWM0 (primary) |
| SK6812 data (alt) | DIN | **12** | 32 | same PWM0 waveform |
| SK6812 data (alt) | DIN | **13** | 33 | PWM1, same frame |
| SK6812 | 5V / GND | — | 2 / 6 | strip power (external 5V OK) |
| Camera | USB | — | USB | `VideoCapture(0)` |

LCD backpack address: **`0x27`** (PCF8574), I2C port `1`.

PWM note: keep `dtparam=audio=off` and do not let firmware hold the PWM clock, or the LED strip will glitch.

## 40-pin header (used pins marked)

```
                         3V3  [ 1] [ 2]  5V
                   SDA / GPIO2  [ 3] [ 4]  5V          ← LCD SDA / LCD VCC
                   SCL / GPIO3  [ 5] [ 6]  GND         ← LCD SCL / GND
                         GPIO4  [ 7] [ 8]  GPIO14
                          GND  [ 9] [10]  GPIO15
                 BUTTON  GPIO17 [11] [12]  GPIO18  LED ← button / SK6812 DIN
                        GPIO27 [13] [14]  GND
                        GPIO22 [15] [16]  GPIO23
                         3V3  [17] [18]  GPIO24
                        GPIO10 [19] [20]  GND
                         GPIO9 [21] [22]  GPIO25
                        GPIO11 [23] [24]  GPIO8
                          GND [25] [26]  GPIO7
                         GPIO0 [27] [28]  GPIO1
                         GPIO5 [29] [30]  GND
                         GPIO6 [31] [32]  GPIO12  LED  ← SK6812 alt (PWM0)
                 LED    GPIO13 [33] [34]  GND         ← SK6812 alt (PWM1)
                        GPIO19 [35] [36]  GPIO16
                        GPIO26 [37] [38]  GPIO20
                          GND [39] [40]  GPIO21
```

## Wiring sketches

### Button

```
Pi                          Button module
──                          ─────────────
GND                    →    G
3.3V (phys 1)          →    V
GPIO17 (phys 11)       →    S
```

### LCD 16×2 (I2C)

```
Pi                          LCD I2C
──                          ───────
GPIO2 / SDA (phys 3)   →    SDA
GPIO3 / SCL (phys 5)   →    SCL
5V (phys 4)            →    VCC
GND (phys 6)           →    GND
```

### SK6812 strip

```
Pi                          SK6812
──                          ──────
GPIO18 (phys 12)       →    DIN   (preferred)
  or GPIO12 (phys 32)  →    DIN   (same PWM0)
  or GPIO13 (phys 33)  →    DIN   (PWM1 mirror)
5V / external 5V       →    +5V
GND                    →    GND   (common ground with Pi)
```

Code drives **18 + 12 + 13** together so the data wire can sit on any of those three pads.

## Quick check

```bash
sudo i2cdetect -y 1          # expect 27 for LCD
pinctrl get 17               # button
pinctrl get 12 13 18         # LED PWM pads
```
