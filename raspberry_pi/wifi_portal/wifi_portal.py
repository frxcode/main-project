#!/usr/bin/env python3
"""
Автономний captive-portal для налаштування Wi-Fi на Raspberry Pi.

Логіка:
  1. При старті чекаємо, чи NetworkManager підключиться до збереженої мережі.
  2. Якщо інтернету немає — піднімаємо точку доступу `Verifier-Setup`
     (192.168.4.1) і виводимо SSID + IP на I2C LCD (0x27).
  3. Усі DNS-запити клієнтів ведуть на портал (dnsmasq-shared.d),
     телефон/ноутбук сам відкриває сторінку вибору мережі.
  4. Після введення SSID/пароля — вимикаємо AP, підключаємось як клієнт,
     показуємо нову IP-адресу на LCD. Якщо не вдалося — повертаємо AP.

Працює повністю локально, без зовнішніх серверів. Потрібен root (порт 80, nmcli).
"""

import os
import re
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

AP_SSID = "Verifier-Setup"
AP_CON_NAME = "verifier-setup-ap"
AP_IP = "192.168.4.1"
WIFI_IFACE = "wlan0"
PORTAL_PORT = 80
BOOT_WAIT_SECONDS = 45      # скільки чекати збережену мережу при старті
CONNECT_WAIT_SECONDS = 35   # скільки чекати підключення до нової мережі
LCD_ADDR = 0x27

# ----------------------------- LCD (I2C 0x27) -----------------------------
try:
    from RPLCD.i2c import CharLCD
except ImportError:
    CharLCD = None

_lcd = None
_last_lines = ("", "")


def init_lcd():
    global _lcd
    if CharLCD is None:
        print("[LCD] RPLCD не встановлено, працюю без екрана", flush=True)
        return
    try:
        _lcd = CharLCD("PCF8574", LCD_ADDR, cols=16, rows=2)
    except Exception as exc:
        print(f"[LCD] Не вдалося ініціалізувати LCD: {exc}", flush=True)
        _lcd = None


def lcd(line1, line2=""):
    """Вивід на 16x2 LCD (лише латиниця — HD44780 не знає кирилиці)."""
    global _last_lines
    l1, l2 = str(line1)[:16], str(line2)[:16]
    if (l1, l2) == _last_lines:
        return
    _last_lines = (l1, l2)
    print(f"[LCD] {l1} | {l2}", flush=True)
    if _lcd is None:
        return
    try:
        _lcd.clear()
        _lcd.cursor_pos = (0, 0)
        _lcd.write_string(l1)
        _lcd.cursor_pos = (1, 0)
        _lcd.write_string(l2)
    except Exception as exc:
        print(f"[LCD] Помилка виводу: {exc}", flush=True)


# ----------------------------- nmcli helpers -----------------------------
def run(args, timeout=30):
    return subprocess.run(
        args, capture_output=True, text=True, timeout=timeout
    )


def is_connected():
    """Чи є робоча мережа: NM connectivity, підключений інтерфейс або ping.

    Важливо: локальна мережа без інтернету теж вважається робочою,
    щоб портал не розривав з'єднання зі шкільним сервером.
    """
    r = run(["nmcli", "-t", "-f", "CONNECTIVITY", "general"])
    if r.returncode == 0 and r.stdout.strip() == "full":
        return True
    # Будь-який ethernet/wifi у стані connected (крім нашої власної AP)
    r = run(["nmcli", "-t", "-f", "DEVICE,TYPE,STATE,CONNECTION", "dev"])
    for line in (r.stdout or "").splitlines():
        parts = line.split(":")
        if len(parts) < 4:
            continue
        device, dev_type, state, connection = parts[0], parts[1], parts[2], parts[3]
        if dev_type not in ("wifi", "ethernet"):
            continue
        if state == "connected" and connection != AP_CON_NAME:
            return True
    r = run(["ping", "-c", "1", "-W", "2", "8.8.8.8"], timeout=6)
    return r.returncode == 0


