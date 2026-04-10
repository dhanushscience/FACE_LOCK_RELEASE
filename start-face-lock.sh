#!/bin/bash
source /home/ps/Downloads/app/venv/bin/activate
# Face Lock Startup Wrapper Script

# Wait for system to be ready
sleep 5

# Kill any existing instances
sudo pkill -9 -f face_lock_tk.py 2>/dev/null
sudo pkill -9 -f startx 2>/dev/null
sudo pkill -9 -f Xorg 2>/dev/null
sudo pkill -9 -f unclutter 2>/dev/null
sleep 2

# Remove stale X locks
sudo rm -f /tmp/.X*-lock /tmp/.X11-unix/X* 2>/dev/null

# Clear the screen
sudo sh -c "clear > /dev/tty1" 2>/dev/null

# Change to application directory
cd /home/ps/Downloads/app

# Start X server with the application on vt1 (takes over console display)
exec startx /home/ps/Downloads/app/run_ui.sh -- :0 -br -nocursor vt1
