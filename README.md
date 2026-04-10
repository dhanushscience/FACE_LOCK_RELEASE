# Face Lock Authentication System

A facial recognition-based attendance and authentication system running on Raspberry Pi with touchscreen display.

## � Quick Start Commands

```bash
# Start application as service (recommended)
sudo systemctl start face-lock.service

# Stop application service
sudo systemctl stop face-lock.service

# Check service status
systemctl status face-lock.service

# Start application manually (alternative)
cd /home/kavacha/Downloads/app && ./start-face-lock.sh

# Stop manual application
sudo pkill -f face_lock_tk.py

# Check if running
ps aux | grep face_lock_tk.py | grep -v grep

# View service logs
sudo journalctl -u face-lock.service -f

# Enable auto-start on boot
sudo systemctl enable face-lock.service

# Disable auto-start
sudo systemctl disable face-lock.service

# Disable screen sleep (run once)
cd /home/kavacha/Downloads/app && ./disable-sleep.sh && sudo reboot
```

## �📋 Table of Contents
- [Features](#features)
- [System Requirements](#system-requirements)
- [Installation](#installation)
- [Configuration](#configuration)
- [Auto-Start Setup](#auto-start-setup)
- [Usage](#usage)
- [Application Management](#application-management)
- [File Structure](#file-structure)
- [Troubleshooting](#troubleshooting)

## ✨ Features

### User Features
- **Facial Recognition Login/Logout**: Automatic attendance tracking via face recognition
- **Real-time Camera Feed**: Live preview with timestamp and status display
- **Idle Screen**: Analog clock display when inactive
- **Auto-timeout Protection**: Returns to main screen after 15 seconds of inactivity in admin menu

### Admin Features
- **Member Registration**: Register new faces with ID and role assignment
- **Member Management**: Edit or delete existing members
- **Attendance Logs**: View individual attendance history
- **Face Recapture**: Update existing member photos
- **Role Management**: Assign Admin or Member roles
- **Google Sheets Integration**: Automatic sync with cloud spreadsheet

### Technical Features
- **Auto-restart on Crash**: Systemd service ensures reliability
- **RAM Usage Logging**: Monitors memory consumption every 5 minutes
- **Touch-optimized UI**: Designed for 480x320 touchscreen displays
- **Persistent Camera Thread**: Efficient background processing

## 🖥️ System Requirements

### Hardware
- Raspberry Pi 3/4/5
- Pi Camera Module (v1, v2, or HQ)
- 3.5" - 7" Touchscreen Display (480x320 or higher)
- Minimum 2GB RAM recommended

### Software
- Raspberry Pi OS (Bullseye or later)
- Python 3.9+
- X Server (for GUI display)

## 📦 Installation

### 1. Clone or Copy Files
```bash
cd /home/kavacha/Downloads/app
```

### 2. Set Up Virtual Environment
```bash
python3 -m venv venv
source venv/bin/activate
```

### 3. Install System Dependencies
```bash
sudo apt-get update
sudo apt-get install -y python3-dev python3-pip libcamera-dev
sudo apt-get install -y libatlas-base-dev libhdf5-dev libhdf5-serial-dev
sudo apt-get install -y libjasper-dev libqtgui4 libqt4-test
sudo apt-get install -y libcairo2-dev pkg-config
```

### 4. Install Python Packages
```bash
pip install numpy
pip install opencv-python
pip install face-recognition
pip install picamera2
pip install gspread oauth2client
pip install Pillow
pip install cairosvg
```

### 5. Enable Camera
```bash
sudo raspi-config
# Navigate to: Interface Options -> Camera -> Enable
sudo reboot
```

### 6. Disable Screen Sleep/Blanking (Important!)
```bash
cd /home/kavacha/Downloads/app
./disable-sleep.sh
sudo reboot
```

This prevents the screen from going to sleep during operation.

### 7. Hide Boot CLI (Optional - Kiosk Mode)
To boot directly to the application without showing console messages:

```bash
# Backup current boot configuration
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak

# Edit cmdline.txt to add quiet splash parameters
# Change 'console=tty1' to 'console=tty3'
# Add: quiet splash logo.nologo vt.global_cursor_default=0

# Disable login prompt on main display
sudo systemctl mask getty@tty1.service

# Reboot to apply
sudo reboot
```

**What this does:**
- Redirects boot messages to tty3 (not visible on display)
- Hides Raspberry Pi logo and boot text
- Hides cursor
- Disables login prompt
- Your app appears immediately after boot

## ⚙️ Configuration

### 1. Directory Structure
Ensure the following directories exist:
```bash
mkdir -p /home/kavacha/Downloads/app/known_faces
mkdir -p /home/kavacha/Downloads/app/face_recognition_models
```

### 2. Google Sheets Setup
1. Create a Google Cloud Project
2. Enable Google Sheets API and Google Drive API
3. Create a Service Account and download JSON credentials
4. Save credentials as: `/home/kavacha/Face_rec/service_account.json`
5. Share your Google Sheet with the service account email
6. Update `SHEET_URL` in `face_lock_tk.py` with your sheet URL

### 3. Configuration Variables
Edit these paths in `face_lock_tk.py` if needed:
```python
KNOWN_FACES_DIR = "/home/kavacha/Downloads/app/known_faces"
SUPERUSER_PATH  = "/home/kavacha/Downloads/app/superusers.txt"
SERVICE_ACCOUNT_JSON = "/home/kavacha/Face_rec/service_account.json"
SHEET_URL = "https://docs.google.com/spreadsheets/d/YOUR_SHEET_ID/edit"
LOGO_PATH = "/home/kavacha/Downloads/app/Vector.svg"
```

### 4. Master Password
Default admin password: `Admin@Kyros`
(Can be changed in `MASTER_PASSWORD` variable)

## 🚀 Auto-Start Setup

The application is configured to start automatically at boot using **systemd service** which ensures reliable startup and automatic restart on failure.

### Configuration Files
- **Service File**: `/etc/systemd/system/face-lock.service`
- **Startup Script**: `/home/kavacha/Downloads/app/start-face-lock.sh`
- **Service Logs**: `journalctl -u face-lock.service`

### Systemd Service File (`/etc/systemd/system/face-lock.service`)
```ini
[Unit]
Description=Face Lock Authentication System
After=graphical.target network.target
Wants=graphical.target

[Service]
Type=forking
User=root
WorkingDirectory=/home/kavacha/Downloads/app
ExecStart=/home/kavacha/Downloads/app/start-face-lock.sh
Restart=on-failure
RestartSec=10
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=graphical.target
```

### Startup Script (`start-face-lock.sh`)
```bash
#!/bin/bash
# Face Lock Startup Wrapper Script

# Wait for system to be ready
sleep 5

# Kill any existing instances
sudo pkill -9 -f face_lock_tk.py 2>/dev/null
sudo pkill -9 -f startx 2>/dev/null
sleep 2

# Clear the screen
clear > /dev/tty1 2>/dev/null

# Start X server with the application on vt1 (takes over console display)
sudo startx /home/kavacha/Downloads/app/venv/bin/python3 face_lock_tk.py -- vt1 &
```

### Boot Configuration (Kiosk Mode - Optional)

**File:** `/boot/firmware/cmdline.txt`

To hide CLI and boot directly to the app:

```bash
console=serial0,115200 console=tty3 root=PARTUUID=0a97593f-02 rootfstype=ext4 fsck.repair=yes rootwait quiet splash logo.nologo vt.global_cursor_default=0 cfg80211.ieee80211_regdom=IN ads7846.swapxy=1 ads7846.invertx=1 ads7846.inverty=1
```

**Key parameters:**
- `console=tty3` - Redirects console away from display
- `quiet` - Reduces boot messages
- `splash` - Shows splash screen instead of text
- `logo.nologo` - Hides Raspberry Pi logo
- `vt.global_cursor_default=0` - Hides cursor

### Installation Steps (Already Completed)
```bash
# 1. Make startup script executable
chmod +x /home/kavacha/Downloads/app/start-face-lock.sh

# 2. Copy service file to systemd directory
sudo cp /home/kavacha/face-lock.service /etc/systemd/system/

# 3. Reload systemd configuration
sudo systemctl daemon-reload

# 4. Enable service to start on boot
sudo systemctl enable face-lock.service

# 5. Start service now (optional)
sudo systemctl start face-lock.service

# 6. Check service status
systemctl status face-lock.service
```

### Verify Auto-Start After Reboot

After rebooting your Raspberry Pi:

```bash
# 1. Wait 10-15 seconds after boot for system to initialize

# 2. Check service status
systemctl status face-lock.service

# 3. Check if application is running
ps aux | grep face_lock_tk.py | grep -v grep

# 4. View service logs
sudo journalctl -u face-lock.service -n 50

# 5. View real-time logs
sudo journalctl -u face-lock.service -f

# 6. Manual test of startup script
cd /home/kavacha/Downloads/app
./start-face-lock.sh
```

**Expected Behavior:**
- Service is **enabled** (shows in `systemctl is-enabled face-lock.service`)
- Application starts automatically 10-15 seconds after boot
- Display shows the Face Lock interface with camera feed or clock
- No manual intervention required
- Service automatically restarts if application crashes

**Troubleshooting Auto-Start:**
See the [Troubleshooting](#troubleshooting) section below for common issues.

### Kiosk Mode (No CLI on Boot)

For a clean, appliance-like experience where only your app is visible:

**Configuration:**
1. Boot cmdline configured to hide messages (console=tty3, quiet, splash)
2. Login prompt disabled on main display (getty@tty1 masked)
3. X server runs on vt1 (takes over main console)
4. Cursor hidden globally

**Result:** Display shows only the Face Lock app from power-on to shutdown.

## 📖 Usage

### Starting the Application

#### Automatic Start (Recommended)
The application starts automatically when the Raspberry Pi boots (already configured via cron).

#### Manual Start
```bash
cd /home/kavacha/Downloads/app
./start-face-lock.sh
```

Or directly:
```bash
cd /home/kavacha/Downloads/app
sudo startx $(pwd)/venv/bin/python3 face_lock_tk.py
```

#### Stop Running Application
```bash
# Kill all instances
sudo pkill -9 -f face_lock_tk.py
sudo pkill -9 -f startx
```

### User Operations

#### Login/Logout
1. Press **LOGIN** or **LOGOUT** button on main screen
2. Look at the camera
3. System will recognize and process automatically
4. Status will be synced to Google Sheets

#### Admin Access
1. Press **MENU** button
2. Superuser face will be verified
3. Access granted to admin panel (15-second timeout)

### Admin Operations

#### Register New Member
1. Access Admin Panel
2. Click **📝 REG**
3. Enter ID (tap to open keypad)
4. Enter Name
5. Select Role (Member/Admin)
6. Position face in camera view
7. Click **SAVE**

#### Edit Member
1. Access Admin Panel
2. Click **👥 EDIT**
3. Select member from list
4. Modify ID, Name, or Role
5. Click **UPDATE** or **DELETE**

#### View Attendance Logs
1. Access Admin Panel
2. Click **📋 LOGS**
3. Select member from list
4. View date-wise login/logout times

#### Recapture Photo
1. Access Admin Panel
2. Click **📸 RECAP**
3. Select member from list
4. Position face and click **SAVE**

## 🛠️ Application Management

### Using Systemd Service (Recommended)

#### Start Application
```bash
sudo systemctl start face-lock.service
```

#### Stop Application
```bash
sudo systemctl stop face-lock.service
```

#### Restart Application
```bash
sudo systemctl restart face-lock.service
```

#### Check Status
```bash
systemctl status face-lock.service
```

#### View Logs
```bash
# View recent logs
sudo journalctl -u face-lock.service -n 50

# View real-time logs
sudo journalctl -u face-lock.service -f

# View logs since last boot
sudo journalctl -u face-lock.service -b
```

### Manage Auto-Start
```bash
# Enable auto-start on boot
sudo systemctl enable face-lock.service

# Disable auto-start
sudo systemctl disable face-lock.service

# Check if enabled
systemctl is-enabled face-lock.service
```

### Manual Start (Alternative)
```bash
# Start manually without service
cd /home/kavacha/Downloads/app
./start-face-lock.sh

# Stop manual instances
sudo pkill -f face_lock_tk.py
```

### Check If Running
```bash
# Check service status
systemctl status face-lock.service

# Check for running processes
ps aux | grep face_lock_tk.py | grep -v grep

# Check for X server
ps aux | grep startx | grep -v grep
```

### View Application Logs
```bash
# View application RAM log
tail -f /home/kavacha/Downloads/app/ram_log.txt
```

## 📁 File Structure

```
/home/kavacha/Downloads/app/
├── face_lock_tk.py              # Main application file
├── start-face-lock.sh           # Startup wrapper script (for SPI display)
├── service-control.sh           # Legacy service management script
├── disable-sleep.sh             # Disable screen blanking script
├── README.md                    # This file
├── Vector.svg                   # Logo for idle screen
├── ram_log.txt                  # RAM usage log
├── superusers.txt               # Admin user list (auto-generated)
├── venv/                        # Python virtual environment
│   └── bin/python3
├── known_faces/                 # Stored face images
│   ├── Name_ID.jpg
│   └── ...
└── face_recognition_models/     # Face recognition model files

/home/kavacha/
├── face-lock.service            # Systemd service definition
└── Face_rec/
    └── service_account.json     # Google API credentials

/boot/firmware/
├── cmdline.txt                  # Boot parameters (console=tty3, quiet, splash)
└── cmdline.txt.bak              # Backup of original boot config

/etc/systemd/system/
├── face-lock.service            # Installed systemd service file
└── getty@tty1.service           # Masked (disabled login prompt)
```

## 🐛 Troubleshooting

### Application Won't Start

**Check service status:**
```bash
systemctl status face-lock.service
```

**Check if process is running:**
```bash
ps aux | grep face_lock_tk.py | grep -v grep
```

**View service logs:**
```bash
sudo journalctl -u face-lock.service -n 50
```

**Manual start for testing:**
```bash
cd /home/kavacha/Downloads/app
./start-face-lock.sh
```

**Common issues:**
- **Camera busy**: Another process is using the camera. Solution:
  ```bash
  sudo pkill -f face_lock_tk.py
  sudo systemctl restart face-lock.service
  ```
- **Camera not enabled**: Run `sudo raspi-config` → Interface Options → Camera
- **X Server not starting**: Check if SPI display drivers are loaded
- **Startup script not executable**: Run `chmod +x /home/kavacha/Downloads/app/start-face-lock.sh`
- **Service file issues**: Verify service file exists:
  ```bash
  ls -l /etc/systemd/system/face-lock.service
  ```

### Application Won't Start at Boot

**Check if service is enabled:**
```bash
systemctl is-enabled face-lock.service
```

If it says "disabled", enable it:
```bash
sudo systemctl enable face-lock.service
```

**Check service status after boot:**
```bash
systemctl status face-lock.service
sudo journalctl -u face-lock.service -b
```

**Test startup script manually:**
```bash
cd /home/kavacha/Downloads/app
./start-face-lock.sh
```

**Reinstall service if needed:**
```bash
sudo cp /home/kavacha/face-lock.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable face-lock.service
sudo systemctl start face-lock.service
```

### Camera Not Working

```bash
# Test camera
libcamera-hello

# Check camera detection
vcgencmd get_camera

# Restart camera service
sudo systemctl restart camera
```

### Face Recognition Not Accurate

1. Ensure good lighting conditions
2. Position face 1-2 feet from camera
3. Look directly at camera during registration
4. Recapture face photos if needed
5. Adjust tolerance in code (default: 0.45)

### Google Sheets Not Syncing

1. Verify service account JSON path
2. Check internet connection
3. Ensure sheet is shared with service account email
4. Check logs: `./service-control.sh logs`

### Admin Menu Timeout Issues

- Default timeout: 15 seconds of inactivity
- Any button press resets the timer
- Timeout applies across all admin pages
- Modify `timeout_ms=15000` in code to adjust (value in milliseconds)

### High RAM Usage

Check RAM log:
```bash
tail -f /home/kavacha/Downloads/app/ram_log.txt
```

RAM is logged every 5 minutes automatically.

### CLI Visible on Boot (Kiosk Mode Not Working)

If you see console messages during boot:

**Check boot configuration:**
```bash
cat /boot/firmware/cmdline.txt
```

Should contain: `console=tty3 quiet splash logo.nologo vt.global_cursor_default=0`

**Restore kiosk mode:**
```bash
# Backup current config
sudo cp /boot/firmware/cmdline.txt /boot/firmware/cmdline.txt.bak

# Get your PARTUUID
PARTUUID=$(grep -oP 'PARTUUID=\K[^ ]+' /boot/firmware/cmdline.txt.bak | head -1)

# Apply kiosk configuration
echo "console=serial0,115200 console=tty3 root=PARTUUID=$PARTUUID rootfstype=ext4 fsck.repair=yes rootwait quiet splash logo.nologo vt.global_cursor_default=0 [your other params]" | sudo tee /boot/firmware/cmdline.txt

# Disable getty
sudo systemctl mask getty@tty1.service

# Reboot
sudo reboot
```

**Revert to normal (show CLI):**
```bash
sudo cp /boot/firmware/cmdline.txt.bak /boot/firmware/cmdline.txt
sudo systemctl unmask getty@tty1.service
sudo reboot
```

### Screen Goes to Sleep

If screen blanking occurs despite running the disable-sleep script:

**Quick fix (temporary):**
```bash
DISPLAY=:0 xset s off
DISPLAY=:0 xset -dpms
DISPLAY=:0 xset s noblank
```

**Permanent fix:**
```bash
cd /home/kavacha/Downloads/app
./disable-sleep.sh
sudo reboot
```

**Check if disable-blanking service is running:**
```bash
sudo systemctl status disable-blanking.service
```

## 📊 Google Sheets Format

Expected columns in your Google Sheet:

| Date (A) | ID (B) | Name (C) | In Time (D) | Out Time (E) |
|----------|---------|----------|-------------|--------------|
| 05-02-2026 | 123 | John Doe | 09:00 | 17:30 |
| 05-02-2026 | 456 | Jane Smith | 08:45 | 18:00 |

- **Date Format**: DD-MM-YYYY
- **Time Format**: HH:MM (24-hour)
- ID matching is performed on Column B only

## 🔒 Security Notes

1. Service runs as root for camera/hardware access
2. Master password stored in plain text (modify `MASTER_PASSWORD` in code)
3. Superuser list stored in `superusers.txt`
4. Google credentials stored in JSON file (keep secure)
5. Application runs in fullscreen kiosk mode with no cursor

## 📝 Admin Timeout Behavior

- **Timeout Duration**: 15 seconds
- **Applies To**: All admin pages (MENU, REG, EDIT, LOGS, RECAP)
- **Reset On**: Any button click, list selection, or keypad interaction
- **Action**: Returns to main screen with idle clock display
- **Purpose**: Prevents unauthorized access if admin walks away

## 🎨 UI Customization

Colors and fonts can be modified in the `COLORS` and `FONT_*` dictionaries:

```python
COLORS = {
    "bg": "#0f0f0f",
    "panel": "#1a1a1a",
    "login": "#27ae60",
    "logout": "#c0392b",
    # ... more colors
}

FONT_MAIN = ("Segoe UI", 12)
FONT_BTN_LARGE = ("Segoe UI", 16, "bold")
```

## 📞 Support

For issues or questions:
1. Check logs: `./service-control.sh logs`
2. Review troubleshooting section
3. Check RAM usage: `cat ram_log.txt`
4. Verify camera: `libcamera-hello`

## 🔄 Updates and Maintenance

### Updating the Application
```bash
# Stop the service
sudo systemctl stop face-lock.service

# Make your changes to face_lock_tk.py

# Restart the service
sudo systemctl restart face-lock.service

# View logs to verify
sudo journalctl -u face-lock.service -f
```

### Backing Up Data
```bash
# Backup face database
cp -r /home/kavacha/Downloads/app/known_faces ~/backup_faces_$(date +%Y%m%d)

# Backup superuser list
cp /home/kavacha/Downloads/app/superusers.txt ~/backup_superusers_$(date +%Y%m%d).txt
```

## 📜 Version History

- **v1.2** - Current release with systemd service + kiosk mode
  - **Kiosk mode boot** - Direct to app with no CLI visible
  - Boot splash screen configuration (quiet, logo.nologo)
  - Console redirection to tty3
  - Cursor hiding (vt.global_cursor_default=0)
  - **Systemd service autostart** for reliable boot initialization
  - Automatic restart on failure
  - Centralized logging via journalctl
  - Service management commands
  - Improved camera resource handling
  - X server on vt1 takes over console display
  - 15-second admin timeout
  - RAM usage logging every 5 minutes

- **v1.1** - Cron-based autostart
  - Cron @reboot autostart
  - Startup wrapper script using `startx` command
  - Desktop autostart fallback

- **v1.0** - Initial release
  - Facial recognition login/logout
  - Google Sheets integration
  - Admin panel with member management

---

**Application Location**: `/home/kavacha/Downloads/app/`  
**Auto-Start Method**: Systemd service + startx (for SPI display)  
**Service File**: `/etc/systemd/system/face-lock.service`  
**Startup Script**: `/home/kavacha/Downloads/app/start-face-lock.sh`  
**Last Updated**: February 5, 2026
