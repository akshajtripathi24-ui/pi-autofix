#!/bin/bash
# Locks down where Pi AutoFix runs the AI's code.
#
# It creates a separate user called "autofix" with no password, no home folder and no login.
# The AI's tests run as that user, so even buggy or bad code can't change or delete your files.
# It also makes your home folder private, so that user can't read your files either.
set -e
SANDBOX=autofix
ME="$(id -un)"

if id "$SANDBOX" >/dev/null 2>&1; then
  echo "User '$SANDBOX' already exists."
else
  sudo useradd --system --no-create-home --shell /usr/sbin/nologin "$SANDBOX"
  echo "Created user '$SANDBOX'."
fi

# Let you (and the AutoFix service) start programs as that user, without a password.
# "timeout" makes sure every program it runs has a time limit.
echo "$ME ALL=($SANDBOX) NOPASSWD: /usr/bin/timeout" | sudo tee /etc/sudoers.d/pi-autofix > /dev/null
sudo chmod 440 /etc/sudoers.d/pi-autofix
sudo visudo -cf /etc/sudoers.d/pi-autofix > /dev/null || { sudo rm -f /etc/sudoers.d/pi-autofix; echo "Sudo rule failed - removed it."; exit 1; }

# Only you can open your home folder
chmod 700 "$HOME"

echo
if sudo -n -u "$SANDBOX" /usr/bin/timeout 5 /usr/bin/python3 -c 'print("ok")' > /dev/null 2>&1; then
  echo "✓ Separate sandbox user works."
else
  echo "✗ Couldn't run Python as '$SANDBOX'. Is python3 installed?"; exit 1
fi
if sudo -n -u "$SANDBOX" /usr/bin/timeout 5 /usr/bin/unshare -rn /usr/bin/python3 -c 'print("ok")' > /dev/null 2>&1; then
  echo "✓ Internet is blocked for the AI's code."
else
  echo "! Couldn't block the internet for the AI's code (your Pi's system doesn't allow it). Everything else still works."
fi
echo
echo "Done! Restart AutoFix to use the sandbox:  sudo systemctl restart pi-autofix"
