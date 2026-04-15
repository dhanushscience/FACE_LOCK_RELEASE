#!/bin/bash
# ===================================================================
# Face Lock Kiosk - Master Installation & Initialization Script
# ===================================================================

if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (using sudo)"
  exit 1
fi

APP_DIR="$(pwd)"
USER_HOME=$(eval echo "~${SUDO_USER}")

echo "================================================="
echo "1. CLEAN UP"
echo "================================================="
touch offline_data.json registered_users.txt superusers.txt master_password.txt
mkdir -p known_faces
chown -R ${SUDO_USER}:${SUDO_USER} .

echo "================================================="
echo "2. FIXING PATHS"
echo "================================================="
sed -i "s|/home/ps/Downloads/FACE_LOCK_RELEASE|$APP_DIR|g" face_lock_tk.py
sed -i "s|/home/ps/Face_rec/service_account.json|$USER_HOME/Face_rec/service_account.json|g" face_lock_tk.py

echo "================================================="
echo "3. SILENCING BOOT PROCESS (KIOSK MODE) & DISABLE SLEEP"
echo "================================================="
grep -q "disable_splash" /boot/firmware/config.txt || echo "disable_splash=1" >> /boot/firmware/config.txt
grep -q "boot_delay" /boot/firmware/config.txt || echo "boot_delay=0" >> /boot/firmware/config.txt
echo 'console=serial0,115200 console=tty3 root=PARTUUID=7b243c12-02 rootfstype=ext4 fsck.repair=yes rootwait loglevel=0 quiet splash logo.nologo vt.global_cursor_default=0 systemd.show_status=false fbcon=map:2 plymouth.ignore-serial-consoles' > /boot/firmware/cmdline.txt

mkdir -p /etc/X11/xorg.conf.d/
cat << 'XX11XX' > /etc/X11/xorg.conf.d/10-blanking.conf
Section "ServerFlags"
    Option "BlankTime" "0"
    Option "StandbyTime" "0"
    Option "SuspendTime" "0"
    Option "OffTime" "0"
EndSection
XX11XX

echo "================================================="
echo "4. DEPENDENCIES"
echo "================================================="
apt-get update
apt-get install -y xinit xorg openbox python3-venv python3-pip python3-dev libcamera-dev python3-libcamera python3-picamera2 cmake libopenblas-dev libx11-dev libgtk-3-dev
sudo -u ${SUDO_USER} python3 -m venv ${APP_DIR}/venv --system-site-packages
${APP_DIR}/venv/bin/pip install gspread oauth2client opencv-python-headless pillow numpy cairosvg face_recognition

echo "================================================="
echo "5. DISPLAY CALIBRATION & PAD TEST"
echo "================================================="
cat << 'XCALIBX' > calib_test.py
import tkinter as tk
root = tk.Tk()
root.attributes('-fullscreen', True)
root.configure(bg='red')
lbl = tk.Label(root, text="DIAGNOSTIC MODE\nTap corners to verify padding. Tap this text to close.", font=("Arial", 20), bg='red', fg='white')
lbl.pack(expand=True)
lbl.bind("<Button-1>", lambda e: root.destroy())
root.mainloop()
XCALIBX
chown ${SUDO_USER}:${SUDO_USER} calib_test.py
su - ${SUDO_USER} -c "startx ${APP_DIR}/venv/bin/python3 ${APP_DIR}/calib_test.py -- -nocursor"
rm calib_test.py

echo "================================================="
echo "6. CAMERA HARDWARE TEST"
echo "================================================="
cat << 'XCAMX' > cam_test.py
import cv2, sys, time
from picamera2 import Picamera2
picam2 = Picamera2()
config = picam2.create_preview_configuration()
picam2.configure(config)
picam2.start()
print("Camera started. Capturing test frame in 2 seconds...")
time.sleep(2)
try:
    frame = picam2.capture_array()
    if frame is not None and frame.shape[0] > 0:
        print("[SUCCESS] Frame received correctly!")
        picam2.stop(); sys.exit(0)
except Exception as e:
    pass
picam2.stop(); sys.exit(1)
XCAMX
chown ${SUDO_USER}:${SUDO_USER} cam_test.py
su - ${SUDO_USER} -c "${APP_DIR}/venv/bin/python3 ${APP_DIR}/cam_test.py"
read -p "Did the camera initialize properly? (Y/N): " cam_check
if [[ "$cam_check" != "Y" && "$cam_check" != "y" ]]; then echo "Aborting."; exit 1; fi
rm cam_test.py

echo "================================================="
echo "7. SYSTEMD AUTOSTART DEPLOYMENT"
echo "================================================="
cat << XSERVX > /etc/systemd/system/face-lock.service
[Unit]
Description=Face Lock Authentication System
After=systemd-user-sessions.service plymouth-quit-wait.service

[Service]
Type=simple
User=${SUDO_USER}
Environment=DISPLAY=:0
WorkingDirectory=${APP_DIR}
ExecStart=/bin/bash ${APP_DIR}/start-face-lock.sh
Restart=always
RestartSec=5
StandardInput=tty
TTYPath=/dev/tty1
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
XSERVX
systemctl daemon-reload
systemctl enable face-lock
echo "INSTALL COMPLETE. REBOOTING in 3 sec..."; sleep 3; reboot
