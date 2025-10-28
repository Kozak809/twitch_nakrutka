import argparse
import random
import time
import os
import json
import sys
import signal
from urllib.parse import urlparse
from multiprocessing import Process, current_process, freeze_support
from pathlib import Path

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.common.by import By
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from webdriver_manager.chrome import ChromeDriverManager


def parse_args():
    parser = argparse.ArgumentParser(
        description="Twitch chat bot spawner via Selenium.",
        prefix_chars='-'  # Support both single and double dash
    )
    parser.add_argument("-url", "--url", required=True, help="Twitch channel URL, e.g. https://www.twitch.tv/somechannel")
    parser.add_argument("-greetings", "--greetings", default="hi.txt", help="Path to greetings file (first message)")
    parser.add_argument("-phrases", "--phrases", default="phrases.txt", help="Path to phrases file (regular messages)")
    parser.add_argument("-headless", "--headless", action="store_true", help="Run Chrome in headless mode")
    parser.add_argument("-users-dir", "--users-dir", default="users", help="Directory containing user cookie files")
    parser.add_argument("-min-interval", "--min-interval", type=int, default=20, help="Minimum seconds between messages")
    parser.add_argument("-max-interval", "--max-interval", type=int, default=120, help="Maximum seconds between messages")
    return parser.parse_args()


def read_lines(path: str):
    if not os.path.isabs(path):
        path = os.path.join(os.getcwd(), path)
    if not os.path.exists(path):
        raise FileNotFoundError(f"File not found: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return [line.strip() for line in f if line.strip()]


def load_user_cookies(user_file: Path) -> dict:
    """Load cookies from user file in format: cookie_name cookie_value"""
    cookies = {}
    if user_file.exists():
        with open(user_file, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#'):
                    parts = line.split(maxsplit=1)
                    if len(parts) == 2:
                        cookies[parts[0]] = parts[1]
    return cookies


def save_user_cookies(user_file: Path, cookies: dict):
    """Save cookies to user file in format: cookie_name cookie_value"""
    with open(user_file, 'w', encoding='utf-8') as f:
        for name, value in sorted(cookies.items()):
            f.write(f"{name} {value}\n")


def get_all_cookies_from_driver(driver: webdriver.Chrome) -> dict:
    """Extract all cookies from driver as dict"""
    cookies = {}
    for cookie in driver.get_cookies():
        cookies[cookie['name']] = cookie['value']
    return cookies


def setup_users_directory(users_dir: str) -> Path:
    """Create users directory if it doesn't exist"""
    path = Path(users_dir)
    path.mkdir(exist_ok=True)
    return path


def get_user_files(users_dir: Path) -> list[Path]:
    """Get all user cookie files from users directory"""
    return sorted(users_dir.glob('user*.txt'))


def get_channel_name_from_url(url: str) -> str:
    parsed = urlparse(url)
    # Expecting path like "/channel" or "/videos" etc; we only need the first segment
    segments = [seg for seg in parsed.path.split('/') if seg]
    if not segments:
        raise ValueError("Cannot parse channel name from URL. Expected format like https://www.twitch.tv/<channel>")
    return segments[0]


def create_driver(headless: bool = False) -> webdriver.Chrome:
    options = webdriver.ChromeOptions()
    if headless:
        # Use new headless for Chrome 109+
        options.add_argument("--headless=new")
    options.add_argument("--disable-blink-features=AutomationControlled")
    options.add_argument("--disable-gpu")
    options.add_argument("--no-sandbox")
    options.add_argument("--disable-dev-shm-usage")
    options.add_argument("--lang=ru-RU,ru")
    options.add_experimental_option("excludeSwitches", ["enable-automation"]) 
    options.add_experimental_option('useAutomationExtension', False)

    service = Service(ChromeDriverManager().install())
    driver = webdriver.Chrome(service=service, options=options)
    driver.set_window_size(1200, 900)
    return driver


def load_cookies_to_driver(driver: webdriver.Chrome, cookies: dict):
    """Load all cookies into driver"""
    # Go to base domain first so we can set cookies
    driver.get("https://www.twitch.tv/")
    time.sleep(1)
    
    # Add all cookies
    for name, value in cookies.items():
        try:
            driver.add_cookie({
                "name": name,
                "value": value,
                "domain": ".twitch.tv",
                "path": "/",
                "secure": True,
            })
        except Exception as e:
            print(f"Warning: Could not add cookie {name}: {e}")


def wait_for_chat_ready(driver: webdriver.Chrome):
    """Wait for chat input to be ready on the main channel page"""
    # Wait for the actual editor div to be ready
    WebDriverWait(driver, 30).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "div.chat-wysiwyg-input__editor[data-a-target='chat-input']"))
    )


def accept_consent_if_present(driver: webdriver.Chrome, timeout: int = 5):
    try:
        # Try to accept consent if a banner appears (selector may vary over time)
        WebDriverWait(driver, timeout).until(
            EC.element_to_be_clickable((By.CSS_SELECTOR, "button[aria-label='Accept']"))
        ).click()
    except Exception:
        pass


