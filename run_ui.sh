#!/bin/bash
xset s off
xset -dpms
xset s noblank
xsetroot -solid black
exec /home/ps/Downloads/FACE_LOCK_RELEASE/venv/bin/python3 /home/ps/Downloads/FACE_LOCK_RELEASE/face_lock_tk.py
