#!/usr/bin/env bash
# Встановлення автономного Wi-Fi captive-порталу на Raspberry Pi.
# Запуск: sudo bash install.sh   (з папки wifi_portal)
set -e

if [ "$(id -u)" -ne 0 ]; then
    echo "Запустіть через sudo: sudo bash install.sh"
    exit 1
fi

cd "$(dirname "$0")"

echo "==> 1/6 Пакети..."
apt-get update
apt-get install -y network-manager python3-pip i2c-tools

echo "==> 2/6 Увімкнення I2C..."
raspi-config nonint do_i2c 0 || true

echo "==> 3/6 Python-бібліотека LCD..."
pip3 install --break-system-packages RPLCD smbus2 2>/dev/null \
    || pip3 install RPLCD smbus2

echo "==> 4/6 Копіювання порталу..."
install -d /opt/wifi-portal
install -m 755 wifi_portal.py /opt/wifi-portal/wifi_portal.py

echo "==> 5/6 Captive DNS (усі запити в режимі AP -> 192.168.4.1)..."
install -d /etc/NetworkManager/dnsmasq-shared.d
cat > /etc/NetworkManager/dnsmasq-shared.d/90-captive.conf <<'EOF'
# Captive portal: у режимі точки доступу всі домени ведуть на портал
address=/#/192.168.4.1
EOF

echo "==> 6/6 Служба systemd..."
install -m 644 wifi-portal.service /etc/systemd/system/wifi-portal.service
systemctl daemon-reload
systemctl enable wifi-portal.service

echo
echo "Готово! Перевірка без перезавантаження:"
echo "  sudo systemctl start wifi-portal && journalctl -u wifi-portal -f"
echo "Або просто:  sudo reboot"
