import sys
import os
import threading
import concurrent.futures
import subprocess
import numpy as np
import cv2
import face_recognition
import gspread
import time
import gc
import math
import json
import socket
import urllib.request
from datetime import datetime
from oauth2client.service_account import ServiceAccountCredentials
import logging

import tkinter as tk
from tkinter import ttk, messagebox, Toplevel
from PIL import Image, ImageTk, ImageDraw, ImageFont, ImageOps
import cairosvg
import io
import requests

import ota_updater  # OTA update module

# ────────────────────────────────────────────────
# LOGGING CONFIG
# ────────────────────────────────────────────────
LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "system.log")
APPS_SCRIPT_WEBHOOK_URL = "https://script.google.com/macros/s/AKfycbwWnwwdgud-oVwQZP7Eou50Yn_DnsMHQBDVOwuZDIyETngnbFuCzdbri0K-4BSsXSO76A/exec"

logger = logging.getLogger("FaceLockSystem")
logger.setLevel(logging.DEBUG)
handler = logging.FileHandler(LOG_FILE)
formatter = logging.Formatter('%(asctime)s | %(levelname)-8s | %(message)s', datefmt='%Y-%m-%d %H:%M:%S')
handler.setFormatter(formatter)
logger.addHandler(handler)

# Also add console handler for critical errors
console_handler = logging.StreamHandler()
console_handler.setLevel(logging.WARNING)
console_handler.setFormatter(formatter)
logger.addHandler(console_handler)

# ────────────────────────────────────────────────
# CONFIG
# ────────────────────────────────────────────────
try:
    from picamera2 import Picamera2
except ImportError:
    print("Warning: Picamera2 not found. Ensure you are running on Raspberry Pi.")
    Picamera2 = None

KNOWN_FACES_DIR = "/home/ps/Downloads/FACE_LOCK_RELEASE/known_faces"
SUPERUSER_PATH  = "/home/ps/Downloads/FACE_LOCK_RELEASE/superusers.txt"
REGISTERED_USERS_PATH = "/home/ps/Downloads/FACE_LOCK_RELEASE/registered_users.txt"
OFFLINE_DATA_PATH = "/home/ps/Downloads/FACE_LOCK_RELEASE/offline_data.json"
MASTER_PASSWORD_PATH = "/home/ps/Downloads/FACE_LOCK_RELEASE/master_password.txt"
SERVICE_ACCOUNT_JSON = "/home/ps/Face_rec/service_account.json"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1NuuD5GTKc-PYY2teu_HDyDDBnnldIvSP23ZGfWHxBDE/edit"
LOGO_PATH = "/home/ps/Downloads/FACE_LOCK_RELEASE/Vector.svg"

os.makedirs(KNOWN_FACES_DIR, exist_ok=True)

sheet = None
client = None
creds = None
network_connected = False
offline_queue = []

# Load offline queue from file if exists
if os.path.exists(OFFLINE_DATA_PATH):
    try:
        with open(OFFLINE_DATA_PATH, 'r') as f:
            offline_queue = json.load(f)
        print(f"Loaded {len(offline_queue)} offline entries")
    except Exception as e:
        print(f"Error loading offline data: {e}")
        offline_queue = []

def check_network_connectivity():
    """Check if network is available by testing connection to Google DNS"""
    global network_connected
    try:
        # Single fast check — DNS reachability is sufficient
        socket.create_connection(("8.8.8.8", 53), timeout=2)
        network_connected = True
        return True
    except:
        network_connected = False
        return False

def safe_sheet_call(func, *args, timeout_sec=10, default=None, **kwargs):
    """Run a Google Sheets API call with a timeout. Returns default on failure."""
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
            future = executor.submit(func, *args, **kwargs)
            return future.result(timeout=timeout_sec)
    except concurrent.futures.TimeoutError:
        print(f"Sheet call timed out after {timeout_sec}s: {func.__name__}")
        return default
    except Exception as e:
        print(f"Sheet call failed: {func.__name__}: {e}")
        return default

def save_offline_queue():
    """Save offline queue to JSON file"""
    try:
        with open(OFFLINE_DATA_PATH, 'w') as f:
            json.dump(offline_queue, f, indent=2)
    except Exception as e:
        print(f"Error saving offline queue: {e}")

def sync_offline_data():
    """Sync offline data to Google Sheets when connection is restored"""
    global offline_queue, sheet
    if not offline_queue or not sheet:
        return
    
    print(f"Syncing {len(offline_queue)} offline entries...")
    synced_entries = []
    
    for entry in offline_queue[:]:  # Create a copy to iterate
        try:
            date_str = entry['date']
            eid = entry['id']
            name = entry['name']
            time_str = entry['time']
            action = entry['action']
            
            # Fetch all records with timeout
            records = safe_sheet_call(sheet.get_all_values, timeout_sec=15, default=None)
            if records is None:
                print("Sheet unreachable during sync, will retry later")
                break
            found = False
            
            for idx, row in enumerate(records[1:], start=1):
                if len(row) < 5:
                    continue
                
                row_id = str(row[1]).strip()
                if row_id == eid and row[0] == date_str:
                    found = True
                    existing_in = row[3] if len(row) > 3 else ""
                    existing_out = row[4] if len(row) > 4 else ""
                    
                    if action == "LOGIN" and (not existing_in or existing_in == "00:00"):
                        sheet.update_cell(idx + 1, 4, time_str)
                        synced_entries.append(entry)
                        print(f"Synced LOGIN: {name} at {time_str}")
                    elif action == "LOGOUT" and existing_in and (not existing_out or existing_out == "00:00"):
                        sheet.update_cell(idx + 1, 5, time_str)
                        synced_entries.append(entry)
                        print(f"Synced LOGOUT: {name} at {time_str}")
                    else:
                        synced_entries.append(entry)  # Already synced or invalid
                    break
            
            if not found and action == "LOGIN":
                # Create new row
                new_row = [date_str, eid, name, time_str, "00:00"]
                sheet.append_row(new_row)
                synced_entries.append(entry)
                print(f"Synced new entry: {name} at {time_str}")
                
        except Exception as e:
            print(f"Error syncing entry: {e}")
            break  # Stop syncing if there's an error
    
    # Remove synced entries from queue
    for entry in synced_entries:
        offline_queue.remove(entry)
    
    save_offline_queue()
    print(f"Sync complete. {len(offline_queue)} entries remaining")

try:
    scope = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive"
    ]
    creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_JSON, scope)
    client = gspread.authorize(creds)
    sheet = client.open_by_url(SHEET_URL).worksheet("Attendance")
    check_network_connectivity()
    if network_connected:
        print("Connected to Google Sheets")
        # Sync any pending offline data
        threading.Thread(target=sync_offline_data, daemon=True).start()
except Exception as e:
    print("Google Sheets offline:", e)
    check_network_connectivity()

# ────────────────────────────────────────────────
# STYLING
# ────────────────────────────────────────────────
COLORS = {
    "bg": "#0f0f0f",
    "panel": "#1a1a1a",
    "input_bg": "#222222",
    "text": "white",
    "login": "#27ae60",
    "logout": "#c0392b",
    "more": "#7f8c8d",
    "admin_btn": "#2c3e50",
    "save": "#2980b9",
    "cancel": "#555555",
    "camera_border": "#333333"
}

FONT_MAIN = ("Segoe UI", 12)
FONT_BOLD = ("Segoe UI", 12, "bold")
FONT_BTN_LARGE = ("Segoe UI", 16, "bold")
FONT_BTN_MED = ("Segoe UI", 14, "bold")

# ────────────────────────────────────────────────
# CAMERA THREAD
# ────────────────────────────────────────────────
class CameraWorker(threading.Thread):
    def __init__(self):
        super().__init__()
        self.running = True
        self.active = False 
        self.is_registering = False
        
        self.current_frame = None
        self.frame_lock = threading.Lock()
        
        self.latest_status = ("INITIALIZING...", (0, 220, 220))
        self.latest_user = "Unknown"
        self.new_frame_available = False

        if Picamera2:
            self.picam2 = Picamera2()
            config = self.picam2.create_preview_configuration(
                main={"format": 'BGR888', "size": (320, 240)},
                buffer_count=2, 
                controls={} 
            )
            self.picam2.configure(config)
            # Delay camera start to avoid X server conflicts during initialization
            self._camera_started = False
        else:
            self.picam2 = None 
            self._camera_started = True  # Mark as "started" so it doesn't block

        self.superusers = []
        self.known_encodings = []
        self.known_names = []
        threading.Thread(target=self.load_faces, daemon=True).start()
    
    def start_camera(self):
        """Start the camera hardware - call after X server is stable"""
        if self.picam2 and not self._camera_started:
            self.picam2.start()
            time.sleep(0.5)
            self._camera_started = True
            print("Camera started")

    def load_faces(self):
        print("Loading faces...")
        # Build new lists locally, then swap atomically to avoid race conditions
        new_encodings = []
        new_names = []
        if not os.path.exists(KNOWN_FACES_DIR):
             os.makedirs(KNOWN_FACES_DIR)
        for fn in os.listdir(KNOWN_FACES_DIR):
            # Skip legacy deleted photos (they may still exist on disk)
            if fn.startswith('*(DELETED)'):
                continue
            if fn.lower().endswith((".jpg", ".jpeg")):
                path = os.path.join(KNOWN_FACES_DIR, fn)
                try:
                    img = face_recognition.load_image_file(path)
                    encs = face_recognition.face_encodings(img)
                    if encs:
                        # Convert: "Ananya S_1.jpg" -> "Ananya S|1"
                        name_without_ext = os.path.splitext(fn)[0]
                        if '_' in name_without_ext:
                            # Split from right to handle names with underscores
                            # Clean up spaces that might exist around underscore
                            parts = name_without_ext.rsplit('_', 1)
                            if len(parts) == 2:
                                name_part = parts[0].strip()
                                id_part = parts[1].strip()
                                formatted_name = f"{name_part}|{id_part}"
                            else:
                                formatted_name = name_without_ext.strip()
                        else:
                            formatted_name = name_without_ext.strip()
                        new_encodings.append(encs[0])
                        new_names.append(formatted_name)
                except: pass
        # Atomic swap — camera thread always sees consistent pair of lists
        self.known_encodings = new_encodings
        self.known_names = new_names
        if os.path.exists(SUPERUSER_PATH):
            with open(SUPERUSER_PATH, "r") as f:
                # Store simple list of strings
                self.superusers = [line.strip() for line in f if line.strip()]
        print(f"Faces loaded: {len(self.known_names)}")

    def remove_face(self, combined_name):
        """Immediately remove a user from in-memory encoding lists (thread-safe atomic swap)"""
        try:
            if combined_name in self.known_names:
                idx = self.known_names.index(combined_name)
                new_names = list(self.known_names)
                new_encodings = list(self.known_encodings)
                new_names.pop(idx)
                new_encodings.pop(idx)
                # Atomic swap
                self.known_encodings = new_encodings
                self.known_names = new_names
                print(f"Immediately removed face: {combined_name}")
        except Exception as e:
            print(f"Error removing face from memory: {e}")

    def run(self):
        count = 0
        gc_counter = 0
        while self.running:
            try:
                # Wait for camera to be started
                if not getattr(self, '_camera_started', False):
                    time.sleep(0.1)
                    continue
                    
                frame = None
                if self.picam2:
                    frame = self.picam2.capture_array()
                
                if frame is None:
                    time.sleep(0.01)
                    continue

                if not self.active:
                    time.sleep(0.05)
                    continue
                
                frame = cv2.flip(cv2.cvtColor(frame.copy(), cv2.COLOR_RGB2BGR), -1)

                with self.frame_lock:
                    self.current_frame = frame.copy()
                
                count += 1
                
                if not self.is_registering and count % 10 == 0:
                    small = cv2.resize(frame, (0,0), fx=0.5, fy=0.5)
                    rgb_small = cv2.cvtColor(small, cv2.COLOR_BGR2RGB)
                    locs = face_recognition.face_locations(rgb_small,number_of_times_to_upsample=2)
                    encs = face_recognition.face_encodings(rgb_small, locs)
                    user = "Unknown"
                    if encs:
                        matches = face_recognition.compare_faces(self.known_encodings, encs[0], tolerance=0.45)
                        if True in matches:
                            best = np.argmin(face_recognition.face_distance(self.known_encodings, encs[0]))
                            user = self.known_names[best]
                    
                    self.latest_user = user
                    
                    if user != "Unknown":
                        # Just show first part of name for UI cleanliness
                        display_name = user.split('|')[0].upper()
                        status = f"VERIFIED: {display_name}"
                        color = (80, 255, 80) 
                    else:
                        status = "SCANNING: NO FACE"
                        color = (0, 220, 220) 
                    
                    self.latest_status = (status, color)
                    del small, rgb_small, locs, encs
                
                gc_counter += 1
                if gc_counter >= 500:
                    gc.collect()
                    gc_counter = 0
                
                time.sleep(0.01)

            except Exception as e:
                print(f"Camera Stream Error: {e}")
                time.sleep(0.1)

    def get_current_frame(self):
        with self.frame_lock:
            if self.active and self.current_frame is not None:
                return self.current_frame.copy()
        return None

    def pause(self): 
        with self.frame_lock:
            self.active = False
            self.current_frame = None 
        self.latest_user = "Unknown"
        self.latest_status = ("SCANNING...", (0, 220, 220))

    def resume(self): 
        self.active = True

    def stop(self):
        self.running = False
        try:
            if self.picam2:
                self.picam2.stop()
                self.picam2.close()
        except: pass
        gc.collect()