def accept_chat_rules_if_present(driver: webdriver.Chrome, timeout: int = 10):
    """Accept chat rules modal if it appears"""
    try:
        # Look for "Все ясно!" button or similar chat rules acceptance
        xpath_selectors = [
            "//button[contains(text(), 'Все ясно')]",
            "//button[contains(text(), 'Got it')]", 
            "//button[contains(text(), 'OK')]",
            "//button[contains(text(), 'Понятно')]",
            "//button[contains(text(), 'Согласен')]",
            "//div[@data-a-target='chat-rules']//button",
            "//div[contains(@class, 'chat-rules')]//button"
        ]
        
        css_selectors = [
            "button[data-a-target='chat-rules-ok-button']",
            "button[data-test-selector='chat-rules-ok-button']", 
            "div[data-a-target='chat-rules'] button",
            "div[role='dialog'] button",
            ".chat-rules button"
        ]
        
        # Try XPath selectors first (better for text matching)
        for selector in xpath_selectors:
            try:
                element = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.XPATH, selector))
                )
                element.click()
                print("Chat rules modal accepted (XPath)")
                time.sleep(1)
                return
            except Exception:
                continue
        
        # Try CSS selectors
        for selector in css_selectors:
            try:
                element = WebDriverWait(driver, 2).until(
                    EC.element_to_be_clickable((By.CSS_SELECTOR, selector))
                )
                element.click()
                print("Chat rules modal accepted (CSS)")
                time.sleep(1)
                return
            except Exception:
                continue
                
        # Fallback: try to find any button in a modal/overlay with relevant text
        try:
            buttons = driver.find_elements(By.CSS_SELECTOR, "div[role='dialog'] button, div[data-a-target*='modal'] button, button")
            for button in buttons:
                button_text = button.text.lower()
                if any(text in button_text for text in ['ясно', 'got it', 'ok', 'понятно', 'согласен', 'accept', 'принять']):
                    button.click()
                    print(f"Chat rules modal accepted (fallback): {button.text}")
                    time.sleep(1)
                    return
        except Exception:
            pass
            
        print("No chat rules modal found")
            
    except Exception as e:
        print(f"Error checking for chat rules modal: {e}")
        pass


def send_chat_message(driver: webdriver.Chrome, message: str):
    # Find the actual editor div with the correct class
    chat_input = WebDriverWait(driver, 20).until(
        EC.element_to_be_clickable((By.CSS_SELECTOR, "div.chat-wysiwyg-input__editor[data-a-target='chat-input']"))
    )
    
    # Click on the chat input to focus it (this triggers the rules modal)
    print("Clicking on chat input...")
    chat_input.click()
    time.sleep(2)  # Wait for potential modal to appear
    
    print(f"Typing message: {message}")
    
    # Simple approach: just click and type
    # Make sure the element is focused
    chat_input.click()
    time.sleep(0.5)
    
    # Clear any existing content first
    chat_input.send_keys(Keys.CONTROL + "a")  # Select all
    time.sleep(0.2)
    chat_input.send_keys(Keys.DELETE)  # Delete selected content
    time.sleep(0.5)
    
    # Type the message
    chat_input.send_keys(message)
    time.sleep(1)
    
    print("Pressing Enter to send...")
    # Send Enter
    chat_input.send_keys(Keys.ENTER)
    time.sleep(1)