def wait_for_connection(timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if is_connected():
            return True
        time.sleep(3)
    return False


def get_ip(iface=WIFI_IFACE):
    r = run(["ip", "-4", "addr", "show", iface])
    m = re.search(r"inet (\d+\.\d+\.\d+\.\d+)", r.stdout or "")
    if m:
        return m.group(1)
    r = run(["hostname", "-I"])
    parts = (r.stdout or "").split()
    return parts[0] if parts else "?"


def scan_networks():
    """Список мереж [(ssid, signal, secured)] — скануємо ДО підняття AP."""
    run(["nmcli", "dev", "wifi", "rescan"], timeout=20)
    time.sleep(3)
    r = run(["nmcli", "-t", "-f", "SIGNAL,SECURITY,SSID", "dev", "wifi", "list"])
    seen, result = {}, []
    for line in (r.stdout or "").splitlines():
        parts = re.split(r"(?<!\\):", line, maxsplit=2)
        if len(parts) != 3:
            continue
        signal_s, security, ssid = parts
        ssid = ssid.replace("\\:", ":").strip()
        if not ssid or ssid == AP_SSID:
            continue
        try:
            signal = int(signal_s)
        except ValueError:
            signal = 0
        if ssid not in seen or seen[ssid] < signal:
            seen[ssid] = signal
    for ssid, signal in sorted(seen.items(), key=lambda kv: -kv[1]):
        result.append({"ssid": ssid, "signal": signal})
    return result


def start_ap():
    run(["nmcli", "con", "delete", AP_CON_NAME])
    r = run([
        "nmcli", "con", "add",
        "type", "wifi",
        "ifname", WIFI_IFACE,
        "con-name", AP_CON_NAME,
        "autoconnect", "no",
        "ssid", AP_SSID,
        "802-11-wireless.mode", "ap",
        "802-11-wireless.band", "bg",
        "ipv4.method", "shared",
        "ipv4.addresses", f"{AP_IP}/24",
    ])
    if r.returncode != 0:
        print(f"[AP] add failed: {r.stderr}", flush=True)
        return False
    r = run(["nmcli", "con", "up", AP_CON_NAME], timeout=40)
    if r.returncode != 0:
        print(f"[AP] up failed: {r.stderr}", flush=True)
        return False
    print("[AP] Точка доступу піднята", flush=True)
    return True


def stop_ap():
    run(["nmcli", "con", "down", AP_CON_NAME])
    run(["nmcli", "con", "delete", AP_CON_NAME])


def connect_wifi(ssid, password):
    """Підключення як клієнт. Повертає (ok, ip_or_error)."""
    run(["nmcli", "dev", "wifi", "rescan"], timeout=20)
    time.sleep(3)
    cmd = ["nmcli", "dev", "wifi", "connect", ssid, "ifname", WIFI_IFACE]
    if password:
        cmd += ["password", password]
    r = run(cmd, timeout=60)
    if r.returncode != 0:
        run(["nmcli", "con", "delete", ssid])  # прибрати невдалий профіль
        return False, (r.stderr or r.stdout or "nmcli error").strip()
    if wait_for_connection(CONNECT_WAIT_SECONDS):
        return True, get_ip()
    # Асоціація пройшла, але інтернету немає — лишаємо профіль, повідомляємо
    return True, get_ip()


# ----------------------------- HTTP-портал -----------------------------
STATE = {
    "networks": [],       # кеш сканування (зроблений до підняття AP)
    "busy": False,
    "last_error": "",
}

PAGE = """<!DOCTYPE html>
<html lang="uk">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Налаштування Wi-Fi — Verifier</title>
<style>
  :root {{ --brand:#4f6ef7; --text:#0f1b2d; --soft:#5a6b82; --border:#e6ebf3; }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,"Segoe UI",Roboto,sans-serif; background:#f5f7fb;
         color:var(--text); min-height:100vh; display:flex; align-items:center;
         justify-content:center; padding:16px; }}
  .card {{ width:100%; max-width:420px; background:#fff; border:1px solid var(--border);
          border-radius:20px; padding:28px; box-shadow:0 20px 50px rgba(15,27,45,.12); }}
  h1 {{ font-size:20px; margin-bottom:4px; }}
  p.sub {{ color:var(--soft); font-size:14px; margin-bottom:20px; }}
  .net {{ display:flex; justify-content:space-between; align-items:center; padding:12px 14px;
         border:1px solid var(--border); border-radius:12px; margin-bottom:8px; cursor:pointer;
         font-size:15px; }}
  .net:hover {{ border-color:var(--brand); background:#eef2ff; }}
  .sig {{ color:var(--soft); font-size:13px; }}
  label {{ display:block; font-size:13px; font-weight:600; color:var(--soft); margin:14px 0 6px; }}
  input {{ width:100%; padding:12px 14px; border:1px solid #d6deea; border-radius:12px;
          font-size:15px; outline:none; }}
  input:focus {{ border-color:var(--brand); box-shadow:0 0 0 4px rgba(79,110,247,.15); }}
  button {{ width:100%; margin-top:18px; padding:14px; border:0; border-radius:12px;
           background:var(--brand); color:#fff; font-size:15px; font-weight:700; cursor:pointer; }}
  button:hover {{ background:#3a56e0; }}
  .err {{ background:#fdecec; border:1px solid #f6c9ca; color:#cf3338; font-size:13px;
         padding:10px 12px; border-radius:10px; margin-bottom:14px; }}
  .empty {{ color:var(--soft); font-size:14px; padding:10px 0; }}
</style>
</head>
<body>
<div class="card">
  <h1>Wi-Fi для перевірника</h1>
  <p class="sub">Оберіть шкільну мережу та введіть пароль. Після підключення
  IP-адреса з'явиться на екрані пристрою.</p>
  {error}
  <div id="nets">{networks}</div>
  <form method="POST" action="/connect">
    <label for="ssid">Назва мережі (SSID)</label>
    <input id="ssid" name="ssid" required placeholder="Назва Wi-Fi">
    <label for="password">Пароль</label>
    <input id="password" name="password" type="password" placeholder="Залиште порожнім, якщо мережа відкрита">
    <button type="submit">Підключити</button>
  </form>
</div>
<script>
  document.querySelectorAll('.net').forEach(el => el.onclick = () => {{
      document.getElementById('ssid').value = el.dataset.ssid;
      document.getElementById('password').focus();
  }});
</script>
</body>
</html>"""

CONNECTING_PAGE = """<!DOCTYPE html>
<html lang="uk"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Підключення…</title></head>
<body style="font-family:sans-serif;display:flex;align-items:center;justify-content:center;min-height:100vh;background:#f5f7fb;">
<div style="max-width:420px;text-align:center;padding:30px;">
  <h2>Підключаюсь до «{ssid}»…</h2>
  <p style="color:#5a6b82;">Мережа <b>Verifier-Setup</b> зараз зникне.<br>
  Результат і нову IP-адресу дивіться на LCD-екрані пристрою.<br>
  Якщо пароль невірний — мережа <b>Verifier-Setup</b> з'явиться знову.</p>
</div></body></html>"""


def html_escape(s):
    return (s.replace("&", "&amp;").replace("<", "&lt;")
             .replace(">", "&gt;").replace('"', "&quot;"))


def render_index():
    nets = STATE["networks"]
    if nets:
        items = "".join(
            f'<div class="net" data-ssid="{html_escape(n["ssid"])}">'
            f'<span>{html_escape(n["ssid"])}</span>'
            f'<span class="sig">{n["signal"]}%</span></div>'
            for n in nets
        )
    else:
        items = '<p class="empty">Мереж не знайдено — введіть назву вручну.</p>'
    error = f'<div class="err">{html_escape(STATE["last_error"])}</div>' if STATE["last_error"] else ""
    return PAGE.format(networks=items, error=error)


def apply_credentials(ssid, password):
    """Фоновий потік: AP вниз → клієнт → при невдачі AP назад."""
    STATE["busy"] = True
    STATE["last_error"] = ""
    lcd("Connecting to:", ssid)
    stop_ap()
    ok, info = connect_wifi(ssid, password)
    if ok:
        print(f"[WIFI] Підключено, IP={info}", flush=True)
        lcd("WiFi OK", f"IP:{info}")
        time.sleep(2)
        os._exit(0)  # портал завершено — далі працює основний сервіс
    print(f"[WIFI] Невдача: {info}", flush=True)
    STATE["last_error"] = f"Не вдалося підключитися до «{ssid}». Перевірте пароль."
    lcd("WiFi FAIL", "Try again")
    time.sleep(2)
    start_ap()
    lcd(AP_SSID, f"IP:{AP_IP}")
    STATE["busy"] = False


class PortalHandler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    def _send_html(self, html, code=200):
        body = html.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _redirect_to_portal(self):
        self.send_response(302)
        self.send_header("Location", f"http://{AP_IP}/")
        self.end_headers()

    def do_GET(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        # Captive-detection: будь-який чужий хост/шлях → редірект на портал
        if host != AP_IP or self.path not in ("/", "/index.html"):
            self._redirect_to_portal()
            return
        self._send_html(render_index())

    def do_POST(self):
        if self.path != "/connect":
            self._redirect_to_portal()
            return
        length = int(self.headers.get("Content-Length") or 0)
        form = parse_qs(self.rfile.read(length).decode("utf-8"))
        ssid = (form.get("ssid", [""])[0]).strip()
        password = form.get("password", [""])[0]
        if not ssid:
            self._send_html(render_index())
            return
        if not STATE["busy"]:
            threading.Thread(
                target=apply_credentials, args=(ssid, password), daemon=True
            ).start()
        self._send_html(CONNECTING_PAGE.format(ssid=html_escape(ssid)))


# ----------------------------- main -----------------------------
def main():
    if os.geteuid() != 0:
        print("Запустіть від root (sudo): потрібні порт 80 та nmcli.", file=sys.stderr)
        sys.exit(1)

    init_lcd()
    lcd("Verifier boot", "Checking WiFi...")

    if wait_for_connection(BOOT_WAIT_SECONDS):
        ip = get_ip()
        print(f"[BOOT] Інтернет є, IP={ip} — портал не потрібен", flush=True)
        lcd("WiFi OK", f"IP:{ip}")
        return

    print("[BOOT] Збереженої мережі немає — запускаю портал", flush=True)
    lcd("Scanning WiFi...", "")
    STATE["networks"] = scan_networks()  # скан ДО підняття AP (в AP-режимі не можна)

    if not start_ap():
        lcd("AP ERROR", "See logs")
        sys.exit(1)

    lcd(AP_SSID, f"IP:{AP_IP}")

    server = ThreadingHTTPServer(("0.0.0.0", PORTAL_PORT), PortalHandler)
    print(f"[PORTAL] http://{AP_IP}:{PORTAL_PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        stop_ap()


if __name__ == "__main__":
    main()
