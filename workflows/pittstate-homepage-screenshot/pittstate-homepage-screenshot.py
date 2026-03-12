"""Workflow: pittstate-homepage-screenshot
Generated from Scout session on 2026-03-07.
Session: ae51c197528b | Actions: 1 (navigate + screenshot)

Setup:
    cd workflows/pittstate-homepage-screenshot
    pip install -r requirements.txt
    python pittstate-homepage-screenshot.py
"""
import base64
import os
import random
import time

from botasaurus_driver import Driver, cdp

# --- Configuration ---
BASE_URL = "https://pittstate.edu"
DOWNLOAD_DIR = os.path.join(os.path.expanduser("~"), "Downloads")


def run_workflow():
    """Navigate to Pittsburg State University and screenshot the home page."""
    driver = Driver(headless=False)
    driver.enable_human_mode()
    try:
        # Step 1: Navigate to Pittsburg State University
        driver.get(BASE_URL)
        driver.short_random_sleep()

        # Step 2: Wait for the page to fully render
        driver.wait_for_element("body", wait=10)
        time.sleep(random.uniform(0.3, 0.8))

        # Step 3: Capture screenshot and save to Downloads
        result = driver.run_cdp_command(
            cdp.page.capture_screenshot(format_="png")
        )
        screenshot_path = os.path.join(DOWNLOAD_DIR, "pittstate-homepage.png")
        with open(screenshot_path, "wb") as f:
            f.write(base64.b64decode(result))
        print(f"Screenshot saved to {screenshot_path}")

    finally:
        driver.close()


if __name__ == "__main__":
    run_workflow()