def worker(user_file: Path, channel_url: str, greetings: list[str], phrases: list[str], 
           headless: bool, min_interval: int, max_interval: int, delay: int = 0):
    """Worker process that runs indefinitely, sending messages at random intervals"""
    # Add small delay to stagger browser launches
    if delay > 0:
        time.sleep(delay)
        
    proc_name = current_process().name
    driver = None
    
    try:
        # Load user cookies
        print(f"[{proc_name}] Loading cookies from {user_file.name}...")
        cookies = load_user_cookies(user_file)
        
        if not cookies:
            print(f"[{proc_name}] ERROR: No cookies found in {user_file.name}")
            return
            
        if 'auth-token' not in cookies:
            print(f"[{proc_name}] WARNING: No auth-token found in {user_file.name}")
        
        print(f"[{proc_name}] Starting browser...")
        driver = create_driver(headless=headless)
        
        # Load all cookies
        load_cookies_to_driver(driver, cookies)
        
        # Go directly to the channel page
        print(f"[{proc_name}] Opening channel: {channel_url}")
        driver.get(channel_url)
        accept_consent_if_present(driver, timeout=5)
        
        # Wait for chat to load
        print(f"[{proc_name}] Waiting for chat to load...")
        wait_for_chat_ready(driver)
        
        # Handle initial modal if present
        try:
            chat_input = WebDriverWait(driver, 10).until(
                EC.element_to_be_clickable((By.CSS_SELECTOR, "div.chat-wysiwyg-input__editor[data-a-target='chat-input']"))
            )
            chat_input.click()
            time.sleep(2)
            accept_chat_rules_if_present(driver, timeout=5)
        except Exception as e:
            print(f"[{proc_name}] Error during initial setup: {e}")
        
        print(f"[{proc_name}] Starting infinite message loop (interval: {min_interval}-{max_interval}s)...")
        message_count = 0
        
        # Infinite loop - send messages until manually stopped
        while True:
            try:
                # First message is a greeting, then use regular phrases
                if message_count == 0:
                    message = random.choice(greetings)
                    print(f"[{proc_name}] Sending greeting: {message}")
                else:
                    message = random.choice(phrases)
                    print(f"[{proc_name}] Sending message #{message_count}: {message}")
                
                message_count += 1
                
                send_chat_message(driver, message)
                
                # Save cookies after each message to prevent data loss
                try:
                    updated_cookies = get_all_cookies_from_driver(driver)
                    save_user_cookies(user_file, updated_cookies)
                    print(f"[{proc_name}] Cookies auto-saved ({len(updated_cookies)} cookies)")
                except Exception as e:
                    print(f"[{proc_name}] Warning: Could not auto-save cookies: {e}")
                
                # Random interval between messages
                interval = random.randint(min_interval, max_interval)
                print(f"[{proc_name}] Message sent. Waiting {interval}s until next message...")
                time.sleep(interval)
                
            except KeyboardInterrupt:
                print(f"[{proc_name}] Received stop signal")
                break
            except Exception as e:
                print(f"[{proc_name}] Error during message sending: {e}")
                print(f"[{proc_name}] Waiting 30s before retry...")
                time.sleep(30)
                
    except KeyboardInterrupt:
        print(f"[{proc_name}] Interrupted by user")
    except Exception as e:
        print(f"[{proc_name}] Fatal error: {e}")
    finally:
        if driver:
            # Save cookies first, before closing driver
            try:
                print(f"[{proc_name}] Saving updated cookies to {user_file.name}...")
                updated_cookies = get_all_cookies_from_driver(driver)
                save_user_cookies(user_file, updated_cookies)
                print(f"[{proc_name}] Cookies saved successfully ({len(updated_cookies)} cookies)")
            except Exception as e:
                print(f"[{proc_name}] Warning: Could not save cookies: {e}")
            
            # Then close the driver
            try:
                driver.quit()
                print(f"[{proc_name}] Browser closed")
            except Exception as e:
                print(f"[{proc_name}] Warning: Error closing browser: {e}")


def main():
    args = parse_args()
    
    # Setup users directory
    users_dir = setup_users_directory(args.users_dir)
    print(f"Users directory: {users_dir.absolute()}")
    
    # Get user files
    user_files = get_user_files(users_dir)
    
    if not user_files:
        print(f"\nERROR: No user files found in {users_dir}/")
        print(f"\nPlease create user cookie files in the following format:")
        print(f"  {users_dir}/user1.txt")
        print(f"  {users_dir}/user2.txt")
        print(f"  etc.")
        print(f"\nEach file should contain cookies in format:")
        print(f"  cookie_name cookie_value")
        print(f"\nExample:")
        print(f"  auth-token q7vvuam6zrc8i5vagfbvfogt76ncwz")
        print(f"  login myusername")
        print(f"  persistent 1373358332%3A%3Avlzdkj2oewpuecsexs19bc224iv6ie")
        sys.exit(1)
    
    # Load messages
    greetings = read_lines(args.greetings)
    phrases = read_lines(args.phrases)
    
    print(f"\nLoaded {len(user_files)} user profiles, {len(greetings)} greetings, and {len(phrases)} phrases.")
    print(f"Message interval: {args.min_interval}-{args.max_interval} seconds")
    print(f"Channel: {args.url}")
    print(f"\nStarting {len(user_files)} browsers...")
    print(f"Press Ctrl+C to stop all bots\n")

    # Launch all processes in parallel with small delays to stagger browser startup
    procs: list[Process] = []
    
    try:
        for i, user_file in enumerate(user_files):
            # Small delay between launches to avoid resource conflicts
            delay = i * 3  # 3 seconds between each browser launch
            p = Process(
                target=worker, 
                args=(user_file, args.url, greetings, phrases, args.headless, 
                      args.min_interval, args.max_interval, delay)
            )
            p.daemon = False
            p.start()
            procs.append(p)
            print(f"Started bot {i+1}/{len(user_files)}: {user_file.name}")

        print(f"\nAll {len(user_files)} bots are running!")
        print("Bots will run indefinitely until you press Ctrl+C\n")
        
        # Wait for all processes (they run indefinitely until interrupted)
        for p in procs:
            p.join()
            
    except KeyboardInterrupt:
        print("\n\nStopping all bots...")
        for p in procs:
            if p.is_alive():
                p.terminate()
        
        # Wait for all to finish
        for p in procs:
            p.join(timeout=5)
        
        print("All bots stopped!")


if __name__ == "__main__":
    freeze_support()
    main()