# ────────────────────────────────────────────────
# TKINTER APP
# ────────────────────────────────────────────────
class FaceAuthApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.attributes("-fullscreen", True)
        self.geometry("480x320")
        self.overrideredirect(True) 
        self.configure(bg="black")
        self.config(cursor="none") 

        # --- GIF Splash Screen ---
        self._splash_active = True
        self._splash_frame = tk.Frame(self, bg="black")
        self._splash_frame.place(x=0, y=0, relwidth=1, relheight=1)
        
        self._splash_frames = []
        try:
            gif_path = "/home/ps/Downloads/FACE_LOCK_RELEASE/Splash_PS.gif"
            if os.path.exists(gif_path):
                gif_img = Image.open(gif_path)
                i = 0
                while True:
                    try:
                        gif_img.seek(i)
                        frame_rgba = gif_img.convert("RGBA")
                        self._splash_frames.append(ImageTk.PhotoImage(frame_rgba))
                        i += 1
                    except EOFError:
                        break
        except Exception as e:
            print("Error loading splash:", e)
            
        self._splash_lbl = tk.Label(self._splash_frame, bg="black")
        self._splash_lbl.pack(expand=True)
        if self._splash_frames:
            self._splash_lbl.configure(image=self._splash_frames[0])
            self.update()
        
        self._splash_idx = 0
        def animate_splash():
            if not getattr(self, '_splash_active', True): return
            if not self._splash_frames: return
            self._splash_frame.lift()
            self._splash_idx += 1
            if self._splash_idx < len(self._splash_frames):
                self._splash_lbl.configure(image=self._splash_frames[self._splash_idx])
                self.after(66, animate_splash)
            else:
                if hasattr(self, '_remove_splash_fn'):
                    self._remove_splash_fn()
            
        self.after(50, animate_splash)
        # -------------------------

        self.current_user = "Unknown"
        self.latest_frame = None
        self.status_msg = "INITIALIZING..."
        self.status_clr = (0, 220, 220)
        self.temp_msg = ""
        self.temp_msg_timeout = 0
        self.auth_action_pending = None
        self.list_mode = ""
        self.editing_full_name = ""
        self.current_view = "main"
        
        # New flag to prevent interruptions during Admin entry
        self.is_transitioning = False
        self.is_showing_idle = False
        self.in_admin_mode = False
        
        self.no_face_timer_id = None
        self.idle_timer_id = None
        self.admin_timeout_id = None
        self.member_timeout_id = None
        self.pending_admin_entry_id = None
        self.pending_member_entry_id = None
        
        # Keypad reference for cleanup
        self.active_keypad = None
        
        # Member viewing flag
        self.is_member_viewing = False
        
        # OTA update flags
        self.update_available = False
        self.latest_update_version = ""
        
        # Cache for recent login actions (to handle immediate logout)
        # Format: {user_id: {'date': date_str, 'time': time_str, 'action': 'LOGIN'}}
        self.recent_login_cache = {}
        
        # Load superusers for admin indicator in UI - use set for O(1) lookup
        self.superusers = set()
        if os.path.exists(SUPERUSER_PATH):
            try:
                with open(SUPERUSER_PATH, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line:
                            self.superusers.add(line)
                            # Also add just the name part for flexible matching
                            if '|' in line:
                                self.superusers.add(line.split('|')[0].strip())
            except Exception as e:
                print(f"Error loading superusers: {e}")
        
        # Registration state
        self.reg_captured_frame = None
        self.reg_countdown_active = False
        self.reg_countdown_value = 0
        self.reg_waiting_approval = False
        self.is_recapture_mode = False
        self.recapture_existing_photo = None

        # Hardware Additions
        try:
            from gpiozero import LED
            self.scan_led = LED(26)
            self.scan_led.off()
        except Exception as e:
            print(f"Error initializing LED on GPIO 26: {e}")
            self.scan_led = None

        print("Initializing Hardware...")
        self.worker = CameraWorker()
        self.worker.start()
        
        # Initialize registered users cache in background
        logger.info("Initializing registered users cache...")
        threading.Thread(target=self.update_registered_users_cache, daemon=True).start()
        
        # Start network monitoring thread
        self.start_network_monitor()
        
        # Start cache cleanup thread
        self.start_cache_cleanup()

        # Start Google Docs daily sync
        self.start_daily_log_sync()

        # WiFi saved-network set (populated when panel opens / after connect)
        self._saved_wifi_ssids = set()

        # Start WiFi auto-reconnect monitor
        self._start_wifi_auto_reconnect()

        self.container = tk.Frame(self, bg=COLORS["bg"])
        self.container.pack(fill="both", expand=True)
        if hasattr(self, "_splash_frame") and self._splash_active:
            self._splash_frame.lift()
        
        self.frames = {}
        
        self.init_main_ui()
        self.init_admin_menu()
        self.init_member_list()
        self.init_registration()
        self.init_view_logs()
        self.init_edit_page()
        self.init_wifi_panel()

        # Wait a minimum of 4 seconds so the GIF plays perfectly exactly once (at 66ms/frame defaults), then close
        def remove_splash():
            self._splash_active = False
            if hasattr(self, "_splash_frame") and self._splash_frame.winfo_exists():
                self._splash_frame.destroy()
            self.show_frame("main")
            self.is_showing_idle = True
            if self.worker:
                self.worker.start_camera()
            self.after(100, self.show_idle_screen)
            self.after(200, self.poll_and_update_frame)
            
        self._remove_splash_fn = remove_splash
        self.protocol("WM_DELETE_WINDOW", self.on_close)


    def start_network_monitor(self):
        """Monitor network connectivity and sync offline data when connection is restored"""
        def monitor_loop():
            global network_connected, offline_queue, sheet
            previous_state = network_connected
            update_check_counter = 0
            
            while True:
                try:
                    check_network_connectivity()
                    
                    # Check for OTA updates every 5 minutes (30 * 10s)
                    update_check_counter += 1
                    if update_check_counter >= 30 and network_connected:
                        available, version = ota_updater.check_for_updates()
                        self.update_available = available
                        if available:
                            self.latest_update_version = version
                        update_check_counter = 0
                    
                    # Check if network just came back online
                    if network_connected and not previous_state:
                    if network_connected and not previous_state:
                        print("Network restored! Starting sync...")
                        # Try to reconnect to sheet if needed
                        if sheet is None:
                            try:
                                scope = [
                                    "https://www.googleapis.com/auth/spreadsheets",
                                    "https://www.googleapis.com/auth/drive"
                                ]
                                creds = ServiceAccountCredentials.from_json_keyfile_name(
                                    SERVICE_ACCOUNT_JSON, scope)
                                client = gspread.authorize(creds)
                                sheet = client.open_by_url(SHEET_URL).worksheet("Attendance")
                            except Exception as e:
                                print(f"Failed to reconnect sheet: {e}")
                        
                        # Sync offline data
                        if offline_queue:
                            sync_offline_data()
                    
                    previous_state = network_connected
                    
                except Exception as e:
                    print(f"Network monitor error: {e}")
                
                time.sleep(10)  # Check every 10 seconds
        
        t = threading.Thread(target=monitor_loop, daemon=True)
        t.start()

    def start_daily_log_sync(self):
        """Monitors time to upload local log to Google Docs WebApp at exactly 03:00 AM"""
        def sync_loop():
            last_run_date = None
            while True:
                now = datetime.now()
                # Run at exactly 3:00 AM
                if now.hour == 3 and now.minute == 0 and last_run_date != now.date():
                    last_run_date = now.date()
                    if APPS_SCRIPT_WEBHOOK_URL and os.path.exists(LOG_FILE):
                        logger.info("Initiating daily log push to Google Docs.")
                        try:
                            with open(LOG_FILE, "r") as f:
                                log_content = f.read()
                            
                            if log_content.strip():
                                # Clear the log file immediately to prevent losing new logs
                                with open(LOG_FILE, "w") as f:
                                    f.truncate(0)
                                    
                                payload = {
                                    "timestamp": now.strftime('%Y-%m-%d %H:%M:%S'),
                                    "logs": log_content
                                }
                                response = requests.post(APPS_SCRIPT_WEBHOOK_URL, json=payload, timeout=30)
                                if response.status_code == 200:
                                    logger.info("Successfully pushed daily logs to Google Docs and cleared local file.")
                                else:
                                    # Restore if it fails
                                    with open(LOG_FILE, "a") as f:
                                        f.write(log_content)
                                    print(f"Failed to push logs: HTTP {response.status_code} - Check Google Apps Script access permissions.")
                        except Exception as e:
                            print(f"Error during daily log sync: {e}")
                
                # Check every minute
                time.sleep(60)

        t = threading.Thread(target=sync_loop, daemon=True)
        t.start()

    # ────────────────────────────────────────────────
    # NAVIGATION & CAMERA CONTROL
    # ────────────────────────────────────────────────
    def show_frame(self, name):
        print(f"[DEBUG] show_frame called for: {name}")
        self.current_view = name 
        for frame in self.frames.values():
            frame.pack_forget()
        print(f"[DEBUG] Showing frame: {name}")
        self.frames[name].pack(fill="both", expand=True)
        
        # Clear transitioning flag to allow frame updates
        self.is_transitioning = False
        print(f"[DEBUG] is_transitioning cleared")
        
        # Cancel any pending delayed entry callbacks when navigating
        if self.pending_admin_entry_id:
            self.after_cancel(self.pending_admin_entry_id)
            self.pending_admin_entry_id = None
        if self.pending_member_entry_id:
            self.after_cancel(self.pending_member_entry_id)
            self.pending_member_entry_id = None
        
        # Start admin timeout when entering admin menu
        if name == "admin":
            self.in_admin_mode = True
            self.start_admin_timeout()
            print("[DEBUG] Admin timeout started")
        elif name == "main":
            # Only cancel timeout when truly exiting to main screen
            self.in_admin_mode = False
            self.cancel_admin_timeout()
            print("[DEBUG] Returned to main")

    def activate_camera_mode(self, action_type=None):
        # Don't activate camera if splash is still showing
        if getattr(self, '_splash_active', False):
            print("[DEBUG] Ignoring camera activation - splash still active")
            return
            
        self.is_showing_idle = False
        if self.idle_timer_id:
            try:
                self.after_cancel(self.idle_timer_id)
            except tk.TclError:
                pass
            self.idle_timer_id = None

        self.auth_action_pending = action_type
        self.is_transitioning = False
        
        if action_type:
            self.status_msg = f"SCANNING FOR {action_type}..."
        else:
            self.status_msg = "SCANNING..."
            
        self.status_clr = (0, 220, 220)
        self.current_user = "Unknown"
        
        if self.worker: 
            self.worker.resume()

        # Always cancel existing timer first to prevent dangling timers
        if self.no_face_timer_id:
            self.after_cancel(self.no_face_timer_id)
            self.no_face_timer_id = None
        
        # Turn LED on every time camera is activated
        if getattr(self, 'scan_led', None):
            self.scan_led.on()

        # Only set new timer if not in registration mode
        if not self.worker.is_registering:
            self.no_face_timer_id = self.after(15000, lambda: self.deactivate_camera_mode(go_idle=True))

    def deactivate_camera_mode(self, go_idle=True):
        if self.no_face_timer_id:
            self.after_cancel(self.no_face_timer_id)
            self.no_face_timer_id = None

        if self.worker: self.worker.pause()
        self.auth_action_pending = None
        
        # Turn off LED when scanning stops
        if getattr(self, 'scan_led', None):
            self.scan_led.off()
            
        if hasattr(self, 'btn_more'):
            self.btn_more.config(text="MENU", bg=COLORS["more"], activebackground=COLORS["more"], command=self.handle_menu_click)
        
        # Always clear the is_showing_idle flag when entering admin/controlled mode
        if not go_idle:
            self.is_showing_idle = False
            # Cancel any pending idle screen timer
            if self.idle_timer_id:
                self.after_cancel(self.idle_timer_id)
                self.idle_timer_id = None
        
        if go_idle:
            self.is_showing_idle = True
            self.show_idle_screen()
        else:
            print("Camera Mode: Paused (Admin/Transitional)")

    def show_idle_screen(self):
        # Check if window still exists
        try:
            if not self.winfo_exists():
                return
        except tk.TclError:
            return
            
        w, h = 270, 310
        img = Image.new('RGB', (w, h), color='black')
        draw = ImageDraw.Draw(img)
        
        if os.path.exists(LOGO_PATH):
            try:
                if not hasattr(self, '_cached_logo'):
                    png_data = cairosvg.svg2png(url=LOGO_PATH, output_width=200, output_height=200)
                    self._cached_logo = Image.open(io.BytesIO(png_data))
                    self._cached_logo.thumbnail((200, 200), Image.Resampling.LANCZOS)
                x = (w - self._cached_logo.width) // 2
                y = -50
                if self._cached_logo.mode == 'RGBA':
                    img.paste(self._cached_logo, (x, y), self._cached_logo)
                else:
                    img.paste(self._cached_logo, (x, y))
            except Exception as e:
                print(f"Logo load error: {e}")
            
        cx, cy = w // 2, 160 
        radius = 60
        draw.ellipse((cx-radius, cy-radius, cx+radius, cy+radius), outline="white", width=2)
        
        try:
            num_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
        except:
            num_font = ImageFont.load_default()

        for i in range(1, 13):
            angle = math.radians(i * 30 - 90)
            num_x = cx + (radius - 12) * math.cos(angle)
            num_y = cy + (radius - 12) * math.sin(angle)
            draw.text((num_x, num_y), str(i), fill="red", font=num_font, anchor="mm")

        now = datetime.now()
        sec = now.second
        mnt = now.minute
        hr = now.hour % 12
        
        sec_angle = math.radians(sec * 6 - 90)
        sx = cx + (radius - 8) * math.cos(sec_angle)
        sy = cy + (radius - 8) * math.sin(sec_angle)
        draw.line((cx, cy, sx, sy), fill="#ff3333", width=1)
        
        mnt_angle = math.radians(mnt * 6 - 90)
        mx = cx + (radius - 15) * math.cos(mnt_angle)
        my = cy + (radius - 15) * math.sin(mnt_angle)
        draw.line((cx, cy, mx, my), fill="white", width=2)
        
        hr_angle = math.radians((hr * 30 + mnt * 0.5) - 90)
        hx = cx + (radius - 30) * math.cos(hr_angle)
        hy = cy + (radius - 30) * math.sin(hr_angle)
        draw.line((cx, cy, hx, hy), fill="white", width=3)
        draw.ellipse((cx-3, cy-3, cx+3, cy+3), fill="white")

        text = "Press Login/Logout"
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        except:
            font = ImageFont.load_default()
            
        text_bbox = draw.textbbox((0, 0), text, font=font)
        text_w = text_bbox[2] - text_bbox[0]
        text_x = (w - text_w) // 2
        text_y = 270
        draw.text((text_x, text_y), text, fill="white", font=font)
        
        self.update_image_label(self.cam_label, img)
        
        if self.is_showing_idle and not self.worker.active:
            # Cancel previous idle timer to prevent accumulation of parallel chains
            if self.idle_timer_id:
                try:
                    self.after_cancel(self.idle_timer_id)
                except tk.TclError:
                    pass
                self.idle_timer_id = None
            try:
                self.idle_timer_id = self.after(1000, self.show_idle_screen)
            except tk.TclError:
                pass  # Window destroyed

    def update_image_label(self, label, pil_image):
        try:
            if not self.winfo_exists():
                return
            tk_img = ImageTk.PhotoImage(pil_image)
            label.configure(image=tk_img)
            label.image = tk_img
        except tk.TclError as e:
            print(f"TclError updating image label (X server may be lost): {e}")
        except Exception as e:
            print(f"Error updating image label: {e}") 

    # ────────────────────────────────────────────────
    # LOGIC
    # ────────────────────────────────────────────────
    def handle_attendance(self, action):
        self.activate_camera_mode(action)

    def handle_pwd_login(self):
        self.deactivate_camera_mode(go_idle=False)
        if not os.path.exists(MASTER_PASSWORD_PATH):
            self._open_master_password_dialog(mode="create")
        else:
            self._open_master_password_dialog(mode="login")

    def handle_menu_click(self):
        registered_users = self.get_registered_users_from_cache()
        has_faces = len(self.worker.known_names) > 0 if self.worker else False
        
        # Allow manual password entry from menu when admin access is needed.
        self.btn_more.config(text="PWD", bg=COLORS["admin_btn"], activebackground=COLORS["admin_btn"], command=self.handle_pwd_login)

        if not registered_users and not has_faces:
            logger.info("MENU ACCESS | No users or face database, prompting Master Password")
            self.handle_pwd_login()
            return

        self.activate_camera_mode("ADMIN_CHECK")

    def handle_update(self):
        """Handle OTA update button click."""
        if not self.update_available:
            messagebox.showinfo("No Updates", "No updates available.")
            return
        if messagebox.askyesno("Update Available", f"New version {self.latest_update_version} available. Update now?"):
            # Disable admin timeout during update
            self.cancel_admin_timeout()
            # Pause camera
            if self.worker:
                self.worker.pause()
            # Show progress
            progress = Toplevel(self)
            progress.title("Updating...")
            tk.Label(progress, text="Downloading and applying update...").pack(pady=10)
            progress.geometry("300x100")
            progress.grab_set()
            # Run update in thread
            def update_thread():
                success = ota_updater.perform_update()
                progress.destroy()
                if success:
                    messagebox.showinfo("Success", "Update applied. Device will reboot.")
                    subprocess.run(["sudo", "reboot"], check=True)
                else:
                    messagebox.showerror("Error", "Update failed. Check logs.")
                    if self.worker:
                        self.worker.resume()
                    self.start_admin_timeout()  # Re-enable timeout
            threading.Thread(target=update_thread, daemon=True).start()

    def check_admin_manual_fallback(self):
        pass


    def perform_admin_entry(self, user):
        # 1. Lock the transition immediately so loop doesn't override
        self.is_transitioning = True
        display_name = user.split('|')[0]
        user_id = user.split('|')[1] if '|' in user else 'UNKNOWN'
        logger.info(f"ADMIN ENTRY | Admin {display_name} ({user_id}) entering system")

        # 2. Cancel any pending "No Face" timeouts
        if self.no_face_timer_id:
            self.after_cancel(self.no_face_timer_id)
            self.no_face_timer_id = None

        self.status_msg = f"HELLO ADMIN: {display_name}"
        self.status_clr = (0, 255, 0)
        
        # 3. Immediately transition to admin frame (reduce delay)
        if self.pending_admin_entry_id:
            self.after_cancel(self.pending_admin_entry_id)
        self.pending_admin_entry_id = self.after(300, lambda: self._complete_admin_entry())

    def _complete_admin_entry(self):
        """Complete the admin entry transition"""
        self.pending_admin_entry_id = None
        self.deactivate_camera_mode(go_idle=False)
        self.show_frame("admin")
    def perform_member_entry(self, user):
        """Handle member menu access - show their logs directly"""
        # 1. Lock the transition
        self.is_transitioning = True
        
        # 2. Cancel any pending timeouts
        if self.no_face_timer_id:
            self.after_cancel(self.no_face_timer_id)
            self.no_face_timer_id = None
        
        display_name = user.split('|')[0]
        user_id = user.split('|')[1] if '|' in user else 'UNKNOWN'
        logger.info(f"MEMBER ENTRY | Member {display_name} ({user_id}) accessing logs")
        self.status_msg = f"HELLO: {display_name}"
        self.status_clr = (0, 255, 0)
        
        # 3. Show member logs after brief delay
        if self.pending_member_entry_id:
            self.after_cancel(self.pending_member_entry_id)
        self.pending_member_entry_id = self.after(1000, lambda: self.show_member_logs(user))
    
    def start_admin_timeout(self, timeout_ms=15000):
        """Start or restart the admin menu inactivity timeout (default 15 seconds)"""
        self.cancel_admin_timeout()
        self.admin_timeout_id = self.after(timeout_ms, self.on_admin_timeout)
    
    def cancel_admin_timeout(self):
        """Cancel the admin menu timeout"""
        if self.admin_timeout_id:
            self.after_cancel(self.admin_timeout_id)
            self.admin_timeout_id = None
    
    def on_admin_timeout(self):
        """Handle admin menu timeout - return to main screen"""
        self.admin_timeout_id = None
        logger.info("ADMIN TIMEOUT | Admin session ended due to inactivity (15 seconds)")
        # Close any open keypad before exiting
        if self.active_keypad:
            try:
                self.active_keypad.destroy()
            except:
                pass
            self.active_keypad = None
        # Clean up registration state if timeout fires during registration sub-page
        if self.worker.is_registering:
            self.worker.is_registering = False
            self.reg_countdown_active = False
            self.reg_waiting_approval = False
            self.reg_captured_frame = None
        self.exit_admin_to_main()
    
    def start_member_timeout(self, timeout_ms=15000):
        """Start member viewing timeout (default 15 seconds)"""
        self.cancel_member_timeout()
        self.member_timeout_id = self.after(timeout_ms, self.on_member_timeout)
    
    def cancel_member_timeout(self):
        """Cancel the member timeout"""
        if self.member_timeout_id:
            self.after_cancel(self.member_timeout_id)
            self.member_timeout_id = None
    
    def on_member_timeout(self):
        """Handle member viewing timeout - return to main screen"""
        self.member_timeout_id = None
        logger.info("MEMBER TIMEOUT | Member session ended due to inactivity (15 seconds)")
        self.exit_member_to_main()
    
    def reset_member_timeout_if_needed(self):
        """Reset member timeout when user interacts with log view"""
        if self.is_member_viewing:
            self.start_member_timeout()
    
    def _load_logs_async(self, name, eid):
        """Load logs from sheet (background thread)"""
        if not sheet or not network_connected:
            self.after_idle(lambda: self._populate_logs_offline())
            return
        try:
            recs = safe_sheet_call(sheet.get_all_values, timeout_sec=10, default=None)
            if recs is None:
                self.after_idle(lambda: self._populate_logs_offline())
                return
            filtered = [r for r in recs if len(r) > 1 and str(r[1]) == str(eid)]
            self.after_idle(lambda: self._populate_logs(filtered))
        except Exception as e:
            print(f"Error loading logs: {e}")
            self.after_idle(lambda: self._populate_logs_offline())
    
    def _populate_logs_offline(self):
        """Show offline message in log tree"""
        for i in self.log_tree.get_children():
            self.log_tree.delete(i)
        self.log_tree.insert("", "end", values=("OFFLINE - No data", "", ""))
    
    def _populate_logs(self, filtered):
        """Populate log tree (main thread)"""
        for i in self.log_tree.get_children():
            self.log_tree.delete(i)
        
        if not filtered:
            self.log_tree.insert("", "end", values=("No records", "", ""))
        else:
            for row in filtered:
                d = row[0]
                i_time = row[3] if len(row) > 3 else "00:00"
                o_time = row[4] if len(row) > 4 else "00:00"
                self.log_tree.insert("", "end", values=(d, i_time, o_time))
    
    def start_cache_cleanup(self):
        """Start background thread to cleanup old login cache entries"""
        def cleanup_loop():
            while True:
                time.sleep(3600)  # Run every hour
                try:
                    current_date = datetime.now().strftime("%d-%m-%Y")
                    keys_to_remove = []
                    
                    for key, entry in self.recent_login_cache.items():
                        # Remove entries older than today
                        if entry['date'] != current_date:
                            keys_to_remove.append(key)
                    
                    for key in keys_to_remove:
                        del self.recent_login_cache[key]
                    
                    if keys_to_remove:
                        print(f"Cleaned up {len(keys_to_remove)} old login cache entries")
                except Exception as e: 
                    print(f"Cache cleanup error: {e}")
        
        threading.Thread(target=cleanup_loop, daemon=True).start()

    def perform_sync_and_shutdown(self, action):
        if '|' in self.current_user:
            name_part = self.current_user.split('|')[0].strip()
        else:
            name_part = self.current_user.strip()
            
        logger.info(f"Face recognised: {self.current_user}, initiating {action}")
        
        t_str = datetime.now().strftime("%H:%M")
        stat_str = "IN" if action == "LOGIN" else "OUT" if action == "LOGOUT" else action
        self.temp_msg = f"{name_part} | {t_str} | {stat_str}"
        self.temp_msg_timeout = time.time() + 10  # temporary until _sync updates it
        
        threading.Thread(target=self._sync, args=(action,), daemon=True).start()
        # Track this timer so it can be cancelled if user enters admin before it fires
        if self.no_face_timer_id:
            self.after_cancel(self.no_face_timer_id)
        self.no_face_timer_id = self.after(15000, lambda: self.deactivate_camera_mode(go_idle=True))

    def _sync(self, action):
        global sheet, client, network_connected, offline_queue
        
        # Extract user info
        if '|' in self.current_user:
            name_part, eid_part = self.current_user.split('|')
            eid = eid_part.strip()
            name = name_part.strip()
        else:
            eid = self.current_user.strip()
            name = self.current_user.strip()

        date_str = datetime.now().strftime("%d-%m-%Y")
        time_str = datetime.now().strftime("%H:%M")
        
        # Check network connectivity
        check_network_connectivity()
        
        # If network is disconnected, store offline immediately
        if not network_connected:
            offline_entry = {
                "date": date_str,
                "id": eid,
                "name": name,
                "time": time_str,
                "action": action,
                "timestamp": datetime.now().isoformat()
            }
            offline_queue.append(offline_entry)
            save_offline_queue()
            print(f"Stored offline (no network): {name} - {action} at {time_str}")
            self.after(0, lambda: self.show_feedback(True, f"SAVED OFFLINE ({action})", action))
            return
        
        # Network is connected - try to establish/reconnect sheet if needed
        if sheet is None:
            try:
                scope = [
                    "https://www.googleapis.com/auth/spreadsheets",
                    "https://www.googleapis.com/auth/drive"
                ]
                creds = ServiceAccountCredentials.from_json_keyfile_name(SERVICE_ACCOUNT_JSON, scope)
                client = gspread.authorize(creds)
                sheet = client.open_by_url(SHEET_URL).worksheet("Attendance")
                print("Sheet reconnected successfully")
            except Exception as e:
                # Store offline if sheet connection fails
                print(f"Failed to connect to sheet: {e}")
                offline_entry = {
                    "date": date_str,
                    "id": eid,
                    "name": name,
                    "time": time_str,
                    "action": action,
                    "timestamp": datetime.now().isoformat()
                }
                offline_queue.append(offline_entry)
                save_offline_queue()
                self.after(0, lambda: self.show_feedback(True, f"SAVED OFFLINE ({action})", action))
                return
        try:
            
            try: client.login() 
            except: pass 

            records = safe_sheet_call(sheet.get_all_values, timeout_sec=10, default=None)
            if records is None:
                # Sheet call timed out — store offline
                offline_entry = {
                    "date": date_str,
                    "id": eid,
                    "name": name,
                    "time": time_str,
                    "action": action,
                    "timestamp": datetime.now().isoformat()
                }
                offline_queue.append(offline_entry)
                save_offline_queue()
                self.after(0, lambda: self.show_feedback(True, f"SAVED OFFLINE ({action})", action))
                return
            found = False
            
            # Check cache first for logout to handle immediate logout after login
            cache_key = f"{eid}_{date_str}"
            if action == "LOGOUT" and cache_key in self.recent_login_cache:
                cached_entry = self.recent_login_cache[cache_key]
                print(f"Found cached login for {name} at {cached_entry['time']}")
                # The user has logged in recently, so we should be able to find their record
                # But let's give the sheet a moment to update if needed
                time.sleep(0.5)  # Brief delay to allow sheet to update
                records = safe_sheet_call(sheet.get_all_values, timeout_sec=10, default=records)  # Refetch records, fallback to existing
            
            for idx, row in enumerate(records):
                if len(row) < 2: continue
                
                # Only check Column B (ID) - Column index 1
                row_id = str(row[1]).strip()
                
                # Match by ID only and check date
                if row_id == eid and row[0] == date_str:
                    found = True
                    existing_in = row[3] if len(row) > 3 else ""
                    existing_out = row[4] if len(row) > 4 else ""
                    status_text = ""
                    is_success = False
                    
                    if action == "LOGIN":
                        if existing_out and existing_out != "00:00": 
                            status_text = f"{name}\nSESSION ENDED"
                            is_success = False
                        elif existing_in and existing_in != "00:00": 
                            status_text = f"{name}\nALREADY IN"
                            is_success = False
                        else:
                            sheet.update_cell(idx + 1, 4, time_str)
                            
                            # Verify if written in Excel
                            time.sleep(1)
                            written_row = safe_sheet_call(sheet.row_values, idx + 1, timeout_sec=5)
                            if written_row and len(written_row) > 3 and written_row[3] == time_str:
                                logger.info(f"Verified from excel: {name} logged IN at {time_str}")
                            else:
                                logger.warning(f"Failed to verify excel write for: {name} logging IN at {time_str}")

                            status_text = f"{name}\nLOGGED IN SUCCESSFULLY!"
                            is_success = True
                            # Cache this login
                            self.recent_login_cache[cache_key] = {
                                'date': date_str,
                                'time': time_str,
                                'action': 'LOGIN'
                            }
                            
                    elif action == "LOGOUT":
                        if not existing_in or existing_in == "00:00": 
                            status_text = f"{name}\nLOGIN FIRST"
                            is_success = False
                        elif existing_out and existing_out != "00:00": 
                            status_text = f"{name}\nALREADY OUT"
                            is_success = False
                        else:
                            sheet.update_cell(idx + 1, 5, time_str)
                            
                            # Verify if written in Excel
                            time.sleep(1)
                            written_row = safe_sheet_call(sheet.row_values, idx + 1, timeout_sec=5)
                            if written_row and len(written_row) > 4 and written_row[4] == time_str:
                                logger.info(f"Verified from excel: {name} logged OUT at {time_str}")
                            else:
                                logger.warning(f"Failed to verify excel write for: {name} logging OUT at {time_str}")

                            status_text = f"{name}\nLOGGED OUT SUCCESSFULLY!"
                            is_success = True
                            # Remove from cache after successful logout
                            if cache_key in self.recent_login_cache:
                                del self.recent_login_cache[cache_key]
                    
                    self.after(0, lambda s=is_success, m=status_text, a=action: self.show_feedback(s, m, a))
                    break
            
            if not found:
                # User not found for today - try to create new entry
                try:
                    # Try to fetch name from sheet if needed
                    user_name = name
                    if not user_name or user_name == eid:
                        # Search for name in sheet
                        for r in records:
                            if len(r) > 2 and str(r[1]).strip() == str(eid):
                                user_name = str(r[2]).strip()
                                break
                    
                    # Create new row with today's date
                    if action == "LOGIN":
                        new_row = [date_str, eid, user_name, time_str, "00:00"]
                        sheet.append_row(new_row)
                        
                        # Verify if written in Excel
                        time.sleep(1)
                        # We refetch the records to find the newly appended row
                        new_records = safe_sheet_call(sheet.get_all_values, timeout_sec=10, default=[])
                        if len(new_records) > 0 and len(new_records[-1]) > 3 and new_records[-1][3] == time_str and str(new_records[-1][1]).strip() == eid:
                            logger.info(f"Verified from excel (new row): {user_name} logged IN at {time_str}")
                        else:
                            logger.warning(f"Failed to verify excel row append for: {user_name} logging IN at {time_str}")

                        self.after(0, lambda: self.show_feedback(True, f"{user_name}\nLOGGED IN SUCCESSFULLY!", action))
                        # Cache this login
                        cache_key = f"{eid}_{date_str}"
                        self.recent_login_cache[cache_key] = {
                            'date': date_str,
                            'time': time_str,
                            'action': 'LOGIN'
                        }
                    else:
                        # Can't logout if never logged in
                        self.after(0, lambda: self.show_feedback(False, "LOGIN FIRST", action))
                except Exception as e:
                    print(f"Error creating new entry: {e}")
                    self.after(0, lambda: self.show_feedback(False, "USER NOT FOUND TODAY", action))

        except Exception as e:
            print(f"Sync Logic Error: {e}")
            self.after(0, lambda: self.show_feedback(False, "SYNC ERROR", action))

    # ────────────────────────────────────────────────
    # POLLING LOOP (UI UPDATE)
    # ────────────────────────────────────────────────
    def poll_and_update_frame(self):
        try:
            # Check if window still exists to prevent X server errors
            if not self.winfo_exists():
                return
                
            # Update update button text
            if hasattr(self, 'update_btn'):
                if self.update_available:
                    self.update_btn.config(text=f"UPDATE [{self.latest_update_version}]")
                else:
                    self.update_btn.config(text="UPDATE")
            
            # If we are transitioning to Admin menu, SKIP all logic updates
            if self.is_transitioning:
                return  # Don't schedule here - finally block handles rescheduling

            if self.worker and self.worker.active and not self.is_showing_idle:
                w_status, w_clr = self.worker.latest_status
                self.status_msg = w_status
                self.status_clr = w_clr 
                
                user = self.worker.latest_user
                if user != self.current_user:
                    self.current_user = user
                    
                    if self.auth_action_pending and user != "Unknown":
                        if self.no_face_timer_id:
                            self.after_cancel(self.no_face_timer_id)
                            self.no_face_timer_id = None
                        
                        action = self.auth_action_pending
                        self.auth_action_pending = None
                        
                        if action == "ADMIN_CHECK":
                            # --- FIX: LOOSE MATCHING FOR ADMIN ---
                            # Check full string OR just the name part
                            is_admin = False
                            user_name_only = user.split('|')[0].strip()
                            
                            for su in self.worker.superusers:
                                su_clean = su.strip()
                                # Match against full string or just name part
                                if user == su_clean or user_name_only == su_clean.split('|')[0].strip():
                                    is_admin = True
                                    break
                            
                            if is_admin:
                                self.perform_admin_entry(user)
                            else:
                                # Member recognized - show their logs
                                self.perform_member_entry(user)
                                
                        elif action in ["LOGIN", "LOGOUT"]:
                            self.perform_sync_and_shutdown(action)

                frame = self.worker.get_current_frame()
                if frame is not None:
                    self.process_and_display(frame)
                    
            # Process pending Tkinter events periodically to prevent freeze
            # self.update_idletasks()
            
        except tk.TclError as e:
            # X server connection lost - don't reschedule
            print(f"TclError in poll_and_update_frame (X server may be lost): {e}")
            return
        except Exception as e:
            print(f"Error in poll_and_update_frame: {e}")
            import traceback
            traceback.print_exc()
        finally:
            # Always reschedule to prevent blank screen (unless X server is gone)
            try:
                if self.winfo_exists():
                    self.after(33, self.poll_and_update_frame)
            except tk.TclError:
                pass  # Window destroyed, don't reschedule

    def process_and_display(self, frame):
        try:
            # Check window exists before processing
            if not self.winfo_exists():
                return
                
            self.latest_frame = frame.copy()
            # Flip the display frame 180 degrees so the UI feed matches camera orientation
            frame = cv2.flip(frame, -1)
            h, w, ch = frame.shape
            
            ts = datetime.now().strftime("%d/%m/%y %H:%M:%S")
            # Color-code clock: Green if connected, Red if offline
            clock_color = (0, 255, 0) if network_connected else (0, 0, 255)
            cv2.putText(frame, ts, (w-250, 30), cv2.FONT_HERSHEY_DUPLEX, 0.7, clock_color, 1)
            
            overlay = frame.copy()
            cv2.rectangle(overlay, (0, h-60), (w, h), (0,0,0), -1)
            cv2.addWeighted(overlay, 0.6, frame, 0.4, 0, frame)

            display_text = self.status_msg
            display_clr = self.status_clr 

            if time.time() < self.temp_msg_timeout:
                display_text = self.temp_msg
                display_clr = (0, 255, 0) 
            
            # Removed WELCOME:NAME override to match user request
            # if "VERIFIED:" in display_text:
            #     user_name = display_text.replace("VERIFIED: ", "")
            #     display_text = f"Welcome: {user_name}"

            cv2.putText(frame, display_text, (10, h-20), cv2.FONT_HERSHEY_DUPLEX, 0.5, display_clr, 1)

            img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            im_pil = Image.fromarray(img_rgb)
        except tk.TclError as e:
            print(f"TclError in process_and_display: {e}")
            return
        except Exception as e:
            print(f"Error in process_and_display: {e}")
            return
        
        try:
            if self.current_view == "main":
                im_copy = im_pil.copy()
                im_copy.thumbnail((270, 310), Image.Resampling.LANCZOS)
                final_img = Image.new("RGB", (270, 310), "black")
                x_off = (270 - im_copy.width) // 2
                y_off = (310 - im_copy.height) // 2
                final_img.paste(im_copy, (x_off, y_off))
                self.update_image_label(self.cam_label, final_img)
                
            elif self.current_view == "reg":
                im_copy = im_pil.copy()
                
                # Draw countdown if active
                if self.reg_countdown_active and self.reg_countdown_value > 0:
                    draw = ImageDraw.Draw(im_copy)
                    # Draw semi-transparent overlay
                    overlay = Image.new('RGBA', im_copy.size, (0, 0, 0, 128))
                    im_copy = im_copy.convert('RGBA')
                    im_copy = Image.alpha_composite(im_copy, overlay)
                    im_copy = im_copy.convert('RGB')
                    
                    # Draw countdown face_locations 
                    draw = ImageDraw.Draw(im_copy)
                    try:
                        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
                    except:
                        font = ImageFont.load_default()
                    
                    text = str(self.reg_countdown_value)
                    bbox = draw.textbbox((0, 0), text, font=font)
                    text_w = bbox[2] - bbox[0]
                    text_h = bbox[3] - bbox[1]
                    x = (im_copy.width - text_w) // 2
                    y = (im_copy.height - text_h) // 2
                    draw.text((x, y), text, fill="yellow", font=font)
                
                im_copy.thumbnail((200, 240), Image.Resampling.LANCZOS)
                final_img = Image.new("RGB", (200, 240), "black")
                x_off = (200 - im_copy.width) // 2
                y_off = (240 - im_copy.height) // 2
                final_img.paste(im_copy, (x_off, y_off))
                self.update_image_label(self.reg_cam, final_img)
        except Exception as e:
            print(f"Error updating camera view: {e}")
            import traceback
            traceback.print_exc()

    # ────────────────────────────────────────────────
    # PAGES
    # ────────────────────────────────────────────────
    def init_main_ui(self):
        f = tk.Frame(self.container, bg=COLORS["bg"])
        self.frames["main"] = f
        
        cam_frame = tk.Frame(f, bg="black", bd=2, relief="solid")
        cam_frame.pack(side="left", padx=2, pady=2)
        
        self.cam_label = tk.Label(cam_frame, bg="black", width=270, height=310)
        self.cam_label.pack()
        
        # Initialize with blank image to prevent crashes
        blank_img = Image.new('RGB', (270, 310), color='black')
        blank_tk = ImageTk.PhotoImage(blank_img)
        self.cam_label.configure(image=blank_tk)
        self.cam_label.image = blank_tk
        
        p = tk.Frame(f, bg=COLORS["panel"], padx=5, pady=5)
        p.pack(side="left", fill="both", expand=True, padx=2, pady=2)
        
        btn_in = tk.Button(p, text="LOGIN", bg=COLORS["login"], fg="white", 
                            font=FONT_BTN_LARGE, bd=0, activebackground=COLORS["login"],
                            command=lambda: self.handle_attendance("LOGIN"))
        btn_in.pack(side="top", fill="both", expand=True, pady=5)

        btn_out = tk.Button(p, text="LOGOUT", bg=COLORS["logout"], fg="white", 
                            font=FONT_BTN_LARGE, bd=0, activebackground=COLORS["logout"],
                            command=lambda: self.handle_attendance("LOGOUT"))
        btn_out.pack(side="top", fill="both", expand=True, pady=5)

        self.btn_more = tk.Button(p, text="MENU", bg=COLORS["more"], fg="white",
                             font=FONT_BTN_MED, bd=0, activebackground=COLORS["more"],
                             command=self.handle_menu_click)
        self.btn_more.pack(side="top", fill="both", expand=True, pady=5)


    def init_admin_menu(self):
        f = tk.Frame(self.container, bg=COLORS["bg"])
        self.frames["admin"] = f

        # ── Header row: title on left, WiFi icon on right ──
        header_row = tk.Frame(f, bg=COLORS["bg"])
        header_row.pack(fill="x", pady=(8, 0), padx=8)

        tk.Label(header_row, text="ADMIN PANEL", bg=COLORS["bg"], fg="white",
                 font=("Segoe UI", 20, "bold")).pack(side="left")

        # Lock icon button for changing Master Password
        self.lock_icon_canvas = tk.Canvas(header_row, width=40, height=40,
                                          bg=COLORS["bg"], highlightthickness=0)
        self.lock_icon_canvas.pack(side="right", padx=2)
        self._draw_lock_icon(self.lock_icon_canvas, "#e74c3c")
        self.lock_icon_canvas.bind("<Button-1>", lambda e: self._open_master_password_dialog(mode="create"))

        # WiFi icon button (canvas-drawn arcs)
        self.wifi_icon_canvas = tk.Canvas(header_row, width=40, height=40,
                                          bg=COLORS["bg"], highlightthickness=0)
        self.wifi_icon_canvas.pack(side="right", padx=2)
        self._draw_wifi_icon(self.wifi_icon_canvas, "#2980b9", label=True)
        self.wifi_icon_canvas.bind("<Button-1>", lambda e: self.show_wifi_panel())

        grid = tk.Frame(f, bg=COLORS["bg"])
        grid.pack(expand=True, fill="both", padx=10, pady=8)
        
        ops = [("REG", lambda: self.go_list("reg")), ("EDIT", lambda: self.go_list("edit")),
               ("LOGS", lambda: self.go_list("logs")), ("RECAP", lambda: self.go_list("recap")),
               ("UPDATE", self.handle_update)]
        
        for i, (txt, func) in enumerate(ops):
            b = tk.Button(grid, text=txt, bg=COLORS["admin_btn"], fg="white", font=FONT_BOLD, bd=0,
                          command=func)
            b.grid(row=i//2, column=i%2, padx=5, pady=5, sticky="nsew", ipady=20)
            if txt == "UPDATE":
                self.update_btn = b
        
        grid.columnconfigure(0, weight=1); grid.columnconfigure(1, weight=1)
        grid.rowconfigure(0, weight=1); grid.rowconfigure(1, weight=1); grid.rowconfigure(2, weight=1)
        
        bk = tk.Button(f, text="EXIT ADMIN", bg=COLORS["cancel"], fg="white", font=FONT_BOLD, bd=0,
                       command=self.exit_admin_to_main)
        bk.pack(fill="x", pady=0, ipady=5)

    def init_member_list(self):
        f = tk.Frame(self.container, bg=COLORS["bg"])
        self.frames["list"] = f
        
        # Button container at bottom
        btn_container = tk.Frame(f, bg=COLORS["bg"])
        btn_container.pack(side="bottom", fill="x")
        
        # NEW button (only shown for reg mode)
        self.new_reg_btn = tk.Button(btn_container, text="NEW REGISTRATION", bg=COLORS["save"], fg="white", 
                                     font=FONT_BOLD, bd=0, command=self.start_new_registration)
        
        bk = tk.Button(btn_container, text="CLOSE", bg=COLORS["cancel"], fg="white", font=FONT_BOLD, bd=0,
                       command=lambda: [self.start_admin_timeout(), self.show_frame("admin")])
        bk.pack(side="bottom", fill="x", ipady=15)

        sb = tk.Scrollbar(f, width=30)
        sb.pack(side="right", fill="y")
        
        # Use larger readable font for better visibility
        self.list_w = tk.Listbox(f, bg=COLORS["panel"], fg="white", font=("Courier", 14, "bold"), bd=0, 
                                 highlightthickness=0, yscrollcommand=sb.set, selectbackground="#333",
                                 activestyle="none")
        self.list_w.pack(side="left", fill="both", expand=True)
        sb.config(command=self.list_w.yview)
        self.list_w.bind('<<ListboxSelect>>', self.handle_list_click)

    def init_registration(self):
        f = tk.Frame(self.container, bg=COLORS["bg"])
        self.frames["reg"] = f
        
        # Top title label
        title_label = tk.Label(f, text="REGISTRATION PANEL", bg=COLORS["bg"], fg="white", 
                              font=("Segoe UI", 14, "bold"))
        title_label.pack(pady=5)
        
        # Container for photo and fields (to ensure proper alignment)
        content_frame = tk.Frame(f, bg=COLORS["bg"])
        content_frame.pack(fill="both", expand=True)
        
        # Left side - Camera/Photo
        cam_frame = tk.Frame(content_frame, bg="black", bd=2, relief="solid")
        cam_frame.pack(side="left", padx=2, anchor="n")
        self.reg_cam = tk.Label(cam_frame, bg="black", width=200, height=240)
        self.reg_cam.pack()
        
        # Right side - Fields and Buttons
        frm = tk.Frame(content_frame, bg=COLORS["bg"])
        frm.pack(side="right", fill="both", expand=True, padx=2, anchor="n")
        
        self.in_id = tk.Entry(frm, bg=COLORS["input_bg"], fg="white", font=FONT_MAIN, relief="flat", justify="center")
        self.in_id.insert(0, "ID (Tap)")
        self.in_id.pack(fill="x", pady=5, ipady=5)
        self.in_id.bind("<Button-1>", lambda e: self.open_keypad(self.in_id, auto_fetch=True))
        
        self.in_name = tk.Entry(frm, bg=COLORS["input_bg"], fg="white", font=FONT_MAIN, relief="flat", justify="center")
        self.in_name.insert(0, "NAME")
        self.in_name.pack(fill="x", pady=5, ipady=5)
        
        self.in_role = ttk.Combobox(frm, values=["Member", "Admin"], font=FONT_MAIN)
        self.in_role.set("Member")
        self.in_role.pack(fill="x", pady=5)
        
        self.capture_btn = tk.Button(frm, text="CAPTURE", bg=COLORS["save"], fg="white", font=FONT_BOLD, bd=0,
                       command=self.start_capture)
        self.capture_btn.pack(fill="x", pady=5, ipady=10)
        
        bk = tk.Button(frm, text="CLOSE", bg=COLORS["cancel"], fg="white", font=FONT_BOLD, bd=0,
                       command=self.exit_reg)
        bk.pack(fill="x", pady=5, ipady=5)

    def init_view_logs(self):
        f = tk.Frame(self.container, bg=COLORS["bg"])
        self.frames["logs"] = f
         
        # Add label at top to show whose log is being viewed
        self.log_user_name_label = tk.Label(f, text="", bg=COLORS["bg"], fg="white", 
                                            font=("Segoe UI", 16, "bold"))
        self.log_user_name_label.pack(pady=10)
        
        self.logs_close_btn = tk.Button(f, text="CLOSE", bg=COLORS["cancel"], fg="white", font=FONT_BOLD, bd=0,
                       command=self.handle_logs_close)
        self.logs_close_btn.pack(side="bottom", fill="x", ipady=5)

        sb = tk.Scrollbar(f, width=30)
        sb.pack(side="right", fill="y")

        cols = ("Date", "In", "Out")
        self.log_tree = ttk.Treeview(f, columns=cols, show="headings", yscrollcommand=sb.set)
        
        self.log_tree.column("Date", width=180, anchor="center")
        self.log_tree.column("In", width=120, anchor="center")
        self.log_tree.column("Out", width=120, anchor="center")

        for c in cols: self.log_tree.heading(c, text=c)
        
        self.log_tree.pack(side="left", fill="both", expand=True)
        sb.config(command=self.log_tree.yview)
        
        # Bind mouse and key events to reset member timeout
        self.log_tree.bind("<Button-1>", lambda e: self.reset_member_timeout_if_needed())
        self.log_tree.bind("<MouseWheel>", lambda e: self.reset_member_timeout_if_needed())
        self.log_tree.bind("<Key>", lambda e: self.reset_member_timeout_if_needed())

    def init_edit_page(self):
        f = tk.Frame(self.container, bg=COLORS["bg"])
        self.frames["edit"] = f
        
        # Top title label
        title_label = tk.Label(f, text="EDITING PANEL", bg=COLORS["bg"], fg="white", 
                              font=("Segoe UI", 14, "bold"))
        title_label.pack(pady=5)
        
        # Container for photo and fields (to ensure proper alignment)
        content_frame = tk.Frame(f, bg=COLORS["bg"])
        content_frame.pack(fill="both", expand=True)
        
        # Left side - Photo (200x240, similar to registration camera)
        photo_frame = tk.Frame(content_frame, bg="black", bd=2, relief="solid")
        photo_frame.pack(side="left", padx=2, anchor="n")
        
        self.edit_photo_label = tk.Label(photo_frame, bg="black", text="No Photo", 
                                         fg="gray", font=("Segoe UI", 10), width=200, height=240)
        self.edit_photo_label.pack()
        
        # Right side - Fields and Buttons
        right_frame = tk.Frame(content_frame, bg=COLORS["bg"])
        right_frame.pack(side="right", fill="both", expand=True, padx=0, anchor="n")
        
        # ID field (non-editable) - black text
        self.e_id = tk.Entry(right_frame, bg=COLORS["input_bg"], fg="black", font=FONT_MAIN, 
                            relief="flat", justify="center", state="readonly")
        self.e_id.pack(fill="x", pady=5, ipady=5)
        
        # Name field (non-editable) - black text
        self.e_name_display = tk.Entry(right_frame, bg=COLORS["input_bg"], fg="black", font=FONT_MAIN, 
                                       relief="flat", justify="center", state="readonly")
        self.e_name_display.pack(fill="x", pady=5, ipady=5)
        
        # Role selector
        self.e_role = ttk.Combobox(right_frame, values=["Member", "Admin"], font=FONT_MAIN)
        self.e_role.set("Member")
        self.e_role.pack(fill="x", pady=5)
        
        # UPDATE and DELETE buttons in a single row
        button_row = tk.Frame(right_frame, bg=COLORS["bg"])
        button_row.pack(fill="x", pady=5)
        
        self.edit_update_btn = tk.Button(button_row, text="UPDATE", bg=COLORS["login"], fg="white", 
                                        font=FONT_BOLD, bd=0, command=self.process_edit_update)
        self.edit_update_btn.pack(side="left", fill="x", expand=True, padx=(0, 2), ipady=8)
        
        self.edit_delete_btn = tk.Button(button_row, text="DELETE", bg=COLORS["logout"], fg="white", 
                                        font=FONT_BOLD, bd=0, command=self.process_edit_delete)
        self.edit_delete_btn.pack(side="left", fill="x", expand=True, padx=(2, 0), ipady=8)
        
        # CLOSE button
        bk = tk.Button(right_frame, text="CLOSE", bg=COLORS["cancel"], fg="white", font=FONT_BOLD, bd=0,
                       command=lambda: [self.start_admin_timeout(), self.show_frame("list")])
        bk.pack(fill="x", pady=5, ipady=5)

    # ────────────────────────────────────────────────
    # KEYPAD & LIST HELPERS
    # ────────────────────────────────────────────────
    def _reset_master_timer(self, event=None):
        if hasattr(self, '_master_pwd_timer') and self._master_pwd_timer:
            self.after_cancel(self._master_pwd_timer)
            self._master_pwd_timer = None
        # Expire after 20 seconds of inactivity
        self._master_pwd_timer = self.after(20000, self._master_timer_expire)
        
    def _cancel_master_timer(self):
        if hasattr(self, '_master_pwd_timer') and self._master_pwd_timer:
            self.after_cancel(self._master_pwd_timer)
            self._master_pwd_timer = None

    def _master_timer_expire(self):
        self._master_pwd_timer = None
        if self.active_keypad:
            self._close_master_keypad(self.active_keypad)

    def _open_master_password_dialog(self, mode="login"):
        """Full-screen Toplevel with QWERTY keyboard for master password entry."""
        self._master_pwd_mode = mode
        if self.active_keypad:
            try:
                self.active_keypad.destroy()
            except Exception:
                pass

        kp = tk.Toplevel(self)
        self.active_keypad = kp
        kp.overrideredirect(True)
        kp.configure(bg=COLORS["bg"])
        kp.geometry(f"480x320+{self.winfo_x()}+{self.winfo_y()}")
        kp.lift()
        kp.grab_set()
        
        # Start and bind activity reset events
        self._reset_master_timer()
        kp.bind("<Any-KeyPress>", self._reset_master_timer)
        kp.bind("<Button>", self._reset_master_timer)

        title_bar = tk.Frame(kp, bg=COLORS["panel"])
        title_bar.pack(fill="x")
        title_text = "  [LOCK]  Create Password" if mode == "create" else "  [LOCK]  Master Password"
        tk.Label(title_bar, text=title_text, bg=COLORS["panel"], fg="white", font=("Segoe UI", 12, "bold")).pack(side="left", padx=8, pady=5)
        tk.Button(title_bar, text="  X  ", bg=COLORS["cancel"], fg="white", font=("Segoe UI", 11, "bold"), bd=0, command=lambda: self._close_master_keypad(kp)).pack(side="right", padx=4, pady=3, ipady=2)

        pwd_row = tk.Frame(kp, bg=COLORS["bg"])
        pwd_row.pack(fill="x", padx=6, pady=4)
        tk.Label(pwd_row, text="Password:", bg=COLORS["bg"], fg="#aaaaaa", font=("Segoe UI", 10)).pack(side="left")

        self._master_pwd_var = tk.StringVar()
        self._master_pwd_show = (mode == "create")
        show_char = "" if self._master_pwd_show else "*"
        self._master_pwd_entry = tk.Entry(pwd_row, textvariable=self._master_pwd_var, bg=COLORS["input_bg"], fg="white", font=("Segoe UI", 13), relief="flat", show=show_char, width=18)
        self._master_pwd_entry.pack(side="left", fill="x", expand=True, padx=6, ipady=5)

        eye_text = "HIDE" if self._master_pwd_show else "SHOW"
        self._master_eye_btn = tk.Button(pwd_row, text=eye_text, bg=COLORS["admin_btn"], fg="white", font=("Segoe UI", 11), bd=0, padx=4, command=self._toggle_master_visibility)
        self._master_eye_btn.pack(side="right", padx=2, ipady=4)

        self._master_status_label = tk.Label(kp, text="", bg=COLORS["bg"], fg="#e74c3c", font=("Segoe UI", 9, "bold"))
        self._master_status_label.pack(pady=(0, 2))

        self._master_kb_frame = tk.Frame(kp, bg=COLORS["bg"])
        self._master_kb_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))
        self._master_kb_shift = False
        self._master_kb_special = False
        self._build_master_keyboard(self._master_kb_frame)

    def _close_master_keypad(self, kp):
        self._cancel_master_timer()
        try:
            kp.grab_release()
            kp.destroy()
        except Exception: pass
        if self.active_keypad == kp:
            self.active_keypad = None
        self.deactivate_camera_mode(go_idle=True)

    def _toggle_master_visibility(self):
        self._master_pwd_show = not self._master_pwd_show
        self._master_pwd_entry.config(show="" if self._master_pwd_show else "*")
        self._master_eye_btn.config(text="HIDE" if self._master_pwd_show else "SHOW")

    def _build_master_keyboard(self, frame):
        for w in frame.winfo_children(): w.destroy()
        
        if getattr(self, '_master_kb_special', False):
            rows = [
                ["1","2","3","4","5","6","7","8","9","0"],
                ["!","@","#","$","%","^","&","*","(",")"],
                ["_","+","=","{","}","[","]","\\","|","DEL"],
                ["ABC",";",":","'","\"","SPACE","<",">","/","OK"]
            ]
        elif getattr(self, '_master_kb_shift', False):
            rows = [
                ["Q","W","E","R","T","Y","U","I","O","P"],
                ["A","S","D","F","G","H","J","K","L","DEL"],
                ["v/","Z","X","C","V","B","N","M","@","_"],
                ["123","-",".","SPACE",",","!","?","OK"]
            ]
        else:
            rows = [
                ["q","w","e","r","t","y","u","i","o","p"],
                ["a","s","d","f","g","h","j","k","l","DEL"],
                ["/^","z","x","c","v","b","n","m","@","_"],
                ["123","-",".","SPACE",",","!","?","OK"]
            ]
            
        for row in rows:
            rw = tk.Frame(frame, bg=COLORS["bg"])
            rw.pack(fill="x", pady=1)
            for col_idx, key in enumerate(row):
                self._make_master_kb_button(rw, key, col_idx)
            for i in range(len(row)): rw.columnconfigure(i, weight=1)

    def _make_master_kb_button(self, parent, key, col_idx):
        if key == "DEL":
            cmd = lambda: self._master_kb_key_press(self._master_pwd_var.get()[:-1], replace=True)
            btn = tk.Button(parent, text="DEL", bg="#555555", fg="white", font=("Segoe UI", 9, "bold"), bd=0, relief="flat", command=cmd)
        elif key == "SPACE":
            cmd = lambda: self._master_kb_key_press(" ")
            btn = tk.Button(parent, text="SPACE", bg=COLORS["admin_btn"], fg="white", font=("Segoe UI", 9, "bold"), bd=0, relief="flat", command=cmd)
        elif key == "OK":
            btn_text = "CREATE" if getattr(self, "_master_pwd_mode", "login") == "create" else "LOGIN"
            btn = tk.Button(parent, text=btn_text, bg="#27ae60", fg="white", font=("Segoe UI", 9, "bold"), bd=0, relief="flat", command=self._verify_master_password)
        elif key in ("/^", "v/"):
            color = COLORS["save"] if getattr(self, '_master_kb_shift', False) else COLORS["admin_btn"]
            btn = tk.Button(parent, text=("A^" if key == "/^" else "az"), bg=color, fg="white", font=("Segoe UI", 9, "bold"), bd=0, relief="flat", command=self._master_kb_toggle_shift)
        elif key == "123":
            btn = tk.Button(parent, text="123", bg=COLORS["admin_btn"], fg="white", font=("Segoe UI", 9, "bold"), bd=0, relief="flat", command=self._master_kb_toggle_special)
        elif key == "ABC":
            btn = tk.Button(parent, text="ABC", bg=COLORS["save"], fg="white", font=("Segoe UI", 9, "bold"), bd=0, relief="flat", command=self._master_kb_toggle_special)
        else:
            btn = tk.Button(parent, text=key, bg=COLORS["admin_btn"], fg="white", font=("Segoe UI", 11, "bold"), bd=0, relief="flat", command=lambda c=key: self._master_kb_key_press(c))
        btn.grid(row=0, column=col_idx, sticky="nsew", padx=1, ipady=5)

    def _master_kb_key_press(self, char, replace=False):
        if replace:
            self._master_pwd_var.set(char)
        else:
            self._master_pwd_var.set(self._master_pwd_var.get() + char)

    def _master_kb_toggle_shift(self):
        self._master_kb_shift = not getattr(self, '_master_kb_shift', False)
        self._master_kb_special = False
        self._build_master_keyboard(self._master_kb_frame)

    def _master_kb_toggle_special(self):
        self._master_kb_special = not getattr(self, '_master_kb_special', False)
        self._master_kb_shift = False
        self._build_master_keyboard(self._master_kb_frame)

    def _verify_master_password(self):
        pwd = self._master_pwd_var.get()
        if not pwd:
            return

        mode = getattr(self, "_master_pwd_mode", "login")
        if mode == "create":
            ans = messagebox.askyesno("Confirm", "Create master password?\nPress Yes to save, No to recreate.", parent=self.active_keypad)
            if ans:
                with open(MASTER_PASSWORD_PATH, "w") as f:
                    f.write(pwd)
                logger.info("MASTER PASSWORD | Created new master password")
                messagebox.showinfo("Saved", "Password created successfully!", parent=self.active_keypad)
                if self.active_keypad:
                    self.active_keypad.grab_release()
                    self.active_keypad.destroy()
                    self.active_keypad = None
                self._open_master_password_dialog(mode="login")
            else:
                self._master_pwd_var.set("")
            return

        saved_pwd = ""
        if os.path.exists(MASTER_PASSWORD_PATH):
            with open(MASTER_PASSWORD_PATH, "r") as f:
                saved_pwd = f.read().strip()

        if pwd == saved_pwd and saved_pwd != "":
            logger.info("ADMIN LOGIN | Master password verified successfully")
            if self.active_keypad:
                self.active_keypad.grab_release()
                self.active_keypad.destroy()
                self.active_keypad = None
            self.perform_admin_entry("Master Admin|000")
        else:
            logger.warning("ADMIN LOGIN | Master password verification failed - incorrect password")
            self._master_status_label.config(text="Incorrect Password")
            self._master_pwd_var.set("")

    def open_keypad(self, target_input, auto_fetch=False):
        if self.in_admin_mode:
            self.start_admin_timeout()
        
        # Close existing keypad if open
        if self.active_keypad:
            try:
                self.active_keypad.destroy()
            except:
                pass
        
        kp = tk.Toplevel(self)
        self.active_keypad = kp
        kp.geometry("280x280")
        kp.overrideredirect(True)
        kp.configure(bg="#222")
        
        x = self.winfo_x() + (self.winfo_width()//2) - 140
        y = self.winfo_y() + (self.winfo_height()//2) - 140
        kp.geometry(f"+{x}+{y}")
        
        current_val = target_input.get().replace("ID (Tap)", "")
        disp = tk.Entry(kp, justify='center', font=("Arial", 18), bg="#111", fg="white", relief="flat")
        disp.insert(0, current_val)
        disp.pack(fill="x", pady=5, padx=5, ipady=5)
        
        btn_frame = tk.Frame(kp, bg="#222")
        btn_frame.pack(expand=True, fill="both")
        
        keys = ['1','2','3','4','5','6','7','8','9','DEL','0','DONE']
        r, c = 0, 0
        for k in keys:
            color = "#444"
            cmd = lambda x=k: disp.insert(tk.END, x)
            if k == 'DEL': 
                cmd = lambda: disp.delete(len(disp.get())-1, tk.END)
            elif k == 'DONE': 
                color = "#2980b9"
                cmd = lambda: self.finalize_keypad(target_input, disp.get(), kp, auto_fetch)
            
            b = tk.Button(btn_frame, text=k, bg=color, fg="white", font=FONT_BOLD, bd=0, command=cmd)
            b.grid(row=r, column=c, sticky="nsew", padx=2, pady=2)
            c += 1
            if c > 2: c=0; r+=1
        
        for i in range(3): btn_frame.columnconfigure(i, weight=1)
        for i in range(4): btn_frame.rowconfigure(i, weight=1)

    def finalize_keypad(self, target_input, val, dialog, auto_fetch):
        target_input.delete(0, tk.END)
        target_input.insert(0, val)
        dialog.destroy()
        self.active_keypad = None
        if auto_fetch and val:
            # Run search in background thread
            threading.Thread(target=self._search_sheet_name_async, args=(val,), daemon=True).start()
            # Show loading state
            self.in_name.delete(0, tk.END)
            self.in_name.insert(0, "Searching...")

    def _search_sheet_name_async(self, eid):
        """Search for name in sheet (background thread)"""
        if not sheet or not network_connected:
            self.after_idle(lambda: self._update_name_field("Sheet offline"))
            return
        
        try:
            records = safe_sheet_call(sheet.get_all_values, timeout_sec=10, default=None)
            if records is None:
                self.after_idle(lambda: self._update_name_field("Sheet offline"))
                return
            for r in records:
                if len(r) > 2 and str(r[1]).strip() == str(eid).strip():
                    name = str(r[2]).strip()
                    # Strip legacy DELETED marker for display
                    if name.startswith("*(DELETED)"):
                        name = name.replace("*(DELETED)", "").strip()
                    if name and name not in ["Name", "NAME", "", "N/A"]:
                        self.after_idle(lambda n=name: self._update_name_field(n))
                        return
            self.after_idle(lambda: self._update_name_field("User not found"))
        except Exception as e:
            print(f"Error searching sheet: {e}")
            self.after_idle(lambda: self._update_name_field("Search failed"))
    
    def _update_name_field(self, text):
        """Update name field from main thread"""
        self.in_name.delete(0, tk.END)
        self.in_name.insert(0, text)
    
    def search_sheet_name(self, eid):
        """Deprecated: Use _search_sheet_name_async instead"""
        # Kept for backward compatibility
        self._search_sheet_name_async(eid)

    def exit_admin_to_main(self):
        """Properly transition from admin menu back to main screen with idle display"""
        # Close any open keypad before exiting
        if self.active_keypad:
            try:
                self.active_keypad.destroy()
            except:
                pass
            self.active_keypad = None
        
        self.in_admin_mode = False
        self.cancel_admin_timeout()
        self.show_frame("main")
        self.deactivate_camera_mode(go_idle=True)
    
    def format_user_list_entry(self, item, index):
        """
        Format user entry for list display with better spacing and admin indicator.
        Format: Sl.No | Name | [★] | ID
        """
        try:
            if '|' in item:
                name, user_id = item.split('|', 1)
                name = name.strip()
                user_id = user_id.strip()
                
                # Fast admin check using set lookup (O(1) instead of O(n))
                is_admin = (item in self.superusers or name in self.superusers)
                
                # Simple, clean format for performance
                admin_marker = "★" if is_admin else " "
                formatted = f"{index:>2}. {name:<20} {admin_marker}  ID:{user_id}"
                return formatted
            else:
                # Fallback for items without pipe separator
                formatted = f"{index:>2}. {item:<20}    ID:N/A"
                return formatted
        except Exception as e:
            print(f"Error formatting list entry: {e}")
            return f"{index}. {item}"
    
    def extract_user_from_formatted_entry(self, formatted_entry):
        """Extract original user info (NAME|ID) from formatted list entry"""
        try:
            # Skip separator lines and headers
            if formatted_entry.startswith("─") or formatted_entry.startswith("---") or formatted_entry.startswith("No."):
                return None
            
            # Parse formatted entry: "1. Name              ★  ID:123"
            # Remove serial number and split by ID:
            if ". " in formatted_entry and "ID:" in formatted_entry:
                # Remove the number part
                after_number = formatted_entry.split(". ", 1)[1]
                # Split by ID:
                if "ID:" in after_number:
                    parts = after_number.split("ID:")
                    name = parts[0].replace("★", "").strip()
                    user_id = parts[1].strip()
                    if user_id and user_id != "N/A":
                        return f"{name}|{user_id}"
            
            # Fallback: return as is
            return formatted_entry
        except Exception as e:
            print(f"Error extracting user info: {e}")
            return formatted_entry
    
    def go_list(self, mode):
        self.start_admin_timeout()
        self.list_mode = mode
        self.list_w.delete(0, tk.END)
        
        if mode == "reg":
            # For registration: load asynchronously to avoid UI blocking
            self.list_w.delete(0, tk.END)
            self.list_w.insert(tk.END, "--- Loading users from sheet... ---")
            # Show NEW button for reg mode
            try:
                self.new_reg_btn.pack(side="top", fill="x", ipady=15)
            except Exception as e:
                print(f"[ERROR] Error packing button: {e}")
            
            self.show_frame("list")
            # Load in background thread
            threading.Thread(target=self._load_reg_list_async, daemon=True).start()
            return
        else:
            # For other modes (logs, recap, edit): use cache for registered users
            # This ensures we only show users whose photos exist
            items = []
            if os.path.exists(REGISTERED_USERS_PATH):
                try:
                    with open(REGISTERED_USERS_PATH, "r") as f:
                        items = [line.strip() for line in f if line.strip()]
                except Exception as e:
                    print(f"Error reading cache: {e}")
                    # Fallback to scanning directory if cache read fails
                    files = [f.replace('.jpg', '') for f in os.listdir(KNOWN_FACES_DIR)
                             if f.endswith('.jpg') and not f.startswith('*(DELETED)')]
                    for f in files:
                        if '_' in f:
                            f_clean = f.strip()
                            parts = f_clean.rsplit('_', 1)
                            if len(parts) == 2:
                                name_part = parts[0].strip()
                                id_part = parts[1].strip()
                                formatted = f"{name_part}|{id_part}"
                                items.append(formatted)
                            else:
                                items.append(f_clean)
                        else:
                            items.append(f)
            else:
                # Cache doesn't exist, create it and read from directory
                self.update_registered_users_cache()
                if os.path.exists(REGISTERED_USERS_PATH):
                    with open(REGISTERED_USERS_PATH, "r") as f:
                        items = [line.strip() for line in f if line.strip()]
        
        # Sort by ID number (the part after |)
        def get_sort_key(item):
            try:
                if '|' in item:
                    id_part = item.split('|')[1]
                    return int(id_part)
                return 999999  # Put items without ID at the end
            except:
                return 999999
        
        items.sort(key=get_sort_key)
        
        # If no items, show appropriate message
        if len(items) == 0:
            if mode == "reg":
                self.list_w.insert(tk.END, "--- No new users to register ---")
            elif mode == "edit":
                self.list_w.insert(tk.END, "--- No registered users found ---")
            else:
                self.list_w.insert(tk.END, "--- No registered members ---")
        else:
            # Add simple header - ONE separator only for performance
            header = "No. Name                 Admin ID"
            separator = "─" * 40
            self.list_w.insert(tk.END, header)
            self.list_w.insert(tk.END, separator)
            
            # Add formatted items WITHOUT separators between entries
            for idx, item in enumerate(items, start=1):
                formatted = self.format_user_list_entry(item, idx)
                self.list_w.insert(tk.END, formatted)
        
        # Hide NEW button for non-reg modes
        self.new_reg_btn.pack_forget()
        
        self.show_frame("list")
    
    def get_registered_users_from_cache(self):
        """Read registered users from cache file"""
        registered_ids = set()
        if os.path.exists(REGISTERED_USERS_PATH):
            try:
                with open(REGISTERED_USERS_PATH, "r") as f:
                    for line in f:
                        line = line.strip()
                        if line and '|' in line:
                            _, eid = line.split('|', 1)
                            registered_ids.add(eid.strip())
            except Exception as e:
                print(f"Error reading registered users cache: {e}")
        return registered_ids

    def refresh_superusers(self):
        """Reload `SUPERUSER_PATH` into `self.superusers` set for UI checks."""
        try:
            new_set = set()
            if os.path.exists(SUPERUSER_PATH):
                with open(SUPERUSER_PATH, "r") as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        new_set.add(line)
                        if '|' in line:
                            new_set.add(line.split('|')[0].strip())
            self.superusers = new_set
        except Exception as e:
            print(f"Error refreshing superusers: {e}")
    
    def update_registered_users_cache(self):
        """Update cache file with current registered users from directory"""
        try:
            registered_users = []
            for f in os.listdir(KNOWN_FACES_DIR):
                # Skip legacy deleted photos if present
                if f.startswith('*(DELETED)'):
                    continue
                if f.endswith('.jpg'):
                    fname = f.replace('.jpg', '')
                    if '_' in fname:
                        parts = fname.rsplit('_', 1)
                        if len(parts) == 2:
                            name_part = parts[0].strip()
                            id_part = parts[1].strip()
                            registered_users.append(f"{name_part}|{id_part}")
            
            with open(REGISTERED_USERS_PATH, "w") as f:
                for user in registered_users:
                    f.write(user + "\n")
            print(f"Updated registered users cache: {len(registered_users)} users")
        except Exception as e:
            print(f"Error updating registered users cache: {e}")
    
    def get_all_users(self):
        """[DEPRECATED - NO LONGER USED] Get all users from sheet (for edit menu)
        Edit menu now uses cached registered users instead of fetching from sheet.
        """
        all_users = []
        if not sheet or not network_connected:
            return all_users
        
        try:
            records = safe_sheet_call(sheet.get_all_values, timeout_sec=10, default=None)
            if records is None:
                return all_users
            seen_ids = set()
            
            # Get all users from sheet
            for r in records:
                if len(r) > 2:
                    eid = str(r[1]).strip()
                    name = str(r[2]).strip()
                    
                    # Skip header rows - check for common header patterns
                    if not eid or not name:
                        continue
                    if eid.lower() in ["id", "employee id", "emp id", "employee_id"]:
                        continue
                    if name.lower() in ["name", "names", "employee name", "n/a", "na"]:
                        continue
                    # Skip if ID is not numeric
                    if not eid.replace(" ", "").isdigit():
                        continue
                    # Skip duplicates
                    if eid in seen_ids:
                        continue
                    
                    # Strip legacy DELETED marker from sheet name for display
                    if name.startswith("*(DELETED)"):
                        name = name.replace("*(DELETED)", "").strip()
                    all_users.append(f"{name}|{eid}")
                    seen_ids.add(eid)
        except Exception as e:
            print(f"Error getting all users: {e}")
        
        return all_users
    
    def get_unregistered_users(self):
        """Get list of users from sheet who don't have photos or have deleted photos"""
        unregistered = []
        if not sheet or not network_connected:
            return unregistered
        
        try:
            # Use cache instead of scanning directory
            registered_ids = self.get_registered_users_from_cache()
            
            # Fetch sheet data with timeout handling
            records = safe_sheet_call(sheet.get_all_values, timeout_sec=10, default=None)
            if records is None:
                return unregistered
            
            # Find users in sheet without photos
            seen_users = set()  # Track unique users to prevent duplicates
            for r in records:
                if len(r) > 2:
                    eid = str(r[1]).strip()
                    name = str(r[2]).strip()
                    
                    # Skip header rows - check for common header patterns
                    if not eid or not name:
                        continue
                    if eid.lower() in ["id", "employee id", "emp id", "employee_id"]:
                        continue
                    if name.lower() in ["name", "names", "employee name", "n/a", "na"]:
                        continue
                    # Skip if ID is not numeric
                    if not eid.replace(" ", "").isdigit():
                        continue
                    
                    # Skip already registered users
                    if eid not in registered_ids:
                        # Strip legacy DELETED marker from sheet name for display
                        if name.startswith("*(DELETED)"):
                            name = name.replace("*(DELETED)", "").strip()
                        user_key = f"{name}|{eid}"
                        if user_key not in seen_users:
                            unregistered.append(user_key)
                            seen_users.add(user_key)
            
        except Exception as e:
            print(f"Error getting unregistered users: {e}")
        
        return unregistered
    
    def _load_reg_list_async(self):
        """Load registration list in background thread"""
        try:
            items = self.get_unregistered_users()
            # Update UI in main thread - use after_idle to prevent multiple calls
            self.after_idle(lambda items=items: self._populate_reg_list(items))
        except Exception as e:
            print(f"[ERROR] Error loading reg list: {e}")
            import traceback
            traceback.print_exc()
            self.after_idle(lambda: self._populate_reg_list([]))
    
    def _populate_reg_list(self, items):
        """Populate registration list (called from main thread)"""
        if self.list_mode != "reg":
            return  # User navigated away
        
        self.list_w.delete(0, tk.END)
        
        # Sort by ID number
        def get_sort_key(item):
            try:
                if '|' in item:
                    id_part = item.split('|')[1]
                    return int(id_part)
                return 999999
            except:
                return 999999
        
        items.sort(key=get_sort_key)
        
        if len(items) == 0:
            self.list_w.insert(tk.END, "--- No new users to register ---")
        else:
            # Add simple header
            header = "No. Name                 Admin ID"
            separator = "─" * 40
            self.list_w.insert(tk.END, header)
            self.list_w.insert(tk.END, separator)
            
            # Add formatted items
            for idx, item in enumerate(items, start=1):
                formatted = self.format_user_list_entry(item, idx)
                self.list_w.insert(tk.END, formatted)
    
    def _load_edit_list_async(self):
        """[DEPRECATED - NO LONGER USED] Load edit list in background thread
        Edit menu now uses cached registered users for better performance.
        """
        try:
            items = self.get_all_users()
            # Update UI in main thread
            self.after_idle(lambda items=items: self._populate_edit_list(items))
        except Exception as e:
            print(f"[ERROR] Error loading edit list: {e}")
            import traceback
            traceback.print_exc()
            self.after_idle(lambda: self._populate_edit_list([]))
    
    def _populate_edit_list(self, items):
        """Populate edit list (called from main thread)"""
        if self.list_mode != "edit":
            return  # User navigated away
        
        self.list_w.delete(0, tk.END)
        
        # Sort by ID number
        def get_sort_key(item):
            try:
                if '|' in item:
                    id_part = item.split('|')[1]
                    return int(id_part)
                return 999999
            except:
                return 999999
        
        items.sort(key=get_sort_key)
        
        if len(items) == 0:
            self.list_w.insert(tk.END, "--- No users found ---")
        else:
            # Add simple header
            header = "No. Name                 Admin ID"
            separator = "─" * 40
            self.list_w.insert(tk.END, header)
            self.list_w.insert(tk.END, separator)
            
            # Add formatted items
            for idx, item in enumerate(items, start=1):
                formatted = self.format_user_list_entry(item, idx)
                self.list_w.insert(tk.END, formatted)

    def handle_list_click(self, event):
        self.start_admin_timeout()
        sel = self.list_w.curselection()
        if not sel: return
        user_info = self.list_w.get(sel[0])
        
        # Ignore clicks on message items, headers, and separators
        if user_info.startswith("---") or user_info.startswith("─") or user_info.startswith("No. Name"):
            return
        
        # Extract actual user info from formatted entry
        user_info = self.extract_user_from_formatted_entry(user_info)
        if not user_info:
            return
        
        if self.list_mode == "logs": 
            self.show_logs(user_info)
        elif self.list_mode == "recap": 
            self.recapture(user_info)
        elif self.list_mode == "reg":
            self.recapture(user_info)  # For unregistered users, open registration with prefilled data
        elif self.list_mode == "edit": 
            self.edit_member(user_info)

    def show_logs(self, info):
        """Load logs asynchronously for admin view"""
        try:
            name, eid = info.split('|')
        except:
            return
        
        self.start_admin_timeout()
        self.is_member_viewing = False
        
        # Update label and show loading message
        if not sheet or not network_connected:
            self.log_user_name_label.config(text=f"{name}'s Log (OFFLINE)")
        else:
            self.log_user_name_label.config(text=f"{name}'s Log")
        for i in self.log_tree.get_children():
            self.log_tree.delete(i)
        self.log_tree.insert("", "end", values=("Loading...", "", ""))
        self.show_frame("logs")
        
        # Load in background
        threading.Thread(target=self._load_logs_async, args=(name, eid), daemon=True).start()
    
    def show_member_logs(self, user):
        """Show logs for a member who accessed via menu"""
        self.pending_member_entry_id = None
        self.deactivate_camera_mode(go_idle=False)
        self.is_member_viewing = True
        
        if not sheet:
            # Show error message before returning
            self.log_user_name_label.config(text="Sheet Offline - Cannot Load Logs")
            for i in self.log_tree.get_children():
                self.log_tree.delete(i)
            self.log_tree.insert("", "end", values=("OFFLINE", "", ""))
            self.show_frame("logs")
            self.after(3000, self.exit_member_to_main)
            return
        
        try:
            name, eid = user.split('|')
        except:
            self.exit_member_to_main()
            return
        
        # Start member timeout
        self.start_member_timeout()
        
        # Update label and show loading message
        self.log_user_name_label.config(text=f"{name}'s Log")
        for i in self.log_tree.get_children():
            self.log_tree.delete(i)
        self.log_tree.insert("", "end", values=("Loading...", "", ""))
        self.show_frame("logs")
        
        # Load in background
        threading.Thread(target=self._load_logs_async, args=(name, eid), daemon=True).start()
    
    def handle_logs_close(self):
        """Handle close button in logs view - different behavior for admin vs member"""
        if self.is_member_viewing:
            self.exit_member_to_main()
        else:
            self.start_admin_timeout()
            self.show_frame("list")
    
    def exit_member_to_main(self):
        """Exit member viewing and return to main screen"""
        self.is_member_viewing = False
        self.cancel_member_timeout()
        self.show_frame("main")
        self.deactivate_camera_mode(go_idle=True)

    def recapture(self, info):
        try: name, eid = info.split('|')
        except: return
        self.start_admin_timeout()
        
        # Set recapture mode flag
        self.is_recapture_mode = True
        
        # Load existing photo if it exists
        file_name = info.replace('|', '_')
        photo_path = os.path.join(KNOWN_FACES_DIR, f"{file_name}.jpg")
        
        if os.path.exists(photo_path):
            try:
                # Load existing photo
                self.recapture_existing_photo = Image.open(photo_path)
            except Exception as e:
                print(f"Error loading existing photo: {e}")
                self.recapture_existing_photo = None
        else:
            self.recapture_existing_photo = None
        
        self.in_name.delete(0, tk.END); self.in_name.insert(0, name)
        self.in_id.delete(0, tk.END); self.in_id.insert(0, eid)
        self.go_reg()

    def start_new_registration(self):
        """Start a fresh registration (not recapture)"""
        self.start_admin_timeout()
        # Clear recapture mode
        self.is_recapture_mode = False
        self.recapture_existing_photo = None
        # Clear input fields
        self.in_id.delete(0, tk.END)
        self.in_id.insert(0, "ID (Tap)")
        self.in_name.delete(0, tk.END)
        self.in_name.insert(0, "NAME")
        self.in_role.set("Member")
        # Go to registration screen
        self.go_reg()

    def edit_member(self, info):
        self.start_admin_timeout()
        self.editing_full_name = info
        try: 
            name, eid = info.split('|')
        except: 
            return
        
        # Load and display photo
        file_name = info.replace('|', '_')
        photo_path = os.path.join(KNOWN_FACES_DIR, f"{file_name}.jpg")
        
        try:
            photo_img = Image.open(photo_path)
            # Resize to match registration camera size (200x240)
            photo_img.thumbnail((200, 240), Image.Resampling.LANCZOS)
            photo_tk = ImageTk.PhotoImage(photo_img)
            self.edit_photo_label.config(image=photo_tk, text="")
            self.edit_photo_label.image = photo_tk  # Keep a reference
        except Exception as e:
            print(f"Error loading photo: {e}")
            self.edit_photo_label.config(image='', text="Photo Error", fg="red")
        
        # Update readonly ID field
        self.e_id.config(state="normal")
        self.e_id.delete(0, tk.END)
        self.e_id.insert(0, eid)
        self.e_id.config(state="readonly")
        
        # Update readonly Name display field
        self.e_name_display.config(state="normal")
        self.e_name_display.delete(0, tk.END)
        self.e_name_display.insert(0, name)
        self.e_name_display.config(state="readonly")
        
        is_admin = False
        if os.path.exists(SUPERUSER_PATH):
            with open(SUPERUSER_PATH, "r") as f:
                admins = [line.strip() for line in f if line.strip()]
                if info in admins: 
                    is_admin = True
        self.e_role.set("Admin" if is_admin else "Member")
        self.show_frame("edit")

    def process_edit_update(self):
        self.start_admin_timeout()
        info = self.editing_full_name

        # Update role only (do not delete photo)
        try:
            name, eid = info.split('|')
        except:
            return

        admins = []
        if os.path.exists(SUPERUSER_PATH):
            with open(SUPERUSER_PATH, "r") as f:
                admins = [line.strip() for line in f if line.strip()]

        if info in admins:
            admins.remove(info)
        # Add to admins if role changed to Admin
        if self.e_role.get() == "Admin" and info not in admins:
            admins.append(info)

        with open(SUPERUSER_PATH, "w") as f:
            for s in admins:
                f.write(s + "\n")

        # Refresh in-memory superusers set used by UI
        try:
            self.refresh_superusers()
        except Exception:
            pass

        # Update registered users cache and reload faces
        self.update_registered_users_cache()
        threading.Thread(target=self.worker.load_faces).start()
        self.go_list("edit")

    def process_edit_delete(self):
        self.start_admin_timeout()
        info = self.editing_full_name
        if not messagebox.askyesno("Delete", f"Delete {info}?"):
            return

        # Permanently remove the user's photo file
        file_name = info.replace('|', '_')
        photo_path = os.path.join(KNOWN_FACES_DIR, f"{file_name}.jpg")
        try:
            if os.path.exists(photo_path):
                os.remove(photo_path)
                print(f"Permanently removed photo: {photo_path}")
        except Exception as e:
            print(f"Error removing photo: {e}")

        # Immediately remove from in-memory face lists so deleted user can't authenticate
        self.worker.remove_face(info)

        # Update superusers file - remove user if present
        admins = []
        if os.path.exists(SUPERUSER_PATH):
            with open(SUPERUSER_PATH, "r") as f:
                admins = [line.strip() for line in f if line.strip()]

        if info in admins:
            admins.remove(info)
            with open(SUPERUSER_PATH, "w") as f:
                for s in admins:
                    f.write(s + "\n")
            # Refresh in-memory superusers set used by UI
            try:
                self.refresh_superusers()
            except Exception:
                pass

        # Update registered users cache and reload faces
        self.update_registered_users_cache()
        threading.Thread(target=self.worker.load_faces).start()
        self.go_list("edit")
    
    def _mark_user_deleted_in_sheet(self, info):
        """Mark user as deleted in Google Sheet"""
        global sheet, client
        # Legacy behavior disabled: we no longer mark users as deleted in the sheet with
        # a special '*(DELETED)' prefix. This function is retained as a noop for
        # backward compatibility and auditing but will not modify remote data.
        print(f"Sheet delete-marker disabled for: {info}")

    def go_reg(self):
        self.start_admin_timeout()
        # Don't activate camera yet - wait for capture button
        self.worker.is_registering = False
        self.reg_captured_frame = None
        self.reg_countdown_active = False
        self.reg_waiting_approval = False
        self.show_frame("reg")
        
        # If in recapture mode and we have an existing photo, show it
        if self.is_recapture_mode and self.recapture_existing_photo:
            self.show_existing_photo_in_reg()
        else:
            # Show static "Enter ID to begin" message
            self.show_reg_idle_screen()

    def start_capture(self):
        """Start capture process with countdown"""
        self.start_admin_timeout()
        nid = self.in_id.get()
        nname = self.in_name.get()
        
        # Validate inputs
        if not nid or "Tap" in nid or not nname or nname in ["NAME", "User not found", "Sheet offline", ""]:
            return
        
        # Activate camera and start countdown
        self.worker.is_registering = True
        self.activate_camera_mode()
        self.reg_countdown_active = True
        self.reg_countdown_value = 3
        self.countdown_step()
    
    def countdown_step(self):
        """Handle countdown animation"""
        # Check if user has exited registration screen or cancelled
        if not self.reg_countdown_active or self.current_view != "reg":
            return
            
        if self.reg_countdown_value > 0:
            self.after(1000, self.countdown_step)
            self.reg_countdown_value -= 1
        else:
            # Countdown finished - capture!
            self.reg_countdown_active = False
            self.capture_frame()
    
    def capture_frame(self):
        """Capture the current frame and show approval dialog"""
        # Check if user is still on registration screen
        if self.current_view != "reg":
            return
            
        if self.latest_frame is not None:
            self.reg_captured_frame = self.latest_frame.copy()
            # Pause camera
            self.worker.is_registering = False
            self.deactivate_camera_mode(go_idle=False)
            # Show approval dialog
            self.show_approval_dialog()
    
    def show_approval_dialog(self):
        """Show approval dialog with captured image"""
        # Double check user is still on registration screen
        if self.current_view != "reg":
            self.reg_waiting_approval = False
            return
            
        self.reg_waiting_approval = True
        dialog = tk.Toplevel(self)
        dialog.geometry("400x240")
        dialog.overrideredirect(True)
        dialog.configure(bg=COLORS["bg"])
        
        x = self.winfo_x() + (self.winfo_width()//2) - 200
        y = self.winfo_y() + (self.winfo_height()//2) - 120
        dialog.geometry(f"+{x}+{y}")
        
        # Left side - captured image
        left_frame = tk.Frame(dialog, bg="black", bd=2, relief="solid")
        left_frame.pack(side="left", padx=5, pady=5)
        
        if self.reg_captured_frame is not None:
            frame = self.reg_captured_frame.copy()
            img_rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            im_pil = Image.fromarray(img_rgb)
            im_pil.thumbnail((200, 230), Image.Resampling.LANCZOS)
            tk_img = ImageTk.PhotoImage(im_pil)
            
            img_label = tk.Label(left_frame, image=tk_img, bg="black")
            img_label.image = tk_img
            img_label.pack()
        
        # Right side - buttons
        right_frame = tk.Frame(dialog, bg=COLORS["bg"])
        right_frame.pack(side="right", fill="both", expand=True, padx=5, pady=5)
        
        tk.Label(right_frame, text="Approve?", bg=COLORS["bg"], fg="white", font=FONT_BOLD).pack(pady=10)
        
        approve_btn = tk.Button(right_frame, text="APPROVE", bg=COLORS["login"], fg="white", font=FONT_BOLD, bd=0,
                               command=lambda: self.approve_capture(dialog))
        approve_btn.pack(fill="x", pady=8, ipady=12)
        
        recapture_btn = tk.Button(right_frame, text="RECAPTURE", bg=COLORS["save"], fg="white", font=FONT_BOLD, bd=0,
                                 command=lambda: self.recapture_action(dialog))
        recapture_btn.pack(fill="x", pady=8, ipady=12)
        
        exit_btn = tk.Button(right_frame, text="EXIT", bg=COLORS["cancel"], fg="white", font=FONT_BOLD, bd=0,
                            command=lambda: self.cancel_capture(dialog))
        exit_btn.pack(fill="x", pady=8, ipady=12)
    
    def approve_capture(self, dialog):
        """Save the captured frame and exit"""
        dialog.destroy()
        self.reg_waiting_approval = False
        
        nid, nname = self.in_id.get(), self.in_name.get()
        combined = f"{nname}|{nid}"
        role = self.in_role.get()
        
        # Save in background
        threading.Thread(target=self._perform_save_async, args=(combined, self.reg_captured_frame.copy(), role)).start()
        
        # Show success and exit
        self.show_success_message()
    
    def show_success_message(self):
        """Show success message briefly then exit registration"""
        # Create temporary message overlay on reg_cam
        w, h = 200, 240
        img = Image.new('RGB', (w, h), color='#27ae60')
        draw = ImageDraw.Draw(img)
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 20)
        except:
            font = ImageFont.load_default()
        
        text = "SAVED!\nSUCCESS"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (w - text_w) // 2
        y = (h - text_h) // 2
        draw.text((x, y), text, fill="white", font=font, align="center")
        
        self.update_image_label(self.reg_cam, img)
        
        # Exit after 2 seconds
        self.after(2000, self.complete_registration)
    
    def complete_registration(self):
        """Complete registration and return to admin menu"""
        self.in_id.delete(0, tk.END)
        self.in_id.insert(0, "ID (Tap)")
        self.in_name.delete(0, tk.END)
        self.in_name.insert(0, "NAME")
        self.in_role.set("Member")
        self.reg_captured_frame = None
        self.is_recapture_mode = False
        self.recapture_existing_photo = None
        self.worker.is_registering = False
        self.start_admin_timeout()
        self.show_frame("admin")
    
    def recapture_action(self, dialog):
        """Recapture - start countdown again"""
        dialog.destroy()
        self.reg_waiting_approval = False
        # Start capture process again
        self.start_capture()
    
    def cancel_capture(self, dialog):
        """Cancel capture and return to admin menu"""
        dialog.destroy()
        self.reg_waiting_approval = False
        self.exit_reg()
    
    def show_reg_idle_screen(self):
        """Show idle message on registration camera area"""
        w, h = 200, 240
        img = Image.new('RGB', (w, h), color='black')
        draw = ImageDraw.Draw(img)
        
        try:
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        except:
            font = ImageFont.load_default()
        
        text = "Enter ID\nto begin"
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
        x = (w - text_w) // 2
        y = (h - text_h) // 2
        draw.text((x, y), text, fill="white", font=font, align="center")
        
        self.update_image_label(self.reg_cam, img)
    
    def show_existing_photo_in_reg(self):
        """Show existing photo in registration camera area during recapture"""
        if not self.recapture_existing_photo:
            self.show_reg_idle_screen()
            return
        
        try:
            # Resize existing photo to fit the registration camera area (200x240)
            w, h = 200, 240
            photo_copy = self.recapture_existing_photo.copy()
            
            # Calculate aspect ratio and resize
            photo_copy.thumbnail((w, h), Image.Resampling.LANCZOS)
            
            # Create a black background and paste the photo centered
            img = Image.new('RGB', (w, h), color='black')
            photo_w, photo_h = photo_copy.size
            x_offset = (w - photo_w) // 2
            y_offset = (h - photo_h) // 2
            img.paste(photo_copy, (x_offset, y_offset))
            
            # Add text overlay indicating this is the current photo
            draw = ImageDraw.Draw(img)
            try:
                font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 12)
            except:
                font = ImageFont.load_default()
            
            text = "Current Photo"
            bbox = draw.textbbox((0, 0), text, font=font)
            text_w = bbox[2] - bbox[0]
            # Position text at the top
            draw.rectangle([0, 0, w, 20], fill='black')
            draw.text(((w - text_w) // 2, 2), text, fill="yellow", font=font)
            
            self.update_image_label(self.reg_cam, img)
        except Exception as e:
            print(f"Error showing existing photo: {e}")
            self.show_reg_idle_screen()

    def _perform_save_async(self, combined_name, frame, role):
        # Convert pipe format to underscore format for file storage
        file_name = combined_name.replace('|', '_')
        path = os.path.join(KNOWN_FACES_DIR, f"{file_name}.jpg")
        
        # Check if there's a deleted photo for this user and remove it
        # No soft-delete concept: remove any existing photo (overwrite)
        if os.path.exists(path):
            try:
                os.remove(path)
            except Exception as e:
                print(f"Error removing existing photo before save: {e}")
        
        # Save the new photo
        cv2.imwrite(path, frame)
        
        if role == "Admin":
            # Read existing admins to avoid duplicates
            admins = []
            if os.path.exists(SUPERUSER_PATH):
                with open(SUPERUSER_PATH, "r") as f:
                    admins = [line.strip() for line in f if line.strip()]
            
            # Only add if not already in the list
            if combined_name not in admins:
                admins.append(combined_name)
                with open(SUPERUSER_PATH, "w") as f:
                    for s in admins:
                        f.write(s + "\n")
                self.worker.superusers.append(combined_name)
                # Refresh UI-facing superusers set
                try:
                    self.refresh_superusers()
                except Exception:
                    pass

        try:
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            encs = face_recognition.face_encodings(rgb)
            if encs:
                if combined_name in self.worker.known_names:
                    idx = self.worker.known_names.index(combined_name)
                    self.worker.known_encodings[idx] = encs[0]
                else:
                    self.worker.known_encodings.append(encs[0])
                    self.worker.known_names.append(combined_name)
        except Exception as e:
            print(f"Error saving face: {e}")
        
        # Update registered users cache after registration
        self.update_registered_users_cache()

    def exit_reg(self):
        """Exit registration and return to admin menu"""
        self.worker.is_registering = False
        self.reg_countdown_active = False
        self.reg_waiting_approval = False
        self.reg_captured_frame = None
        self.is_recapture_mode = False
        self.recapture_existing_photo = None
        self.deactivate_camera_mode(go_idle=False)
        # Clear fields
        self.in_id.delete(0, tk.END)
        self.in_id.insert(0, "ID (Tap)")
        self.in_name.delete(0, tk.END)
        self.in_name.insert(0, "NAME")
        self.in_role.set("Member")
        self.start_admin_timeout()
        self.show_frame("admin")

    # ────────────────────────────────────────────────
    # WIFI CONFIGURATION
    # ────────────────────────────────────────────────

    def _draw_lock_icon(self, canvas, color):
        canvas.delete("all")
        w = int(canvas["width"])
        h = int(canvas["height"])
        cx, cy = w // 2, h // 2
        # Body
        body_w, body_h = 16, 12
        canvas.create_rectangle(cx - body_w//2, cy, cx + body_w//2, cy + body_h, fill=color, outline=color)
        # Shackle
        shackle_r = 5
        canvas.create_arc(cx - shackle_r, cy - shackle_r - 2, cx + shackle_r, cy + shackle_r - 2,
                          start=0, extent=180, outline=color, width=2, style=tk.ARC)

    def show_feedback(self, success, message, action):
        self.is_transitioning = True
        self.deactivate_camera_mode(go_idle=False)
        
        # Create a white image for the camera feed area (270x310)
        w, h = 270, 310
        img = Image.new('RGB', (w, h), color='white')
        draw = ImageDraw.Draw(img)
        
        try:
            font_icon = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 80)
            font_text = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", 16)
        except:
            font_icon = ImageFont.load_default()
            font_text = ImageFont.load_default()
            
        icon = "✔" if success else "✘"
        color = "#27ae60" if success else "#c0392b"
        
        # Calculate icon position
        bbox_icon = draw.textbbox((0, 0), icon, font=font_icon)
        icon_w = bbox_icon[2] - bbox_icon[0]
        icon_h = bbox_icon[3] - bbox_icon[1]
        x_icon = (w - icon_w) // 2
        y_icon = (h // 2) - icon_h - 10
        
        draw.text((x_icon, y_icon), icon, fill=color, font=font_icon)
        
        # Calculate text position (multiline support)
        lines = message.split('\n')
        y_text = y_icon + icon_h + 20
        total_h = sum([draw.textbbox((0, 0), l, font=font_text)[3] - draw.textbbox((0, 0), l, font=font_text)[1] + 5 for l in lines])
        
        for line in lines:
            bbox_text = draw.textbbox((0, 0), line, font=font_text)
            line_w = bbox_text[2] - bbox_text[0]
            line_h = bbox_text[3] - bbox_text[1]
            x_line = (w - line_w) // 2
            
            draw.text((x_line, y_text), line, fill="black", font=font_text)
            y_text += line_h + 5
            
        # Draw on the existing camera panel instead of switching frames
        self.update_image_label(self.cam_label, img)
        
        # Auto-dismiss after 3 seconds back to idle state
        self.after(3000, lambda: self.deactivate_camera_mode(go_idle=True))

    def _draw_wifi_icon(self, canvas, color, label=False):
        """Draw a WiFi symbol (arcs + dot) on a Canvas widget."""
        canvas.delete("all")
        w = int(canvas["width"])
        h = int(canvas["height"])
        cx = w // 2
        # arcs drawn from bottom-center upward
        base_y = h - 8 if not label else h - 6
        radii = [(14, 2), (9, 2), (5, 2)]
        for r, lw in radii:
            canvas.create_arc(cx - r, base_y - r, cx + r, base_y + r,
                              start=20, extent=140,
                              style=tk.ARC, outline=color, width=lw)
        canvas.create_oval(cx - 2, base_y - 2, cx + 2, base_y + 2,
                           fill=color, outline=color)
        if label:
            canvas.create_text(cx, 8, text="WiFi", fill=color,
                               font=("Segoe UI", 7, "bold"))

    def init_wifi_panel(self):
        """Build the WiFi configuration frame."""
        f = tk.Frame(self.container, bg=COLORS["bg"])
        self.frames["wifi"] = f

        # ── Header ──
        hdr = tk.Frame(f, bg=COLORS["panel"])
        hdr.pack(fill="x")

        back_btn = tk.Button(hdr, text="← BACK", bg=COLORS["cancel"], fg="white",
                             font=FONT_BOLD, bd=0,
                             command=lambda: [self.start_admin_timeout(),
                                             self.show_frame("admin")])
        back_btn.pack(side="left", padx=6, pady=5, ipadx=4, ipady=4)

        tk.Label(hdr, text="WiFi Configuration", bg=COLORS["panel"], fg="white",
                 font=("Segoe UI", 13, "bold")).pack(side="left", padx=8)

        # ── Current connection row ──
        cur_row = tk.Frame(f, bg=COLORS["input_bg"])
        cur_row.pack(fill="x", padx=6, pady=(4, 0))

        tk.Label(cur_row, text=" Connected:", bg=COLORS["input_bg"], fg="#aaaaaa",
                 font=("Segoe UI", 10)).pack(side="left")

        self.wifi_current_label = tk.Label(cur_row, text="Checking…",
                                           bg=COLORS["input_bg"], fg="#27ae60",
                                           font=("Segoe UI", 11, "bold"))
        self.wifi_current_label.pack(side="left", padx=6)

        # ── Scan button ──
        tk.Button(f, text="  [>>]  SCAN NETWORKS", bg=COLORS["save"],
                  fg="white", font=FONT_BOLD, bd=0,
                  command=self.wifi_scan_networks
                  ).pack(fill="x", padx=6, pady=4, ipady=7)

        # ── Available networks list ──
        tk.Label(f, text="Available Networks:", bg=COLORS["bg"], fg="#aaaaaa",
                 font=("Segoe UI", 9)).pack(anchor="w", padx=8)

        net_frame = tk.Frame(f, bg=COLORS["bg"])
        net_frame.pack(fill="both", expand=True, padx=6)

        net_sb = tk.Scrollbar(net_frame, width=18)
        net_sb.pack(side="right", fill="y")

        self.wifi_list = tk.Listbox(net_frame, bg=COLORS["panel"], fg="white",
                                    font=("Courier", 12, "bold"), bd=0,
                                    highlightthickness=0, yscrollcommand=net_sb.set,
                                    selectbackground="#2c3e50", activestyle="none",
                                    height=4)
        self.wifi_list.pack(side="left", fill="both", expand=True)
        net_sb.config(command=self.wifi_list.yview)
        self.wifi_list.bind("<<ListboxSelect>>", self._on_wifi_list_select)

        # ── Saved networks section ──
        tk.Label(f, text="Saved Networks:", bg=COLORS["bg"], fg="#aaaaaa",
                 font=("Segoe UI", 9)).pack(anchor="w", padx=8, pady=(3, 0))

        saved_outer = tk.Frame(f, bg=COLORS["panel"])
        saved_outer.pack(fill="x", padx=6, pady=(0, 4))

        self.saved_wifi_frame = tk.Frame(saved_outer, bg=COLORS["panel"])
        self.saved_wifi_frame.pack(fill="x")

    def show_wifi_panel(self):
        """Navigate to WiFi panel and refresh status."""
        self.start_admin_timeout()
        self.show_frame("wifi")
        self._refresh_wifi_status()
        self._refresh_saved_networks()

    # ── WiFi helpers (background threads → main-thread callbacks) ──

    def _refresh_wifi_status(self):
        def _worker():
            try:
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                    capture_output=True, text=True, timeout=5)
                for line in r.stdout.strip().splitlines():
                    if line.startswith("yes:"):
                        ssid = line.split(":", 1)[1].strip()
                        self.after_idle(lambda s=ssid:
                            self.wifi_current_label.config(text=f"  {s}", fg="#27ae60"))
                        return
                self.after_idle(lambda:
                    self.wifi_current_label.config(text="  Not connected", fg="#c0392b"))
            except Exception:
                self.after_idle(lambda:
                    self.wifi_current_label.config(text="  Unavailable", fg="#c0392b"))
        threading.Thread(target=_worker, daemon=True).start()

    def _refresh_saved_networks(self):
        def _worker():
            try:
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                    capture_output=True, text=True, timeout=5)
                saved = []
                for line in r.stdout.strip().splitlines():
                    parts = line.split(":")
                    if len(parts) >= 2 and "802-11-wireless" in parts[1]:
                        saved.append(parts[0].strip())
                self.after_idle(lambda s=saved: self._populate_saved_networks(s))
            except Exception:
                self.after_idle(lambda: self._populate_saved_networks([]))
        threading.Thread(target=_worker, daemon=True).start()

    def _populate_saved_networks(self, saved_list):
        # Keep the saved set in sync so _on_wifi_list_select can check it fast
        self._saved_wifi_ssids = set(saved_list)

        for w in self.saved_wifi_frame.winfo_children():
            w.destroy()
        if not saved_list:
            tk.Label(self.saved_wifi_frame, text="  No saved networks",
                     bg=COLORS["panel"], fg="#777777",
                     font=("Segoe UI", 10)).pack(anchor="w", padx=5, pady=3)
            return
        # Get current SSID to highlight it
        current_text = self.wifi_current_label.cget("text").strip()
        for ssid in saved_list:
            is_active = (ssid == current_text)
            row = tk.Frame(self.saved_wifi_frame, bg=COLORS["panel"])
            row.pack(fill="x", padx=3, pady=1)
            name_color = "#27ae60" if is_active else "white"
            prefix = "[*] " if is_active else "    "
            tk.Label(row, text=f"{prefix}{ssid}", bg=COLORS["panel"], fg=name_color,
                     font=("Segoe UI", 11)).pack(side="left", fill="x", expand=True)
            tk.Button(row, text="FORGET", bg=COLORS["logout"], fg="white",
                      font=("Segoe UI", 9, "bold"), bd=0, padx=6,
                      command=lambda s=ssid: self._wifi_forget_confirm(s)
                      ).pack(side="right", padx=3, pady=2, ipady=3)

    def wifi_scan_networks(self):
        """Kick off background WiFi scan and update list."""
        self.start_admin_timeout()
        self.wifi_list.delete(0, tk.END)
        self.wifi_list.insert(tk.END, "  Scanning… please wait")
        self._wifi_networks = []

        def _worker():
            try:
                r = subprocess.run(
                    ["nmcli", "-t", "-f", "SSID,SECURITY,SIGNAL",
                     "dev", "wifi", "list", "--rescan", "yes"],
                    capture_output=True, text=True, timeout=20)
                nets = []
                seen = set()
                for line in r.stdout.strip().splitlines():
                    parts = line.split(":")
                    if len(parts) < 3:
                        continue
                    ssid = parts[0].strip()
                    security = parts[1].strip()
                    try:
                        signal = int(parts[2].strip())
                    except ValueError:
                        signal = 0
                    if ssid and ssid not in seen:
                        seen.add(ssid)
                        nets.append((ssid, security, signal))
                nets.sort(key=lambda x: x[2], reverse=True)
                self.after_idle(lambda n=nets: self._populate_wifi_list(n))
            except subprocess.TimeoutExpired:
                self.after_idle(lambda: self._wifi_list_msg("  Scan timed out"))
            except Exception as e:
                self.after_idle(lambda: self._wifi_list_msg("  Scan failed"))
        threading.Thread(target=_worker, daemon=True).start()

    def _populate_wifi_list(self, nets):
        self.wifi_list.delete(0, tk.END)
        self._wifi_networks = nets
        if not nets:
            self.wifi_list.insert(tk.END, "  No networks found")
            return
        for ssid, security, signal in nets:
            if signal >= 75:
                bars = "▂▄▆█"
            elif signal >= 50:
                bars = "▂▄▆ "
            elif signal >= 25:
                bars = "▂▄  "
            else:
                bars = "▂   "
            lock = " [+]" if security else "   "
            self.wifi_list.insert(tk.END, f"  {bars}  {ssid}{lock}")

    def _wifi_list_msg(self, msg):
        self.wifi_list.delete(0, tk.END)
        self.wifi_list.insert(tk.END, msg)

    def _on_wifi_list_select(self, event):
        self.start_admin_timeout()
        sel = self.wifi_list.curselection()
        if not sel:
            return
        idx = sel[0]
        if not hasattr(self, "_wifi_networks") or idx >= len(self._wifi_networks):
            return
        ssid, security, _ = self._wifi_networks[idx]
        # If this network is already saved, connect directly without asking for password
        if ssid in self._saved_wifi_ssids:
            self._wifi_connect(ssid, None, None)
        elif security:
            self._open_wifi_password_dialog(ssid)
        else:
            self._wifi_connect(ssid, None, None)

    # ── On-screen QWERTY keyboard for password entry ──

    def _open_wifi_password_dialog(self, ssid):
        """Full-screen Toplevel with QWERTY keyboard for password entry."""
        self.start_admin_timeout()
        if self.active_keypad:
            try:
                self.active_keypad.destroy()
            except Exception:
                pass

        kp = tk.Toplevel(self)
        self.active_keypad = kp
        kp.overrideredirect(True)
        kp.configure(bg=COLORS["bg"])
        kp.geometry(f"480x320+{self.winfo_x()}+{self.winfo_y()}")
        kp.lift()
        kp.grab_set()

        # ── Title bar ──
        title_bar = tk.Frame(kp, bg=COLORS["panel"])
        title_bar.pack(fill="x")

        # Lock icon + SSID label
        tk.Label(title_bar, text=f"  [LOCK]  {ssid}", bg=COLORS["panel"], fg="white",
                 font=("Segoe UI", 12, "bold")).pack(side="left", padx=8, pady=5)

        tk.Button(title_bar, text="  X  ", bg=COLORS["cancel"], fg="white",
                  font=("Segoe UI", 11, "bold"), bd=0,
                  command=lambda: self._close_wifi_keypad(kp)
                  ).pack(side="right", padx=4, pady=3, ipady=2)

        # ── Password row ──
        pwd_row = tk.Frame(kp, bg=COLORS["bg"])
        pwd_row.pack(fill="x", padx=6, pady=4)

        tk.Label(pwd_row, text="Password:", bg=COLORS["bg"], fg="#aaaaaa",
                 font=("Segoe UI", 10)).pack(side="left")

        self._wifi_pwd_var = tk.StringVar()
        self._wifi_pwd_show = False

        self._pwd_entry = tk.Entry(pwd_row, textvariable=self._wifi_pwd_var,
                                   bg=COLORS["input_bg"], fg="white",
                                   font=("Segoe UI", 13), relief="flat",
                                   show="*", width=18)
        self._pwd_entry.pack(side="left", fill="x", expand=True, padx=6, ipady=5)
        # Reset inactivity timer on every keystroke in the entry field
        self._wifi_pwd_var.trace_add("write", lambda *_: [
            self.cancel_admin_timeout(),
            self.start_admin_timeout(30000)
        ])

        self._eye_btn = tk.Button(pwd_row, text="SHOW", bg=COLORS["admin_btn"],
                                  fg="white", font=("Segoe UI", 11), bd=0, padx=4,
                                  command=lambda: self._toggle_pwd_visibility())
        self._eye_btn.pack(side="right", padx=2, ipady=4)

        # ── Status label (for connecting / error feedback) ──
        self._wifi_status_label = tk.Label(kp, text="", bg=COLORS["bg"],
                                           fg="#f39c12",
                                           font=("Segoe UI", 9, "italic"))
        self._wifi_status_label.pack(pady=(0, 2))

        # ── Keyboard frame ──
        self._kb_frame = tk.Frame(kp, bg=COLORS["bg"])
        self._kb_frame.pack(fill="both", expand=True, padx=4, pady=(0, 4))

        self._kb_shift = False
        self._kb_special = False
        self._current_ssid = ssid
        self._current_kp = kp
        self._build_keyboard(self._kb_frame)

    def _close_wifi_keypad(self, kp):
        try:
            kp.grab_release()
            kp.destroy()
        except Exception:
            pass
        self.active_keypad = None

    def _toggle_pwd_visibility(self):
        self._wifi_pwd_show = not self._wifi_pwd_show
        self._pwd_entry.config(show="" if self._wifi_pwd_show else "*")
        self._eye_btn.config(text="HIDE" if self._wifi_pwd_show else "SHOW")

    def _build_keyboard(self, frame):
        """Render QWERTY or symbol keyboard rows into `frame`."""
        for w in frame.winfo_children():
            w.destroy()

        if self._kb_special:
            rows = [
                ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
                ["!", "@", "#", "$", "%", "^", "&", "*", "(", ")"],
                ["_", "+", "=", "{", "}", "[", "]", "\\", "|", "DEL"],
                ["ABC", ";", ":", "'", '"', "SPACE", "<", ">", "/", "OK"],
            ]
        elif self._kb_shift:
            rows = [
                ["Q", "W", "E", "R", "T", "Y", "U", "I", "O", "P"],
                ["A", "S", "D", "F", "G", "H", "J", "K", "L", "DEL"],
                ["v/", "Z", "X", "C", "V", "B", "N", "M", "@", "_"],
                ["123", "-", ".", "SPACE", ",", "!", "?", "OK"],
            ]
        else:
            rows = [
                ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
                ["a", "s", "d", "f", "g", "h", "j", "k", "l", "DEL"],
                ["/^", "z", "x", "c", "v", "b", "n", "m", "@", "_"],
                ["123", "-", ".", "SPACE", ",", "!", "?", "OK"],
            ]

        for row in rows:
            rw = tk.Frame(frame, bg=COLORS["bg"])
            rw.pack(fill="x", pady=1)
            for col_idx, key in enumerate(row):
                self._make_kb_button(rw, key, col_idx)
            for i in range(len(row)):
                rw.columnconfigure(i, weight=1)

    def _make_kb_button(self, parent, key, col_idx):
        """Create a single keyboard button and grid it."""
        if key == "DEL":
            btn = tk.Button(parent, text="DEL", bg="#555555", fg="white",
                            font=("Segoe UI", 9, "bold"), bd=0, relief="flat",
                            command=lambda: self._kb_key_press(
                                self._wifi_pwd_var.get()[:-1], replace=True))
        elif key == "SPACE":
            btn = tk.Button(parent, text="SPACE", bg=COLORS["admin_btn"], fg="white",
                            font=("Segoe UI", 9, "bold"), bd=0, relief="flat",
                            command=lambda: self._kb_key_press(" "))
        elif key == "OK":
            btn = tk.Button(parent, text="CONNECT", bg="#27ae60", fg="white",
                            font=("Segoe UI", 9, "bold"), bd=0, relief="flat",
                            command=lambda: self._wifi_connect(
                                self._current_ssid,
                                self._wifi_pwd_var.get(),
                                self._current_kp))
        elif key in ("/^", "v/"):
            color = COLORS["save"] if self._kb_shift else COLORS["admin_btn"]
            label = "A^" if key == "/^" else "az"
            btn = tk.Button(parent, text=label, bg=color, fg="white",
                            font=("Segoe UI", 9, "bold"), bd=0, relief="flat",
                            command=lambda: self._kb_toggle_shift())
        elif key == "123":
            btn = tk.Button(parent, text="123", bg=COLORS["admin_btn"], fg="white",
                            font=("Segoe UI", 9, "bold"), bd=0, relief="flat",
                            command=lambda: self._kb_toggle_special())
        elif key == "ABC":
            btn = tk.Button(parent, text="ABC", bg=COLORS["save"], fg="white",
                            font=("Segoe UI", 9, "bold"), bd=0, relief="flat",
                            command=lambda: self._kb_toggle_special())
        else:
            btn = tk.Button(parent, text=key, bg=COLORS["admin_btn"], fg="white",
                            font=("Segoe UI", 11, "bold"), bd=0, relief="flat",
                            command=lambda c=key: self._kb_key_press(c))
        btn.grid(row=0, column=col_idx, sticky="nsew", padx=1, ipady=5)

    def _kb_key_press(self, char, replace=False):
        """Handle any key press: reset inactivity timer, then append/replace."""
        # Cancel the admin timeout while keyboard is in active use
        self.cancel_admin_timeout()
        if replace:
            self._wifi_pwd_var.set(char)
        else:
            self._wifi_pwd_var.set(self._wifi_pwd_var.get() + char)
        # Restart a generous timeout (30 s) after each keypress
        self.start_admin_timeout(30000)

    def _kb_toggle_shift(self):
        self.cancel_admin_timeout()
        self._kb_shift = not self._kb_shift
        self._kb_special = False
        self._build_keyboard(self._kb_frame)
        self.start_admin_timeout(30000)

    def _kb_toggle_special(self):
        self.cancel_admin_timeout()
        self._kb_special = not self._kb_special
        self._kb_shift = False
        self._build_keyboard(self._kb_frame)
        self.start_admin_timeout(30000)

    # ── Connection logic ──

    def _wifi_connect(self, ssid, password, dialog):
        """Connect to a WiFi network; dialog is the Toplevel to close (or None)."""
        self.start_admin_timeout()
        if hasattr(self, "_wifi_status_label"):
            try:
                self._wifi_status_label.config(
                    text=f"  Connecting to {ssid}…", fg="#f39c12")
            except Exception:
                pass
        self.wifi_current_label.config(text=f"  Connecting to {ssid}…", fg="#f39c12")

        def _worker():
            try:
                cmd = ["nmcli", "dev", "wifi", "connect", ssid]
                if password:
                    cmd += ["password", password]
                r = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
                success = r.returncode == 0 and (
                    "successfully" in r.stdout.lower() or
                    "activated" in r.stdout.lower())
                if success:
                    self.after_idle(lambda: self._on_wifi_connected(ssid, dialog))
                else:
                    err = (r.stderr.strip() or r.stdout.strip() or "Unknown error")
                    # ── Remove any partial/bad profile that nmcli may have saved ──
                    if password:  # only clean up when a new password was attempted
                        try:
                            subprocess.run(
                                ["nmcli", "connection", "delete", ssid],
                                capture_output=True, timeout=5)
                        except Exception:
                            pass
                    self.after_idle(lambda e=err: self._on_wifi_failed(ssid, e, dialog))
            except subprocess.TimeoutExpired:
                # Clean up partial profile on timeout too
                if password:
                    try:
                        subprocess.run(
                            ["nmcli", "connection", "delete", ssid],
                            capture_output=True, timeout=5)
                    except Exception:
                        pass
                self.after_idle(lambda: self._on_wifi_failed(
                    ssid, "Connection timed out", dialog))
            except Exception as exc:
                self.after_idle(lambda e=str(exc): self._on_wifi_failed(
                    ssid, e, dialog))
        threading.Thread(target=_worker, daemon=True).start()

    def _on_wifi_connected(self, ssid, dialog):
        if dialog:
            self._close_wifi_keypad(dialog)
        self.wifi_current_label.config(text=f"  {ssid}", fg="#27ae60")
        self._refresh_saved_networks()
        check_network_connectivity()

    def _on_wifi_failed(self, ssid, error, dialog):
        print(f"[WiFi] Failed to connect to '{ssid}': {error}")
        msg = "Wrong password or connection failed"
        if hasattr(self, "_wifi_status_label"):
            try:
                self._wifi_status_label.config(
                    text=f"  ✗ {msg}", fg="#c0392b")
            except Exception:
                pass
        self.wifi_current_label.config(text=f"  Failed: {ssid}", fg="#c0392b")
        # Reset status after 4 seconds
        self.after(4000, self._refresh_wifi_status)

    def _wifi_forget_confirm(self, ssid):
        """Show confirmation dialog before forgetting a network."""
        self.start_admin_timeout()
        if self.active_keypad:
            try:
                self.active_keypad.destroy()
            except Exception:
                pass

        dlg = tk.Toplevel(self)
        dlg.overrideredirect(True)
        dlg.configure(bg=COLORS["bg"])
        dlg.geometry(f"300x140+{self.winfo_x() + 90}+{self.winfo_y() + 90}")
        dlg.lift()
        dlg.grab_set()
        self.active_keypad = dlg

        tk.Label(dlg, text="Forget Network?", bg=COLORS["bg"], fg="white",
                 font=("Segoe UI", 13, "bold")).pack(pady=(14, 4))
        tk.Label(dlg, text=f"  {ssid}", bg=COLORS["bg"], fg="#f39c12",
                 font=("Segoe UI", 11)).pack(pady=(0, 12))

        btn_row = tk.Frame(dlg, bg=COLORS["bg"])
        btn_row.pack(fill="x", padx=12)

        def _do_forget():
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass
            self.active_keypad = None
            self._wifi_forget(ssid)

        def _cancel():
            try:
                dlg.grab_release()
                dlg.destroy()
            except Exception:
                pass
            self.active_keypad = None

        tk.Button(btn_row, text="YES, FORGET", bg=COLORS["logout"], fg="white",
                  font=FONT_BOLD, bd=0, command=_do_forget
                  ).pack(side="left", fill="x", expand=True, padx=(0, 4), ipady=8)
        tk.Button(btn_row, text="CANCEL", bg=COLORS["cancel"], fg="white",
                  font=FONT_BOLD, bd=0, command=_cancel
                  ).pack(side="left", fill="x", expand=True, ipady=8)

    def _wifi_forget(self, ssid):
        """Delete a saved WiFi connection via nmcli (called after confirmation)."""
        def _worker():
            try:
                subprocess.run(
                    ["nmcli", "connection", "delete", ssid],
                    capture_output=True, text=True, timeout=10)
                self.after_idle(self._refresh_saved_networks)
                self.after_idle(self._refresh_wifi_status)
            except Exception as e:
                print(f"[WiFi] Forget error: {e}")
        threading.Thread(target=_worker, daemon=True).start()

    def _start_wifi_auto_reconnect(self):
        """Background thread: every 45 s, if not connected, connect to the
        saved network with the highest available signal strength."""
        def _loop():
            # Initial delay so the app is fully up before first attempt
            time.sleep(20)
            while True:
                try:
                    self._wifi_auto_reconnect_once()
                except Exception as e:
                    print(f"[WiFi AutoReconnect] Error: {e}")
                time.sleep(45)
        threading.Thread(target=_loop, daemon=True).start()

    def _wifi_auto_reconnect_once(self):
        """Single auto-reconnect attempt (runs in background thread)."""
        # 1. Are we already connected?
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "ACTIVE,SSID", "dev", "wifi"],
                capture_output=True, text=True, timeout=5)
            for line in r.stdout.strip().splitlines():
                if line.startswith("yes:"):
                    return  # Already connected — nothing to do
        except Exception:
            return

        # 2. Get saved WiFi profiles
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "NAME,TYPE", "connection", "show"],
                capture_output=True, text=True, timeout=5)
            saved = set()
            for line in r.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) >= 2 and "802-11-wireless" in parts[1]:
                    saved.add(parts[0].strip())
        except Exception:
            return

        if not saved:
            return

        # 3. Scan available networks
        try:
            r = subprocess.run(
                ["nmcli", "-t", "-f", "SSID,SIGNAL", "dev", "wifi", "list",
                 "--rescan", "yes"],
                capture_output=True, text=True, timeout=20)
            candidates = []  # (signal, ssid)
            seen = set()
            for line in r.stdout.strip().splitlines():
                parts = line.split(":")
                if len(parts) < 2:
                    continue
                ssid = parts[0].strip()
                try:
                    signal = int(parts[1].strip())
                except ValueError:
                    signal = 0
                if ssid and ssid in saved and ssid not in seen:
                    seen.add(ssid)
                    candidates.append((signal, ssid))
        except Exception:
            return

        if not candidates:
            return

        # 4. Pick highest-signal saved network and connect
        candidates.sort(reverse=True)
        best_ssid = candidates[0][1]
        print(f"[WiFi AutoReconnect] Connecting to best saved network: {best_ssid} "
              f"(signal {candidates[0][0]})")
        try:
            subprocess.run(
                ["nmcli", "dev", "wifi", "connect", best_ssid],
                capture_output=True, text=True, timeout=30)
            # Refresh UI label if panel is open
            self.after_idle(self._refresh_wifi_status)
            check_network_connectivity()
        except Exception as e:
            print(f"[WiFi AutoReconnect] Connect failed: {e}")

    def on_close(self):
        if getattr(self, 'scan_led', None):
            self.scan_led.off()
        if self.worker: self.worker.stop()
        self.destroy()

if __name__ == "__main__":
    app = FaceAuthApp()
    app.mainloop()