#!/bin/bash
xset s off
xset -dpms
xset s noblank
xsetroot -solid black
exec /home/ps/Downloads/app/venv/bin/python3 /home/ps/Downloads/app/face_lock_tk.py
