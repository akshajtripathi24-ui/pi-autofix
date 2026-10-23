#!/bin/bash
# Makes Pi AutoFix start automatically whenever the Pi boots,
# and restart by itself if it ever crashes.
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"

sudo tee /etc/systemd/system/pi-autofix.service > /dev/null << EOF
[Unit]
Description=Pi AutoFix
After=network-online.target
Wants=network-online.target

[Service]
User=$USER
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py
Restart=always
RestartSec=10
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

sudo systemctl daemon-reload
sudo systemctl enable pi-autofix
sudo systemctl restart pi-autofix
IP="$(hostname -I | awk '{print $1}')"
echo
echo "Done! Pi AutoFix now runs in the background and starts on boot."
echo "Open:          http://$IP:8001"
echo "See its log:   journalctl -u pi-autofix -f"
echo "Stop it:       sudo systemctl stop pi-autofix"
echo "Turn off auto: sudo systemctl disable --now pi-autofix"
