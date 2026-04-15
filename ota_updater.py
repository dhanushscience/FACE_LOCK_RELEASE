import os
import subprocess
import requests  # For checking remote version if using a web-hosted repo
import logging
from packaging import version  # For version comparison (install via pip)

# Assuming your repo is on GitHub; adjust for private repos
REPO_URL = "https://github.com/dhanushscience/FACE_LOCK_RELEASE"  # Replace with your actual repo URL
VERSION_FILE_URL = f"{REPO_URL}/raw/main/version.txt"  # Remote version file
LOCAL_VERSION_FILE = "/home/ps/Downloads/FACE_LOCK_RELEASE/current_version.txt"
APP_DIR = "/home/ps/Downloads/FACE_LOCK_RELEASE"

logger = logging.getLogger("OTAUpdater")

def get_current_version():
    """Read local version from file."""
    if os.path.exists(LOCAL_VERSION_FILE):
        with open(LOCAL_VERSION_FILE, 'r') as f:
            return f.read().strip()
    return "0.0.0"  # Default if no file

def get_latest_version():
    """Fetch latest version from remote repo."""
    try:
        response = requests.get(VERSION_FILE_URL, timeout=10)
        response.raise_for_status()
        return response.text.strip()
    except Exception as e:
        logger.error(f"Failed to fetch latest version: {e}")
        return None

def check_for_updates():
    """Check if an update is available."""
    current = get_current_version()
    latest = get_latest_version()
    if latest and version.parse(latest) > version.parse(current):
        return True, latest
    return False, current

def perform_update():
    """Pull updates from Git and update dependencies. Returns True on success."""
    try:
        # Change to app directory
        os.chdir(APP_DIR)

        # Pull latest changes
        result = subprocess.run(["git", "pull", "origin", "main"], capture_output=True, text=True, timeout=60)
        if result.returncode != 0:
            logger.error(f"Git pull failed: {result.stderr}")
            return False

        # Update dependencies if requirements.txt exists
        if os.path.exists("requirements.txt"):
            subprocess.run(["pip", "install", "-r", "requirements.txt"], check=True)

        # Update local version file
        latest = get_latest_version()
        with open(LOCAL_VERSION_FILE, 'w') as f:
            f.write(latest)

        logger.info("Update successful.")
        return True
    except Exception as e:
        logger.error(f"Update failed: {e}")
        return False