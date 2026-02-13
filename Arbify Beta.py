import time
from datetime import datetime
import os
import random
import warnings
import re
import json
import threading
from queue import Queue, Empty
from urllib.parse import urljoin, urlparse, parse_qs, unquote
import base64
import platform
import tempfile
import shutil
import uuid  # correlation_id-hoz
from collections import deque
import sys
import asyncio

# Try to import aiohttp for async parallel URL extraction
try:
    import aiohttp
    AIOHTTP_AVAILABLE = True
except ImportError:
    AIOHTTP_AVAILABLE = False
    print("⚠️ aiohttp not available - install with: pip install aiohttp")
    print("   Parallel URL extraction will be slower without it.")

warnings.filterwarnings("ignore", category=ResourceWarning)

import requests
import undetected_chromedriver as uc
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.common.keys import Keys
from selenium.webdriver.common.action_chains import ActionChains
from selenium.common.exceptions import (
    StaleElementReferenceException,
    NoSuchWindowException,
    WebDriverException,
)

# Supabase SDK import for direct database queries
try:
    from supabase import create_client
    SUPABASE_SDK_AVAILABLE = True
    print("✅ Supabase SDK loaded successfully")
except ImportError as e:
    SUPABASE_SDK_AVAILABLE = False
    print(f"⚠️ Supabase SDK import failed: {e}")
    print("   Run: pip install supabase")
    print("   Database reconciliation will use fallback method.")
except Exception as e:
    SUPABASE_SDK_AVAILABLE = False
    print(f"⚠️ Unexpected error importing Supabase SDK: {e}")
    print("   Database reconciliation will use fallback method.")


# --- DEBUG kapcsoló HTTP hívásokhoz ---
DEBUG_HTTP = os.getenv("DEBUG_HTTP", "0") == "1"

IS_MAC = (platform.system() == "Darwin")
KEY_MOD = Keys.COMMAND if IS_MAC else Keys.CONTROL

# --- Driver életjelző ---
DRIVER_DEAD = False

def _is_driver_connection_error(exc: Exception) -> bool:
    """
    Felismeri a klasszikus 'HTTPConnectionPool / WinError 10061 / Max retries exceeded' típusú hibákat,
    amikor a WebDriver HTTP szerver már halott.
    """
    txt = str(exc)
    if "HTTPConnectionPool" in txt and "/window/handles" in txt:
        return True
    if "Failed to establish a new connection" in txt:
        return True
    if "WinError 10061" in txt:
        return True
    if "Max retries exceeded with url: /session/" in txt:
        return True
    return False


def _safe_window_handles(label: str):
    """
    driver.window_handles biztonságos wrapper:
    - DRIVER_DEAD vagy driver is None → üres lista
    - driver/window_handles hiba esetén:
        - ha connection error → DRIVER_DEAD=True
        - logol, és üres listát ad vissza
    """
    global driver, DRIVER_DEAD

    if DRIVER_DEAD or driver is None:
        return []

    try:
        return driver.window_handles
    except WebDriverException as e:
        msg = str(getattr(e, "msg", str(e))).splitlines()[0]
        if _is_driver_connection_error(e):
            DRIVER_DEAD = True
            warn(f"[win_handles] driver leállt (WebDriverException): {msg} (label={label})")
            return []
        warn(f"[win_handles] hiba: {msg} (label={label})")
        return []
    except Exception as e:
        msg = str(e).splitlines()[0]
        if _is_driver_connection_error(e):
            DRIVER_DEAD = True
            warn(f"[win_handles] driver leállt (Exception): {msg} (label={label})")
            return []
        warn(f"[win_handles] váratlan hiba: {msg} (label={label})")
        return []


# ---------- CONFIG ----------
DEFAULT_BASE = "https://en.surebet.com"
LOGIN_URL = "https://surebet.com/users/sign_in"
CHECK_INTERVAL = 1.25
MAIN_URL = "https://en.surebet.com/surebets"


ACCOUNTS = {
    "acc1": {  # első account
        "email": "nosztalgiakonzol@gmail.com",
        "password": "Pankix123!",
        "profile_dir": os.path.abspath("./profile_surebet_acc1"),
    },
    "acc2": {  # második account
        "email": "secretcodeforme@gmail.com",
        "password": "Pankix123!",
        "profile_dir": os.path.abspath("./profile_surebet_acc2"),
    },
    # "acc3": {  # harmadik account - TEMPORARILY DISABLED
    #     "email": "bodabeni2@gmail.com",
    #     "password": "Pankix123!",
    #     "profile_dir": os.path.abspath("./profile_surebet_acc3"),
    # },
}

ACCOUNT_ROTATE_MIN = float(os.getenv("SB_ACCOUNT_ROTATE_MIN", "30.5"))
ACCOUNT_ROTATION_PAUSE_SEC = 17  # Pause before account rotation
RUNTIME_STATE_FILE = "runtime_state.json"  # persistent timer state

# Runtime state management functions (must be defined before usage below)
def load_runtime_state():
    """
    Betölti a perzisztens futásidő állapotot.
    Tartalmazza:
    - accumulated_minutes: az összes eddig felhalmozott futási idő percben
    - last_session_start: az utolsó session indítási időpontja (epoch)
    - current_account: jelenleg aktív account (acc1/acc2)
    - next_account: következő account váltás célpontja
    - account_rotation_pending: igaz ha account váltás folyamatban van
    """
    default_state = {
        "accumulated_minutes": 0.0,
        "last_session_start": None,
        "current_account": None,
        "next_account": None,
        "account_rotation_pending": False
    }
    
    if os.path.exists(RUNTIME_STATE_FILE):
        try:
            with open(RUNTIME_STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f)
                # Ensure all fields exist (backward compatibility)
                for key, value in default_state.items():
                    if key not in state:
                        state[key] = value
                return state
        except Exception:
            return default_state
    return default_state

def save_runtime_state(state: dict):
    """Elmenti a perzisztens futásidő állapotot."""
    try:
        with open(RUNTIME_STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

# Parancssori argumentum feldolgozás (--acc=acc1 vagy --acc=acc2)
forced_account = None
for arg in sys.argv:
    if arg.startswith("--acc="):
        forced_account = arg.split("=", 1)[1].strip()

# Opcionális: env változóval is válthatsz (SB_ACTIVE_ACCOUNT=acc2)
env_account = os.getenv("SB_ACTIVE_ACCOUNT")

# Betöltjük a runtime state-et a perzisztens account információért
runtime_state_for_account = load_runtime_state()
persisted_account = None

# Ha account rotation volt folyamatban, akkor a next_account-ot használjuk
if runtime_state_for_account.get("account_rotation_pending"):
    persisted_account = runtime_state_for_account.get("next_account")
    print(f"⚠️ Account rotation volt folyamatban - használjuk a tervezett accountot: {persisted_account}")
# Különben a current_account-ot próbáljuk
elif runtime_state_for_account.get("current_account"):
    persisted_account = runtime_state_for_account.get("current_account")
    print(f"💾 Mentett account betöltve: {persisted_account}")

# Account kiválasztás prioritási sorrendben:
# 1. Command-line parameter (--acc=)
# 2. Environment variable (SB_ACTIVE_ACCOUNT)
# 3. Persisted account (runtime_state.json)
# 4. Default (acc1)
if forced_account in ACCOUNTS:
    ACTIVE_ACCOUNT_KEY = forced_account
    print(f"✅ Account forrás: command-line parameter (--acc={forced_account})")
elif env_account in ACCOUNTS:
    ACTIVE_ACCOUNT_KEY = env_account
    print(f"✅ Account forrás: environment variable (SB_ACTIVE_ACCOUNT={env_account})")
elif persisted_account in ACCOUNTS:
    ACTIVE_ACCOUNT_KEY = persisted_account
    print(f"✅ Account forrás: persisted state (mentett állapot)")
else:
    ACTIVE_ACCOUNT_KEY = "acc1"   # default
    print(f"✅ Account forrás: default (nincs mentett állapot)")

ACTIVE_ACCOUNT = ACCOUNTS[ACTIVE_ACCOUNT_KEY]

# Startup logging for debugging
print(f"🔑 Starting with account: {ACTIVE_ACCOUNT_KEY}")
print(f"📂 Profile directory: {ACTIVE_ACCOUNT['profile_dir']}")
print(f"📧 Email: {ACTIVE_ACCOUNT['email']}")
print(f"🎯 Command line args: {sys.argv}")
if forced_account:
    print(f"   Forced account from --acc parameter: {forced_account}")
if env_account:
    print(f"   Environment SB_ACTIVE_ACCOUNT: {env_account}")
print("-" * 60)



WAIT_FOR_REDIRECT = 15
SEEN_FILE = "seen_ids.txt"
FOUND_LINKS_FILE = "found_links.txt"
LINK_CACHE_FILE = "link_cache.json"

# Supabase Edge Functions
SUPABASE_URL = "https://sonudgyyvxncdcganppl.supabase.co"
SUPABASE_ANON_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNvbnVkZ3l5dnhuY2RjZ2FucHBsIiwicm9sZSI6ImFub24iLCJpYXQiOjE3NjAwMzA5NDMsImV4cCI6MjA3NTYwNjk0M30.QhtBEhUYoZU8dukJ2bNcy95bXW7unxln8NPe_13eBQ4"
SUPABASE_SERVICE_KEY = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpc3MiOiJzdXBhYmFzZSIsInJlZiI6InNvbnVkZ3l5dnhuY2RjZ2FucHBsIiwicm9sZSI6InNlcnZpY2Vfcm9sZSIsImlhdCI6MTc2MDAzMDk0MywiZXhwIjoyMDc1NjA2OTQzfQ.6mmHZJ2QS3a4TywxZ-lswdcvwPCF5NCYLe6CuiO8-3A"

SAVE_TIP_URL    = f"{SUPABASE_URL}/functions/v1/save-tip"
UPDATE_TIP_URL  = f"{SUPABASE_URL}/functions/v1/update-tip"
DELETE_TIP_URL  = f"{SUPABASE_URL}/functions/v1/delete-tip"
UPDATE_TIPS_BATCH_URL = f"{SUPABASE_URL}/functions/v1/update-tips-batch"
DELETE_TIPS_BATCH_URL = f"{SUPABASE_URL}/functions/v1/delete-tips-batch"
LIST_ACTIVE_TIPS_URL = f"{SUPABASE_URL}/functions/v1/list-active-tips"  # Új endpoint az adatbázis ID-k lekéréséhez

HTTP_HEADERS = {
    "Content-Type": "application/json",
    "apikey": SUPABASE_ANON_KEY,
    "Authorization": f"Bearer {SUPABASE_ANON_KEY}",
}

# Gyors beállítások
RESOLVE_TIMEOUT = 1.5
RESOLVE_STABLE_PERIOD = 0
RESOLVE_POLL_INTERVAL = 0
HANDLE_WAIT_TIMEOUT = 0.5
HEADLESS = False

# ============================================================================
# 🎨 CSS BETÖLTÉS KIKAPCSOLÁSA (Speed optimization teszt)
# ============================================================================
# Ha True: CSS nem töltődik be (gyorsabb, de csúnya)
# Ha False: CSS töltődik be (lassabb, de szép)
# Könnyen visszaállítható ha problémát okoz!
DISABLE_CSS = True  # Set to False to re-enable CSS if causes problems

# ============================================================================
# 🔄 JAVASCRIPT-BASED PAGE REFRESH (Safer, more natural)
# ============================================================================
# Ha True: JavaScript execution (location.reload()) - természetesebb, kevésbé detektálható
# Ha False: driver.refresh() - hagyományos Selenium módszer
USE_JAVASCRIPT_REFRESH = True  # True = JS execution, False = driver.refresh()

# ============================================================================
# 📦 BATCH OPERATIONS (Faster DOM queries)
# ============================================================================
# Ha True: Több elem egyszerre 1 query-vel (40-60% gyorsabb)
# Ha False: Hagyományos egyenkénti lekérdezés
USE_BATCH_OPERATIONS = True  # True = batch mode, False = traditional

# ============================================================================
# 🎭 USER AGENT ROTATION (Bot detection avoidance)
# ============================================================================
# User Agent lista - véletlenszerűen választva minden Chrome indításkor
USER_AGENTS = [
    # Chrome 120 - Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 119 - Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
    # Chrome 118 - Windows 10
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/118.0.0.0 Safari/537.36",
    # Chrome 120 - Windows 11
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    # Chrome 119 - Windows 11
    "Mozilla/5.0 (Windows NT 11.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/119.0.0.0 Safari/537.36",
]

# ============================================================================
# 🖱️ MOUSE MOVEMENT SIMULATION (Human-like behavior)
# ============================================================================
# Mouse movement szimuláció ritkán (10 percenként)
last_mouse_movement_time = 0
MOUSE_MOVEMENT_INTERVAL = 600  # 10 minutes

# ============================================================================
# 🗂️ ACCOUNT-SPECIFIC CHROME PROFILES (Complete isolation)
# ============================================================================
ACCOUNT_PROFILES = {
    "nosztalgiakonzol": "C:/Chrome/Profile_Nosztalgiakonzol",
    "secretcodeforme": "C:/Chrome/Profile_SecretCodeForMe",
    "default": "C:/Chrome/Profile_Default"
}

# ============================================================================
# 📁 ACCOUNT-SPECIFIC WORKING DIRECTORIES (File isolation)
# ============================================================================
ACCOUNT_WORKING_DIRS = {
    "nosztalgiakonzol": "account_nosztalgiakonzol",
    "secretcodeforme": "account_secretcodeforme",
    "default": "account_default"
}

# ============================================================================
# 🎨 ACCOUNT-SPECIFIC BROWSER FINGERPRINTS (Look like different users)
# ============================================================================
ACCOUNT_FINGERPRINTS = {
    "nosztalgiakonzol": {
        "screen_width": 1920,
        "screen_height": 1080,
        "timezone": "Europe/Budapest",  # Hungary timezone
        "language": "en-US",
        "canvas_noise": True,
        "webgl_vendor": "Intel Inc.",
        "webgl_renderer": "Intel Iris OpenGL Engine"
    },
    "secretcodeforme": {
        "screen_width": 1366,
        "screen_height": 768,
        "timezone": "Europe/Budapest",  # Hungary timezone
        "language": "en-GB",
        "canvas_noise": True,
        "webgl_vendor": "Google Inc. (NVIDIA)",
        "webgl_renderer": "ANGLE (NVIDIA GeForce GTX 1660 Ti)"
    },
    "default": {
        "screen_width": 1536,
        "screen_height": 864,
        "timezone": "Europe/Budapest",  # Hungary timezone
        "language": "en-US",
        "canvas_noise": False,
        "webgl_vendor": "Intel Inc.",
        "webgl_renderer": "Intel HD Graphics"
    }
}

# Current account key (set by command line argument)
current_account = "default"

FIX_URL_WAIT_SEC = 17
NAV_HARD_LIMIT_SEC = 20.0

NAV_DEBUG_INTERVAL = 2.0  # másodpercenkénti NAV debug log (0 = kikapcsolva)

TAB_CLEANUP_INTERVAL = 250.0   # ennyi másodpercenként nézünk rá a nyitott tabokra (3 perc)
TAB_CLEANUP_MIN_AGE = 70.0     # ennél fiatalabb ismeretlen tabot nem zárunk be (biztonsági buffer)

# Egyszerre ennyi tbody-pár / linkpár fusson a NAV workerben
NAV_WORKER_MAX_PAIRS = 11

# Egy párra mennyi ideig várunk maximum (másodpercben)
PAIR_TIMEOUT_SEC = FIX_URL_WAIT_SEC  # 17 mp - optimalizált timeout

# Instant timeout: ha nincs külső target, várjunk minimum ennyi időt megnyitás után
NO_EXTERNAL_TARGET_MIN_WAIT_SEC = 5.0  # 5 mp minimum várakozás

# Milyen gyakran kérdezzük le CDP-vel a Target.getTargets-et (másodperc)
CDP_POLL_INTERVAL = 0.40  # 400 ms - optimalizált polling rate

# Logoljuk-e, ha egy pár mindkét végső linkje megvan és a pár lezárult
LOG_PAIR_DONE = True


OPEN_WITHIN_PAIR_MS = 75             # két link ugyanazon párban: A 0ms, B +80ms
OPEN_PAIR_STAGGER_MS_BASE = 175  

# NAV-specifikus feloldás
NAV_MIN_WAIT = 0.0         # en.surebet.com/nav-on minimum türelmi idő
RESOLVE_TIMEOUT_NAV = 3   # NAV-on hosszabb plafon
NAV_STABLE_AFTER_EXIT = 0.42 # ha kimentünk NAV-ról, ennyit várunk stabilan

# =============================================================================
# 🚀 PERFORMANCE & SAFETY IMPROVEMENTS (4 NEW FEATURES)
# =============================================================================
# These features improve stability, performance, and bot detection avoidance
ENABLE_RESOURCE_LEAK_FIX = True        # Tab cleanup guarantee (finally blocks)
ENABLE_WEBDRIVER_REMOVAL = True        # Bot detection bypass (navigator.webdriver)
ENABLE_CANVAS_RANDOMIZATION = True     # Fingerprint randomization
ENABLE_EFFICIENT_POLLING = True        # Event-driven (less CPU, faster reaction)
# =============================================================================

# =============================================================================
# 🎯 CONTENT HASH CHECKING (Smart Refresh Detection)
# =============================================================================
# Detects real content changes before refreshing pages
# Prevents unnecessary refreshes when content hasn't actually changed
# Works even when server headers (ETag/Last-Modified) are unreliable
ENABLE_CONTENT_HASH_CHECKING = False   # Disabled - using simple time-based refresh with bot-proofing instead
CONTENT_HASH_VERBOSE_LOGGING = True    # Detailed logging (easily toggleable)
CONTENT_HASH_USE_QUICK_CHECK = True    # Use fast signature check before full hash
CONTENT_HASH_ENHANCED_MODE = True      # Enhanced detection (IDs, classes, deletion detection)
CONTENT_HASH_PERIODIC_FULL_CHECK = 10  # Force full hash check every N quick checks
CONTENT_HASH_TRACK_IDS = True          # Track element IDs for better detection
# =============================================================================

# =============================================================================
# TEXT VERIFICATION REMOVED - Using enhanced content-based hash instead
# =============================================================================

# =============================================================================
# 🎯 CONDITIONAL SCRAPING (Only scrape tabs after refresh)
# =============================================================================
# Skips cycling through tabs when content hasn't changed
# Only scrapes tabs after they've been refreshed (have fresh data)
ENABLE_CONDITIONAL_SCRAPING = True     # Only scrape tabs after refresh
SCRAPING_SKIP_LOGGING = True           # Log when tabs are skipped
SCRAPING_FORCE_FIRST_TIME = True       # Force scrape on first encounter
# =============================================================================

# =============================================================================
# 🔄 JSON AUTO-UPDATE (Real-time updates without page refresh)
# =============================================================================
# Fetch JSON and inject HTML into page without full refresh
# Like MAIN page autoupdate, but for GROUP and NEXT pages
ENABLE_JSON_AUTO_UPDATE = True          # Enable JSON auto-update
JSON_UPDATE_INTERVAL = 35               # Update every 35 seconds
JSON_SHOW_UPDATE_TIME = True            # Show "Updated X seconds ago"
# =============================================================================

# =============================================================================
# 🔒 HEADER MINIMIZATION (Bot Detection Reduction)
# =============================================================================
# Uses minimal header set to reduce bot detection
# Removes unnecessary headers like DNT, X-Requested-With
ENABLE_MINIMAL_HEADERS = True          # Use minimal header set
MINIMAL_HEADERS = {
    'User-Agent': '',  # Set dynamically
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'en-US,en;q=0.9'
}
# =============================================================================

# --- BOOTSTRAP FÁZIS: indulás után X másodpercig csak tabnyitás + ID-gyűjtés ---
RUN_STARTED_AT = 0.0        # induláskor beállítjuk __main__-ben
BOOTSTRAP_SEC = 50.0        # legacy, not used in dynamic mode
BOOTSTRAP_CLEANUP_DONE = False  # jelzi, hogy a post-bootstrap cleanup már lefutott-e
BOOTSTRAP_COMPLETED = False      # jelzi, hogy a dinamikus bootstrap befejeződött

def in_bootstrap_phase() -> bool:
    """
    True: amíg a dinamikus bootstrap fut (BOOTSTRAP_COMPLETED == False).
    Ezalatt:
      - NINCS SAVE / UPDATE / DELETE Supabase felé
      - NINCS NAV worker
      - csak main/group/next oldalak nyitása + tbody ID gyűjtés történik
    """
    return not BOOTSTRAP_COMPLETED

# --- ACTIVE / GONE ---
ACTIVE_FILE = "active_ids.txt"
DISAPPEAR_GRACE_SEC = 4.5

# --- DATABASE RECONCILIATION CONFIG ---
DB_RECONCILE_ENABLED = True  # adatbázis és active_ids.txt szinkronizálása induláskor
DB_RECONCILE_MAX_DELETES = 500  # biztonsági limit: max ennyi ID törölhető egy menetben
DB_RECONCILE_DONE = False  # jelzi, hogy a reconciliation már lefutott
DB_RECONCILE_HISTORY_FILE = "db_reconcile_history.txt"  # reconciliation történet logolása

# --- ACCOUNT SWITCH TRIGGERS ---
# (RUNTIME_STATE_FILE and functions moved to line ~131 for early availability)

CONSECUTIVE_FAILED_SAVES_LIMIT = 55  # switch account after this many consecutive failures
consecutive_failed_saves = 0  # counter for consecutive failed saves

# --- UPDATE CONFIG ---
UPDATE_MIN_INTERVAL = 2.0
UPDATE_DECIMALS = 2

# --- GROUP-LINK KEZELÉS ---
GROUP_EMPTY_CLOSE_TB_THRESHOLD = 1
GROUP_REOPEN_BACKOFF_SEC = 120
GROUP_ERR_BACKOFF_SEC = 90
GROUP_SELECTOR = "tbody.surebet_record"

# --- GROUP RÉSZLEGES REFRESH ---
GROUP_REFRESH_MIN = 35
GROUP_REFRESH_MAX = 55
GROUP_REFRESH_SKIP_ON_NEW_SEC = 10

# --- MAIN OLDAL PLAY/REFRESH ---
MAIN_REFRESH_MIN = 50
MAIN_REFRESH_MAX = 75

# --- MAIN PAGINATE WRAPPER REFRESH ---
MAIN_PAGINATE_REFRESH_MIN = 70
MAIN_PAGINATE_REFRESH_MAX = 90

# --- NEXT PAGE KEZELÉS ---
NEXT_REFRESH_MIN = 28
NEXT_REFRESH_MAX = 42
NEXT_SELECTOR = "tbody.surebet_record"
NEXT_EMPTY_CLOSE_TB_THRESHOLD = 0

# --- LOG kapcsoló ---
LOG_ENABLED = True
# Csendesítők a "már nyitva" spamre:
LOG_GROUP_ALREADY_OPEN_VERBOSE = False  # ha True, ír; ha False, elnémítva
LOG_NEXT_ALREADY_OPEN_VERBOSE  = False  # ha True, ír; ha False, elnémítva

# --- NAV backoff ---
NAV_RETRY_BASE = 20.0   # sec
NAV_RETRY_MAX  = 300.0  # sec

# NAV-specifikus időzítések NAV-only feloldáshoz
NAV_LEAVE_TIMEOUT = 3      # max ennyi ideig várunk, hogy elhagyja a surebet.com-ot
NAV_STABLE_PERIOD = 0.0                      # ha >0, ennyit várunk stabilan a külső URL-en mielőtt elfogadjuk
NAV_LEAVE_POLL_INTERVAL = 0.005

# --- ROUND-ROBIN RESOLVER ---
ROUND_ROBIN_MAX_MS = 7000     # meddig pörgünk összesen egy csomagon (ms)
ROBIN_SPIN_SLEEP = 0.15        # 0.0 – tényleg full-gáz pörgetés
MAX_BODY_SNIFF = 1200         # ennyi karakterig nézünk bele a body-ba "not found"-ot keresni

HMAP_MAX_SEC = 60  # max ennyi másodpercet engedünk hmap + URL olvasásra

# --- WINDOW CLOSURE COORDINATION ---
CLOSING_HANDLES = set()  # Ablak handle-ek, amik épp bezáródnak (race condition védelem)

# --- CDP COORDINATION ---
PENDING_CDP_CLOSES = {}  # targetId -> bezárás kezdés időpontja (float)

# --- DIAGNOSTIC LOGGING SYSTEM ---
class DiagnosticLogger:
    """
    Részletes diagnosztikai logolás file-ba minden crash előzményével.
    Automatikus sorszámozott file-ok: diagnostic-log-1.txt, diagnostic-log-2.txt, stb.
    """
    def __init__(self):
        self.log_file = None
        self.log_number = self._get_next_log_number()
        self.operation_buffer = deque(maxlen=100)  # Utolsó 100 művelet memóriában
        self.cdp_stats = {"opens": 0, "closes": 0, "errors": 0}
        self.last_health_check = time.time()
        self.loop_iteration = 0
        self._init_log_file()
    
    def _get_next_log_number(self):
        """Megkeresi a következő szabad log sorszámot"""
        n = 1
        while os.path.exists(f"diagnostic-log-{n}.txt"):
            n += 1
        return n
    
    def _init_log_file(self):
        """Új log file létrehozása"""
        self.log_file = f"diagnostic-log-{self.log_number}.txt"
        with open(self.log_file, "w", encoding="utf-8") as f:
            f.write("=" * 80 + "\n")
            f.write(f"DIAGNOSTIC LOG #{self.log_number}\n")
            f.write(f"Started: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Account: {ACTIVE_ACCOUNT_KEY}\n")
            f.write(f"Python: {sys.version}\n")
            f.write(f"Platform: {platform.system()} {platform.release()}\n")
            f.write("=" * 80 + "\n\n")
        print(f"📋 Diagnostic log: {self.log_file}")
    
    def log_event(self, category: str, message: str, level: str = "INFO"):
        """Esemény logolása file-ba és operation bufferbe"""
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        entry = f"[{timestamp}] [{level}] [{category}] {message}"
        
        # Buffer-be mentés
        self.operation_buffer.append(entry)
        
        # File-ba írás
        try:
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write(entry + "\n")
        except Exception:
            pass  # Ne akadjon el a logolás hibája miatt
    
    def log_cdp_lifecycle(self, action: str, target_id: str = None, url: str = None, error: str = None):
        """CDP target életciklus logolása"""
        parts = [action]
        if target_id:
            parts.append(f"targetId={target_id[:12]}")
        if url:
            parts.append(f"url={url[:80]}")
        if error:
            parts.append(f"error={error[:100]}")
            self.cdp_stats["errors"] += 1
        else:
            if "open" in action.lower():
                self.cdp_stats["opens"] += 1
            elif "close" in action.lower():
                self.cdp_stats["closes"] += 1
        
        self.log_event("CDP", " | ".join(parts), "ERROR" if error else "INFO")
    
    def log_session_health(self, window_count: int, active_targets: int = None, memory_mb: int = None):
        """Session egészségállapot periodic logolása"""
        now = time.time()
        if now - self.last_health_check < 30:  # Max 30 másodpercenként
            return
        
        self.last_health_check = now
        parts = [f"windows={window_count}"]
        if active_targets is not None:
            parts.append(f"targets={active_targets}")
        if memory_mb is not None:
            parts.append(f"memory={memory_mb}MB")
        parts.append(f"CDP(open={self.cdp_stats['opens']}, close={self.cdp_stats['closes']}, err={self.cdp_stats['errors']})")
        
        self.log_event("HEALTH", " | ".join(parts))
    
    def log_loop_timing(self, duration_sec: float):
        """Main loop iteráció időtartam mérése"""
        self.loop_iteration += 1
        
        if duration_sec > 5.0:
            self.log_event("TIMING", f"SLOW ITERATION #{self.loop_iteration}: {duration_sec:.2f}s", "WARN")
        elif self.loop_iteration % 50 == 0:  # Minden 50. iterációnál
            self.log_event("TIMING", f"Iteration #{self.loop_iteration}: {duration_sec:.3f}s")
    
    def log_queue_status(self, open_tasks: int = None, dispatcher_queue: int = None, nav_queue: int = None):
        """Queue méretek tracking"""
        parts = []
        if open_tasks is not None:
            parts.append(f"OPEN_TASKS={open_tasks}")
        if dispatcher_queue is not None:
            parts.append(f"DISPATCHER={dispatcher_queue}")
        if nav_queue is not None:
            parts.append(f"NAV={nav_queue}")
        
        if parts:
            self.log_event("QUEUE", " | ".join(parts))
    
    def log_race_condition(self, operation: str, handles_state: str = None, pending_cdp: int = None):
        """Race condition tracking"""
        parts = [operation]
        if handles_state:
            parts.append(f"handles={handles_state}")
        if pending_cdp is not None:
            parts.append(f"pending_cdp={pending_cdp}")
        
        self.log_event("RACE", " | ".join(parts), "WARN")
    
    def log_crash_context(self, exception: Exception, exception_type: str = None):
        """Crash context + operation buffer dump"""
        self.log_event("CRASH", "="*60, "ERROR")
        self.log_event("CRASH", f"Exception: {type(exception).__name__}: {str(exception)[:200]}", "ERROR")
        if exception_type:
            self.log_event("CRASH", f"Type: {exception_type}", "ERROR")
        
        # Utolsó műveletek dump-ja
        self.log_event("CRASH", "-"*60, "ERROR")
        self.log_event("CRASH", f"Last {len(self.operation_buffer)} operations before crash:", "ERROR")
        for entry in self.operation_buffer:
            try:
                with open(self.log_file, "a", encoding="utf-8") as f:
                    f.write(entry + "\n")
            except Exception:
                pass
        
        self.log_event("CRASH", "="*60, "ERROR")
    
    def log_milestone(self, milestone: str):
        """Kritikus életciklus események (login, bootstrap, cleanup stb.)"""
        self.log_event("MILESTONE", milestone, "INFO")
        print(f"📍 {milestone}")

# Global diagnosztikai logger instance
DIAG_LOGGER = DiagnosticLogger()

# --- CDP-BASED TBODY READING FEATURE FLAG ---
USE_CDP_FOR_TBODY_READING = True  # True = CDP próbálkozás Selenium fallback-kel, False = csak Selenium
# Ha USE_CDP_FOR_TBODY_READING = True:
#   - CDP Runtime.evaluate próbálkozás minden tabon
#   - Hiba esetén automatikus Selenium fallback
#   - 70-80% gyorsabb, kevesebb race condition, kevesebb "no such window" hiba
# Ha USE_CDP_FOR_TBODY_READING = False:
#   - Csak Selenium (eredeti működés)
#   - 100% backward compatibility

# --- NAV CDP DEBUG (URL figyelés tabváltás nélkül) ---
DEBUG_NAV_CDP = True          # ha zavar a log, állítsd False-ra
DEBUG_NAV_CDP_INTERVAL = 2.0  # másodpercenként logoljuk a NAV / külső page targeteket


def _cdp_dump_nav_targets(label: str = ""):
    """
    CDP-ből kiírja az összes 'page' target URL-jét, ami:
      - surebet.com/nav ... VAGY
      - bármilyen külső http(s) host (valid_external)
    """
    if not DEBUG_NAV_CDP:
        return
    try:
        info = _safe_cdp_cmd("Target.getTargets", {}, label=f"NAVCDP dump {label}")
        if not isinstance(info, dict):
            return
        targets = info.get("targetInfos", []) or []
    except Exception as e:
        warn(f"[NAVCDP] Target.getTargets hiba: {e}")
        return

# --- NETWORK LOGGING FOR API DISCOVERY ---
ENABLE_NETWORK_LOGGING = True
NETWORK_LOG_DIR = "./network_logs"
last_network_log_save = 0

def save_network_logs_to_file():
    """
    Save captured network requests to log files for API endpoint discovery
    """
    if not ENABLE_NETWORK_LOGGING:
        return
        
    try:
        # Create logs directory
        os.makedirs(NETWORK_LOG_DIR, exist_ok=True)
        
        # Get performance logs from Chrome
        logs = driver.get_log('performance')
        
        # Open log files
        all_log = os.path.join(NETWORK_LOG_DIR, "all_requests.txt")
        xhr_log = os.path.join(NETWORK_LOG_DIR, "xhr_requests.txt")
        json_log = os.path.join(NETWORK_LOG_DIR, "json_requests.txt")
        
        with open(all_log, "a", encoding="utf-8") as f_all, \
             open(xhr_log, "a", encoding="utf-8") as f_xhr, \
             open(json_log, "a", encoding="utf-8") as f_json:
            
            for entry in logs:
                try:
                    log_entry = json.loads(entry['message'])
                    message = log_entry.get('message', {})
                    
                    if message.get('method') == 'Network.responseReceived':
                        params = message.get('params', {})
                        response = params.get('response', {})
                        url = response.get('url', '')
                        status = response.get('status', 0)
                        mime_type = response.get('mimeType', '')
                        request_id = params.get('requestId', '')
                        
                        # Skip data URIs and chrome extensions
                        if url.startswith('data:') or url.startswith('chrome-extension:'):
                            continue
                        
                        # Log all requests
                        f_all.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {url}\n")
                        f_all.write(f"  Status: {status}\n")
                        f_all.write(f"  Type: {mime_type}\n\n")
                        
                        # Log XHR/Fetch requests
                        resource_type = params.get('type', '')
                        if resource_type in ['XHR', 'Fetch'] or 'XMLHttpRequest' in str(response):
                            f_xhr.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] XHR/Fetch: {url}\n")
                            f_xhr.write(f"  Status: {status}\n")
                            f_xhr.write(f"  Type: {mime_type}\n\n")
                        
                        # Log JSON requests
                        if 'json' in url.lower() or 'json' in mime_type.lower():
                            f_json.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] JSON: {url}\n")
                            f_json.write(f"  Status: {status}\n")
                            f_json.write(f"  Mime: {mime_type}\n\n")
                            
                except Exception as e:
                    # Skip malformed log entries
                    continue
                    
        log(f"[NETWORK-LOG] Saved network logs to {NETWORK_LOG_DIR}")
                    
    except Exception as e:
        warn(f"[NETWORK-LOG] Error saving logs: {e}")

    lines = []
    for t in targets:
        try:
            if t.get("type") != "page":
                continue
            url = (t.get("url") or "").strip()
            if not url:
                continue

            # Csak a NAV és a külső oldalak érdekesek
            if "surebet.com/nav" in url or valid_external(url):
                tid = t.get("targetId")
                lines.append(f"    - {tid} | {url}")
        except Exception:
            continue

    if lines:
        log(f"[NAVCDP] {label} {len(lines)} target:")
        for ln in lines:
            print(ln)


EARLY_ACCEPT_POLL_MS = 200
EARLY_ACCEPT_MAX_SEC = 8

def log(msg):
    if LOG_ENABLED:
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")

def warn(msg):
    print(f"[{datetime.now().strftime('%H:%M:%S')}] {msg}")


# ============================================================================
# NEW HELPER FUNCTIONS FOR OPTIMIZATIONS
# ============================================================================

def get_chrome_profile_for_account(account_key):
    """Get Chrome profile path for specific account"""
    return ACCOUNT_PROFILES.get(account_key, ACCOUNT_PROFILES["default"])


def get_account_file_path(account_key, filename):
    """
    Get account-specific file path.
    Creates account folder if doesn't exist.
    """
    account_dir = ACCOUNT_WORKING_DIRS.get(account_key, ACCOUNT_WORKING_DIRS["default"])
    
    # Create directory if doesn't exist
    if not os.path.exists(account_dir):
        os.makedirs(account_dir)
        log(f"📁 Created account directory: {account_dir}")
    
    return os.path.join(account_dir, filename)


def get_fingerprint_for_account(account_key):
    """Get browser fingerprint for specific account"""
    return ACCOUNT_FINGERPRINTS.get(account_key, ACCOUNT_FINGERPRINTS["default"])


def refresh_page_safe():
    """
    Biztonságosan frissíti az oldalt.
    JavaScript execution használata (természetesebb, kevésbé detektálható).
    """
    try:
        if USE_JAVASCRIPT_REFRESH:
            # JavaScript-based refresh (safer, more natural)
            driver.execute_script("location.reload(true)")
            log("🔄 Oldal frissítve (JavaScript execution)")
        else:
            # Traditional Selenium refresh (fallback)
            driver.refresh()
            log("🔄 Oldal frissítve (Selenium refresh)")
        
        return True
        
    except Exception as e:
        warn(f"Oldal frissítés hiba: {e}")
        # Fallback to traditional method
        try:
            driver.refresh()
            log("🔄 Oldal frissítve (fallback)")
            return True
        except:
            return False


def find_elements_batch(selector, description="elements", timeout=10):
    """
    Batch operáció: több elem egyszerre lekérése.
    
    Előny: 1 DOM query több elem helyett (40-60% gyorsabb).
    Fallback: hiba esetén visszaáll az egyenkénti módszerre.
    
    Args:
        selector: CSS selector
        description: leírás logging-hoz
        timeout: max várakozási idő
        
    Returns:
        list: megtalált elemek listája
    """
    if not USE_BATCH_OPERATIONS:
        # Batch mode disabled, return empty (caller handles traditional way)
        log(f"⚙️ Batch operations kikapcsolva - traditional mode")
        return None
    
    try:
        log(f"📦 Batch operáció: {description} keresése (selector: {selector})")
        
        # Wait for at least one element
        WebDriverWait(driver, timeout).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, selector))
        )
        
        # Get all matching elements
        elements = driver.find_elements(By.CSS_SELECTOR, selector)
        
        log(f"✅ Batch siker: {len(elements)} {description} megtalálva 1 query-vel")
        return elements
        
    except TimeoutException:
        warn(f"⏱️ Timeout: {description} nem találhatók - fallback traditional módra")
        return None
        
    except Exception as e:
        warn(f"❌ Batch hiba: {e} - fallback traditional módra")
        return None


def simulate_mouse_movement():
    """
    Szimulál random egér mozgást hogy emberibbnek tűnjön.
    Ritkán fut (10 percenként) hogy ne legyen túl gyakori.
    """
    try:
        # Get window size
        window_size = driver.get_window_size()
        width = window_size['width']
        height = window_size['height']
        
        # Random number of movements (2-4)
        num_movements = random.randint(2, 4)
        
        for i in range(num_movements):
            # Random position
            x = random.randint(50, width - 50)
            y = random.randint(50, height - 50)
            
            # Move mouse
            action = ActionChains(driver)
            action.move_by_offset(x, y)
            action.perform()
            
            # Small random delay between movements
            time.sleep(random.uniform(0.1, 0.3))
        
        log(f"🖱️ Mouse movement szimuláció kész ({num_movements} mozgás)")
        
    except Exception as e:
        # Silent fail - not critical
        pass


def load_seen():
    s = set()
    if os.path.exists(SEEN_FILE):
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            for line in f:
                parts = line.strip().split(" | ", 1)
                if len(parts) == 2:
                    _, tid = parts
                    s.add(tid)
    return s

def save_seen_line(tbody_id):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(SEEN_FILE, "a", encoding="utf-8") as f:
        f.write(f"{ts} | {tbody_id}\n")

def remove_seen_line(tbody_id):
    if not os.path.exists(SEEN_FILE):
        return
    try:
        with open(SEEN_FILE, "r", encoding="utf-8") as f:
            lines = [ln for ln in f.readlines() if f" | {tbody_id}" not in ln]
        with open(SEEN_FILE, "w", encoding="utf-8") as f:
            f.writelines(lines)
    except Exception:
        pass

def load_active():
    s = set()
    if os.path.exists(ACTIVE_FILE):
        with open(ACTIVE_FILE, "r", encoding="utf-8") as f:
            for line in f:
                tid = line.strip()
                if tid:
                    s.add(tid)
    return s

def save_active_all(active_set: set):
    with open(ACTIVE_FILE, "w", encoding="utf-8") as f:
        for tid in sorted(active_set):
            f.write(tid + "\n")

def load_link_cache():
    if os.path.exists(LINK_CACHE_FILE):
        try:
            with open(LINK_CACHE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}

def save_link_cache(cache: dict):
    try:
        with open(LINK_CACHE_FILE, "w", encoding="utf-8") as f:
            json.dump(cache, f, indent=2, ensure_ascii=False)
    except Exception:
        pass

# =============================================================================
# 🚀 NEW HELPER FUNCTIONS FOR 4 IMPROVEMENTS
# =============================================================================

def remove_webdriver_flag(driver_instance):
    """
    Eltávolítja a navigator.webdriver flag-et CDP injection-nel.
    Bot detection bypass - a böngésző nem jelzi hogy automatizált.
    """
    if not ENABLE_WEBDRIVER_REMOVAL:
        return
    
    try:
        driver_instance.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined
                });
            """
        })
        log("🎭 WebDriver flag eltávolítva (bot detection bypass)")
    except Exception as e:
        warn(f"WebDriver flag removal hiba: {e}")

def inject_canvas_noise(driver_instance):
    """
    Canvas fingerprint randomization - minden session egyedi fingerprint.
    Követhetetlen, privacy védelem.
    """
    if not ENABLE_CANVAS_RANDOMIZATION:
        return
    
    try:
        driver_instance.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
            "source": """
                const originalToDataURL = HTMLCanvasElement.prototype.toDataURL;
                const originalToBlob = HTMLCanvasElement.prototype.toBlob;
                
                const noise = () => Math.random() * 0.1 - 0.05;
                
                HTMLCanvasElement.prototype.toDataURL = function() {
                    const context = this.getContext('2d');
                    if (context) {
                        const imageData = context.getImageData(0, 0, this.width, this.height);
                        for (let i = 0; i < imageData.data.length; i += 4) {
                            imageData.data[i] = Math.min(255, Math.max(0, imageData.data[i] + noise()));
                            imageData.data[i+1] = Math.min(255, Math.max(0, imageData.data[i+1] + noise()));
                            imageData.data[i+2] = Math.min(255, Math.max(0, imageData.data[i+2] + noise()));
                        }
                        context.putImageData(imageData, 0, 0);
                    }
                    return originalToDataURL.apply(this, arguments);
                };
                
                HTMLCanvasElement.prototype.toBlob = function() {
                    const context = this.getContext('2d');
                    if (context) {
                        const imageData = context.getImageData(0, 0, this.width, this.height);
                        for (let i = 0; i < imageData.data.length; i += 4) {
                            imageData.data[i] = Math.min(255, Math.max(0, imageData.data[i] + noise()));
                            imageData.data[i+1] = Math.min(255, Math.max(0, imageData.data[i+1] + noise()));
                            imageData.data[i+2] = Math.min(255, Math.max(0, imageData.data[i+2] + noise()));
                        }
                        context.putImageData(imageData, 0, 0);
                    }
                    return originalToBlob.apply(this, arguments);
                };
            """
        })
        log("🎨 Canvas fingerprint randomization aktiválva")
    except Exception as e:
        warn(f"Canvas noise injection hiba: {e}")

# =============================================================================
# 🎯 CONTENT HASH CHECKING - Smart Refresh Detection
# =============================================================================
# These functions detect real content changes to avoid unnecessary page refreshes
# Works even when server headers (ETag/Last-Modified) are unreliable

import hashlib

# Content hash checking metrics
content_hash_metrics = {
    'checks_performed': 0,
    'quick_checks': 0,
    'full_hashes': 0,
    'changes_detected': 0,
    'refreshes_skipped': 0,
    'total_check_time_ms': 0,
    'periodic_check_counter': 0  # NEW: Track periodic checks
}

def _log_hash_check(msg, verbose_only=False):
    """
    Log content hash checking messages
    
    Args:
        msg: Message to log
        verbose_only: Only log if CONTENT_HASH_VERBOSE_LOGGING is True
    """
    if not verbose_only or CONTENT_HASH_VERBOSE_LOGGING:
        log(f"[HASH] {msg}")

def get_page_signature():
    """
    Get quick page signature (fast structure check)
    ENHANCED: Now tracks element IDs for better deletion detection
    
    Returns:
        dict: Enhanced signature with tbody/row counts, first/last IDs, and ID lists
    
    Speed: ~1-2ms (slightly slower but much more accurate)
    """
    if not ENABLE_CONTENT_HASH_CHECKING:
        return None
    
    try:
        start_time = time.time()
        
        # Enhanced signature with ID tracking if enabled
        if CONTENT_HASH_ENHANCED_MODE and CONTENT_HASH_TRACK_IDS:
            signature = driver.execute_script("""
                const tbodys = document.querySelectorAll('tbody');
                const rows = document.querySelectorAll('tbody tr');
                
                let firstId = '';
                let lastId = '';
                let tbodyIds = [];
                let rowIds = [];
                
                // Collect tbody IDs
                tbodys.forEach((tbody, idx) => {
                    const id = tbody.id || tbody.dataset.id || tbody.className || '';
                    tbodyIds.push(id ? id.substring(0, 30) : `tbody-${idx}`);
                });
                
                // Collect first/last row IDs
                if (rows.length > 0) {
                    firstId = rows[0].id || rows[0].className || '';
                    lastId = rows[rows.length - 1].id || rows[rows.length - 1].className || '';
                    
                    // Collect row IDs (limited to first and last 5 for performance)
                    const rowsToCheck = Math.min(rows.length, 10);
                    for (let i = 0; i < Math.min(5, rows.length); i++) {
                        rowIds.push(rows[i].id || rows[i].className || '');
                    }
                    for (let i = Math.max(0, rows.length - 5); i < rows.length; i++) {
                        if (rowIds.length < 10) {
                            rowIds.push(rows[i].id || rows[i].className || '');
                        }
                    }
                }
                
                return {
                    tbody_count: tbodys.length,
                    row_count: rows.length,
                    first_id: firstId.substring(0, 50),
                    last_id: lastId.substring(0, 50),
                    tbody_ids: tbodyIds,  // NEW: Track tbody IDs
                    row_ids: rowIds        // NEW: Track sample row IDs
                };
            """)
        else:
            # Basic signature (original version)
            signature = driver.execute_script("""
                const tbodys = document.querySelectorAll('tbody');
                const rows = document.querySelectorAll('tbody tr');
                
                let firstId = '';
                let lastId = '';
                
                if (rows.length > 0) {
                    firstId = rows[0].id || rows[0].className || '';
                    lastId = rows[rows.length - 1].id || rows[rows.length - 1].className || '';
                }
                
                return {
                    tbody_count: tbodys.length,
                    row_count: rows.length,
                    first_id: firstId.substring(0, 50),
                    last_id: lastId.substring(0, 50)
                };
            """)
        
        elapsed_ms = (time.time() - start_time) * 1000
        content_hash_metrics['quick_checks'] += 1
        content_hash_metrics['total_check_time_ms'] += elapsed_ms
        
        # Log signature
        _log_hash_check(
            f"Quick signature: {signature['tbody_count']} tbodys, {signature['row_count']} rows ({elapsed_ms:.1f}ms)",
                verbose_only=True
            )
        
        return signature
        
    except Exception as e:
        warn(f"[HASH] Page signature error: {e}")
        return None

def get_content_hash():
    """
    Get full content hash of tbody elements
    ENHANCED: Now includes structure metadata for better accuracy
    
    Returns:
        str: MD5 hash of tbody content + metadata, or None if error
    
    Speed: ~3-6ms (slightly slower but much more accurate)
    """
    if not ENABLE_CONTENT_HASH_CHECKING:
        return None
    
    try:
        start_time = time.time()
        
        # Enhanced hash with structure metadata if enabled
        if CONTENT_HASH_ENHANCED_MODE:
            content_data = driver.execute_script("""
                const tbodys = document.querySelectorAll('tbody');
                
                // Collect comprehensive data
                const data = {
                    count: tbodys.length,
                    html: Array.from(tbodys).map(t => t.innerHTML).join(''),
                    ids: Array.from(tbodys).map(t => t.id || ''),
                    classes: Array.from(tbodys).map(t => t.className || ''),
                    row_counts: Array.from(tbodys).map(t => 
                        t.querySelectorAll('tr').length
                    )
                };
                
                return JSON.stringify(data);
            """)
            
            # Create hash from all data
            content_hash = hashlib.md5(content_data.encode('utf-8')).hexdigest()
        else:
            # Basic hash (original version)
            tbody_content = driver.execute_script("""
                const tbodys = document.querySelectorAll('tbody');
                return Array.from(tbodys).map(t => t.innerHTML).join('');
            """)
            
            # Create hash
            content_hash = hashlib.md5(tbody_content.encode('utf-8')).hexdigest()
        
        elapsed_ms = (time.time() - start_time) * 1000
        content_hash_metrics['full_hashes'] += 1
        content_hash_metrics['total_check_time_ms'] += elapsed_ms
        
        _log_hash_check(
            f"Full hash: {content_hash[:12]}... ({elapsed_ms:.1f}ms)",
            verbose_only=True
        )
        
        return content_hash
        
    except Exception as e:
        warn(f"[HASH] Content hash error: {e}")
        return None

def check_content_changed(url, last_signature=None, last_hash=None):
    """
    Check if page content has actually changed
    ULTRA-AGGRESSIVE: 5-layer detection system to NEVER miss changes!
    
    Layers:
    1. Pre-check: Immediate tbody count (catches zero immediately)
    2. Decrease detection: Compare with last count (catches deletions)
    3. Signature check: Quick comparison (fast detection)
    4. Full hash check: Complete verification (accurate)
    5. Post-check: Final verification (catches changes during check)
    
    Args:
        url: Page URL (for logging)
        last_signature: Previous page signature
        last_hash: Previous content hash
    
    Returns:
        tuple: (changed: bool, new_signature: dict, new_hash: str, reason: str)
    """
    if not ENABLE_CONTENT_HASH_CHECKING:
        return True, None, None, "hash_checking_disabled"
    
    content_hash_metrics['checks_performed'] += 1
    content_hash_metrics['periodic_check_counter'] += 1
    check_start = time.time()
    
    try:
        # ═══════════════════════════════════════════════════════════
        # LAYER 1: PRE-CHECK - Immediate tbody count
        # ═══════════════════════════════════════════════════════════
        _log_hash_check("🔍 LAYER 1: Pre-check tbody count", verbose_only=True)
        
        try:
            initial_count = driver.execute_script("""
                return document.querySelectorAll('tbody').length;
            """)
            
            # CRITICAL: If zero, force refresh IMMEDIATELY!
            if initial_count == 0:
                _log_hash_check("🚨 LAYER 1: ZERO TBODY DETECTED!", verbose_only=False)
                _log_hash_check("🔥 FORCING IMMEDIATE REFRESH!", verbose_only=False)
                return True, None, None, "zero_tbody_layer1"
                
            _log_hash_check(f"✓ Layer 1: {initial_count} tbody found", verbose_only=True)
            
        except Exception as e:
            _log_hash_check(f"⚠️ Layer 1 failed: {e}", verbose_only=True)
            initial_count = None
        
        # ═══════════════════════════════════════════════════════════
        # LAYER 2: DECREASE DETECTION - Compare with last count
        # ═══════════════════════════════════════════════════════════
        if initial_count is not None and last_signature and 'tbody_count' in last_signature:
            _log_hash_check("🔍 LAYER 2: Comparing tbody count", verbose_only=True)
            
            last_count = last_signature.get('tbody_count', 0)
            
            if initial_count < last_count:
                _log_hash_check("🚨 LAYER 2: COUNT DECREASED!", verbose_only=False)
                _log_hash_check(f"   Was: {last_count}, Now: {initial_count}", verbose_only=False)
                _log_hash_check("🔥 FORCING REFRESH (deletion detected)!", verbose_only=False)
                return True, None, None, "count_decreased_layer2"
            
            _log_hash_check(f"✓ Layer 2: Count stable ({last_count} → {initial_count})", verbose_only=True)
        
        # ═══════════════════════════════════════════════════════════
        # LAYER 3: SIGNATURE CHECK - Quick comparison
        # ═══════════════════════════════════════════════════════════
        _log_hash_check("🔍 LAYER 3: Signature check", verbose_only=True)
        
        current_signature = get_page_signature()
        
        if current_signature is None:
            _log_hash_check("⚠️ Layer 3: Signature check failed, assuming changed", verbose_only=True)
            return True, None, last_hash, "signature_error"
        
        # Additional zero check in signature
        if current_signature.get('tbody_count', 0) == 0:
            _log_hash_check("🚨 LAYER 3: ZERO TBODY in signature!", verbose_only=False)
            _log_hash_check("🔥 FORCING REFRESH!", verbose_only=False)
            return True, current_signature, None, "zero_tbody_layer3"
        
        # Check for count decrease in signature
        if last_signature and 'tbody_count' in last_signature:
            if current_signature.get('tbody_count', 0) < last_signature.get('tbody_count', 0):
                _log_hash_check("🚨 LAYER 3: Signature count decreased!", verbose_only=False)
                _log_hash_check("🔥 FORCING REFRESH!", verbose_only=False)
                return True, current_signature, None, "signature_decreased_layer3"
        
        # Convert to string for comparison
        sig_str = json.dumps(current_signature, sort_keys=True)
        last_sig_str = json.dumps(last_signature, sort_keys=True) if last_signature else None
        
        # Check if it's time for periodic full check
        force_full_check = (
            CONTENT_HASH_ENHANCED_MODE and 
            content_hash_metrics['periodic_check_counter'] >= CONTENT_HASH_PERIODIC_FULL_CHECK
        )
        
        if force_full_check:
            content_hash_metrics['periodic_check_counter'] = 0
            _log_hash_check("🔍 LAYER 5: PERIODIC CHECK (safety net)", verbose_only=True)
        
        # Skip quick check if it's time for periodic full check
        if CONTENT_HASH_USE_QUICK_CHECK and sig_str == last_sig_str and not force_full_check:
            _log_hash_check("✓ Layer 3: Signature unchanged", verbose_only=True)
            
            # ═══════════════════════════════════════════════════════════
            # LAYER 4: POST-CHECK - Final verification
            # ═══════════════════════════════════════════════════════════
            _log_hash_check("🔍 LAYER 4: Post-check verification", verbose_only=True)
            
            try:
                final_count = driver.execute_script("""
                    return document.querySelectorAll('tbody').length;
                """)
                
                if final_count == 0:
                    _log_hash_check("🚨 LAYER 4: ZERO TBODY in final check!", verbose_only=False)
                    _log_hash_check("🔥 FORCING REFRESH!", verbose_only=False)
                    return True, current_signature, last_hash, "zero_tbody_layer4"
                
                if initial_count is not None and final_count != initial_count:
                    _log_hash_check(f"🚨 LAYER 4: Count changed during check!", verbose_only=False)
                    _log_hash_check(f"   Start: {initial_count}, End: {final_count}", verbose_only=False)
                    _log_hash_check("🔥 FORCING REFRESH!", verbose_only=False)
                    return True, current_signature, last_hash, "count_drift_layer4"
                
                _log_hash_check(f"✓ Layer 4: Final count stable ({final_count})", verbose_only=True)
                
            except Exception as e:
                _log_hash_check(f"⚠️ Layer 4 failed: {e}", verbose_only=True)
            
            # All layers passed, no change
            elapsed_ms = (time.time() - check_start) * 1000
            content_hash_metrics['refreshes_skipped'] += 1
            
            _log_hash_check(
                f"✅ All layers passed: NO CHANGE ({elapsed_ms:.1f}ms) - Refresh SKIPPED",
                verbose_only=False
            )
            
            return False, current_signature, last_hash, "all_layers_no_change"
        
        # ═══════════════════════════════════════════════════════════
        # FULL HASH CHECK - Signature changed or periodic check
        # ═══════════════════════════════════════════════════════════
        _log_hash_check("🔍 Full hash check (signature changed or periodic)", verbose_only=True)
        
        current_hash = get_content_hash()
        
        if current_hash is None:
            _log_hash_check("⚠️ Hash calculation failed, assuming changed", verbose_only=True)
            return True, current_signature, last_hash, "hash_error"
        
        if current_hash == last_hash:
            # False positive from signature check OR periodic check found no change
            
            # ═══════════════════════════════════════════════════════════
            # LAYER 4: POST-CHECK - Final verification
            # ═══════════════════════════════════════════════════════════
            _log_hash_check("🔍 LAYER 4: Post-check verification", verbose_only=True)
            
            try:
                final_count = driver.execute_script("""
                    return document.querySelectorAll('tbody').length;
                """)
                
                if final_count == 0:
                    _log_hash_check("🚨 LAYER 4: ZERO TBODY in final check!", verbose_only=False)
                    _log_hash_check("🔥 FORCING REFRESH!", verbose_only=False)
                    return True, current_signature, current_hash, "zero_tbody_layer4"
                
                _log_hash_check(f"✓ Layer 4: Final count = {final_count}", verbose_only=True)
                
            except Exception as e:
                _log_hash_check(f"⚠️ Layer 4 failed: {e}", verbose_only=True)
            
            elapsed_ms = (time.time() - check_start) * 1000
            content_hash_metrics['refreshes_skipped'] += 1
            
            reason = "periodic_no_change" if force_full_check else "hash_no_change"
            
            _log_hash_check(
                f"✅ Full hash: NO CHANGE ({elapsed_ms:.1f}ms) - Refresh SKIPPED",
                verbose_only=False
            )
            
            return False, current_signature, current_hash, reason
        
        # ═══════════════════════════════════════════════════════════
        # CHANGE DETECTED!
        # ═══════════════════════════════════════════════════════════
        elapsed_ms = (time.time() - check_start) * 1000
        content_hash_metrics['changes_detected'] += 1
        
        _log_hash_check(
            f"🔥 CHANGE DETECTED ({elapsed_ms:.1f}ms) - Will REFRESH",
            verbose_only=False
        )
        
        if CONTENT_HASH_VERBOSE_LOGGING:
            _log_hash_check(f"   Old hash: {last_hash[:12] if last_hash else 'none'}...", verbose_only=True)
            _log_hash_check(f"   New hash: {current_hash[:12]}...", verbose_only=True)
        
        return True, current_signature, current_hash, "content_changed"
        
    except Exception as e:
        warn(f"[HASH] Content check error: {e}")
        return True, last_signature, last_hash, f"error_{str(e)[:20]}"

def log_content_hash_metrics():
    """Log content hash checking statistics"""
    if not ENABLE_CONTENT_HASH_CHECKING:
        return
    
    m = content_hash_metrics
    
    if m['checks_performed'] == 0:
        return
    
    avg_time = m['total_check_time_ms'] / m['checks_performed'] if m['checks_performed'] > 0 else 0
    skip_rate = (m['refreshes_skipped'] / m['checks_performed'] * 100) if m['checks_performed'] > 0 else 0
    
    log("=" * 70)
    log("📊 CONTENT HASH CHECKING STATISTICS")
    log("=" * 70)
    log(f"  Total checks performed: {m['checks_performed']}")
    log(f"  Quick checks: {m['quick_checks']}")
    log(f"  Full hashes: {m['full_hashes']}")
    log(f"  Changes detected: {m['changes_detected']}")
    log(f"  Refreshes skipped: {m['refreshes_skipped']} ({skip_rate:.1f}%)")
    log(f"  Avg check time: {avg_time:.2f}ms")
    log(f"  Total time spent: {m['total_check_time_ms']:.1f}ms")
    log("=" * 70)

# =============================================================================
# 🎲 SMART SLEEP - Random Jitter for Unpredictable Timing
# =============================================================================

import random

def smart_sleep(base_seconds, jitter_percent=0.3):
    """
    Sleep with random jitter for unpredictable timing (bot detection reduction)
    
    Args:
        base_seconds (float): Base sleep time in seconds
        jitter_percent (float): Variation percentage (0.3 = ±30%)
        
    Example:
        smart_sleep(10, 0.3)  # Sleeps 7-13 seconds (10 ± 30%)
        smart_sleep(5, 0.2)   # Sleeps 4-6 seconds (5 ± 20%)
        
    Performance: 0% impact (still waits same average time)
    Bot detection: -30% (unpredictable pattern)
    """
    min_sleep = base_seconds * (1 - jitter_percent)
    max_sleep = base_seconds * (1 + jitter_percent)
    sleep_time = random.uniform(min_sleep, max_sleep)
    time.sleep(sleep_time)

# =============================================================================
# 🤖 HUMAN BEHAVIOR SIMULATION - Anti-Detection
# =============================================================================

def random_mouse_movement(driver):
    """
    Move mouse to random coordinates to simulate human behavior
    Uses smooth movements with random speed
    """
    try:
        from selenium.webdriver.common.action_chains import ActionChains
        
        # Get viewport dimensions
        viewport_width = driver.execute_script("return window.innerWidth;")
        viewport_height = driver.execute_script("return window.innerHeight;")
        
        # Random target coordinates (avoid edges)
        target_x = random.randint(50, viewport_width - 50)
        target_y = random.randint(50, viewport_height - 50)
        
        # Move mouse with ActionChains
        actions = ActionChains(driver)
        actions.move_by_offset(target_x, target_y).perform()
        
        # Small pause
        time.sleep(random.uniform(0.1, 0.3))
        
    except Exception as e:
        pass  # Silently fail - not critical

def random_scroll_behavior(driver):
    """
    Scroll the page randomly to simulate human reading behavior
    """
    try:
        # Get page dimensions
        page_height = driver.execute_script("return document.body.scrollHeight")
        viewport_height = driver.execute_script("return window.innerHeight")
        
        if page_height <= viewport_height:
            return  # Nothing to scroll
        
        # Random scroll direction and amount
        scroll_direction = random.choice(['down', 'down', 'down', 'up'])  # 75% down, 25% up
        
        if scroll_direction == 'down':
            scroll_amount = random.randint(100, 400)
            driver.execute_script(f"window.scrollBy({{top: {scroll_amount}, behavior: 'smooth'}});")
        else:
            scroll_amount = random.randint(50, 200)
            driver.execute_script(f"window.scrollBy({{top: -{scroll_amount}, behavior: 'smooth'}});")
        
        # Pause as if reading
        time.sleep(random.uniform(0.5, 1.5))
        
    except Exception as e:
        pass  # Silently fail

def safe_random_click(driver):
    """
    Click on a SAFE element (non-interactive) to simulate human behavior
    NEVER clicks on links, buttons, or any functional elements
    """
    try:
        # Find safe elements to click (non-interactive decorative elements)
        safe_selectors = [
            "h1, h2, h3, h4, h5, h6",  # Headers
            ".container:not(a):not(button)",  # Containers
            ".header:not(a):not(button)",  # Header areas
            ".title:not(a):not(button)",  # Title text
            "thead th:not([onclick])",  # Table headers (non-sortable)
        ]
        
        for selector in safe_selectors:
            try:
                elements = driver.find_elements(By.CSS_SELECTOR, selector)
                if not elements:
                    continue
                
                # Filter out interactive elements
                safe_elements = []
                for element in elements[:20]:  # Check first 20
                    try:
                        # Check if element has click handlers or is interactive
                        tag_name = element.tag_name.lower()
                        if tag_name in ['a', 'button', 'input', 'select', 'textarea']:
                            continue
                        
                        # Check for onclick attribute
                        if element.get_attribute('onclick'):
                            continue
                        
                        # Check for href (link)
                        if element.get_attribute('href'):
                            continue
                        
                        # Check if inside a link
                        parent = element.find_element(By.XPATH, "..")
                        if parent and parent.tag_name.lower() == 'a':
                            continue
                        
                        safe_elements.append(element)
                    except:
                        continue
                
                if safe_elements:
                    # Click a random safe element
                    target = random.choice(safe_elements)
                    target.click()
                    time.sleep(random.uniform(0.2, 0.5))
                    return  # Success
                    
            except:
                continue
        
    except Exception as e:
        pass  # Silently fail - not critical

def simulate_human_activity(driver, force=False):
    """
    Randomly simulate human behaviors (mouse movement, scrolling, clicking)
    Call this periodically during normal operations
    
    Args:
        driver: Selenium WebDriver instance
        force: If True, always perform some action (for testing)
    
    Note: This function can block for 0.5-2 seconds. Use simulate_human_activity_async()
          for non-blocking execution.
    """
    try:
        # Random chance of doing something (or nothing)
        # Reduced by 50% from original: 10% + 15% + 5% = 30% vs 60% before
        rand = random.random()
        
        if force or rand < 0.10:  # 10% chance: mouse movement (was 20%)
            random_mouse_movement(driver)
        elif force or rand < 0.25:  # 15% chance: scrolling (was 30%)
            random_scroll_behavior(driver)
        elif force or rand < 0.30:  # 5% chance: safe clicking (was 10%)
            safe_random_click(driver)
        # 70% chance: do nothing (natural idle, was 40%)
        
    except Exception as e:
        pass  # Silently fail - not critical

def simulate_human_activity_async(driver):
    """
    Non-blocking version of simulate_human_activity()
    Runs human behaviors in a background thread so it doesn't slow down the main script
    
    Args:
        driver: Selenium WebDriver instance
    
    Usage:
        simulate_human_activity_async(driver)  # Returns immediately!
        # Your script continues without waiting
    
    Performance:
        - Zero impact on main thread (0% slowdown)
        - Behaviors run in background
        - Thread auto-cleanup (daemon=True)
    """
    import threading
    
    try:
        # Create and start background thread
        thread = threading.Thread(
            target=simulate_human_activity,
            args=(driver,),
            daemon=True  # Thread dies when main program exits
        )
        thread.start()
        # Returns immediately - no blocking!
        
    except Exception as e:
        pass  # Silently fail - not critical

# =============================================================================


# ---------- Chrome init (100% friss profil minden indításnál) ----------

PROFILE_DIR = ACTIVE_ACCOUNT["profile_dir"]
os.makedirs(PROFILE_DIR, exist_ok=True)

chrome_options = Options()

if HEADLESS:
    chrome_options.add_argument("--headless=new")

# 🔥 Minden account a saját fix profilkönyvtárát használja
chrome_options.add_argument(f"--user-data-dir={PROFILE_DIR}")

# (Opcionális) ha akarod mellé, maradhat az incognito is, de nem szükséges:
# chrome_options.add_argument("--incognito")

# Gyorsító / tiltó flag-ek
chrome_options.add_argument("--disable-features=OptimizationHints,TranslateUI")
chrome_options.add_argument("--disable-site-isolation-trials")
chrome_options.add_argument("--disable-translate")
chrome_options.add_argument("--disable-infobars")
chrome_options.add_argument("--disable-sync")
chrome_options.add_argument("--disable-client-side-phishing-detection")
# GPU disabled only in headless mode (see line 546)
chrome_options.add_argument("--disable-dev-shm-usage")
chrome_options.add_argument("--disable-blink-features=AutomationControlled")
chrome_options.add_argument("--window-size=960,540")
chrome_options.add_argument("--disable-popup-blocking")

# 🚀 Multi-tab performance optimizations (40-72 tabs)
# Memory management - increase heap size for JavaScript/V8
chrome_options.add_argument("--max-old-space-size=4096")  # 4GB heap for JavaScript
chrome_options.add_argument("--js-flags=--max-old-space-size=4096")  # V8 heap size

# Background tab optimization - aggressive memory management
chrome_options.add_argument("--aggressive-cache-discard")  # Aggressive cache cleanup
# chrome_options.add_argument("--aggressive-tab-discard")  # ❌ REMOVED - Breaks tab switching!
chrome_options.add_argument("--disable-background-timer-throttling")  # Timer optimization
chrome_options.add_argument("--disable-backgrounding-occluded-windows")  # Window optimization
chrome_options.add_argument("--disable-renderer-backgrounding")  # Renderer optimization

# Process optimization - reduce number of processes
chrome_options.add_argument("--disable-features=IsolateOrigins,site-per-process")  # Fewer processes
chrome_options.add_argument("--no-sandbox")  # Disable sandbox (faster, less secure)
chrome_options.add_argument("--disable-setuid-sandbox")  # Additional sandbox disable
# chrome_options.add_argument("--disable-gpu")  # ❌ REMOVED - User requested, might slow down

# Resource optimization - disable unnecessary features
chrome_options.add_argument("--disable-extensions")  # No extension overhead
chrome_options.add_argument("--disable-plugins")  # Disable Flash, PDF, etc.
chrome_options.add_argument("--disable-web-security")  # Faster loading (less security)

# Performance optimizations: reduce RAM usage and speed up page loads
chrome_options.add_argument("--blink-settings=imagesEnabled=false")  # Disable images
chrome_options.add_argument("--disable-remote-fonts")  # Disable remote fonts
chrome_options.add_argument("--disk-cache-size=50000000")  # 50MB disk cache
chrome_options.add_argument("--media-cache-size=50000000")  # 50MB media cache

# Optimization #3: Network optimizations (+10-15% speed)
chrome_options.add_argument("--enable-quic")  # Enable QUIC protocol (faster than TCP)
chrome_options.add_argument("--enable-tcp-fast-open")  # TCP Fast Open
chrome_options.add_argument("--dns-prefetch-disable")  # Disable DNS prefetch (save bandwidth)

# 🎭 User Agent Rotation - Random selection for bot detection avoidance
selected_user_agent = random.choice(USER_AGENTS)
log(f"🎭 User Agent kiválasztva: {selected_user_agent[:80]}...")
chrome_options.add_argument(f"--user-agent={selected_user_agent}")

# Prefs 1
prefs1 = {
    "credentials_enable_service": False,
    "profile.password_manager_enabled": False,
    "profile.default_content_setting_values.notifications": 2,
    "translate_whitelists": {"lt": "en"},
    "translate": {"enabled": "true"},
}
chrome_options.add_experimental_option("prefs", prefs1)

# Performance optimizations: disable images, CSS, geolocation, etc.
# 🎨 CSS kikapcsolás/bekapcsolás (DISABLE_CSS változó alapján)
if DISABLE_CSS:
    log("🎨 CSS betöltés KIKAPCSOLVA (DISABLE_CSS=True) - Gyorsabb de csúnya")
    css_setting = 2  # 2 = Block CSS
else:
    log("🎨 CSS betöltés BEKAPCSOLVA (DISABLE_CSS=False) - Lassabb de szép")
    css_setting = 1  # 1 = Allow CSS

prefs2 = {
    "profile.default_content_setting_values.popups": 1,
    "profile.managed_default_content_settings.images": 2,  # Disable images
    "profile.default_content_setting_values.stylesheets": css_setting,  # CSS: configurable! (FIXED: was "stylesheet" singular)
    "profile.managed_default_content_settings.geolocation": 2,
    "profile.managed_default_content_settings.notifications": 2,
    "profile.managed_default_content_settings.media_stream": 2,
}
chrome_options.add_experimental_option("prefs", prefs2)

# Logging
try:
    chrome_options.set_capability("pageLoadStrategy", "eager")
    chrome_options.set_capability("goog:loggingPrefs", {"performance": "ALL"})
except Exception:
    pass

# 🔥 Chrome indítása egyszer, tisztán
try:
    driver = uc.Chrome(options=chrome_options, version_main=143)
except Exception as e:
    print(f"First Chrome start attempt failed: {e}")
    try:
        driver = uc.Chrome(options=chrome_options)
    except Exception as e2:
        print(f"❌ Chrome start FAILED: {e2}")
        raise SystemExit(1)

uc.Chrome.__del__ = lambda self: None

# =============================================================================
# 🚀 Apply WebDriver & Canvas improvements after driver creation
# =============================================================================
if ENABLE_WEBDRIVER_REMOVAL or ENABLE_CANVAS_RANDOMIZATION:
    remove_webdriver_flag(driver)
    inject_canvas_noise(driver)
# =============================================================================



# --- CDP gyorsítók / tiltások ---
try:
    driver.execute_cdp_cmd("Network.enable", {})
    driver.execute_cdp_cmd("Network.setBypassServiceWorker", {"bypass": True})
    
    # Optimization #1: CDP Request Blocking (+20-30% speed)
    # Conservative approach: block only images and fonts (safest for tbody scraping)
    driver.execute_cdp_cmd("Network.setBlockedURLs", {
        "urls": [
            # Images (not needed for tbody scraping)
            "*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg", "*.bmp",
            # Fonts (not needed)
            "*.woff", "*.woff2", "*.ttf", "*.otf", "*.eot",
            # Icons (not needed)
            "*.ico", "*favicon*", "*apple-touch-icon*", "*mask-icon*", "*mstile*"
        ]
    })
    try:
        driver.execute_cdp_cmd("Emulation.setEmulatedMedia", {
            "features": [{"name": "prefers-reduced-motion", "value": "reduce"}]
        })
    except Exception:
        pass

    log("🚀 OPTIMIZATIONS: CDP request blocking (images/fonts), SW bypass, reduced motion.")

    driver.execute_cdp_cmd("Page.enable", {})

    # 1. injektor: jelöld EXT ablakokat + window.name getter/setter védelem
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": r"""
        (function(){
          try {
            if (location && location.hostname && !/(\.|^)surebet\.com$/i.test(location.hostname)) {
              try { window.name = (window.name || '') + '|EXT'; } catch(e){}
            }
            try {
              Object.defineProperty(window, 'name', {
                configurable: true,
                enumerable: true,
                set: function(v){ try{ this._n=v; }catch(e){} return v; },
                get: function(){ try{ return this._n || ''; }catch(e){} return ''; }
              });
            } catch(e){}
          } catch(e){}
        })();
        """
    })

    # 2. injektor: olcsó flag, hogy külső oldalon vagyunk
    driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {
        "source": r"""
        (function(){
          try{
            var host=(location.hostname||"").toLowerCase();
            var isSB=/(\.|^)surebet\.com$/.test(host);
            if(!isSB){ try{ window.__SB_EXT__=1; }catch(e){} }
          }catch(e){}
        })();
        """
    })

except Exception as e:
    warn(f"⚠️ CDP init részben sikertelen: {e}")


try:
    driver.set_script_timeout(30)
except Exception:
    pass
    


# gyenge animációtiltás (CSS) – best-effort injektor
def _inject_disable_animations():
    try:
        driver.execute_script("""
        (function(){
          try{
            var st = document.getElementById('__noanim');
            if (st) return;
            st = document.createElement('style');
            st.id='__noanim';
            st.textContent='*{animation:none!important;transition:none!important;scroll-behavior:auto!important}';
            document.head && document.head.appendChild(st);
          }catch(e){}
        })();
        """)
    except Exception:
        pass
        
def tiny_keepalive_ping():
    """
    Pici scroll fel-le, hogy legyen activity a fő tabon.
    """
    try:
        _safe_execute_script("window.scrollBy(0, 1); window.scrollBy(0, -1);")
        _safe_execute_script("window.scrollBy(0, -1);")
    except Exception:
        pass


def ensure_active_window():
    try:
        h = driver.current_window_handle
        if h in driver.window_handles:
            return True
    except Exception:
        pass
    try:
        handles = driver.window_handles
        if not handles:
            return False
        if 'MAIN_HANDLE' in globals() and MAIN_HANDLE and MAIN_HANDLE in handles:
            driver.switch_to.window(MAIN_HANDLE)
            return True
        driver.switch_to.window(handles[0])
        return True
    except Exception:
        return False


def _safe_execute_script(script, *args):
    """
    driver.execute_script biztonságos wrapper:
    - ha közben bezáródott a window, megpróbálunk visszaváltani egy élőre
    - 2 próbálkozás NoSuchWindowException esetén
    """
    for _ in range(2):
        try:
            if not ensure_active_window():
                raise NoSuchWindowException("No alive window to execute script")
            return driver.execute_script(script, *args)
        except NoSuchWindowException:
            time.sleep(0.05)
            continue
    # ha eddig sem sikerült, még egy utolsó próbálkozás
    if not ensure_active_window():
        raise NoSuchWindowException("No alive window after retries")
    return driver.execute_script(script, *args)


def _safe_execute_async_script(script, *args):
    for _ in range(2):
        try:
            if not ensure_active_window():
                raise NoSuchWindowException("No alive window to execute async script")
            return driver.execute_async_script(script, *args)
        except NoSuchWindowException:
            time.sleep(0.05)
            continue
    if not ensure_active_window():
        raise NoSuchWindowException("No alive window after retries (async)")
    return driver.execute_async_script(script, *args)
    
def _safe_cdp_cmd(method: str, params: dict | None = None, *, label: str = ""):
    """
    CDP hívásokhoz védőréteg.
    - Ha DRIVER_DEAD=True → azonnal skip
    - Ha nincs driver, vagy nincsenek window handle-ök → visszaad None-t.
    - Ha 'no such window' / 'web view not found' / stb. hibát kapunk → log + None.
    - Ha driver connection error (HTTPConnectionPool / WinError 10061...), akkor DRIVER_DEAD=True,
      és innentől minden cdp hívás skip-el.
    - Target.closeTarget: duplikált bezárás védelem PENDING_CDP_CLOSES-szal
    """
    global driver, DRIVER_DEAD, PENDING_CDP_CLOSES

    if params is None:
        params = {}

    # ha már tudjuk, hogy halott
    if DRIVER_DEAD:
        warn(f"[CDP] {method} skip – DRIVER_DEAD=True (label={label})")
        return None

    # driver már None? (pl. shutdown / restart közben)
    if driver is None:
        warn(f"[CDP] {method} skip – driver is None (label={label})")
        return None

    # van-e élő window? (safe wrapperrel)
    handles = _safe_window_handles(label=f"{method} pre")
    if DRIVER_DEAD:
        warn(f"[CDP] {method} skip – DRIVER_DEAD=True window_handles után (label={label})")
        return None
    if not handles:
        warn(f"[CDP] {method} skip – nincs window (label={label})")
        return None

    # CDP koordináció: Target.closeTarget duplikált bezárás védelem
    if method == "Target.closeTarget":
        target_id = params.get("targetId")
        if target_id:
            # Ha már bezárás alatt van, skip
            if target_id in PENDING_CDP_CLOSES:
                # Timeout ellenőrzés: ha 5s óta bent van, eltávolítjuk (esetleg lefagyott)
                if time.time() - PENDING_CDP_CLOSES[target_id] > 5.0:
                    PENDING_CDP_CLOSES.pop(target_id, None)
                else:
                    # Még mindig friss, skip
                    return None
            # Regisztráljuk hogy bezárás alatt van
            PENDING_CDP_CLOSES[target_id] = time.time()

    try:
        result = driver.execute_cdp_cmd(method, params)
        
        # 📋 Diagnostic logging: CDP command sikeres
        if method == "Target.createTarget":
            url = params.get("url", "")
            DIAG_LOGGER.log_cdp_lifecycle("TARGET_CREATE_SUCCESS", url=url)
        elif method == "Target.closeTarget":
            target_id = params.get("targetId")
            if target_id:
                PENDING_CDP_CLOSES.pop(target_id, None)
                DIAG_LOGGER.log_cdp_lifecycle("TARGET_CLOSE_SUCCESS", target_id=target_id)
        
        return result
    except Exception as e:
        msg = str(e).lower()
        
        # 📋 Diagnostic logging: CDP command hiba
        if method == "Target.createTarget":
            url = params.get("url", "")
            DIAG_LOGGER.log_cdp_lifecycle("TARGET_CREATE_ERROR", url=url, error=str(e)[:100])
        elif method == "Target.closeTarget":
            target_id = params.get("targetId")
            if target_id:
                PENDING_CDP_CLOSES.pop(target_id, None)
                DIAG_LOGGER.log_cdp_lifecycle("TARGET_CLOSE_ERROR", target_id=target_id, error=str(e)[:100])

        # Sikertelen bezárás után is eltávolítjuk (ne maradjon bent)
        if method == "Target.closeTarget":
            target_id = params.get("targetId")
            if target_id:
                PENDING_CDP_CLOSES.pop(target_id, None)

        # ha ez is driver connection error → beállítjuk a flaget
        if _is_driver_connection_error(e):
            DRIVER_DEAD = True
            warn(f"[CDP] {method} driver-connection hiba, DRIVER_DEAD=True (label={label}): {e}")
            return None

        # tipikus „ablak megszűnt" hibák
        if (
            "no such window" in msg
            or "web view not found" in msg
            or "disconnected: not connected to devtools" in msg
            or "chrome not reachable" in msg
        ):
            warn(f"[CDP] {method} skip – window already closed/devtools detached (label={label}): {e}")
            return None

        # egyéb CDP hiba – logoljuk, de nem ölünk meg semmit
        warn(f"[CDP] {method} hiba (label={label}): {e}")
        return None


# ---------- CRASH RECOVERY ----------
def restart_application():
    """
    Univerzális crash recovery: újraindítja az alkalmazást.
    - Tries graceful driver.quit()
    - Kills Chrome processes as backup
    - Restarts the script with os.execv()
    """
    global driver
    
    warn("⚠️ Session crash detected, restarting application...")
    
    # Try graceful close
    try:
        if driver is not None:
            driver.quit()
    except Exception as e:
        warn(f"driver.quit() failed: {e}")
    
    # Backup: Kill Chrome processes
    try:
        if platform.system() == "Windows":
            os.system("taskkill /F /IM chrome.exe /T 2>nul")
            os.system("taskkill /F /IM chromedriver.exe /T 2>nul")
        else:
            os.system("pkill -9 chrome 2>/dev/null")
            os.system("pkill -9 chromedriver 2>/dev/null")
    except Exception as e:
        warn(f"Chrome process kill failed: {e}")
    
    # Small delay before restart
    time.sleep(2)
    
    # Restart script with proper path handling (handles spaces in filename)
    warn("🔄 Restarting script...")
    script_path = os.path.abspath(__file__)
    restart_command = [sys.executable, script_path] + sys.argv[1:]
    
    # Detailed logging for debugging
    warn(f"🔄 Restart command: {restart_command}")
    warn(f"📂 Script path: {script_path}")
    warn(f"🐍 Python executable: {sys.executable}")
    warn(f"📝 Preserving args: {sys.argv[1:]}")
    
    os.execv(sys.executable, restart_command)


# ---------- URL utilok ----------
def is_http_url(u: str | None) -> bool:
    if not u: return False
    try:
        p = urlparse(u)
        return p.scheme in ("http", "https") and bool(p.netloc)
    except Exception:
        return False

def is_surebet_url(u: str | None) -> bool:
    if not u: return False
    try:
        host = urlparse(u).netloc.lower()
        return host.endswith("surebet.com")
    except Exception:
        return False

def is_nav_url(u: str | None) -> bool:
    if not u: return False
    try:
        p = urlparse(u)
        return p.netloc.lower().endswith("surebet.com") and p.path.startswith("/nav")
    except Exception:
        return False

def valid_external(u: str | None) -> bool:
    return is_http_url(u) and not is_surebet_url(u)

def _maybe_b64_decode(s: str) -> str | None:
    s2 = (s or "").strip()
    if not re.match(r'^[A-Za-z0-9+/=_-]{8,}$', s2):
        return None
    try:
        pad = '=' * (-len(s2) % 4)
        for variant in (s2, s2.replace('-', '+').replace('_', '/')):
            try:
                return base64.b64decode(variant + pad).decode('utf-8', errors='ignore')
            except Exception:
                continue
        return None
    except Exception:
        return None

def extract_target_from_nav(nav_url: str) -> str | None:
    try:
        p = urlparse(nav_url)
        q = parse_qs(p.query)
        keys = ["to","url","u","target","redirect","dest","link","r","q"]
        for k in keys:
            if k in q and q[k]:
                raw = q[k][0]
                cand = unquote(raw)
                if cand and cand.startswith(("http://","https://")):
                    return cand
                b = _maybe_b64_decode(raw)
                if b and b.startswith(("http://","https://")):
                    return b
                m = re.search(r'(https?://[^\s"\'<>]+)', cand)
                if m:
                    return m.group(1)
        return None
    except Exception:
        return None
        
# --- "Page not found" detektor + gyors tab állapot olvasó ---

NOT_FOUND_PATTERNS = [
    r"\bpage not found\b",
    r"\b404\b",
    r"\bnot found\b",
    r"\bseite nicht gefunden\b",
    r"\bстраница не найдена\b",
    r"\bpagina non trovata\b",
    r"\bpágina no encontrada\b",
    r"\bno encontrado\b",
    r"\bhittades inte\b",
]

def _looks_not_found_text(txt: str) -> bool:
    if not txt:
        return False
    lo = txt.lower()
    for pat in NOT_FOUND_PATTERNS:
        if re.search(pat, lo):
            return True
    return False
    
def _surebet_h1_not_found() -> bool:
    """
    Hipergyors 404-check: csak az <h1 class="title"> szöveget olvassa.
    True: ha pontosan 'Page not found' (case-insensitive/trim).
    """
    try:
        return bool(_safe_execute_script(r"""
            try {
              var h = document.querySelector('h1.title');
              if (!h) return false;
              var t = (h.textContent || '').trim().toLowerCase();
              return t === 'page not found';
            } catch(e) { return false; }
        """))
    except Exception:
        return False


def _superfast_external_url_or_none():
    """
    VILLÁM: 1x execute_script – ha location.href már http(s) és NEM surebet.com → visszaadjuk.
    Ha az injektor beállította a window.__SB_EXT__-et, az is jó jel; ilyenkor
    egy driver.current_url fallback olvasást még megpróbálunk.
    """
    try:
        href, extflag = _safe_execute_script("return [location.href||'', !!window.__SB_EXT__];")
        href = (href or '').strip()
    except Exception:
        href, extflag = "", False

    if href.startswith(("http://","https://")) and not is_surebet_url(href):
        return _sanitize_url(href)

    if extflag:
        try:
            cur = driver.current_url
            if cur.startswith(("http://","https://")) and not is_surebet_url(cur):
                return _sanitize_url(cur)
        except Exception:
            pass

    return None


def _looks_not_found(title_l: str, body_l: str) -> bool:
    # minimál: elég ha bármelyikben felismerjük
    return _looks_not_found_text(title_l) or _looks_not_found_text(body_l)

def _left_surebet(cur: str | None) -> bool:
    return is_http_url(cur) and not is_surebet_url(cur)

def _read_tab_state_quick() -> tuple[str, str, str]:
    """
    Gyors állapotolvasás az aktuális tabról:
    - current_url
    - document.title (lowercased)
    - body innerText (lowercased, MAX_BODY_SNIFF-ig vágva)
    """
    try:
        cur = driver.current_url
    except Exception:
        cur = "about:blank"

    try:
        title = _safe_execute_script("return document.title || ''") or ""
    except Exception:
        title = ""
    try:
        body_text = _safe_execute_script(
            "return (document.body && document.body.innerText) || ''"
        ) or ""
    except Exception:
        body_text = ""

    title_l = title.lower()
    body_l = body_text.lower()[:MAX_BODY_SNIFF]
    return cur, title_l, body_l


def _get_main_frame_id() -> str | None:
    """Visszaadja az aktuális tab main frame-jének frameId-ját (CDP Page.getFrameTree)."""
    try:
        ft = driver.execute_cdp_cmd("Page.getFrameTree", {})
        return ft.get("frameTree", {}).get("frame", {}).get("id")
    except Exception:
        return None


def _drain_perf_for_redirects(target_frame_ids: set[str],
                              reqid_to_frame: dict[str, str]) -> dict[str, str]:
    """
    Kiolvassa az azóta érkezett CDP performance logokat, és visszaadja:
      { frameId -> external_location_url }
    Csak a target_frame_ids-ben lévő frame-ekre figyel.
    """
    redirects = {}
    try:
        logs = driver.get_log("performance")
    except Exception:
        logs = []

    for e in logs:
        try:
            msg = json.loads(e.get("message", "")).get("message", {})
        except Exception:
            continue

        m = msg.get("method")
        p = msg.get("params", {}) or {}

        if m == "Network.requestWillBeSent":
            rid = p.get("requestId")
            fid = p.get("frameId")
            if rid and fid:
                reqid_to_frame[rid] = fid

        elif m == "Network.responseReceived":
            rid = p.get("requestId")
            resp = p.get("response", {}) or {}
            status = int(resp.get("status", 0) or 0)
            if status in (301, 302, 303, 307, 308):
                fid = reqid_to_frame.get(rid)
                if fid in target_frame_ids:
                    hdrs = resp.get("headers", {}) or {}
                    loc = hdrs.get("Location") or hdrs.get("location") or hdrs.get("LOCATION")
                    if loc and valid_external(loc):
                        redirects[fid] = _sanitize_url(loc)

        elif m == "Network.responseReceivedExtraInfo":
            rid = p.get("requestId")
            fid = reqid_to_frame.get(rid)
            if fid in target_frame_ids:
                hdrs = p.get("headers", {}) or {}
                loc = hdrs.get("Location") or hdrs.get("location") or hdrs.get("LOCATION")
                if loc and valid_external(loc):
                    redirects[fid] = _sanitize_url(loc)

    return redirects


# ---------- FAST FINAL szabályok (gyors elfogadás) ----------
def _sanitize_url(u: str | None) -> str | None:
    if not u:
        return None
    s = str(u).strip()
    s = re.sub(r'[,\.;\)\s]+$', '', s)
    return s

def _cdp_debug_log_nav_targets(label: str = ""):
    if NAV_DEBUG_INTERVAL <= 0:
        return

    global driver
    if driver is None:
        return

    try:
        info = _safe_cdp_cmd("Target.getTargets", {}, label=f"NAVDBG {label}")
        if not isinstance(info, dict):
            return
    except Exception as e:
        warn(f"[NAVDBG] Target.getTargets hiba: {e}")
        return

    targets = info.get("targetInfos") or []

    rows = []
    for t in targets:
        if t.get("type") != "page":
            continue

        url = t.get("url") or ""
        if not url.startswith("http"):
            continue

        try:
            host = urlparse(url).netloc.lower()
        except Exception:
            host = ""

        # A fő en.surebet.com oldalt NE listázzuk, csak a bookmaker / külső tabokat
        if "surebet.com" in host:
            continue

        rows.append((t.get("targetId"), host, url))

    if not rows:
        log(f"[NAVDBG] {label} – nincs külső 'page' target")
    else:
        log(f"[NAVDBG] {label} – {len(rows)} külső 'page' target:")
        for tid, host, url in rows:
            short = url if len(url) <= 160 else (url[:157] + "...")
            log(f"    - {tid} [{host}] {short}")

def _host(u: str) -> str:
    try:
        return urlparse(u).netloc.lower()
    except Exception:
        return ""

def _hash(u: str) -> str:
    try:
        return urlparse(u).fragment or ""
    except Exception:
        return ""

def _query_params(u: str) -> dict:
    try:
        return parse_qs(urlparse(u).query)
    except Exception:
        return {}

def _blaze_btpath_ok(u: str) -> bool:
    try:
        q = _query_params(u)
        p = q.get('bt-path', [])
        if not p:
            return False
        raw = p[0]
        dec = unquote(raw)
        if 'undefined' in dec.lower():
            return False
        if re.search(r'\d{10,}', dec):
            return True
        if re.search(r'-\d{10,}$', dec):
            return True
        return False
    except Exception:
        return False


# ---------- kisegítő függvények ----------
def human_type(element, text: str):
    try:
        _safe_execute_script("arguments[0].focus();", element)
    except Exception:
        pass
    try:
        element.click()
    except Exception:
        pass
    try:
        element.send_keys(KEY_MOD, "a")
        element.send_keys(Keys.DELETE)
    except Exception:
        try:
            _safe_execute_script("arguments[0].value='';", element)
        except Exception:
            pass

    ok = False
    try:
        driver.execute_cdp_cmd("Input.insertText", {"text": text})
        ok = True
    except Exception:
        ok = False

    if not ok:
        try:
            element.send_keys(text)
            ok = True
        except Exception:
            ok = False

    try:
        cur = _safe_execute_script("return arguments[0].value;", element)
    except Exception:
        cur = None

    if cur != text:
        try:
            _safe_execute_script("""
                const el = arguments[0], val = arguments[1];
                const proto = Object.getPrototypeOf(el) || HTMLInputElement.prototype;
                const desc = Object.getOwnPropertyDescriptor(proto, 'value')
                           || Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, 'value');
                if (desc && desc.set) desc.set.call(el, val);
                else el.value = val;
                el.dispatchEvent(new Event('input',  {bubbles:true}));
                el.dispatchEvent(new Event('change', {bubbles:true}));
            """, element, text)
        except Exception:
            pass

    time.sleep(0.05)

def get_bet_name(td):
    try:
        abbr = td.find_element(By.TAG_NAME, "abbr")
        val = abbr.get_attribute("data-bs-original-title") or abbr.get_attribute("title") or abbr.get_attribute("aria-label")
        if val:
            return val.strip()
    except:
        pass
    try:
        return td.text.strip() or "Ismeretlen szelvény"
    except:
        return "Ismeretlen szelvény"

def robust_event_text(tbody, attempts=3, sleep=0.06):
    for _ in range(attempts):
        try:
            els = tbody.find_elements(By.CSS_SELECTOR, "td[class^='event event-']")
            texts = []
            for e in els:
                try:
                    t = (e.text or "").strip()
                except StaleElementReferenceException:
                    t = ""
                except Exception:
                    t = ""
                if t:
                    texts.append(t)
            if texts:
                return max(texts, key=lambda t: len(t.strip())).strip()
            return ""
        except StaleElementReferenceException:
            time.sleep(sleep)
        except Exception:
            break
    return ""

def get_first_minor_text(tbody) -> str:
    try:
        minors = tbody.find_elements(By.CSS_SELECTOR, "span.minor")
        for el in minors:
            txt = (el.text or "").strip()
            if txt:
                return txt
    except Exception:
        pass
    return ""

def parse_float(text):
    if text is None:
        return None
    t = str(text).strip().replace(",", ".")
    m = re.search(r"-?\d+(?:\.\d+)?", t)
    try:
        return float(m.group(0)) if m else None
    except:
        return None

def to_float_or_none(val):
    v = parse_float(val)
    return v if v is not None else None

def canonical_bookmaker(name: str) -> str:
    if not name:
        return name
    # zárójeles kiegészítések levágása
    base = re.sub(r"\s*\([^)]*\)\s*", "", name).strip()
    base_norm = re.sub(r"\s+", " ", base).strip()

    alias = {
        "Vegas.hu": "Vegas",
        "Vegas": "Vegas",
        "BetInAsia (Black)": "BetInAsia",
        "BetInAsia Black": "BetInAsia",
        "BetInAsia": "BetInAsia",
        "Tippmix Pro": "Tippmixpro",
        "Tippmixpro": "Tippmixpro",
        "Boabet": "Boabet",
        "BetWinner": "Betwinner",
        "Betwinner": "Betwinner",
        "Rockyspin": "RockySpin",
        "RockySpin": "RockySpin",

        # KÉRT MÓDOSÍTÁS: Parimatch → Betmatch
        "Parimatch": "Betmatch",
        "PariMatch": "Betmatch",
        "Pari Match": "Betmatch",
        "PARIMATCH": "Betmatch",
    }
    return alias.get(base_norm, base_norm)

def normalize_match_start(s: str) -> str:
    if not s:
        return s
    s = s.replace(".", "/").strip()
    m = re.search(r"(\d{1,2})/(\d{1,2})\s+(\d{1,2}):(\d{2})", s)
    if m:
        d, mo, hh, mm = map(int, m.groups())
        year = datetime.now().year
        try:
            dt = datetime(year, mo, d, hh, mm)
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            pass
    return s

def compute_profit_percent(odds1, odds2) -> str:
    try:
        o1 = float(odds1); o2 = float(odds2)
        s = 1.0/o1 + 1.0/o2
        val = max(0.0, (1.0 - s) * 100.0)
        return f"{val:.2f}%"
    except:
        return "0.00%"

def find_profit_percent(tbody):
    selectors = [
        "td.profit", "td[class*='profit']", "td.gain", "td.percent", "td.max_profit",
        ".profit", ".gain", ".percent"
    ]
    for sel in selectors:
        try:
            el = tbody.find_element(By.CSS_SELECTOR, sel)
            txt = el.text.strip()
            m = re.search(r"[-+]?\d+(?:[.,]\d+)?\s*%", txt)
            if m:
                return m.group(0).replace(",", ".")
        except:
            pass
    try:
        txt = tbody.text
        m = re.search(r"[-+]?\d+(?:[.,]\d+)?\s*%", txt)
        if m:
            return m.group(0).replace(",", ".")
    except:
        pass
    return None

def norm_odds(val):
    if val is None:
        return None
    try:
        return f"{float(val):.{UPDATE_DECIMALS}f}"
    except:
        v = parse_float(str(val))
        return f"{v:.{UPDATE_DECIMALS}f}" if v is not None else None

def norm_profit_str(s):
    if not s:
        return f"{0.0:.{UPDATE_DECIMALS}f}%"
    try:
        t = str(s).replace(",", ".")
        m = re.search(r"-?\d+(?:\.\d+)?", t)
        v = float(m.group(0)) if m else 0.0
        return f"{v:.{UPDATE_DECIMALS}f}%"
    except:
        return f"{0.0:.{UPDATE_DECIMALS}f}%"

def percent_to_float(s: str | None):
    if not s:
        return None
    try:
        m = re.search(r"-?\d+(?:\.\d+)?", s.replace(",", "."))
        return float(m.group(0)) if m else None
    except:
        return None

def iso_or_none(s: str | None):
    if not s:
        return None
    if re.match(r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$", s):
        return s
    return None

def log_found_link(name, href, bet, odd):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    with open(FOUND_LINKS_FILE, "a", encoding="utf-8") as f:
        f.write(f"[{now}] {name} -> {href} | {bet} | {odd}\n")

# --- ÚJ: Cím-tisztító csak match/league mezőkre ---
def _clean_title(s: str | None) -> str | None:
    if s is None:
        return None
    t = str(s)
    t = re.sub(r"\[\d+\]", "", t)     # [123456] kidob
    t = t.replace(".", "")            # pontok törlése
    t = re.sub(r"\s+", " ", t).strip(" -—–\u2013\u2014").strip()
    return t or None

# --- HTTP helpers with Connection Pooling ---
# Optimization #2: Connection Pooling (+10-20% speed)
# Reuses TCP connections instead of creating new ones for each request
try:
    from requests.adapters import HTTPAdapter
    from urllib3.util.retry import Retry
    
    # Create a session with connection pooling
    HTTP_SESSION = requests.Session()
    
    # Configure retry strategy
    retry_strategy = Retry(
        total=3,  # Total retries
        backoff_factor=0.1,  # Wait 0.1s, then 0.2s, then 0.4s between retries
        status_forcelist=[429, 500, 502, 503, 504],  # Retry on these status codes
    )
    
    # Configure adapter with connection pooling
    adapter = HTTPAdapter(
        pool_connections=100,  # Pool size
        pool_maxsize=100,      # Max connections in pool
        max_retries=retry_strategy,
        pool_block=False       # Don't block when pool is full
    )
    
    # Mount adapter for both HTTP and HTTPS
    HTTP_SESSION.mount("http://", adapter)
    HTTP_SESSION.mount("https://", adapter)
    
    print("✅ Connection pooling enabled (100 connections)")
except ImportError:
    # Fallback to basic requests if urllib3 not available
    HTTP_SESSION = requests
    print("⚠️ Connection pooling not available - using basic requests")

def http_post(url: str, payload: dict, timeout=12) -> tuple[int, dict]:
    """
    Részletes JSON visszaadása + saját X-Correlation-Id header.
    A válaszban: {"message", "issues", "correlation_id", "__status__", ...}
    """
    try:
        corr_id = str(uuid.uuid4())
        headers = dict(HTTP_HEADERS)
        headers["X-Correlation-Id"] = corr_id

        r = HTTP_SESSION.post(url, headers=headers, json=payload, timeout=timeout)

        try:
            data = r.json()
        except Exception:
            data = {"raw": r.text}

        data.setdefault("correlation_id",
                        r.headers.get("x-correlation-id") or data.get("correlation_id") or corr_id)
        data["__status__"] = r.status_code

        if DEBUG_HTTP:
            print(f"HTTP {r.status_code} {url} cid={data.get('correlation_id')}")
            try:
                print(json.dumps(data, ensure_ascii=False)[:1200])
            except Exception:
                print(str(data)[:1200])

        return r.status_code, data
    except Exception as e:
        return 0, {"error": "request_exception", "message": str(e)}

# ===================== ASZINKRON DISZPÉCSER + BATCH =====================
class AsyncHttpDispatcher:
    def __init__(self):
        self.q_save   = Queue(maxsize=10000)
        self.q_update = Queue(maxsize=10000)
        self.q_delete = Queue(maxsize=10000)
        self.result_q = Queue(maxsize=10000)

        self.UPDATE_BATCH_MAX = 50
        self.UPDATE_BATCH_FLUSH_SEC = 1.2

        self.DELETE_BATCH_MAX = 50
        self.DELETE_BATCH_FLUSH_SEC = 1.5

        self.HTTP_TIMEOUT = 12

        self._stop = threading.Event()
        self._thr = threading.Thread(target=self._run, daemon=True)
        self._thr.start()

    def stop(self):
        self._stop.set()
        self._thr.join(timeout=5)

    def get_results(self, max_items=200):
        items = []
        for _ in range(max_items):
            try:
                items.append(self.result_q.get_nowait())
            except Empty:
                break
        return items

    def enqueue_save(self, item: dict):
        try:
            self.q_save.put_nowait(item)
        except Exception:
            warn("⚠️ SAVE queue full, item dropped")

    def enqueue_update(self, payload: dict):
        try:
            self.q_update.put_nowait(payload)
        except Exception:
            warn("⚠️ UPDATE queue full, item dropped")

    def enqueue_delete(self, tip_id: str):
        try:
            self.q_delete.put_nowait(tip_id)
        except Exception:
            warn("⚠️ DELETE queue full, item dropped")

    def _run(self):
        upd_bucket = []
        upd_first_ts = None

        del_bucket = []
        del_first_ts = None

        while not self._stop.is_set():
            did_anything = False

            try:
                save_item = self.q_save.get_nowait()
                did_anything = True
                self._process_save_item(save_item)
            except Empty:
                pass

            now = time.time()
            try:
                while True:
                    upd_item = self.q_update.get_nowait()
                    upd_bucket.append(upd_item)
                    if upd_first_ts is None:
                        upd_first_ts = now
                    if len(upd_bucket) >= self.UPDATE_BATCH_MAX:
                        break
            except Empty:
                pass

            if upd_bucket:
                if (time.time() - (upd_first_ts or time.time())) >= self.UPDATE_BATCH_FLUSH_SEC or len(upd_bucket) >= self.UPDATE_BATCH_MAX:
                    self._flush_update_batch(upd_bucket)
                    upd_bucket = []
                    upd_first_ts = None
                    did_anything = True

            now = time.time()
            try:
                while True:
                    del_item = self.q_delete.get_nowait()
                    del_bucket.append(del_item)
                    if del_first_ts is None:
                        del_first_ts = now
                    if len(del_bucket) >= self.DELETE_BATCH_MAX:
                        break
            except Empty:
                pass

            if del_bucket:
                if (time.time() - (del_first_ts or time.time())) >= self.DELETE_BATCH_FLUSH_SEC or len(del_bucket) >= self.DELETE_BATCH_MAX:
                    self._flush_delete_batch(del_bucket)
                    del_bucket = []
                    del_first_ts = None
                    did_anything = True

            if not did_anything:
                time.sleep(0.02)

        if upd_bucket:
            self._flush_update_batch(upd_bucket)
        if del_bucket:
            self._flush_delete_batch(del_bucket)

        try:
            while True:
                save_item = self.q_save.get_nowait()
                self._process_save_item(save_item)
        except Empty:
            pass

    def _process_save_item(self, it: dict):
        tip_payload = it.get("tip_payload", {})
        status, data = http_post(SAVE_TIP_URL, tip_payload, timeout=self.HTTP_TIMEOUT)

        # siker csak akkor, ha 2xx ÉS ok:true
        ok = (200 <= status < 300) and isinstance(data, dict) and (data.get("ok") is True)

        if ok:
            self.result_q.put({
                "type": "save_ok",
                "id": tip_payload.get("id"),
                "state_info": it.get("state_info"),
                "finals": it.get("finals"),
                "resp": data,
            })
            return

        # duplikáció kezelése
        low = json.dumps(data, ensure_ascii=False).lower() if isinstance(data, dict) else str(data).lower()
        if status == 409 or any(k in low for k in ["duplicate", "unique", "already exists", "conflict"]):
            upd_payload = it.get("update_payload")
            if upd_payload:
                s2, d2 = http_post(UPDATE_TIP_URL, upd_payload, timeout=self.HTTP_TIMEOUT)
                if (200 <= s2 < 300) and isinstance(d2, dict) and d2.get("ok") is True:
                    self.result_q.put({
                        "type": "save_dup_updated",
                        "id": tip_payload.get("id"),
                        "state_info": it.get("state_info"),
                        "finals": it.get("finals"),
                        "update_payload": upd_payload,
                        "resp": d2,
                    })
                    return
                else:
                    self.result_q.put({
                        "type": "save_dup_update_fail",
                        "id": tip_payload.get("id"),
                        "status": s2,
                        "error": d2,
                    })
                    return
            else:
                self.result_q.put({
                    "type": "save_duplicate",
                    "id": tip_payload.get("id"),
                    "state_info": it.get("state_info"),
                    "finals": it.get("finals"),
                    "resp": data,
                })
                return

        # minden más hiba
        self.result_q.put({
            "type": "save_error",
            "id": tip_payload.get("id"),
            "status": status,
            "error": data
        })

    def _flush_update_batch(self, items: list[dict]):
        try:
            payload = {"items": items}
            status, data = http_post(UPDATE_TIPS_BATCH_URL, payload, timeout=self.HTTP_TIMEOUT)
            if (200 <= status < 300) and isinstance(data, dict) and data.get("ok") is True:
                for it in items:
                    self.result_q.put({"type": "update_ok", "id": it.get("id"), "payload": it, "resp": data})
                return
        except Exception:
            pass
        for it in items:
            s, d = http_post(UPDATE_TIP_URL, it, timeout=self.HTTP_TIMEOUT)
            if (200 <= s < 300) and isinstance(d, dict) and d.get("ok") is True:
                self.result_q.put({"type": "update_ok", "id": it.get("id"), "payload": it, "resp": d})
            else:
                self.result_q.put({"type": "update_error", "id": it.get("id"), "status": s, "error": d, "payload": it})

    def _flush_delete_batch(self, ids: list[str]):
        uniq_ids = list(dict.fromkeys(ids))
        try:
            payload = {"ids": uniq_ids}
            status, data = http_post(DELETE_TIPS_BATCH_URL, payload, timeout=self.HTTP_TIMEOUT)
            if (200 <= status < 300) and isinstance(data, dict) and data.get("ok") is True:
                for tid in uniq_ids:
                    self.result_q.put({"type": "delete_ok", "id": tid, "resp": data})
                return
        except Exception:
            pass
        for tid in uniq_ids:
            s, d = http_post(DELETE_TIP_URL, {"type": "gone", "id": tid}, timeout=self.HTTP_TIMEOUT)
            if (200 <= s < 300) and isinstance(d, dict) and d.get("ok") is True:
                self.result_q.put({"type": "delete_ok", "id": tid, "resp": d})
            else:
                self.result_q.put({"type": "delete_error", "id": tid, "status": s, "error": d})

dispatcher = AsyncHttpDispatcher()

# ====== GLOBÁLIS NYITÁSI VÁRÓLISTA (lookahead a 3-as csomagokhoz) ======
OPEN_TASKS = deque()
OPEN_TASKS_MAX = 5000

def enqueue_open_task(task: dict):
    """Feladat (tbody-id) nyitásának előkészítése lookahead-dal.
       Csak akkor tesszük be, ha még nincs link-final megoldva azonnal.
       Duplikáció ellenőrzéssel - ugyanaz az ID csak egyszer lehet a queue-ban."""
    try:
        tbody_id = task.get("id")
        
        # Deduplication check: skip if already in queue
        if any(t.get("id") == tbody_id for t in OPEN_TASKS):
            return  # Already queued, skip
        
        if len(OPEN_TASKS) < OPEN_TASKS_MAX:
            OPEN_TASKS.append(task)
        else:
            warn("⚠️ OPEN_TASKS megtelt, dobom a legrégebbit")
            OPEN_TASKS.popleft()
            OPEN_TASKS.append(task)
    except Exception as e:
        warn(f"⚠️ enqueue_open_task hiba: {e}")

def remove_gone_ids_from_open_tasks(gone_ids: set):
    """
    Eltűnt tbody ID-ket eltávolítja az OPEN_TASKS sorból.
    Ezzel elkerüljük, hogy feleslegesen nyissunk meg linkeket már nem létező elemekhez.
    
    Args:
        gone_ids: Eltűnt tbody ID-k halmaza
    """
    if not gone_ids or not OPEN_TASKS:
        return
    
    try:
        # Végigmegyünk az OPEN_TASKS soron és csak azokat tartjuk meg, amik nincsenek a gone_ids-ben
        original_len = len(OPEN_TASKS)
        filtered_tasks = deque([task for task in OPEN_TASKS if task.get("id") not in gone_ids])
        
        removed_count = original_len - len(filtered_tasks)
        if removed_count > 0:
            OPEN_TASKS.clear()
            OPEN_TASKS.extend(filtered_tasks)
            log(f"🧹 OPEN_TASKS tisztítás: {removed_count} eltűnt elem eltávolítva (maradt: {len(OPEN_TASKS)})")
    except Exception as e:
        warn(f"⚠️ remove_gone_ids_from_open_tasks hiba: {e}")

# ---------- stale-biztos DOM snapshot ----------
def dom_snapshot_by_id(tbody_id: str, attempts=4, sleep=0.08):
    js = r"""
    const id = arguments[0];
    function snap(id){
      const sel1 = 'tbody.surebet_record[data-id="'+id+'"]';
      const sel2 = 'tbody.surebet_record[dataid="'+id+'"]';
      const row = document.querySelector(sel1) || document.querySelector(sel2);
      if (!row) return null;

      const getTxt = el => el ? (el.textContent || '').trim() : '';
      const q = (r, s) => r ? r.querySelector(s) : null;
      const qa = (r, s) => r ? Array.from(r.querySelectorAll(s)) : [];

      const values = qa(row, "td.value[class*='odd_record_']");
      const coeffs = qa(row, "td.coeff");

      const a1 = values[0] ? q(values[0], 'a') : null;
      const a2 = values[1] ? q(values[1], 'a') : null;

      const href1 = a1 ? a1.href : null;
      const href2 = a2 ? a2.href : null;

      const odds1_text = getTxt(values[0] || null);
      const odds2_text = getTxt(values[1] || null);

      const getBet = (cell) => {
        const ab = cell ? (cell.querySelector('abbr,[data-bs-original-title],[title],[aria-label]')) : null;
        const cands = [
          ab ? (ab.getAttribute('data-bs-original-title')||'') : '',
          ab ? (ab.getAttribute('title')||'') : '',
          ab ? (ab.getAttribute('aria-label')||'') : '',
          getTxt(cell || null)
        ].map(s => (s||'').trim()).filter(Boolean);
        return cands[0] || 'Ismeretlen szelvény';
      };

      const bet1 = getBet(coeffs[0] || null);
      const bet2 = getBet(coeffs[1] || null);

      // Bookmaker nevek (max 2)
      const bookers = qa(row, "td.booker a").map(a => getTxt(a)).filter(Boolean).slice(0,2);

      // EVENT cellák
      const evTds = qa(row, "td[class^='event event-']");
      const evAnchors = evTds
        .map(td => q(td, "a[target='_blank']"))
        .filter(Boolean)
        .map(a => getTxt(a))
        .filter(Boolean);

      // Két anchor is lehet – a rövidebbik kell
      let event_anchor_text = "";
      if (evAnchors.length === 1) {
        event_anchor_text = evAnchors[0];
      } else if (evAnchors.length >= 2) {
        event_anchor_text = evAnchors.sort((a,b) => a.length - b.length)[0];
      }

      // League a td.event... alatti span.minor-ból, ha kettő van -> rövidebbik kell
      const evMinors = evTds
        .map(td => getTxt(q(td, 'span.minor')))
        .filter(Boolean);

      let league_minor = "";
      if (evMinors.length === 1) {
        league_minor = evMinors[0];
      } else if (evMinors.length >= 2) {
        league_minor = evMinors.sort((a,b) => a.length - b.length)[0];
      }

      // sport_minor: meghagyjuk, ha kell később
      const minorsAll = qa(row, "span.minor").map(el => getTxt(el)).filter(Boolean);
      const sport_minor = minorsAll.length ? minorsAll[0] : "";

      // Profit szöveg
      const sels = ['td.profit','td[class*="profit"]','td.gain','td.percent','td.max_profit','.profit','.gain','.percent'];
      let profit = '';
      for (const s of sels) {
         const el = q(row, s);
         const t = getTxt(el);
         if (t) { profit = t; break; }
      }

      // Kezdési idő HTML (abbr)
      const timeabbr = q(row, "td.time abbr");
      const time_html = timeabbr ? (timeabbr.innerHTML || "") : "";

      return {
        href1, href2,
        odds1_text, odds2_text,
        bet1, bet2,
        bookers,
        league_minor,
        sport_minor,
        time_html,
        profit_text: profit,
        event_anchor_text
      };
    }
    return snap(arguments[0]);
    """
    for _ in range(attempts):
        try:
            data = driver.execute_script(js, tbody_id)
            if data:
                return data
        except StaleElementReferenceException:
            pass
        except Exception:
            pass
        time.sleep(sleep)
    return None

# ---------- stale-biztos SAVE előkészítés + batch ----------
def prepare_new_task_for_id(tbody_id):
    snap = dom_snapshot_by_id(tbody_id)
    if not snap:
        return None

    href1 = snap.get("href1")
    href2 = snap.get("href2")

    # match_name az anchor rövidebbik változata
    match_name_raw = (snap.get("event_anchor_text") or "").strip()
    match_name = _clean_title(match_name_raw) or "Ismeretlen meccs"

    # league_name a td.event alatti rövidebbik span.minor
    league_minor = (snap.get("league_minor") or "").strip()
    league_name = _clean_title(league_minor) or ""

    sport_name = (snap.get("sport_minor") or "").strip()

    names_raw = (snap.get("bookers") or [])[:2]
    if len(names_raw) < 2:
        return None
    names = [canonical_bookmaker(n) for n in names_raw]

    odds1_text = (snap.get("odds1_text") or "").strip()
    odds2_text = (snap.get("odds2_text") or "").strip()
    odds1 = to_float_or_none(odds1_text)
    odds2 = to_float_or_none(odds2_text)

    bet1 = (snap.get("bet1") or "").strip() or "Ismeretlen szelvény"
    bet2 = (snap.get("bet2") or "").strip() or "Ismeretlen szelvény"

    profit_dom = (snap.get("profit_text") or "").strip()
    if profit_dom:
        profit_percent = profit_dom
    else:
        if odds1 is not None and odds2 is not None:
            profit_percent = compute_profit_percent(odds1, odds2)
        else:
            profit_percent = "0.00%"

    time_html = snap.get("time_html") or ""
    parts = [p.strip() for p in time_html.replace("<br>", "\n").split("\n") if p.strip()]
    if len(parts) >= 2:
        match_start_raw = f"{parts[0]} {parts[1]}"
    else:
        match_start_raw = (time_html.strip() or "Ismeretlen időpont")
    match_start = normalize_match_start(match_start_raw)
    profit_text = norm_profit_str(profit_percent)

    task = {
        "id": tbody_id,
        "names": names,
        "bets": (bet1, bet2),
        "odds": (odds1 if odds1 is not None else to_float_or_none(odds1_text),
                 odds2 if odds2 is not None else to_float_or_none(odds2_text)),
        "profit_text": profit_text,
        "match_name": match_name,
        "league_name": league_name,
        "sport_name": sport_name,
        "match_start_iso": iso_or_none(match_start),
        "hrefs": (href1, href2),
        "finals": None,
    }

    if tbody_id in link_cache:
        l1 = link_cache[tbody_id].get("link1")
        l2 = link_cache[tbody_id].get("link2")
        if valid_external(l1) and valid_external(l2):
            task["finals"] = (l1, l2)

    return task

def _build_tip_payload_from_task(task):
    tbody_id = task["id"]
    names = task["names"]
    bet1, bet2 = task["bets"]
    odds1, odds2 = task["odds"]
    profit_text = task["profit_text"]

    # Tisztítás csak a cím mezőknél
    match_name = _clean_title(task["match_name"])
    league_name = _clean_title(task["league_name"])

    sport_name = task["sport_name"]
    match_start_iso = task["match_start_iso"]
    final_href1, final_href2 = task.get("finals") or (None, None)

    tip_payload = {
        "id": tbody_id,
        "bookmaker1": names[0],
        "bookmaker2": names[1],
        "profit_percent": profit_text,
        "profit_percent_num": percent_to_float(profit_text),
        "match_name": match_name,
        "league_name": league_name,
        "option1": bet1,
        "option2": bet2,
        "match_start": match_start_iso,
        "link1": final_href1,
        "link2": final_href2,
        "odds1": odds1,
        "odds2": odds2,
        "sport": sport_name,
    }
    return tip_payload

def _build_update_payload_from_task(task):
    tbody_id = task["id"]
    odds1, odds2 = task["odds"]
    profit_text = task["profit_text"]
    return {
        "type": "update",
        "id": tbody_id,
        "odds1": norm_odds(odds1),
        "odds2": norm_odds(odds2),
        "profit_percent": norm_profit_str(profit_text),
        "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    }

# ---------- NAV backoff ----------
nav_retry_attempts = {}   # id -> int
nav_retry_until    = {}   # id -> epoch

nav_backoff_consecutive = 0

def force_main_refresh(reason: str = ""):
    """MAIN tab kemény frissítés + autoupdate biztosítása."""
    global main_refresh_enabled, main_last_refresh, main_next_refresh
    try:
        if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
            driver.switch_to.window(MAIN_HANDLE)
        refresh_page_safe()
        _inject_disable_animations()
        _wait_main_container(timeout=12)
        ensure_main_autoupdate()
        log(f"🔁 MAIN forced refresh {f'({reason})' if reason else ''}")
    except Exception as e:
        warn(f"⚠️ MAIN forced refresh failed: {e}")

def _schedule_nav_backoff(tid: str):
    global nav_backoff_consecutive
    att = nav_retry_attempts.get(tid, 0) + 1
    nav_retry_attempts[tid] = att
    delay = min(NAV_RETRY_BASE * (2 ** (att - 1)), NAV_RETRY_MAX)
    nav_retry_until[tid] = time.time() + delay

    nav_backoff_consecutive += 1
    warn(f"⏳ NAV backoff id={tid} {int(delay)}s (attempt={att})")

    if nav_backoff_consecutive >= 15:
        force_main_refresh("15 consecutive NAV backoffs")
        nav_backoff_consecutive = 0

def _clear_nav_backoff(tid: str):
    global nav_backoff_consecutive
    nav_retry_attempts.pop(tid, None)
    nav_retry_until.pop(tid, None)
    nav_backoff_consecutive = 0

def _open_window_tagged(url: str, tag: str, delay_ms: int):
    js = f"""
        (function(){{
            var go = function(u,t,delay){{
                var w = window.open('about:blank','_blank');
                if (!w) return;
                try {{ w.name = t; }} catch(e) {{}}
                setTimeout(function() {{
                    try {{ w.location.href = u; }} catch(e) {{}}
                }}, delay);
            }};
            go({json.dumps(url)}, {json.dumps(tag)}, {int(delay_ms)});
        }})();
    """
    _safe_execute_script(js)


def _finalize_url_for_handle_fast(handle, opened_at_ts):
    """
    EGYSZERŰ LOGIKA:

    - Ha a tab már nem él: (None, "timeout")
    - Beolvassuk a current_url-t
    - Ha http(s) ÉS NEM surebet.com host → elfogadjuk: ("ok")
    - Minden más (surebet.com, /nav, 404-es surebet oldal, stb.) → (None, "timeout")
      → ezzel NAV backoff lesz, ahogy szeretnéd.
    """
    try:
        if handle not in driver.window_handles:
            return (None, "timeout")
        driver.switch_to.window(handle)
        cur = driver.current_url or ""
    except Exception:
        return (None, "timeout")

    # Külső host? Akkor jó.
    if valid_external(cur):
        return (_sanitize_url(cur), "ok")

    # Minden surebet.com (beleértve /nav + 404) → timeout
    return (None, "timeout")


def _finalize_url_for_handle(handle, opened_at_ts) -> str | None:
    final, _state = _finalize_url_for_handle_fast(handle, opened_at_ts)
    return final

def resolve_pairs_round_robin(pairs) -> tuple[list[tuple[str | None, str | None]], list[tuple[str, str]]]:
    """
    Streaming CDP-s feloldás, most már extra védelemmel.
    """
    global driver, DRIVER_DEAD

    if DRIVER_DEAD:
        warn("[RR] DRIVER_DEAD=True, round-robin resolver skip – minden pár timeout.")
        num_pairs = len(pairs)
        return ([(None, None) for _ in range(num_pairs)],
                [("timeout", "timeout") for _ in range(num_pairs)])

    t0 = time.time()
    num_pairs = len(pairs)
    if num_pairs == 0:
        return [], []


    # normalizált lista: vagy None, vagy (href1, href2)
    norm: list[tuple[str, str] | None] = []
    for p in pairs:
        if p and p[0] and p[1]:
            norm.append((p[0], p[1]))
        else:
            norm.append(None)

    finals_by_pair: list[tuple[str | None, str | None]] = [(None, None) for _ in range(num_pairs)]
    states_by_pair: list[tuple[str, str]] = [("timeout", "timeout") for _ in range(num_pairs)]
    done_pairs = [False] * num_pairs

    # Ha nincs driver vagy nincs window, akkor itt FEJELÜNK KI szépen
    if driver is None:
        warn("[RR] driver is None, minden pár timeout → NAV backoff.")
        return finals_by_pair, states_by_pair

    try:
        if not driver.window_handles:
            warn("[RR] nincs élő Chrome window, round-robin skip → NAV backoff.")
            return finals_by_pair, states_by_pair
    except Exception as e:
        warn(f"[RR] window_handles hiba (round-robin start): {e} → NAV backoff.")
        return finals_by_pair, states_by_pair

    # targetId → {pair_index, pos(1/2)}
    tracking: dict[str, dict] = {}
    num_pairs_to_open = 0
    
    # Track handles before opening targets (for cleanup if CDP fails)
    try:
        handles_before = set(driver.window_handles) if driver else set()
    except Exception:
        handles_before = set()

    # 1) Targetek létrehozása (CDP-safe + window handle validation)
    for idx, p in enumerate(norm):
        if p is None:
            continue
        href1, href2 = p
        created_any = False

        # Optimization #1: Window handle validation before CDP operations
        try:
            if not driver.window_handles:
                warn(f"[RR] Nincs window handle idx={idx} előtt, skip target creation")
                continue
        except Exception:
            warn(f"[RR] Window handle validation hiba idx={idx}, skip target creation")
            continue

        # első oldal
        res1 = _safe_cdp_cmd(
            "Target.createTarget",
            {"url": href1, "background": True},
            label=f"RR href1 idx={idx}",
        )
        tid1 = res1.get("targetId") if isinstance(res1, dict) else None
        if tid1:
            tracking[tid1] = {"pair": idx, "pos": 1}
            created_any = True
        else:
            warn(f"[RR] Target.createTarget sikertelen (href1) idx={idx}")

        # második oldal
        res2 = _safe_cdp_cmd(
            "Target.createTarget",
            {"url": href2, "background": True},
            label=f"RR href2 idx={idx}",
        )
        tid2 = res2.get("targetId") if isinstance(res2, dict) else None
        if tid2:
            tracking[tid2] = {"pair": idx, "pos": 2}
            created_any = True
        else:
            warn(f"[RR] Target.createTarget sikertelen (href2) idx={idx}")

        if created_any:
            num_pairs_to_open += 1

    if not tracking:
        log("resolve_pairs_round_robin: nincs nyitható target (tracking üres / CDP skip)")
        return finals_by_pair, states_by_pair

    open_elapsed = time.time() - t0
    deadline = time.time() + (PAIR_TIMEOUT_SEC or 0.0)
    last_dbg = 0.0

    # 2) Polling CDP-vel (biztonságosan)
    while tracking and time.time() < deadline:
        info = _safe_cdp_cmd("Target.getTargets", {}, label="RR getTargets")
        if not isinstance(info, dict):
            # tipikusan akkor jön ide, ha idő közben bezárult a window / devtools
            warn("[RR] Target.getTargets → None (valószínűleg bezárult a window) → kilépés a round-robinből.")
            break

        targets = info.get("targetInfos", []) or []

        for t in targets:
            try:
                tid = t.get("targetId")
            except Exception:
                continue
            if tid not in tracking:
                continue

            url = (t.get("url") or "").strip()
            if not url:
                continue

            # csak akkor tekintjük késznek, ha már elhagyta a surebet.com-ot
            if not valid_external(url):
                continue

            entry = tracking.get(tid)
            if not entry:
                continue

            pair_idx = entry["pair"]
            pos = entry["pos"]

            if done_pairs[pair_idx]:
                # ezt a párt már lezártuk; a maradék targetet is bezárhatjuk
                # Optimization #3: Robust CDP cleanup with try-except
                try:
                    _safe_cdp_cmd("Target.closeTarget", {"targetId": tid}, label="RR closeTarget (pair already done)")
                except Exception:
                    pass  # Silent fail ha már bezárva vagy hiba van
                tracking.pop(tid, None)
                continue

            clean = _sanitize_url(url)
            f1, f2 = finals_by_pair[pair_idx]
            if pos == 1:
                f1 = clean
            else:
                f2 = clean
            finals_by_pair[pair_idx] = (f1, f2)

            # ha mindkét oldal megvan → pár kész, targetek bezárása
            if f1 and f2:
                states_by_pair[pair_idx] = ("ok", "ok")
                done_pairs[pair_idx] = True

                # csukjuk be a párhoz tartozó összes targetet
                to_close = [tid2 for tid2, info2 in tracking.items() if info2["pair"] == pair_idx]
                for tid2 in to_close:
                    # Optimization #3: Robust CDP cleanup with try-except
                    try:
                        _safe_cdp_cmd("Target.closeTarget", {"targetId": tid2}, label="RR closeTarget (pair done)")
                    except Exception:
                        pass  # Silent fail ha már bezárva vagy hiba van
                    tracking.pop(tid2, None)

                if LOG_PAIR_DONE:
                    log(f"[RR] ✓ Pár kész (idx={pair_idx}) f1={f1} f2={f2}")

        # Instant timeout check: ha nincs külső 'page' target, ne várjunk tovább
        # MÓDOSÍTVA: csak akkor instant timeout, ha már eltelt NO_EXTERNAL_TARGET_MIN_WAIT_SEC a megnyitás óta
        has_external_targets = False
        for t in targets:
            if t.get("type") != "page":
                continue
            url = t.get("url") or ""
            if not url.startswith("http"):
                continue
            try:
                host = urlparse(url).netloc.lower()
                if "surebet.com" not in host:
                    has_external_targets = True
                    break
            except Exception:
                pass
        
        # Instant timeout csak akkor, ha:
        # 1. Nincs külső 'page' target
        # 2. ÉS eltelt NO_EXTERNAL_TARGET_MIN_WAIT_SEC a megnyitás óta (t0)
        elapsed_since_open = time.time() - t0
        if not has_external_targets and tracking and elapsed_since_open >= NO_EXTERNAL_TARGET_MIN_WAIT_SEC:
            log(f"[RR] ⚡ Nincs külső 'page' target + {elapsed_since_open:.1f}s eltelt → instant timeout")
            break

        # debug log 2 mp-enként (ha engedélyezve)
        if NAV_DEBUG_INTERVAL > 0 and (time.time() - last_dbg) >= NAV_DEBUG_INTERVAL:
            _cdp_debug_log_nav_targets("RR streaming poll")
            last_dbg = time.time()

        time.sleep(CDP_POLL_INTERVAL)

    # 3) Timeout után: minden maradék target bezárása (safe CDP + fallback + Optimization #3)
    failed_cdp_closes = []
    for tid in list(tracking.keys()):
        # Optimization #3: Robust CDP cleanup with try-except
        try:
            result = _safe_cdp_cmd("Target.closeTarget", {"targetId": tid}, label="RR closeTarget (timeout)")
        except Exception:
            result = None  # Silent fail
        tracking.pop(tid, None)
        
        # Ha CDP nem működött, jegyezzük fel
        if result is None:
            failed_cdp_closes.append(tid)
    
    # Fallback: ha voltak sikertelen CDP bezárások, próbáljunk Selenium-level cleanup-ot
    if failed_cdp_closes:
        try:
            current_handles = set(driver.window_handles) if driver else set()
            # Új ablakok amik a target nyitások során keletkeztek
            extra_handles = current_handles - handles_before
            
            # VÉDELEM: MAIN_HANDLE, GROUP, NEXT tabok SOHA ne legyenek bezárva
            protected_handles = set()
            if MAIN_HANDLE and MAIN_HANDLE in extra_handles:
                protected_handles.add(MAIN_HANDLE)
                warn(f"[RR] 🛡️ MAIN_HANDLE védelem aktiválva fallback cleanup-ban")
            for info in group_tabs.values():
                h = info.get("handle")
                if h and h in extra_handles:
                    protected_handles.add(h)
            for info in next_tabs.values():
                h = info.get("handle")
                if h and h in extra_handles:
                    protected_handles.add(h)
            
            extra_handles = extra_handles - protected_handles
            
            if extra_handles:
                warn(f"[RR] {len(failed_cdp_closes)} CDP closeTarget sikertelen, {len(extra_handles)} extra ablak – Selenium fallback bezárás")
                
                # Optimization #5: Window context save robustness
                original_handle = None
                try:
                    original_handle = driver.current_window_handle
                    if original_handle not in driver.window_handles:
                        # Current handle már nem él, fallback az első elérhető handle-re
                        original_handle = driver.window_handles[0] if driver.window_handles else None
                except Exception:
                    # Ha bármilyen hiba van, fallback
                    try:
                        original_handle = driver.window_handles[0] if driver.window_handles else None
                    except Exception:
                        original_handle = None
                
                for handle in extra_handles:
                    try:
                        driver.switch_to.window(handle)
                        driver.close()
                        warn(f"[RR] ✓ Fallback close sikeres: {handle}")
                    except Exception as close_err:
                        warn(f"[RR] ⚠️ Fallback close hiba: {handle} - {close_err}")
                
                # Visszaváltunk az eredeti ablakra, ha létezik
                if original_handle and original_handle in driver.window_handles:
                    try:
                        driver.switch_to.window(original_handle)
                    except Exception:
                        pass
        except Exception as fallback_err:
            warn(f"[RR] Fallback cleanup hiba: {fallback_err}")

    total = time.time() - t0
    num_successful = sum(1 for done in done_pairs if done)
    log(
        f"resolve_pairs_round_robin(streaming): {num_pairs_to_open} pár, sikeres={num_successful}, "
        f"open={open_elapsed:.3f}s, total={total:.3f}s, timeout={PAIR_TIMEOUT_SEC:.1f}s"
    )

    return finals_by_pair, states_by_pair


def resolve_two_final_urls_rr(href1, href2):
    """Helper: egy darab pár feloldása az új CDP-s round-robin resolverrel."""
    finals, states = resolve_pairs_round_robin([(href1, href2)])
    return finals[0], states[0]



def resolve_pairs_staggered(pairs, timeout=RESOLVE_TIMEOUT, stable_period=RESOLVE_STABLE_PERIOD, poll_interval=RESOLVE_POLL_INTERVAL):
    """
    NAV-only: nincs előzetes 'fast' ellenőrzés, nincs regex.
    Egyszerűen megnyitjuk a párokat, és mindkét tabnál azt figyeljük,
    mikor hagyja el a surebet.com-ot — akkor elfogadjuk az aktuális URL-t.
    """
    # Normalizáljuk: csak (href1, href2) tuple vagy None
    norm = []
    for p in pairs:
        if p and p[0] and p[1]:
            norm.append((p[0], p[1]))
        else:
            norm.append(None)

    def _guid():
        return f"{int(time.time()*1000)}{random.randint(100,999)}"

    need_open = []
    taginfo = []
    for i, p in enumerate(norm):
        if p is None:
            need_open.append(False)
            taginfo.append({"pair_index": i, "tag1": None, "tag2": None})
        else:
            need_open.append(True)
            tag1 = f"SB|{_guid()}|1|{i}"
            tag2 = f"SB|{_guid()}|2|{i}"
            taginfo.append({"pair_index": i, "tag1": tag1, "tag2": tag2})

    created = []
    prev = set()
    try:
        ensure_active_window()
        prev = set(driver.window_handles)
        now_ts = time.time()

        # 1. pár
        if len(norm) >= 1 and need_open[0]:
            base = 0
            _open_window_tagged(norm[0][0], taginfo[0]["tag1"], base + 0)
            _open_window_tagged(norm[0][1], taginfo[0]["tag2"], base + OPEN_WITHIN_PAIR_MS)

        # 2. pár
        if len(norm) >= 2 and need_open[1]:
            base = OPEN_PAIR_STAGGER_MS_BASE * 1  # 200ms
            _open_window_tagged(norm[1][0], taginfo[1]["tag1"], base + 0)
            _open_window_tagged(norm[1][1], taginfo[1]["tag2"], base + OPEN_WITHIN_PAIR_MS)

        # 3. pár
        if len(norm) >= 3 and need_open[2]:
            base = OPEN_PAIR_STAGGER_MS_BASE * 2  # 400ms
            _open_window_tagged(norm[2][0], taginfo[2]["tag1"], base + 0)
            _open_window_tagged(norm[2][1], taginfo[2]["tag2"], base + OPEN_WITHIN_PAIR_MS)

        target_count = 0
        target_count += 2 if (len(norm) >= 1 and need_open[0]) else 0
        target_count += 2 if (len(norm) >= 2 and need_open[1]) else 0
        target_count += 2 if (len(norm) >= 3 and need_open[2]) else 0

        deadline = time.time() + HANDLE_WAIT_TIMEOUT
        created = []
        while time.time() < deadline:
            try:
                now_handles = driver.window_handles
            except Exception:
                now_handles = []
            newh = list(set(now_handles) - prev)
            if newh:
                created = newh  # ami megvan, AZONNAL dolgozzuk fel
                break
# nincs sleep


        # tag -> handle mapping
        handle_tag = {}
        for h in created:
            try:
                if h not in driver.window_handles:
                    continue
                driver.switch_to.window(h)
                name = _safe_execute_script("return window.name || ''") or ""
            except Exception:
                name = ""
            handle_tag[h] = name

        finals_by_pair = {i: [None, None] for i in range(len(norm))}

        # NAV-only feloldás
        for h in created:
            tag = handle_tag.get(h, "")
            final = _finalize_url_for_handle(h, now_ts)
            m = re.match(r'^SB\|.+\|(1|2)\|(\d+)$', tag)
            if m:
                pos = int(m.group(1))  # 1 vagy 2
                pidx = int(m.group(2))
                if 0 <= pidx < len(norm):
                    finals_by_pair[pidx][pos-1] = final

        out = []
        for i in range(len(norm)):
            out.append(tuple(finals_by_pair[i]))
        return out

    finally:
        try:
            cur = set(driver.window_handles)
            created_list = list(cur - prev) if prev else []
        except Exception:
            created_list = []
        
        # VÉDELEM: MAIN_HANDLE, GROUP, NEXT tabok SOHA ne legyenek bezárva
        protected_handles = set()
        if MAIN_HANDLE and MAIN_HANDLE in created_list:
            protected_handles.add(MAIN_HANDLE)
            warn(f"[resolve_all_pairs_streaming] 🛡️ MAIN_HANDLE védelem aktiválva cleanup-ban")
        for info in group_tabs.values():
            h = info.get("handle")
            if h and h in created_list:
                protected_handles.add(h)
        for info in next_tabs.values():
            h = info.get("handle")
            if h and h in created_list:
                protected_handles.add(h)
        
        # Csak a nem védett handle-eket zárjuk be
        for h in created_list:
            if h in protected_handles:
                continue
            try:
                if h in driver.window_handles:
                    driver.switch_to.window(h)
                    driver.close()
            except Exception:
                pass
        try:
            if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
                driver.switch_to.window(MAIN_HANDLE)
        except Exception:
            pass


def resolve_two_final_urls(href1, href2,
                           timeout=RESOLVE_TIMEOUT,
                           stable_period=RESOLVE_STABLE_PERIOD,
                           poll_interval=RESOLVE_POLL_INTERVAL):
    """
    NAV-only, 2 ablakos: nincs fast/regex; amint elhagyja a surebet-et, elfogadjuk az URL-t.
    """
    if not (href1 and href2):
        return (None, None)

    try:
        original = driver.current_window_handle
    except Exception:
        original = None

    tag1 = f"SB|{int(time.time()*1000)}{random.randint(100,999)}|1|0"
    tag2 = f"SB|{int(time.time()*1000)}{random.randint(100,999)}|2|0"

    prev = set()
    created = []
    try:
        ensure_active_window()
        prev = set(driver.window_handles)
        now_ts = time.time()

        _open_window_tagged(href1, tag1, 0)
        _open_window_tagged(href2, tag2, OPEN_WITHIN_PAIR_MS)

        target_count = 2
        deadline = time.time() + HANDLE_WAIT_TIMEOUT
        created = []
        while time.time() < deadline:
            try:
                now_handles = driver.window_handles
            except Exception:
                now_handles = []
            newh = list(set(now_handles) - prev)
            if newh:
                created = newh
                break
# nincs sleep


        final1 = None
        final2 = None
        for h in created:
            try:
                if h not in driver.window_handles:
                    continue
                driver.switch_to.window(h)
                name = _safe_execute_script("return window.name || ''") or ""
            except Exception:
                name = ""
            final = _finalize_url_for_handle(h, now_ts)
            if name.startswith("SB") and "|1|" in name:
                final1 = final
            elif name.startswith("SB") and "|2|" in name:
                final2 = final

        return (final1, final2)

    finally:
        try:
            cur = set(driver.window_handles)
            created_list = list(cur - prev) if prev else []
        except Exception:
            created_list = []
        for h in created_list:
            try:
                if h in driver.window_handles:
                    driver.switch_to.window(h)
                    driver.close()
            except Exception:
                pass
        try:
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
        except Exception:
            pass


def background_nav_worker():
    """
    NAV-only: OPEN_TASKS folyamatos feldolgozása háttérben.
    """
    global link_cache, DRIVER_DEAD, AIOHTTP_AVAILABLE

    while True:
        # ha a driver halott, itt is lépjünk ki
        if DRIVER_DEAD:
            warn("💀 NAV worker leáll – DRIVER_DEAD=True.")
            break

        try:
            # nincs feladat → pici alvás, hogy ne pörögjön szét a CPU
            if not OPEN_TASKS:
                time.sleep(0.05)
                continue


            # 1) vegyünk ki max NAV_WORKER_MAX_PAIRS feladatot
            todo = []
            while OPEN_TASKS and len(todo) < NAV_WORKER_MAX_PAIRS:
                todo.append(OPEN_TASKS.popleft())

            if not todo:
                continue

            # 2) Extract bookmaker URLs using parallel async extraction
            finals = []
            states = []
            
            log(f"[NAV-WORKER] Processing {len(todo)} pairs with parallel async extraction")
            
            # Try async parallel extraction if aiohttp is available
            if AIOHTTP_AVAILABLE:
                try:
                    # Use asyncio.run() to call async function from sync code
                    pairs_async = asyncio.run(extract_urls_parallel_async(todo, driver))
                    
                    # Process async results
                    successful = 0
                    failed = 0
                    
                    for idx, (f1, f2) in enumerate(pairs_async):
                        task = todo[idx]
                        task_id = task.get('id', 'N/A')
                        
                        # Track extraction failures
                        if 'extraction_failures' not in task:
                            task['extraction_failures'] = 0
                        
                        # Validate both URLs
                        if f1 and f2 and valid_external(f1) and valid_external(f2):
                            successful += 1
                            task['extraction_failures'] = 0  # Reset on success
                            finals.append((f1, f2))
                            states.append(("success", "success"))
                            log(f"[NAV-EXTRACT] ✅ {task_id}: Got both URLs")
                        else:
                            failed += 1
                            task['extraction_failures'] += 1
                            finals.append((f1, f2))
                            states.append(("timeout", "timeout"))
                            
                            # Log failure details
                            if task['extraction_failures'] < 2:
                                log(f"[NAV-EXTRACT] ⚠️ {task_id}: Retry {task['extraction_failures']}/2")
                            else:
                                log(f"[NAV-EXTRACT] ❌ {task_id}: 2 failures, will try CDP fallback")
                    
                    # Show statistics
                    log(f"[NAV-WORKER] ✅ {successful}/{len(todo)} pairs successful, ❌ {failed}/{len(todo)} failed")
                    
                    # Handle CDP fallback for persistent failures
                    for idx, task in enumerate(todo):
                        if task.get('extraction_failures', 0) >= 2 and (not finals[idx][0] or not finals[idx][1]):
                            task_id = task.get('id', 'N/A')
                            h1, h2 = task.get('hrefs', (None, None))
                            
                            if h1 and h2:
                                log(f"[NAV-FALLBACK] 🔄 {task_id}: Trying CDP method after {task['extraction_failures']} failures")
                                try:
                                    # Use old CDP method as fallback
                                    cdp_pairs = [(h1, h2)]
                                    cdp_finals, cdp_states = resolve_pairs_round_robin(cdp_pairs)
                                    
                                    if cdp_finals and len(cdp_finals) > 0:
                                        f1, f2 = cdp_finals[0]
                                        if f1 and f2 and valid_external(f1) and valid_external(f2):
                                            finals[idx] = (f1, f2)
                                            states[idx] = ("success", "success")
                                            task['extraction_failures'] = 0  # Reset after CDP success
                                            log(f"[NAV-FALLBACK] ✅ {task_id}: CDP succeeded")
                                        else:
                                            log(f"[NAV-FALLBACK] ❌ {task_id}: CDP also failed")
                                except Exception as e:
                                    log(f"[NAV-FALLBACK] ❌ {task_id}: CDP error: {e}")
                    
                except Exception as e:
                    log(f"[NAV-WORKER] ⚠️ Async extraction error: {e}, falling back to sequential")
                    # Fallback to sequential extraction
                    AIOHTTP_AVAILABLE = False  # Disable for this session
            
            # Fallback: Sequential extraction if aiohttp not available
            if not AIOHTTP_AVAILABLE:
                log(f"[NAV-WORKER] Using sequential extraction (aiohttp not available)")
                
                for t in todo:
                    h1, h2 = t.get("hrefs") or (None, None)
                    
                    if h1 and h2:
                        try:
                            f1 = extract_bookmaker_url_from_nav_link(h1, driver)
                            f2 = extract_bookmaker_url_from_nav_link(h2, driver)
                            
                            s1 = "success" if (f1 and valid_external(f1)) else "timeout"
                            s2 = "success" if (f2 and valid_external(f2)) else "timeout"
                            
                            finals.append((f1, f2))
                            states.append((s1, s2))
                            
                            if f1 and f2:
                                log(f"[NAV-EXTRACT] ✅ {t.get('id', 'N/A')}: Got both URLs")
                            else:
                                log(f"[NAV-EXTRACT] ⚠️ {t.get('id', 'N/A')}: f1={bool(f1)}, f2={bool(f2)}")
                        except Exception as e:
                            log(f"[NAV-EXTRACT] ❌ {t.get('id', 'N/A')}: Error: {e}")
                            finals.append((None, None))
                            states.append(("timeout", "timeout"))
                    else:
                        finals.append((None, None))
                        states.append(("timeout", "timeout"))

            # 3) Eredmények feldolgozása
            for idx, task in enumerate(todo):
                try:
                    tbody_id = task["id"]

                    # Get finals and states
                    if idx < len(finals):
                        (f1, f2) = finals[idx]
                        (s1, s2) = states[idx]
                    else:
                        # Fallback if something went wrong
                        h1, h2 = task.get("hrefs") or (None, None)
                        if h1 and h2:
                            try:
                                f1 = extract_bookmaker_url_from_nav_link(h1, driver)
                                f2 = extract_bookmaker_url_from_nav_link(h2, driver)
                                s1 = "success" if (f1 and valid_external(f1)) else "timeout"
                                s2 = "success" if (f2 and valid_external(f2)) else "timeout"
                            except Exception as e:
                                log(f"[NAV-EXTRACT-FALLBACK] Error: {e}")
                                f1, f2 = None, None
                                s1, s2 = ("timeout", "timeout")
                        else:
                            f1, f2 = h1, h2
                            s1, s2 = ("timeout", "timeout")

                    task["finals"] = (f1, f2)

                    ok = valid_external(f1) and valid_external(f2)
                    if ok:
                        tip_payload = _build_tip_payload_from_task(task)
                        update_payload = _build_update_payload_from_task(task)
                        dispatcher.enqueue_save({
                            "id": tbody_id,
                            "tip_payload": tip_payload,
                            "update_payload": update_payload,
                            "state_info": {
                                "odds1": tip_payload["odds1"],
                                "odds2": tip_payload["odds2"],
                                "profit_percent": tip_payload["profit_percent"],
                            },
                            "finals": task["finals"],
                        })
                        link_cache[tbody_id] = {
                            "link1": f1,
                            "link2": f2,
                            "saved_at": datetime.now().isoformat()
                        }
                        _clear_nav_backoff(tbody_id)
                    else:
                        # minden nem 'ok' (beleértve a timeout-ot) → NAV backoff
                        # Részletes log hogy miért bukott el a valid_external
                        f1_valid = valid_external(f1)
                        f2_valid = valid_external(f2)
                        
                        # Detect about:blank or surebet.com URLs
                        f1_issue = "None" if f1 is None else ("about:blank" if f1 == "about:blank" else ("surebet.com" if is_surebet_url(f1) else "invalid"))
                        f2_issue = "None" if f2 is None else ("about:blank" if f2 == "about:blank" else ("surebet.com" if is_surebet_url(f2) else "invalid"))
                        
                        if not f1_valid or not f2_valid:
                            warn(f"❌ valid_external failed for {tbody_id}: f1={f1_issue} (url={f1}), f2={f2_issue} (url={f2})")
                        
                        if 'not_found' in (s1, s2):
                            warn(f"🔎 Page not found → NAV backoff: {tbody_id} (s1={s1}, s2={s2})")
                        _schedule_nav_backoff(tbody_id)

                except Exception as task_err:
                    # Ha driver-connection hiba → teljes NAV leáll (propagáljuk)
                    if _is_driver_connection_error(task_err):
                        raise
                    
                    # Egyéb hiba → task eldobása, scraper majd újra felveszi természetes módon
                    tbody_id = task.get('id', 'N/A')
                    warn(f"⚠️ Task feldolgozás hiba, eldobva (scraper majd újra felveszi): {tbody_id} - {task_err}")

            save_link_cache(link_cache)

        except Exception as e:
            warn(f"[NAV-WORKER] Hiba a háttér workerben: {e}")
            time.sleep(1.0)


def batch_save_new_ids(new_ids: list, higher_ids: set | None = None):
    """
    NAV-only:
    - Ha a cache-ben már megvan mindkét külső végső link (task['finals']),
      azonnal SAVE.
    - Különben betesszük a globális OPEN_TASKS várólistába, és
      a fő while-loop végén hívott process_open_tasks() nyitja meg /nav-on át.
    """
    # 🔒 BOOTSTRAP alatt (első 50 mp) nem indítunk új SAVE/NAV feloldást,
    # csak gyűjtjük az ID-ket és nyitjuk a tabokat.
    if in_bootstrap_phase():
        return

    if not new_ids:
        return

    now = time.time()
    tasks = []
    for tid in new_ids:
        # ha láttuk már (file/os), nem új
        if tid in seen:
            continue
        # ha például NEXT/GROUP-ban magasabb prioritású halmazban van, ugorjuk
        if higher_ids and tid in higher_ids:
            continue
        # NAV-backoff: ha várunk még, most ne próbálkozzunk vele
        until = nav_retry_until.get(tid, 0)
        if until and now < until:
            continue

        t = prepare_new_task_for_id(tid)
        if t:
            tasks.append(t)

    if not tasks:
        return

    # 1) Azonnal menthetőek (ha cache-ből már megvan mindkét külső link)
    for t in tasks:
        finals = t.get("finals") or (None, None)
        f1, f2 = finals
        if valid_external(f1) and valid_external(f2):
            tip_payload = _build_tip_payload_from_task(t)
            update_payload = _build_update_payload_from_task(t)
            dispatcher.enqueue_save({
                "id": t["id"],
                "tip_payload": tip_payload,
                "update_payload": update_payload,
                "state_info": {
                    "odds1": tip_payload["odds1"],
                    "odds2": tip_payload["odds2"],
                    "profit_percent": tip_payload["profit_percent"],
                },
                "finals": finals,
            })
            _clear_nav_backoff(t["id"])
        else:
            # 2) Feloldásra várók → globális várólista
            enqueue_open_task(t)

    # FONTOS: itt már NEM hívunk process_open_tasks()-t,
    # hogy egy while-loop iterációban csak EGYSZER fusson NAV-feloldás
    # (a fő ciklus végén: process_open_tasks(max_pairs=6)).

# ---------- UPDATE PATH ----------
def snapshot_update_values_by_id(tbody_id: str):
    js = r"""
    const id = arguments[0];
    const sel1 = 'tbody.surebet_record[data-id="'+id+'"]';
    const sel2 = 'tbody.surebet_record[dataid="'+id+'"]';
    const row = document.querySelector(sel1) || document.querySelector(sel2);
    if (!row) return null;
    const getTxt = (el) => el ? (el.textContent || '').trim() : '';
    const oddsCells = Array.from(row.querySelectorAll('td.value[class*="odd_record_"]'));
    const odds1 = getTxt(oddsCells[0]);
    const odds2 = getTxt(oddsCells[1]);
    const sels = ['td.profit','td[class*="profit"]','td.gain','td.percent','td.max_profit','.profit','.gain','.percent'];
    let profit = '';
    for (const s of sels) {
      const el = row.querySelector(s);
      const t = getTxt(el);
      if (t) { profit = t; break; }
    }
    return {odds1, odds2, profit};
    """
    try:
        return driver.execute_script(js, tbody_id)
    except Exception:
        return None

def handle_update_for_id(tbody_id):
    # 🔒 BOOTSTRAP alatt nem küldünk UPDATE-et – csak figyeljük az ID-ket
    if in_bootstrap_phase():
        return

    try:
        snap = snapshot_update_values_by_id(tbody_id)
        if not snap:
            return
        odds1_text = (snap.get("odds1") or "").strip()
        odds2_text = (snap.get("odds2") or "").strip()
        o1 = parse_float(odds1_text)
        o2 = parse_float(odds2_text)
        profit_dom = (snap.get("profit") or "").strip()
        if profit_dom:
            profit_now = norm_profit_str(profit_dom)
        else:
            if o1 is not None and o2 is not None:
                profit_now = norm_profit_str(compute_profit_percent(o1, o2))
            else:
                profit_now = norm_profit_str("0%")
        o1n = norm_odds(o1 if o1 is not None else odds1_text)
        o2n = norm_odds(o2 if o2 is not None else odds2_text)

        if tbody_id not in last_sent_state:
            last_sent_state[tbody_id] = {"odds1": o1n, "odds2": o2n, "profit_percent": profit_now}
            return

        prev = last_sent_state[tbody_id]
        changed = (o1n != prev.get("odds1")) or (o2n != prev.get("odds2")) or (profit_now != prev.get("profit_percent"))
        can_send = (time.time() - last_update_attempt_ts.get(tbody_id, 0)) >= UPDATE_MIN_INTERVAL

        if changed and can_send:
            payload = {
                "type": "update",
                "id": tbody_id,
                "odds1": o1n,
                "odds2": o2n,
                "profit_percent": profit_now,
                "updated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            _pending_update_buffer.append(payload)
            last_update_attempt_ts[tbody_id] = time.time()
            # ha már sok UPDATE/DELETE gyűlt, azonnal flush
            maybe_flush_immediate()
    except Exception:
        return

# ---------- TAB REGISZTEREK ----------
group_tabs = {}
group_blocked_until = {}
next_tabs  = {}

id_source = {}
last_seen_ts = {}

handle_birth = {}

# ---------- MEMORY LEAK PREVENTION: Periodic cleanup ----------
def cleanup_old_tracking_data():
    """
    Eltávolítja a régi tracking adatokat hogy elkerüljük a memory leak-et.
    Törli az 1 óránál régebben látott ID-k adatait.
    Ezt a main loop-ból hívjuk meg periodikusan (5 percenként).
    """
    try:
        cutoff = time.time() - 3600  # 1 óra
        
        # Azonosítsuk a régi ID-ket (amik >1 órája nem voltak látva)
        old_ids = {tid for tid, ts in last_seen_ts.items() if ts < cutoff}
        
        if old_ids:
            # Töröljük minden tracking dict-ből
            for tid in old_ids:
                last_sent_state.pop(tid, None)
                last_update_ts.pop(tid, None)
                last_update_attempt_ts.pop(tid, None)
                last_seen_ts.pop(tid, None)
                id_source.pop(tid, None)
            
            log(f"🧹 Memory cleanup: {len(old_ids)} régi tracking bejegyzés törölve")
        
        # Tisztítsuk a régi blocked group-okat is (>24 óra)
        blocked_cutoff = time.time()
        old_blocked = {url for url, ts in group_blocked_until.items() if ts < blocked_cutoff}
        if old_blocked:
            for url in old_blocked:
                group_blocked_until.pop(url, None)
            if len(old_blocked) > 5:  # Csak ha sok van
                log(f"🧹 Memory cleanup: {len(old_blocked)} lejárt blocked group törölve")
                
    except Exception as e:
        warn(f"⚠️ cleanup_old_tracking_data hiba: {e}")

# ---------- SERVER OUTAGE PROTECTION ----------
# Server check throttling (10 perc intervallum az első check után)
last_server_check_time = 0
SERVER_CHECK_INTERVAL = 600  # 10 minutes in seconds

def is_surebet_server_available():
    """
    Ellenőrzi hogy a surebet.com szerver elérhető-e.
    HEAD request használata (gyorsabb és kevésbé gyanús mint GET).
    Csak 2 próbálkozás (elég a megbízhatósághoz, kevésbé agresszív).
    
    Returns:
        True if server available
        False if confirmed down (all 2 attempts failed)
    """
    test_url = "https://www.surebet.com"
    max_attempts = 2  # Reduced from 3 (still reliable, less aggressive)
    attempt_delay = 3  # Reduced from 5 (faster checks)
    
    for attempt in range(1, max_attempts + 1):
        try:
            log(f"🔍 Szerver elérhetőség ellenőrzése (kísérlet {attempt}/{max_attempts})...")
            log(f"  ↳ HEAD request használata (gyorsabb, kevésbé gyanús)")
            # Use HEAD instead of GET - faster, less data, less suspicious
            response = requests.head(test_url, timeout=10)
            
            # Success if status < 500 (even 404 means server is responding)
            if response.status_code < 500:
                log(f"✅ Szerver elérhető (status: {response.status_code})")
                return True
            else:
                warn(f"⚠️ Szerver hiba (status: {response.status_code})")
                
        except requests.exceptions.ConnectionError as e:
            warn(f"❌ Connection hiba (kísérlet {attempt}/{max_attempts}): {e}")
        except requests.exceptions.Timeout as e:
            warn(f"⏱️ Timeout (kísérlet {attempt}/{max_attempts}): {e}")
        except Exception as e:
            warn(f"⚠️ Egyéb hiba (kísérlet {attempt}/{max_attempts}): {e}")
        
        # Wait before next attempt (except after last attempt)
        if attempt < max_attempts:
            log(f"⏳ Várakozás {attempt_delay}s következő próba előtt...")
            time.sleep(attempt_delay)
    
    # All attempts failed
    log(f"❌ SZERVER MEGERŐSÍTVE ELÉRHETETLEN ({max_attempts}/{max_attempts} próba sikertelen)")
    return False

def wait_for_server_recovery():
    """
    Megáll és vár amíg a surebet.com szerver újra elérhető lesz.
    5 percenként újrapróbálkozik.
    
    A script TELJESEN LEÁLL ebben a módban - nem végez semmilyen munkát.
    """
    retry_interval = 300  # 5 minutes
    
    log("=" * 80)
    log("🛑 KRITIKUS: SUREBET.COM SZERVER NEM ELÉRHETŐ")
    log("=" * 80)
    log("⏸️  Script LEÁLLT - várakozás a szerver visszatérésére")
    log(f"🔄 Újrapróbálkozás {retry_interval // 60} percenként")
    log("=" * 80)
    
    while True:
        log(f"⏳ Várakozás {retry_interval}s ({retry_interval // 60} perc)...")
        time.sleep(retry_interval)
        
        log("")
        log("🔄 Szerver elérhetőség újraellenőrzése...")
        
        if is_surebet_server_available():
            log("=" * 80)
            log("✅ SZERVER ÚJRA ELÉRHETŐ!")
            log("=" * 80)
            log("▶️  Script folytatódik...")
            log("")
            return True
        else:
            log("❌ Szerver még mindig nem elérhető")
            log("⏸️  Várakozás folytatódik...")
            # Loop continues - wait another 5 minutes

# ---------- GROUP helpers ----------
def is_group_blocked(url, now_ts):
    return now_ts < group_blocked_until.get(url, 0)

def block_group_url(url, seconds, reason=""):
    group_blocked_until[url] = time.time() + seconds
    log(f"⛔ GROUP tiltólista {seconds}s: {url} ({reason})")

def close_group_tab(url):
    global CLOSING_HANDLES
    info = group_tabs.get(url)
    if not info:
        return
    handle = info.get("handle")
    if handle:
        CLOSING_HANDLES.add(handle)  # Jelzés, hogy bezárás alatt van
        # 📋 Diagnostic: race condition tracking
        DIAG_LOGGER.log_race_condition("GROUP_TAB_CLOSING", 
                                       handles_state=f"closing={len(CLOSING_HANDLES)}", 
                                       pending_cdp=len(PENDING_CDP_CLOSES))
    try:
        if handle and handle in driver.window_handles:
            driver.switch_to.window(handle)
            driver.close()
    except Exception:
        pass
    finally:
        group_tabs.pop(url, None)
        if handle:
            CLOSING_HANDLES.discard(handle)  # Eltávolítás bezárás után
        try:
            if driver.window_handles:
                driver.switch_to.window(driver.window_handles[0])
        except Exception:
            pass

def find_group_link_in_tbody(tbody):
    try:
        a = tbody.find_element(By.CSS_SELECTOR, "a.group-link")
        href = a.get_attribute("href")
        if not href:
            return None
        cur = urlparse(driver.current_url)
        base = f"{cur.scheme}://{cur.netloc}"
        return urljoin(base, href)
    except Exception:
        return None

def _rand_group_refresh_interval():
    """Simple random interval between min and max"""
    return random.uniform(GROUP_REFRESH_MIN, GROUP_REFRESH_MAX)

def calculate_group_refresh_interval(info: dict, found_new_element: bool) -> float:
    """
    Calculate adaptive refresh interval based on page state.
    
    States:
    - FRESH_PAGE: First refresh after opening (145-170s)
    - SECOND_REFRESH: Second refresh (85-110s)
    - NORMAL: 3rd+ refresh with regular updates (60-75s)
    - STALE: 7+ refreshes without new elements (70-85s)
    - BOOST: Just found new element (85-110s)
    """
    state = info.get('state', 'FRESH_PAGE')
    refresh_count = info.get('refresh_count', 0)
    no_update_count = info.get('no_update_count', 0)
    
    if found_new_element:
        # Boost mode after finding new element
        return random.uniform(85, 110)
    elif state == 'FRESH_PAGE':
        return random.uniform(145, 170)
    elif state == 'SECOND_REFRESH':
        return random.uniform(85, 110)
    elif no_update_count >= 7:
        # Stale - slow down
        return random.uniform(70, 85)
    else:
        # Normal refresh
        return random.uniform(60, 75)

def open_group_tab_if_needed(group_url):
    now_ts = time.time()
    if group_url in group_tabs:
        if LOG_GROUP_ALREADY_OPEN_VERBOSE:
            log(f"ℹ️ Group már nyitva, nem nyitjuk újra: {group_url}")
        return
    if is_group_blocked(group_url, now_ts):
        log(f"⏳ Group URL tiltva még: {group_url}")
        return

    try:
        original = driver.current_window_handle
    except Exception:
        original = None

    try:
        driver.switch_to.new_window('tab')
        driver.get(group_url)
        _inject_disable_animations()
        handle = driver.current_window_handle

        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        tb_count = len(driver.find_elements(By.CSS_SELECTOR, GROUP_SELECTOR))
        if tb_count <= GROUP_EMPTY_CLOSE_TB_THRESHOLD:
            try:
                driver.close()
            except Exception:
                pass
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
            block_group_url(group_url, GROUP_REOPEN_BACKOFF_SEC, "empty-at-open")
            return

        now = time.time()
        group_tabs[group_url] = {
            "handle": handle,
            "active_ids": set(),
            "created_at": now,
            "last_refresh": now,
            "next_refresh": now + _rand_group_refresh_interval(),
            "needs_scan": True,
            "state": "FRESH_PAGE",
            "refresh_count": 0,
            "no_update_count": 0,
            "has_new_element": False,
        }
        if original and original in driver.window_handles:
            driver.switch_to.window(original)
        log(f"🆕 Group tab nyitva: {group_url}")
        return
    except Exception as e:
        warn(f"⚠️ Group nyitás hiba: {e}")
        block_group_url(group_url, GROUP_ERR_BACKOFF_SEC, "open-fail")
        try:
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
        except Exception:
            pass


# =============================================================================
# 🔄 JSON AUTO-UPDATE FUNCTIONS
# =============================================================================

def fetch_surebets_json_lightweight(driver):
    """
    Fetch surebets JSON WITHOUT full page refresh
    Uses Selenium cookies for authentication
    """
    try:
        # Get authentication from Selenium
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        
        # Get user agent
        user_agent = driver.execute_script("return navigator.userAgent;")
        
        # Make lightweight JSON request
        response = requests.get(
            "https://en.surebet.com/surebets?product=surebets&autoupdate=1&format=json",
            cookies=cookies,
            headers={
                'User-Agent': user_agent,
                'Accept': 'application/json',
                'Referer': 'https://en.surebet.com/surebets',
            },
            timeout=5
        )
        
        if response.status_code == 200:
            return response.json()
        else:
            log(f"[JSON-FETCH] Status {response.status_code}")
            return None
            
    except Exception as e:
        log(f"[JSON-FETCH] Error: {e}")
        return None


def show_update_timestamp(driver):
    """
    Show visual feedback: "Updated X seconds ago"
    """
    try:
        driver.execute_script("""
            // Remove old timestamp if exists
            var oldTimestamp = document.getElementById('json-update-timestamp');
            if (oldTimestamp) {
                oldTimestamp.remove();
            }
            
            // Create new timestamp badge
            var badge = document.createElement('div');
            badge.id = 'json-update-timestamp';
            badge.style.position = 'fixed';
            badge.style.top = '10px';
            badge.style.right = '10px';
            badge.style.backgroundColor = '#28a745';
            badge.style.color = 'white';
            badge.style.padding = '8px 12px';
            badge.style.borderRadius = '4px';
            badge.style.fontSize = '12px';
            badge.style.fontWeight = 'bold';
            badge.style.zIndex = '99999';
            badge.style.boxShadow = '0 2px 4px rgba(0,0,0,0.2)';
            badge.innerHTML = '🔄 Updated 0 seconds ago';
            document.body.appendChild(badge);
            
            // Update timestamp every second
            var startTime = Date.now();
            if (window.updateTimestampInterval) {
                clearInterval(window.updateTimestampInterval);
            }
            window.updateTimestampInterval = setInterval(function() {
                var elapsed = Math.floor((Date.now() - startTime) / 1000);
                badge.innerHTML = '🔄 Updated ' + elapsed + ' seconds ago';
            }, 1000);
        """)
    except Exception as e:
        log(f"[TIMESTAMP] Error: {e}")


async def fetch_json_async_single(url, cookies, user_agent):
    """
    Async fetch single JSON file
    Used for parallel JSON fetching
    """
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                url,
                cookies=cookies,
                headers={
                    'User-Agent': user_agent,
                    'Accept': 'application/json',
                    'Referer': 'https://en.surebet.com/surebets',
                },
                timeout=aiohttp.ClientTimeout(total=5)
            ) as response:
                if response.status == 200:
                    return await response.json()
                else:
                    log(f"[JSON-PARALLEL] Status {response.status} for {url}")
                    return None
    except Exception as e:
        log(f"[JSON-PARALLEL] Error fetching {url}: {e}")
        return None


async def fetch_all_json_parallel_async(tab_info_list):
    """
    Fetch JSON for multiple tabs in parallel
    
    Args:
        tab_info_list: List of (driver, page_type) tuples
        
    Returns:
        List of (driver, page_type, json_data) tuples
    """
    import time
    start_time = time.time()
    
    # Create fetch tasks for all tabs
    tasks = []
    for driver, page_type in tab_info_list:
        # Get cookies and user agent for this tab
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        user_agent = driver.execute_script("return navigator.userAgent;")
        
        # JSON URL (same for all pages currently)
        url = "https://en.surebet.com/surebets?product=surebets&autoupdate=1&format=json"
        
        # Create async task
        task = fetch_json_async_single(url, cookies, user_agent)
        tasks.append((driver, page_type, task))
    
    # Execute all fetches in parallel
    results = []
    for driver, page_type, task in tasks:
        json_data = await task
        results.append((driver, page_type, json_data))
    
    elapsed = time.time() - start_time
    log(f"[JSON-PARALLEL] Fetched {len(results)} JSON files in {elapsed:.2f}s")
    
    return results


def inject_json_updates_parallel(tab_info_list):
    """
    Inject JSON updates to multiple tabs in parallel
    
    Args:
        tab_info_list: List of (driver, page_type) tuples
        
    Returns:
        Dict of {driver: count} with number of surebets per tab
    """
    if not tab_info_list:
        return {}
    
    # Check if aiohttp is available
    if not AIOHTTP_AVAILABLE:
        log("[JSON-PARALLEL] aiohttp not available, using sequential fallback")
        # Fallback to sequential
        results = {}
        for driver, page_type in tab_info_list:
            count = inject_json_updates_to_page(driver, page_type)
            results[driver] = count
        return results
    
    try:
        # Fetch all JSON in parallel
        json_results = asyncio.run(fetch_all_json_parallel_async(tab_info_list))
        
        # Inject HTML to each tab (sequential but fast)
        results = {}
        for driver, page_type, json_data in json_results:
            if json_data:
                count = inject_json_data_to_page(driver, page_type, json_data)
                results[driver] = count
            else:
                results[driver] = 0
        
        return results
        
    except Exception as e:
        log(f"[JSON-PARALLEL] Error: {e}")
        # Fallback to sequential
        results = {}
        for driver, page_type in tab_info_list:
            count = inject_json_updates_to_page(driver, page_type)
            results[driver] = count
        return results


def inject_json_data_to_page(driver, page_type, json_data):
    """
    Inject pre-fetched JSON data into page
    (Used by parallel fetching)
    """
    import time
    start_time = time.time()
    
    try:
        if not json_data:
            log(f"[JSON-INJECT] No JSON data for {page_type}")
            return 0
        
        # Step 1: Parse table array
        table = json_data.get('table', [])
        
        if not table:
            log(f"[JSON-INJECT] No table data in JSON for {page_type}")
            return 0
        
        # Step 2: Build HTML from all items
        t2 = time.time()
        all_tbody_html = ''.join([item.get('html', '') for item in table])
        build_time = (time.time() - t2) * 1000
        log(f"[PERF] HTML build: {build_time:.1f}ms")
        
        # Step 3: Inject into #table-container
        t3 = time.time()
        driver.execute_script("""
            var container = document.querySelector('#table-container');
            if (container) {
                container.innerHTML = arguments[0];
            } else {
                console.error('Container #table-container not found');
            }
        """, all_tbody_html)
        inject_time = (time.time() - t3) * 1000
        log(f"[PERF] Injection: {inject_time:.1f}ms")
        
        # Step 4: Show visual feedback
        t4 = time.time()
        if JSON_SHOW_UPDATE_TIME and page_type in ["GROUP", "NEXT"]:
            show_update_timestamp(driver)
        timestamp_time = (time.time() - t4) * 1000
        log(f"[PERF] Timestamp: {timestamp_time:.1f}ms")
        
        total_time = (time.time() - start_time) * 1000
        log(f"[PERF] TOTAL: {total_time:.1f}ms")
        log(f"[JSON-INJECT] {page_type} updated: {len(table)} surebets")
        return len(table)
        
    except Exception as e:
        log(f"[JSON-INJECT] Error for {page_type}: {e}")
        return 0


def inject_json_updates_to_page(driver, page_type="GROUP"):
    """
    Fetch JSON and inject HTML into page WITHOUT full refresh
    OPTIMIZED: Performance improvements applied
    """
    import time
    start_time = time.time()
    
    try:
        # Step 1: Fetch JSON (with timing)
        t1 = time.time()
        json_data = fetch_surebets_json_lightweight(driver)
        fetch_time = (time.time() - t1) * 1000
        log(f"[PERF] JSON fetch: {fetch_time:.1f}ms")
        
        if not json_data:
            log(f"[JSON-INJECT] Failed to fetch JSON for {page_type}")
            return 0
        
        # Step 2: Parse table array
        table = json_data.get('table', [])
        
        if not table:
            log(f"[JSON-INJECT] No table data in JSON for {page_type}")
            return 0
        
        # Step 3: Build HTML from all items (OPTIMIZED: list join instead of concatenation)
        t2 = time.time()
        all_tbody_html = ''.join([item.get('html', '') for item in table])
        build_time = (time.time() - t2) * 1000
        log(f"[PERF] HTML build: {build_time:.1f}ms")
        
        # Step 4: Inject into #table-container (OPTIMIZED: removed tooltip re-init)
        t3 = time.time()
        driver.execute_script("""
            var container = document.querySelector('#table-container');
            if (container) {
                // Replace all content (no tooltip re-init needed)
                container.innerHTML = arguments[0];
            } else {
                console.error('Container #table-container not found');
            }
        """, all_tbody_html)
        inject_time = (time.time() - t3) * 1000
        log(f"[PERF] Injection: {inject_time:.1f}ms")
        
        # Step 5: Show visual feedback (only on GROUP/NEXT, not MAIN)
        t4 = time.time()
        if JSON_SHOW_UPDATE_TIME and page_type in ["GROUP", "NEXT"]:
            show_update_timestamp(driver)
        timestamp_time = (time.time() - t4) * 1000
        log(f"[PERF] Timestamp: {timestamp_time:.1f}ms")
        
        total_time = (time.time() - start_time) * 1000
        log(f"[PERF] TOTAL: {total_time:.1f}ms")
        log(f"[JSON-INJECT] {page_type} updated: {len(table)} surebets")
        return len(table)
        
    except Exception as e:
        log(f"[JSON-INJECT] Error for {page_type}: {e}")
        return 0


def manage_auto_update(driver, page_type, info):
    """
    Manage auto-update for GROUP/NEXT pages
    Runs every JSON_UPDATE_INTERVAL seconds
    """
    if not ENABLE_JSON_AUTO_UPDATE:
        return False
    
    now = time.time()
    last_update = info.get('last_json_update', 0)
    
    # Check if time to update
    if now - last_update >= JSON_UPDATE_INTERVAL:
        try:
            # Fetch and inject
            count = inject_json_updates_to_page(driver, page_type)
            
            # Update timestamp
            info['last_json_update'] = now
            
            if count > 0:
                log(f"[JSON-UPDATE] {page_type} auto-updated: {count} surebets")
                return True
            
        except Exception as e:
            log(f"[JSON-UPDATE] Error for {page_type}: {e}")
    
    return False


# ============================================================================
# BOOKMAKER URL EXTRACTION (Avoid Rate Limits)
# ============================================================================

def build_url_from_link_obj(link_obj, base_url):
    """
    Build complete URL from link object, handling params/query/data.
    EXACT TRANSLATION of JavaScript buildUrlFromLinkObj function.
    
    Args:
        link_obj: Dictionary with 'url', 'params', 'query', 'data' keys
        base_url: Base URL for making relative URLs absolute
    
    Returns:
        Complete URL string with all parameters, or None if failed
    """
    from urllib.parse import urlparse, urljoin, urlencode, parse_qs
    
    raw_url = link_obj.get('url') if isinstance(link_obj, dict) else None
    if not raw_url:
        return None
    
    try:
        # Make absolute URL
        abs_url = urljoin(base_url, raw_url)
        parsed = urlparse(abs_url)
        
        # Start with existing query params (if any)
        params = {}
        if parsed.query:
            for k, v_list in parse_qs(parsed.query).items():
                params[k] = v_list[0] if v_list else ''
        
        # Helper function to add params (same logic as JavaScript)
        def add_params(obj, label):
            if not obj:
                return 0
            added = 0
            
            if isinstance(obj, list):
                # Handle array: [{name:"key", value:"val"}] or [["k","v"]]
                for it in obj:
                    if isinstance(it, dict) and 'name' in it and 'value' in it:
                        # Format: {name: "key", value: "val"}
                        params[str(it['name'])] = str(it['value'])
                        added += 1
                    elif isinstance(it, (list, tuple)) and len(it) >= 2:
                        # Format: ["key", "val"]
                        params[str(it[0])] = str(it[1])
                        added += 1
                return added
            
            if isinstance(obj, dict):
                # Handle object: {key: "value"}
                for k, v in obj.items():
                    if v is not None and v != '':
                        params[str(k)] = str(v)
                        added += 1
                return added
            
            return 0
        
        # Add params/query/data (same order as JavaScript)
        a = add_params(link_obj.get('params'), 'params')
        b = add_params(link_obj.get('query'), 'query')
        c = add_params(link_obj.get('data'), 'data')
        
        # Build final URL with all parameters
        query_string = urlencode(params, doseq=False)
        
        final_url = f"{parsed.scheme}://{parsed.netloc}{parsed.path}"
        if query_string:
            final_url += f"?{query_string}"
        
        # Preserve hash fragment (fixes vegas.hu URLs)
        if parsed.fragment:
            final_url += f"#{parsed.fragment}"
        
        return final_url
        
    except Exception as e:
        log(f"[URL-BUILD] Error building URL: {e}")
        return None


# ============================================================================
# ASYNC PARALLEL URL EXTRACTION
# ============================================================================

async def fetch_url_async(url, cookies, user_agent):
    """
    Async fetch of single URL with random delay.
    Extracts bookmaker URL from HTML response.
    
    Args:
        url: The nav URL to fetch
        cookies: Dictionary of cookies
        user_agent: User agent string
    
    Returns:
        Bookmaker URL string or None
    """
    import html as html_module
    
    # Random delay 0.17-0.33 seconds to prevent rate limiting
    delay = random.uniform(0.17, 0.33)
    await asyncio.sleep(delay)
    
    try:
        headers = {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Referer': 'https://en.surebet.com/'
        }
        
        # Async HTTP request
        timeout = aiohttp.ClientTimeout(total=3)
        async with aiohttp.ClientSession(cookies=cookies, timeout=timeout) as session:
            async with session.get(url, headers=headers, allow_redirects=False) as response:
                html = await response.text()
                base_url = str(response.url) or url
                
                # Parse HTML to extract bookmaker URL (synchronous parsing is OK)
                bookmaker_url = extract_url_from_html(html, base_url)
                
                return bookmaker_url
                
    except asyncio.TimeoutError:
        log(f"[URL-EXTRACT] ⏱️ Timeout: {url[:60]}...")
        return None
    except Exception as e:
        log(f"[URL-EXTRACT] ❌ Error: {type(e).__name__}: {str(e)[:100]}")
        return None


def extract_url_from_html(html, base_url):
    """
    Extract bookmaker URL from HTML using data-links attribute.
    Synchronous helper function for async fetch.
    
    Args:
        html: HTML content string
        base_url: Base URL for making relative URLs absolute
    
    Returns:
        Bookmaker URL string or None
    """
    import html as html_module
    import json
    from urllib.parse import urlparse
    
    def is_surebet_host(url_str):
        try:
            parsed = urlparse(url_str)
            return parsed.hostname and 'surebet.com' in parsed.hostname
        except:
            return False
    
    try:
        # Method 1: Try BeautifulSoup (if available)
        try:
            from bs4 import BeautifulSoup
            soup = BeautifulSoup(html, 'html.parser')
            nav_element = soup.find(id='navigation')
            
            if nav_element:
                data_links = nav_element.get('data-links')
                if data_links:
                    data_links_decoded = html_module.unescape(data_links)
                    links = json.loads(data_links_decoded)
                    
                    if isinstance(links, list):
                        for item in links:
                            link_obj = item.get('link', {}) if isinstance(item, dict) else {}
                            complete_url = build_url_from_link_obj(link_obj, base_url)
                            
                            if complete_url and not is_surebet_host(complete_url):
                                return complete_url
        except ImportError:
            pass  # BeautifulSoup not available, use regex fallback
        
        # Method 2: Regex fallback (works without BeautifulSoup)
        nav_pattern = r'<[^>]*id=["\']navigation["\'][^>]*data-links=(["\'])([^\1]*?)\1'
        match = re.search(nav_pattern, html, re.DOTALL)
        
        if match:
            data_links_raw = match.group(2)
            data_links_decoded = html_module.unescape(data_links_raw)
            
            try:
                links = json.loads(data_links_decoded)
                
                if isinstance(links, list):
                    for item in links:
                        link_obj = item.get('link', {}) if isinstance(item, dict) else {}
                        complete_url = build_url_from_link_obj(link_obj, base_url)
                        
                        if complete_url and not is_surebet_host(complete_url):
                            return complete_url
            except json.JSONDecodeError:
                pass
        
        return None
        
    except Exception as e:
        log(f"[URL-PARSE] Error: {e}")
        return None


async def extract_urls_parallel_async(tasks, driver):
    """
    Extract all URLs in parallel using asyncio + aiohttp.
    
    Args:
        tasks: List of task dictionaries with 'hrefs' key
        driver: Selenium driver (for cookies and user agent)
    
    Returns:
        List of (url1, url2) tuples for each task
    """
    cookies = {c['name']: c['value'] for c in driver.get_cookies()}
    user_agent = driver.execute_script("return navigator.userAgent;")
    
    # Create fetch tasks for all URLs
    fetch_tasks = []
    
    for task in tasks:
        h1, h2 = task.get('hrefs', (None, None))
        if h1 and h2:
            fetch_tasks.append(fetch_url_async(h1, cookies, user_agent))
            fetch_tasks.append(fetch_url_async(h2, cookies, user_agent))
        else:
            fetch_tasks.append(asyncio.sleep(0, result=None))
            fetch_tasks.append(asyncio.sleep(0, result=None))
    
    # Run all fetches in parallel
    log(f"[NAV-WORKER] 🚀 Starting parallel fetch of {len(fetch_tasks)} URLs...")
    start_time = time.time()
    
    results = await asyncio.gather(*fetch_tasks, return_exceptions=True)
    
    elapsed = time.time() - start_time
    log(f"[NAV-WORKER] ⚡ Parallel fetch completed in {elapsed:.2f}s")
    
    # Group results into pairs
    pairs = []
    for i in range(0, len(results), 2):
        f1 = results[i] if not isinstance(results[i], Exception) else None
        f2 = results[i+1] if not isinstance(results[i+1], Exception) else None
        pairs.append((f1, f2))
    
    return pairs


def extract_bookmaker_url_from_nav_link(nav_url, driver):
    """
    Extract bookmaker redirect URL from nav link WITHOUT opening it in browser.
    Uses the data-links attribute from #navigation element (95%+ reliable).
    This avoids rate limits by only fetching HTML, not opening pages.
    
    EXACT TRANSLATION of JavaScript resolveSurebetNavRedirect function.
    
    Args:
        nav_url: The /nav/surebet/ link to extract from
        driver: Selenium driver (for cookies)
    
    Returns:
        Bookmaker URL string, or None if extraction failed
    """
    import html as html_module
    import json
    from urllib.parse import urlparse
    
    try:
        # Get authentication from Selenium
        cookies = {c['name']: c['value'] for c in driver.get_cookies()}
        user_agent = driver.execute_script("return navigator.userAgent;")
        
        headers = {
            'User-Agent': user_agent,
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Accept-Language': 'en-US,en;q=0.9,hu;q=0.8',
            'Referer': driver.current_url,
            'Connection': 'keep-alive'
        }
        
        # Fetch HTML only (fast, no JavaScript execution)
        log(f"[URL-EXTRACT] Fetching: {nav_url[:80]}...")
        response = requests.get(
            nav_url,
            cookies=cookies,
            headers=headers,
            timeout=3,
            allow_redirects=True
        )
        
        if response.status_code != 200:
            log(f"[URL-EXTRACT] HTTP {response.status_code} for {nav_url}")
            return None
        
        html = response.text
        base_url = response.url or nav_url
        
        # Helper function to check if URL is surebet.com
        def is_surebet_host(url_str):
            try:
                parsed = urlparse(url_str)
                return parsed.hostname and 'surebet.com' in parsed.hostname
            except:
                return False
        
        # PRIMARY METHOD: Parse data-links attribute from #navigation element
        # Try with BeautifulSoup first (if available)
        try:
            from bs4 import BeautifulSoup
            
            soup = BeautifulSoup(html, 'html.parser')
            nav_element = soup.find(id='navigation')
            
            if nav_element:
                data_links_raw = nav_element.get('data-links', '')
                
                if data_links_raw:
                    # Decode HTML entities (like JavaScript decodeHtmlEntities)
                    data_links_decoded = html_module.unescape(data_links_raw)
                    
                    # Parse JSON
                    try:
                        links = json.loads(data_links_decoded)
                        
                        if isinstance(links, list):
                            # Build complete URLs and find first external one
                            for item in links:
                                if not isinstance(item, dict):
                                    continue
                                
                                link_obj = item.get('link', {})
                                built_url = build_url_from_link_obj(link_obj, base_url)
                                
                                if built_url and not is_surebet_host(built_url):
                                    log(f"[URL-EXTRACT] ✅ Found (data-links+BS4): {built_url[:80]}...")
                                    return built_url
                    
                    except json.JSONDecodeError as e:
                        log(f"[URL-EXTRACT] JSON parse error: {e}")
        
        except ImportError:
            pass  # Silently fall back to regex method
        except Exception as e:
            log(f"[URL-EXTRACT] BeautifulSoup method failed: {e}")
        
        # FALLBACK METHOD: Extract data-links with regex (works without BeautifulSoup)
        # Pattern to match: <... id="navigation" ... data-links="...">
        nav_pattern = r'<[^>]*id=["\']navigation["\'][^>]*data-links=(["\'])([^\1]*?)\1'
        match = re.search(nav_pattern, html, re.DOTALL)
        
        if match:
            try:
                # Get the data-links content
                data_links_raw = match.group(2)
                
                # Decode HTML entities
                data_links_decoded = html_module.unescape(data_links_raw)
                
                # Parse JSON
                links = json.loads(data_links_decoded)
                
                if isinstance(links, list):
                    # Build complete URLs and find first external one
                    for item in links:
                        if not isinstance(item, dict):
                            continue
                        
                        link_obj = item.get('link', {})
                        built_url = build_url_from_link_obj(link_obj, base_url)
                        
                        if built_url and not is_surebet_host(built_url):
                            log(f"[URL-EXTRACT] ✅ Found (data-links+regex): {built_url[:80]}...")
                            return built_url
            
            except (json.JSONDecodeError, AttributeError, KeyError) as e:
                log(f"[URL-EXTRACT] Regex fallback parsing failed: {e}")
        
        # If we get here, no external URL was found
        log(f"[URL-EXTRACT] ❌ No external URL found in {nav_url[:60]}...")
        return None
        
    except requests.Timeout:
        log(f"[URL-EXTRACT] ⏱️ Timeout for {nav_url[:60]}...")
        return None
    except Exception as e:
        log(f"[URL-EXTRACT] ❌ Error: {e}")
        return None


def extract_all_bookmaker_urls_from_page(driver):
    """
    Extract all bookmaker URLs from current page WITHOUT opening any links.
    This replaces the old method of opening each tbody link.
    
    Args:
        driver: Selenium driver
    
    Returns:
        List of dicts with 'nav_url' and 'bookmaker_url' keys
    """
    log("[URL-EXTRACT] Starting URL extraction from page...")
    
    try:
        # Get all /nav/surebet/ links from page
        nav_links = driver.execute_script("""
            return Array.from(document.querySelectorAll('a[href*="/nav/surebet/"]'))
                        .map(a => a.href);
        """)
        
        if not nav_links:
            log("[URL-EXTRACT] No /nav/surebet/ links found on page")
            return []
        
        log(f"[URL-EXTRACT] Found {len(nav_links)} nav links to process")
        
        results = []
        processed = 0
        
        for nav_url in nav_links:
            # Extract bookmaker URL
            bookmaker_url = extract_bookmaker_url_from_nav_link(nav_url, driver)
            
            if bookmaker_url:
                # Extract domain for logging
                from urllib.parse import urlparse
                try:
                    domain = urlparse(bookmaker_url).netloc
                except:
                    domain = 'unknown'
                
                results.append({
                    'nav_url': nav_url,
                    'bookmaker_url': bookmaker_url,
                    'domain': domain
                })
                
                log(f"[URL-EXTRACT] ✅ {domain}: {bookmaker_url[:60]}...")
            
            processed += 1
            
            # Small delay to avoid overwhelming server
            if processed < len(nav_links):
                time.sleep(0.1)
        
        log(f"[URL-EXTRACT] Successfully extracted {len(results)}/{len(nav_links)} bookmaker URLs")
        return results
        
    except Exception as e:
        log(f"[URL-EXTRACT] Error during extraction: {e}")
        return []


def maybe_refresh_group_tab(url: str, info: dict) -> bool:
    now = time.time()
    if now - info.get("created_at", now) < GROUP_REFRESH_SKIP_ON_NEW_SEC:
        info["next_refresh"] = now + _rand_group_refresh_interval()
        return False
    if now < info.get("next_refresh", 0):
        return False

    handle = info.get("handle")
    if handle and handle not in driver.window_handles:
        return False

    # 🎯 CONTENT HASH CHECKING - Check if content actually changed before refreshing
    if ENABLE_CONTENT_HASH_CHECKING:
        _log_hash_check(f"📋 Checking GROUP page content: {url[:60]}...", verbose_only=False)
        
        # Switch to the group tab
        try:
            driver.switch_to.window(handle)
        except Exception as e:
            warn(f"[HASH] Could not switch to group tab: {e}")
            # Continue with refresh anyway
        
        # Check if content changed
        last_signature = info.get("content_signature")
        last_hash = info.get("content_hash")
        
        changed, new_signature, new_hash, reason = check_content_changed(url, last_signature, last_hash)
        
        # Update stored hash/signature
        info["content_signature"] = new_signature
        info["content_hash"] = new_hash
        
        if not changed:
            # Content hasn't changed, skip refresh!
            _log_hash_check(f"⏭️ GROUP refresh SKIPPED (reason: {reason})", verbose_only=False)
            info["next_refresh"] = now + _rand_group_refresh_interval()
            
            # 🎯 CONDITIONAL SCRAPING - Clear scraping flag when no refresh
            if ENABLE_CONDITIONAL_SCRAPING:
                info["needs_scraping"] = False
                if SCRAPING_SKIP_LOGGING:
                    _log_hash_check(f"⏭️ Tab will be skipped during scraping (no fresh data)", verbose_only=True)
            
            return False
        else:
            _log_hash_check(f"🔄 GROUP will refresh (reason: {reason})", verbose_only=False)

    ok = False
    try:
        result = _safe_execute_async_script(r"""
            var callback = arguments[0];
            try {
                var sc = document.querySelector('div.table-container.product-table-container');
                if (!sc) { callback({ok:false, err:'container-not-found'}); return; }
                fetch(window.location.href, {cache:'no-store'})
                  .then(r => { if (!r.ok) throw new Error('http-'+r.status); return r.text(); })
                  .then(html => {
                      var parser = new DOMParser();
                      var doc = parser.parseFromString(html, 'text/html');
                      var newSc = doc.querySelector('div.table-container.product-table-container');
                      if (!newSc) { callback({ok:false, err:'new-container-not-found'}); return; }
                      var y = window.scrollY;
                      sc.innerHTML = newSc.innerHTML;
                      window.scrollTo(0, y);
                      callback({ok:true});
                  })
                  .catch(e => callback({ok:false, err:String(e)}));
            } catch(e) { callback({ok:false, err:String(e)}); }
        """)
        ok = bool(result and result.get("ok"))
    except Exception as e:
        warn(f"⚠️ Group részleges refresh hiba: {e}")
        ok = False

    if not ok:
        try:
            refresh_page_safe()
            _inject_disable_animations()
            WebDriverWait(driver, 8).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
            )
            ok = True
        except Exception as e:
            warn(f"⚠️ Group teljes reload hiba: {e}")
            ok = False

    # Update state tracking after refresh attempt
    info["last_refresh"] = now
    info["refresh_count"] = info.get("refresh_count", 0) + 1
    
    if ok:
        # Check if new elements were found since last refresh
        found_new_element = info.get("has_new_element", False)
        
        # Update no_update_count based on whether new elements were found
        if found_new_element:
            info["no_update_count"] = 0
        else:
            info["no_update_count"] = info.get("no_update_count", 0) + 1
        
        # Update state machine
        refresh_count = info["refresh_count"]
        no_update_count = info["no_update_count"]
        
        if refresh_count == 1:
            info["state"] = "SECOND_REFRESH"
        elif no_update_count >= 7:
            info["state"] = "STALE"
        elif found_new_element:
            info["state"] = "BOOST"
        else:
            info["state"] = "NORMAL"
        
        # Calculate next refresh interval based on state
        info["next_refresh"] = now + calculate_group_refresh_interval(info, found_new_element)
        
        # Reset the new element flag
        info["has_new_element"] = False
        
        info["needs_scan"] = True
        
        # 🎯 CONDITIONAL SCRAPING - Mark tab for scraping after refresh
        if ENABLE_CONDITIONAL_SCRAPING:
            info["needs_scraping"] = True
            _log_hash_check(f"✅ GROUP refreshed, marked for scraping", verbose_only=True)
        
        # 🎯 Update hash after successful refresh
        if ENABLE_CONTENT_HASH_CHECKING:
            try:
                new_signature = get_page_signature()
                new_hash = get_content_hash()
                info["content_signature"] = new_signature
                info["content_hash"] = new_hash
                _log_hash_check(f"✅ GROUP refreshed, new hash stored", verbose_only=True)
            except Exception as e:
                warn(f"[HASH] Could not update hash after refresh: {e}")
    else:
        # Failed refresh - use simple interval
        info["next_refresh"] = now + _rand_group_refresh_interval()
    
    return ok

# ---------- NEXT helpers ----------
def _rand_next_refresh_interval():
    return random.uniform(NEXT_REFRESH_MIN, NEXT_REFRESH_MAX)

def find_next_page_link():
    try:
        a = driver.find_element(By.CSS_SELECTOR, "a.next_page")
        href = a.get_attribute("href")
        if not href:
            return None
        cur = urlparse(driver.current_url)
        base = f"{cur.scheme}://{cur.netloc}"
        return urljoin(base, href)
    except Exception:
        return None

def open_next_tab_if_needed(next_url):
    if next_url in next_tabs:
        if LOG_NEXT_ALREADY_OPEN_VERBOSE:
            log(f"ℹ️ NEXT már nyitva: {next_url}")
        return

    try:
        original = driver.current_window_handle
    except Exception:
        original = None

    try:
        driver.switch_to.new_window('tab')
        driver.get(next_url)
        _inject_disable_animations()
        handle = driver.current_window_handle

        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        tb_count = len(driver.find_elements(By.CSS_SELECTOR, NEXT_SELECTOR))
        if tb_count <= NEXT_EMPTY_CLOSE_TB_THRESHOLD:
            try:
                driver.close()
            except Exception:
                pass
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
            log(f"🔒 NEXT zárva üres miatt: {next_url}")
            return

        now = time.time()
        next_tabs[next_url] = {
            "handle": handle,
            "active_ids": set(),
            "created_at": now,
            "last_refresh": now,
            "next_refresh": now + _rand_next_refresh_interval(),
            "needs_scan": True,
        }

        if original and original in driver.window_handles:
            driver.switch_to.window(original)
        log(f"🆕 NEXT tab nyitva: {next_url}")
        return
    except Exception as e:
        warn(f"⚠️ NEXT nyitás hiba: {e}")
        try:
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
        except Exception:
            pass
            
            
def _scan_current_page_ids_and_groups():
    """
    Az AKTUÁLIS oldalon:
      - összes tbody.surebet_record → ID-k
      - minden tbody-ből group-link (ha van)
    Visszatérés: (ids_set, group_urls_set)
    """
    ids = set()
    group_urls = set()
    now_ts = time.time()

    try:
        tbodys = driver.find_elements(By.CSS_SELECTOR, "tbody.surebet_record")
    except Exception:
        return ids, group_urls

    for tbody in tbodys:
        try:
            tid = tbody.get_attribute("data-id") or tbody.get_attribute("dataid")
        except Exception:
            tid = None
        if not tid:
            continue

        ids.add(tid)
        # induláskori last_seen/id_source is legyen rendben
        last_seen_ts[tid] = now_ts
        id_source[tid] = "initial_scan"

        try:
            g = find_group_link_in_tbody(tbody)
            if g:
                group_urls.add(g)
        except Exception:
            pass

    return ids, group_urls


def maybe_refresh_next_tab(url: str, info: dict) -> bool:
    now = time.time()
    if now < info.get("next_refresh", 0):
        return False

    handle = info.get("handle")
    
    # 🎯 CONTENT HASH CHECKING - Check if content actually changed before refreshing
    if ENABLE_CONTENT_HASH_CHECKING and handle:
        _log_hash_check(f"📋 Checking NEXT page content: {url[:60]}...", verbose_only=False)
        
        # Switch to the next tab
        try:
            if handle in driver.window_handles:
                driver.switch_to.window(handle)
            else:
                warn(f"[HASH] Next tab handle not in window_handles, will refresh anyway")
                # Continue with refresh
        except Exception as e:
            warn(f"[HASH] Could not switch to next tab: {e}")
            # Continue with refresh anyway
        
        # Check if content changed
        last_signature = info.get("content_signature")
        last_hash = info.get("content_hash")
        
        changed, new_signature, new_hash, reason = check_content_changed(url, last_signature, last_hash)
        
        # Update stored hash/signature
        info["content_signature"] = new_signature
        info["content_hash"] = new_hash
        
        if not changed:
            # Content hasn't changed, skip refresh!
            _log_hash_check(f"⏭️ NEXT refresh SKIPPED (reason: {reason})", verbose_only=False)
            info["next_refresh"] = now + _rand_next_refresh_interval()
            
            # 🎯 CONDITIONAL SCRAPING - Clear scraping flag when no refresh
            if ENABLE_CONDITIONAL_SCRAPING:
                info["needs_scraping"] = False
                if SCRAPING_SKIP_LOGGING:
                    _log_hash_check(f"⏭️ Tab will be skipped during scraping (no fresh data)", verbose_only=True)
            
            return False
        else:
            _log_hash_check(f"🔄 NEXT will refresh (reason: {reason})", verbose_only=False)

    ok = False
    try:
        result = _safe_execute_async_script(r"""
            var callback = arguments[0];
            try {
                var sc = document.querySelector('div.table-container.product-table-container');
                if (!sc) { callback({ok:false, err:'container-not-found'}); return; }
                fetch(window.location.href, {cache:'no-store'})
                  .then(r => { if (!r.ok) throw new Error('http-'+r.status); return r.text(); })
                  .then(html => {
                      var parser = new DOMParser();
                      var doc = parser.parseFromString(html, 'text/html');
                      var newSc = doc.querySelector('div.table-container.product-table-container');
                      if (!newSc) { callback({ok:false, err:'new-container-not-found'}); return; }
                      var y = window.scrollY;
                      sc.innerHTML = newSc.innerHTML;
                      window.scrollTo(0, y);
                      callback({ok:true});
                  })
                  .catch(e => callback({ok:false, err:String(e)}));
            } catch(e) { callback({ok:false, err:String(e)}); }
        """)
        ok = bool(result and result.get("ok"))
    except Exception as e:
        warn(f"⚠️ NEXT részleges refresh hiba: {e}")
        ok = False

    info["last_refresh"] = now
    info["next_refresh"] = now + _rand_next_refresh_interval()
    if ok:
        info["needs_scan"] = True
        
        # 🎯 CONDITIONAL SCRAPING - Mark tab for scraping after refresh
        if ENABLE_CONDITIONAL_SCRAPING:
            info["needs_scraping"] = True
            _log_hash_check(f"✅ NEXT refreshed, marked for scraping", verbose_only=True)
        
        # 🎯 Update hash after successful refresh
        if ENABLE_CONTENT_HASH_CHECKING:
            try:
                new_signature = get_page_signature()
                new_hash = get_content_hash()
                info["content_signature"] = new_signature
                info["content_hash"] = new_hash
                _log_hash_check(f"✅ NEXT refreshed, new hash stored", verbose_only=True)
            except Exception as e:
                warn(f"[HASH] Could not update hash after refresh: {e}")
    
    return ok
    
    
# === ÚJ: háttér GROUP/NEXT tab-megnyitó + időszakos TAB cleanup ===

GROUP_NEXT_OPEN_QUEUE = Queue(maxsize=2000)
group_open_pending = set()
next_open_pending = set()


def validate_and_switch_tab(handle, tabs_dict, url, tab_type):
    """
    Helper function to validate window handle and switch to tab.
    Returns True if successful, False otherwise.
    Cleans up tabs_dict on failure.
    """
    if handle not in driver.window_handles:
        tabs_dict.pop(url, None)
        log(f"⚠️ {tab_type} tab bezárva, eltávolítva: {handle[:8] if handle else 'None'}")
        return False
    try:
        driver.switch_to.window(handle)
        return True
    except Exception as e:
        tabs_dict.pop(url, None)
        log(f"⚠️ {tab_type} tab hiba, eltávolítva: {handle[:8] if handle else 'None'} - {str(e)[:50]}")
        return False


def _open_group_tab_sync(group_url: str):
    """
    Régi open_group_tab_if_needed logika, de külön függvényben.
    Ezt a háttér worker hívja, a fő ciklus csak queue-ba teszi a kérést.
    """
    now_ts = time.time()
    if group_url in group_tabs:
        if LOG_GROUP_ALREADY_OPEN_VERBOSE:
            log(f"ℹ️ Group már nyitva (sync): {group_url}")
        return
    if is_group_blocked(group_url, now_ts):
        log(f"⏳ Group URL tiltva (sync): {group_url}")
        return

    try:
        original = driver.current_window_handle
    except Exception:
        original = None
    
    # Extra safety: check if original handle still exists
    if original and original not in driver.window_handles:
        original = None

    try:
        driver.switch_to.new_window('tab')
        driver.get(group_url)
        _inject_disable_animations()
        handle = driver.current_window_handle
        handle_birth[handle] = time.time()

        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        tb_count = len(driver.find_elements(By.CSS_SELECTOR, GROUP_SELECTOR))
        if tb_count <= GROUP_EMPTY_CLOSE_TB_THRESHOLD:
            try:
                driver.close()
            except Exception:
                pass
            try:
                if original and original in driver.window_handles:
                    driver.switch_to.window(original)
            except Exception:
                pass
            block_group_url(group_url, GROUP_REOPEN_BACKOFF_SEC, "empty-at-open")
            return

        now = time.time()
        group_tabs[group_url] = {
            "handle": handle,
            "active_ids": set(),
            "created_at": now,
            "last_refresh": now,
            "next_refresh": now + _rand_group_refresh_interval(),
            "needs_scan": True,
        }
        try:
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
        except Exception:
            pass
        log(f"🆕 Group tab nyitva (sync): {group_url}")
        return
    except Exception as e:
        warn(f"⚠️ Group nyitás hiba (sync): {e}")
        block_group_url(group_url, GROUP_ERR_BACKOFF_SEC, "open-fail")
        try:
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
        except Exception:
            pass


def open_group_tab_if_needed(group_url: str):
    """
    ASZINKRON GROUP TAB NYITÁS:
    - itt már NEM hívunk driver.get-et
    - csak betesszük a kérést a queue-ba
    - a háttér worker (_open_group_tab_sync) intézi a lassú munkát
    """
    now_ts = time.time()
    if group_url in group_tabs or group_url in group_open_pending:
        if LOG_GROUP_ALREADY_OPEN_VERBOSE:
            log(f"ℹ️ Group már nyitva vagy épp nyílik: {group_url}")
        return
    if is_group_blocked(group_url, now_ts):
        log(f"⏳ Group URL tiltva (async wrapper): {group_url}")
        return

    group_open_pending.add(group_url)
    try:
        GROUP_NEXT_OPEN_QUEUE.put_nowait({"type": "group", "url": group_url})
    except Exception:
        group_open_pending.discard(group_url)
        warn("⚠️ GROUP_NEXT_OPEN_QUEUE tele, group nyitás kihagyva")


def _open_next_tab_sync(next_url: str):
    """
    Régi open_next_tab_if_needed logika, de külön függvényben.
    Háttér worker használja.
    """
    if next_url in next_tabs:
        if LOG_NEXT_ALREADY_OPEN_VERBOSE:
            log(f"ℹ️ NEXT már nyitva (sync): {next_url}")
        return

    try:
        original = driver.current_window_handle
    except Exception:
        original = None
    
    # Extra safety: check if original handle still exists
    if original and original not in driver.window_handles:
        original = None

    try:
        driver.switch_to.new_window('tab')
        driver.get(next_url)
        _inject_disable_animations()
        handle = driver.current_window_handle
        handle_birth[handle] = time.time()

        WebDriverWait(driver, 8).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        tb_count = len(driver.find_elements(By.CSS_SELECTOR, NEXT_SELECTOR))
        if tb_count <= NEXT_EMPTY_CLOSE_TB_THRESHOLD:
            try:
                driver.close()
            except Exception:
                pass
            try:
                if original and original in driver.window_handles:
                    driver.switch_to.window(original)
            except Exception:
                pass
            log(f"🔒 NEXT zárva üres miatt (sync): {next_url}")
            return

        now = time.time()
        next_tabs[next_url] = {
            "handle": handle,
            "active_ids": set(),
            "created_at": now,
            "last_refresh": now,
            "next_refresh": now + _rand_next_refresh_interval(),
            "needs_scan": True,
        }

        try:
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
        except Exception:
            pass
        log(f"🆕 NEXT tab nyitva (sync): {next_url}")
        return
    except Exception as e:
        warn(f"⚠️ NEXT nyitás hiba (sync): {e}")
        try:
            if original and original in driver.window_handles:
                driver.switch_to.window(original)
        except Exception:
            pass


def open_next_tab_if_needed(next_url: str):
    """
    ASZINKRON NEXT TAB NYITÁS:
    - nem blokkoljuk a fő while ciklust
    - csak sorba tesszük a nyitási kérést
    """
    if next_url in next_tabs or next_url in next_open_pending:
        if LOG_NEXT_ALREADY_OPEN_VERBOSE:
            log(f"ℹ️ NEXT már nyitva vagy épp nyílik: {next_url}")
        return

    next_open_pending.add(next_url)
    try:
        GROUP_NEXT_OPEN_QUEUE.put_nowait({"type": "next", "url": next_url})
    except Exception:
        next_open_pending.discard(next_url)
        warn("⚠️ GROUP_NEXT_OPEN_QUEUE tele, NEXT nyitás kihagyva")


def group_next_opener_worker():
    """
    Háttér worker:
    - GROUP_NEXT_OPEN_QUEUE-ből veszi ki a 'group' / 'next' nyitási feladatokat
    """
    global MAIN_HANDLE, DRIVER_DEAD
    while True:
        if DRIVER_DEAD:
            warn("💀 GROUP/NEXT opener worker leáll – DRIVER_DEAD=True.")
            break

        try:
            task = GROUP_NEXT_OPEN_QUEUE.get(timeout=1.0)

        except Empty:
            continue

        if not isinstance(task, dict):
            GROUP_NEXT_OPEN_QUEUE.task_done()
            continue

        ttype = task.get("type")
        url = task.get("url")
        if not url:
            GROUP_NEXT_OPEN_QUEUE.task_done()
            continue

        try:
            if ttype == "group":
                _open_group_tab_sync(url)
            elif ttype == "next":
                _open_next_tab_sync(url)
        except Exception as e:
            warn(f"[GROUP/NEXT-OPENER] Hiba ({ttype}): {e}")
        finally:
            if ttype == "group":
                group_open_pending.discard(url)
            elif ttype == "next":
                next_open_pending.discard(url)
            GROUP_NEXT_OPEN_QUEUE.task_done()


def cleanup_stray_tabs():
    """
    Időszakos TAB takarítás:

    - Összes window handle-t lekérdezzük
    - MAIN_HANDLE, group_tabs, next_tabs handle-jei VÉDETTEK
    - Minden más:
        - ha külső (valid_external) VAGY surebet NAV (is_nav_url),
        - és TAB_CLEANUP_MIN_AGE-nél régebbi,
      akkor bezárjuk.
    """
    global MAIN_HANDLE

    try:
        handles = list(driver.window_handles)
    except Exception:
        return

    if not handles:
        return

    protected = set()
    try:
        if MAIN_HANDLE and MAIN_HANDLE in handles:
            protected.add(MAIN_HANDLE)
    except Exception:
        pass

    # group tabok védése + halottak kisöprése a dict-ből
    for url, info in list(group_tabs.items()):
        h = info.get("handle")
        if not h or h not in handles:
            group_tabs.pop(url, None)
            continue
        protected.add(h)

    # next tabok védése + halottak kisöprése
    for url, info in list(next_tabs.items()):
        h = info.get("handle")
        if not h or h not in handles:
            next_tabs.pop(url, None)
            continue
        protected.add(h)

    now = time.time()
    closed = 0

    for h in handles:
        if h in protected:
            continue

        birth = handle_birth.get(h)
        age = (now - birth) if birth is not None else (TAB_CLEANUP_MIN_AGE + 1)

        if age < TAB_CLEANUP_MIN_AGE:
            # frissen nyílt ismeretlen tab – még nem nyúlunk hozzá
            continue

        try:
            driver.switch_to.window(h)
            try:
                cur = driver.current_url or ""
            except Exception:
                cur = ""
        except Exception:
            continue

        # Csak külső vagy NAV tabokat csukjunk
        try:
            if valid_external(cur) or is_nav_url(cur):
                try:
                    driver.close()
                    closed += 1
                    handle_birth.pop(h, None)
                except Exception:
                    pass
        except Exception:
            continue

    try:
        if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
            driver.switch_to.window(MAIN_HANDLE)
        elif driver.window_handles:
            driver.switch_to.window(driver.window_handles[0])
    except Exception:
        pass

    if closed:
        log(f"🧹 TAB cleanup: {closed} stray tab bezárva.")


def tab_cleanup_worker():
    """
    Háttér worker – X másodpercenként lefut a cleanup_stray_tabs().
    """
    global DRIVER_DEAD
    while True:
        if DRIVER_DEAD:
            warn("💀 TAB cleanup worker leáll – DRIVER_DEAD=True.")
            break

        try:
            time.sleep(TAB_CLEANUP_INTERVAL)
            cleanup_stray_tabs()

        except Exception as e:
            warn(f"[TAB-CLEANUP] Hiba: {e}")
            time.sleep(5)


# ---------- PLAY/PAUSE → SHIFT+P AUTUPDATE KEZELÉS ----------
# --- Shift+P autoupdate detektálás (ÚJ) ---
SHIFT_P_MAX_TRIES_FIRST_MIN = 6
AUTUPDATE_BANNER_TEXT = "Auto updates — Shift+P to pause them"
LOGIN_TS = None
_autoupdate_attempts = 0

def _autoupdate_banner_present():
    """
    True/False/None — ellenőrzi, hogy látszik-e a "Auto updates — Shift+P to pause them" szöveg.
    A hosszú kötőjeleket normalizáljuk, hogy a vizsgálat stabil legyen.
    """
    try:
        return bool(_safe_execute_script(r"""
            try {
              var target = (arguments[0] || "").toLowerCase();
              var txt = (document.body ? document.body.innerText : (document.documentElement.innerText || "")) || "";
              txt = txt.toLowerCase();
              txt = txt.replace(/\u2014|\u2013/g, '-');   // hosszú kötőjelek -> '-'
              target = target.replace(/\u2014|\u2013/g, '-');
              return txt.indexOf(target) !== -1;
            } catch(e){ return null; }
        """, AUTUPDATE_BANNER_TEXT))
    except Exception:
        return None




def _dismiss_cookie_like_overlays():
    try:
        _safe_execute_script(r"""
        (function(){
          var cands = [
            '#onetrust-banner-sdk', '#CybotCookiebotDialog', '.cc-window',
            '.cookie', '.cookies', '[data-cookie]', '[aria-label*="cookie" i]'
          ];
          cands.forEach(function(sel){
            var el = document.querySelector(sel);
            if (!el) return;
            var st = window.getComputedStyle(el);
            if (st && st.position === 'fixed') {
              el.style.display='none';
              el.style.visibility='hidden';
              el.style.pointerEvents='none';
            }
          });
        })();
        """)
    except Exception:
        pass

def _get_autoupdate_state():
    try:
        txt = _safe_execute_script(r"""
        var w = document.querySelector('div.paginate-and.mb-3');
        return w ? (w.textContent || '').toLowerCase() : '';
        """) or ""
        if not txt:
            return None
        if "auto updates" in txt:
            if "pause them" in txt:
                return "running"
            if "start them" in txt:
                return "stopped"
        return None
    except Exception:
        return None

def _send_shift_p():
    try:
        _safe_execute_script("window.focus(); try{document.activeElement.blur();}catch(e){}")
    except Exception:
        pass
    try:
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
            "type": "keyDown",
            "key": "P",
            "code": "KeyP",
            "windowsVirtualKeyCode": 80,
            "nativeVirtualKeyCode": 80,
            "modifiers": 8
        })
        driver.execute_cdp_cmd("Input.dispatchKeyEvent", {
            "type": "keyUp",
            "key": "P",
            "code": "KeyP",
            "windowsVirtualKeyCode": 80,
            "nativeVirtualKeyCode": 80,
            "modifiers": 8
        })
        return True
    except Exception:
        pass
    try:
        actions = ActionChains(driver)
        actions.key_down(Keys.SHIFT).send_keys('p').key_up(Keys.SHIFT).perform()
        return True
    except Exception:
        pass
    try:
        body = driver.find_element(By.TAG_NAME, "body")
        body.send_keys(Keys.SHIFT, 'p')
        return True
    except Exception:
        return False

MAIN_HANDLE = None
main_refresh_enabled = False
main_last_refresh = 0.0
main_next_refresh = 0.0

last_keepalive_ping_ts = 0.0

paginate_refresh_enabled = False
paginate_last_refresh = 0.0
paginate_next_refresh = 0.0
has_any_next_tab_opened_ever = False

def _rand_main_refresh_interval():
    return random.uniform(MAIN_REFRESH_MIN, MAIN_REFRESH_MAX)

def _rand_paginate_refresh_interval():
    return random.uniform(MAIN_PAGINATE_REFRESH_MIN, MAIN_PAGINATE_REFRESH_MAX)

def _wait_main_container(timeout=8):
    WebDriverWait(driver, timeout).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
    )

def ensure_main_autoupdate():
    """
    Login utáni első 60 mp: Shift+P max 6×, amíg nem látszik a
    '(Auto updates — Shift+P to pause them)' szöveg.
    Később: ha eltűnik, Shift+P max 3×. Ha így sem látszik, timed-refresh fallback.
    """
    global main_refresh_enabled, main_last_refresh, main_next_refresh, _autoupdate_attempts, LOGIN_TS

    present = _autoupdate_banner_present()

    if present:
        main_refresh_enabled = False
        main_next_refresh = 0.0
        _autoupdate_attempts = 0
        return

    first_minute = (time.time() - (LOGIN_TS or 0)) <= 60
    max_tries = SHIFT_P_MAX_TRIES_FIRST_MIN if first_minute else 3

    tries = 0
    while present is False and _autoupdate_attempts < max_tries and tries < max_tries:
        if _send_shift_p():
            time.sleep(0.35)
        tries += 1
        _autoupdate_attempts += 1
        present = _autoupdate_banner_present()

    if present:
        main_refresh_enabled = False
        main_next_refresh = 0.0
        _autoupdate_attempts = 0
    else:
        main_refresh_enabled = True
        main_last_refresh = time.time()
        main_next_refresh = main_last_refresh + _rand_main_refresh_interval()

def maybe_refresh_main_page():
    global main_refresh_enabled, main_last_refresh, main_next_refresh
    if not main_refresh_enabled:
        return
    now = time.time()
    if now < main_next_refresh:
        return
    try:
        current = driver.current_window_handle
        if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
            driver.switch_to.window(MAIN_HANDLE)

        refresh_page_safe()
        _inject_disable_animations()
        _wait_main_container(timeout=10)
        main_last_refresh = now
        main_next_refresh = now + _rand_main_refresh_interval()
        ensure_main_autoupdate()
    except Exception as e:
        warn(f"⚠️ Főoldal reload hiba: {e}")
        main_last_refresh = now
        main_next_refresh = now + _rand_main_refresh_interval()
    finally:
        try:
            if current and current in driver.window_handles:
                driver.switch_to.window(current)
        except Exception:
            pass

def maybe_refresh_main_paginate_and_try_open_next(len_tbodys_main: int):
    global paginate_refresh_enabled, paginate_last_refresh, paginate_next_refresh, has_any_next_tab_opened_ever

    try:
        if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
            driver.switch_to.window(MAIN_HANDLE)
    except Exception:
        return

    next_link = find_next_page_link()
    if next_link:
        open_next_tab_if_needed(next_link)
        has_any_next_tab_opened_ever = True
        paginate_refresh_enabled = False
        return

    if len_tbodys_main == 49:
        if not has_any_next_tab_opened_ever:
            if not paginate_refresh_enabled:
                paginate_refresh_enabled = True
                paginate_last_refresh = time.time()
                paginate_next_refresh = paginate_last_refresh + _rand_paginate_refresh_interval()
        else:
            if not next_tabs:
                if not paginate_refresh_enabled:
                    paginate_refresh_enabled = True
                    paginate_last_refresh = time.time()
                    paginate_next_refresh = paginate_last_refresh + _rand_paginate_refresh_interval()
    else:
        paginate_refresh_enabled = False

    if paginate_refresh_enabled and time.time() >= paginate_next_refresh:
        try:
            _safe_execute_async_script(r"""
                var callback = arguments[0];
                try {
                    var wrap = document.querySelector('div.paginate-and.mb-3');
                    if (!wrap) { callback({ok:false,err:'paginate-wrapper-not-found'}); return; }
                    fetch(window.location.href, {cache:'no-store'})
                      .then(r => { if (!r.ok) throw new Error('http-'+r.status); return r.text(); })
                      .then(html => {
                          var parser = new DOMParser();
                          var doc = parser.parseFromString(html, 'text/html');
                          var newWrap = doc.querySelector('div.paginate-and.mb-3');
                          if (!newWrap) { callback({ok:false,err:'new-wrapper-not-found'}); return; }
                          wrap.innerHTML = newWrap.innerHTML;
                          callback({ok:true});
                      })
                      .catch(e => callback({ok:false,err:String(e)}));
                } catch(e) { callback({ok:false,err:String(e)}); }
            """)
        except Exception:
            pass
        paginate_last_refresh = time.time()
        paginate_next_refresh = paginate_last_refresh + _rand_paginate_refresh_interval()

        try:
            link2 = find_next_page_link()
            if link2:
                open_next_tab_if_needed(link2)
                has_any_next_tab_opened_ever = True
                paginate_refresh_enabled = False
        except Exception:
            pass

# ---------- LOGIN ----------
def _submit_login_form_robust(timeout_after=12):
    _dismiss_cookie_like_overlays()

    BTN_SEL = "#sign-in-form-submit-button, input[type='submit'][name='commit']"
    PW_SEL  = "input[autocomplete='password']"

    try:
        pw = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.CSS_SELECTOR, PW_SEL)))
        pw.send_keys(Keys.ENTER)
        WebDriverWait(driver, timeout_after).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        return True
    except Exception:
        pass

    try:
        btn = WebDriverWait(driver, 6).until(EC.presence_of_element_located((By.CSS_SELECTOR, BTN_SEL)))
        _safe_execute_script("arguments[0].scrollIntoView({block:'center', inline:'center'});", btn)
        _safe_execute_script("arguments[0].click();", btn)
        WebDriverWait(driver, timeout_after).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        return True
    except Exception:
        pass

    try:
        ok = _safe_execute_script(r"""
        (function(){
          var btn = document.querySelector(arguments[0]);
          if(!btn) return false;
          var f = btn.form || btn.closest('form');
          if(!f) return false;
          if (typeof f.requestSubmit === 'function') { f.requestSubmit(btn); }
          else { f.submit(); }
          return true;
        })();
        """, BTN_SEL)
        if ok:
            WebDriverWait(driver, timeout_after).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
            )
            return True
    except Exception:
        pass

    return False

def login():
    global LOGIN_TS, _autoupdate_attempts
    try:
        driver.get(LOGIN_URL)
        _inject_disable_animations()
        time.sleep(0.8)

        # 1) Gyors check: lehet, hogy a login URL már egyből a fő oldalt adja vissza
        try:
            WebDriverWait(driver, 5).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
            )
            log("ℹ️ Már be vagy jelentkezve (table container már látszik), login kihagyva.")
            LOGIN_TS = time.time()
            _autoupdate_attempts = 0
            return
        except Exception:
            pass

        # 2) "You are already signed in." üzenet detektálása
        try:
            body_txt = _safe_execute_script(
                "return ((document.body && document.body.innerText) || "
                "(document.documentElement && document.documentElement.innerText) || '').toLowerCase();"
            ) or ""
        except Exception:
            body_txt = ""

        if "you are already signed in" in body_txt:
            log("ℹ️ 'You are already signed in.' – login lépés skip, ugrás a fő oldalra.")
            driver.get(DEFAULT_BASE)
            _inject_disable_animations()
            WebDriverWait(driver, 15).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
            )
            LOGIN_TS = time.time()
            _autoupdate_attempts = 0
            return

        # 3) Normál login folyamat (form kitöltés)
        email_field = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[autocomplete='email']"))
        )
        password_field = WebDriverWait(driver, 12).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "input[autocomplete='password']"))
        )

        username = os.getenv("SB_USER") or ACTIVE_ACCOUNT["email"]
        password = os.getenv("SB_PASS") or ACTIVE_ACCOUNT["password"]

        human_type(email_field, username)
        human_type(password_field, password)

        if not _submit_login_form_robust(timeout_after=15):
            raise RuntimeError("Nem sikerült elküldeni a bejelentkezési űrlapot.")

        _inject_disable_animations()
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )

        log("✅ Sikeres bejelentkezés.")
        LOGIN_TS = time.time()
        _autoupdate_attempts = 0
        DIAG_LOGGER.log_milestone(f"LOGIN_SUCCESS (account={ACTIVE_ACCOUNT_KEY})")

    except Exception as e:
        print(f"❌ Bejelentkezés sikertelen: {e}")
        DIAG_LOGGER.log_event("LOGIN", f"Login failed: {str(e)[:100]}", "ERROR")
        try:
            # Fallback: még egyszer megnézzük a login oldalt, de itt is kezeljük az "already signed in"-t
            driver.get(LOGIN_URL)
            _inject_disable_animations()
            time.sleep(0.8)

            try:
                body_txt = _safe_execute_script(
                    "return ((document.body && document.body.innerText) || "
                    "(document.documentElement && document.documentElement.innerText) || '').toLowerCase();"
                ) or ""
            except Exception:
                body_txt = ""

            if "you are already signed in" in body_txt:
                log("ℹ️ 'You are already signed in.' (fallback ág) – ugrás a fő oldalra.")
                driver.get(DEFAULT_BASE)
                _inject_disable_animations()
                WebDriverWait(driver, 15).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
                )
                LOGIN_TS = time.time()
                _autoupdate_attempts = 0
                return

            # ha mégis login form van, próbáljuk ENTER-rel elküldeni
            try:
                pw = WebDriverWait(driver, 8).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "input[autocomplete='password']"))
                )
                pw.send_keys(Keys.ENTER)
                WebDriverWait(driver, 12).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
                )
                log("✅ Sikeres bejelentkezés (fallback ENTER).")
                LOGIN_TS = time.time()
                _autoupdate_attempts = 0
                return
            except Exception:
                raise
        except Exception:
            try:
                driver.quit()
            except:
                pass
            raise SystemExit(1)


# ---------- SCAN függvények GROUP/NEXT ----------
def group_scan_tab(url: str, info: dict, higher_ids: set):
    pending_deletes = []
    curr_ids_tab = set()
    should_close = False
    new_ids_for_save = []

    try:
        WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        tbodys = driver.find_elements(By.CSS_SELECTOR, GROUP_SELECTOR)

        if len(tbodys) <= GROUP_EMPTY_CLOSE_TB_THRESHOLD:
            should_close = True

        for tbody in tbodys:
            tid = None
            try:
                tid = tbody.get_attribute("data-id") or tbody.get_attribute("dataid")
            except Exception:
                pass
            if not tid:
                continue

            curr_ids_tab.add(tid)
            last_seen_ts[tid] = time.time()
            id_source[tid] = url

            if tid in higher_ids:
                continue

            if tid in seen:
                handle_update_for_id(tid)
            else:
                new_ids_for_save.append(tid)

        # Mark group if new elements were found
        if new_ids_for_save:
            info["has_new_element"] = True

        batch_save_new_ids(new_ids_for_save, higher_ids=higher_ids)

        gone_here = info.get("active_ids", set()) - curr_ids_tab
        for gid in gone_here:
            pending_deletes.append((url, gid))
        
        # Eltűnt ID-k eltávolítása az OPEN_TASKS sorból
        if gone_here:
            remove_gone_ids_from_open_tasks(gone_here)

        info["active_ids"] = curr_ids_tab
        info["needs_scan"] = False

    except Exception as e:
        warn(f"⚠️ Group szkennelés hiba: {e}")
        should_close = True

    return curr_ids_tab, pending_deletes, should_close

def next_scan_tab(url: str, info: dict, curr_ids_main: set):
    pending_deletes = []
    curr_ids_tab = set()
    should_close = False
    found_next_link = None
    new_ids_for_save = []

    try:
        WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "div.table-container.product-table-container"))
        )
        tbodys = driver.find_elements(By.CSS_SELECTOR, NEXT_SELECTOR)

        if len(tbodys) <= NEXT_EMPTY_CLOSE_TB_THRESHOLD:
            should_close = True

        if len(tbodys) >= 50:
            found_next_link = find_next_page_link()

        for tbody in tbodys:
            tid = None
            try:
                tid = tbody.get_attribute("data-id") or tbody.get_attribute("dataid")
            except Exception:
                pass
            if not tid:
                continue

            curr_ids_tab.add(tid)
            last_seen_ts[tid] = time.time()
            id_source[tid] = url

            if tid in curr_ids_main:
                continue

            if tid in seen:
                handle_update_for_id(tid)
            else:
                new_ids_for_save.append(tid)

        batch_save_new_ids(new_ids_for_save, higher_ids=curr_ids_main)

        gone_here = info.get("active_ids", set()) - curr_ids_tab
        for gid in gone_here:
            pending_deletes.append((url, gid))
        
        # Eltűnt ID-k eltávolítása az OPEN_TASKS sorból
        if gone_here:
            remove_gone_ids_from_open_tasks(gone_here)

        info["active_ids"] = curr_ids_tab
        info["needs_scan"] = False

    except Exception as e:
        warn(f"⚠️ NEXT szkennelés hiba: {e}")
        should_close = True

    return curr_ids_tab, pending_deletes, should_close, found_next_link

# ---------- dispatcher eredmények feldolgozása ----------
pending_delete_ids = set()

# --- UPDATE/DELETE threshold flush beállítások ---
UPDATE_IMMEDIATE_FLUSH_THRESHOLD = 6   # ha ennyi UPDATE+DELETE összejön, azonnal küldjük
DELETE_IMMEDIATE_FLUSH_THRESHOLD = 6

# Bufferek – ide gyűjtjük, amit még NEM küldtünk el a dispatchernek
# MEMORY LEAK FIX: Használjunk bounded deque-t unbounded list helyett
_pending_update_buffer = deque(maxlen=5000)  # UPDATE payloadok, max 5000
_pending_delete_buffer = deque(maxlen=5000)  # DELETE ID-k, max 5000

def process_dispatcher_results(max_items=300):
    global active_ids, seen, consecutive_failed_saves
    results = dispatcher.get_results(max_items=max_items)
    for res in results:
        rtype = res.get("type")
        tid = res.get("id")

        if rtype in ("save_ok", "save_dup_updated"):
            # Sikeres mentés - nullázzuk a hibaszámlálót
            consecutive_failed_saves = 0
            
            st = res.get("state_info", {})
            resp = res.get("resp", {})
            cid = resp.get("correlation_id")
            if tid not in seen:
                seen.add(tid)
                save_seen_line(tid)
            last_sent_state[tid] = {
                "odds1": norm_odds(st.get("odds1")),
                "odds2": norm_odds(st.get("odds2")),
                "profit_percent": norm_profit_str(st.get("profit_percent")),
            }
            last_update_ts[tid] = time.time()
            if tid not in active_ids:
                active_ids.add(tid); save_active_all(active_ids)
            log(f"💾 SAVE kész: {tid} ({'dup→update' if rtype=='save_dup_updated' else 'ok'}) cid={cid}")

        elif rtype == "save_duplicate":
            # Duplikáció is sikeres mentésnek számít
            consecutive_failed_saves = 0
            
            resp = res.get("resp", {})
            cid = resp.get("correlation_id")
            log(f"ℹ️ SAVE duplicate (külön UPDATE nem futott automatikusan): {tid} cid={cid}")

        elif rtype == "save_dup_update_fail":
            # Ez hibának számít
            consecutive_failed_saves += 1
            warn(f"⚠️ SAVE duplicate → UPDATE FAIL id={tid} status={res.get('status')} err={res.get('error')}")
            
            # Ellenőrizzük a limitet
            if consecutive_failed_saves >= CONSECUTIVE_FAILED_SAVES_LIMIT:
                next_key = get_next_account_key(ACTIVE_ACCOUNT_KEY)
                warn(f"🔄 {consecutive_failed_saves} egymás utáni hibás SAVE → Account váltás: {ACTIVE_ACCOUNT_KEY} → {next_key}")
                restart_with_account(next_key)

        elif rtype == "save_error":
            # Ez is hibának számít
            consecutive_failed_saves += 1
            err = res.get("error")
            cid = (err or {}).get("correlation_id") if isinstance(err, dict) else None
            warn(f"⚠️ SAVE hiba id={tid} status={res.get('status')} err={err} cid={cid} (consecutive_fails={consecutive_failed_saves})")
            
            # Ellenőrizzük a limitet
            if consecutive_failed_saves >= CONSECUTIVE_FAILED_SAVES_LIMIT:
                next_key = get_next_account_key(ACTIVE_ACCOUNT_KEY)
                warn(f"🔄 {consecutive_failed_saves} egymás utáni hibás SAVE → Account váltás: {ACTIVE_ACCOUNT_KEY} → {next_key}")
                restart_with_account(next_key)

        elif rtype == "update_ok":
            p = res.get("payload", {})
            resp = res.get("resp", {})
            cid = resp.get("correlation_id")
            if tid:
                last_sent_state[tid] = {
                    "odds1": p.get("odds1"),
                    "odds2": p.get("odds2"),
                    "profit_percent": p.get("profit_percent"),
                }
                last_update_ts[tid] = time.time()
            log(f"🔄 UPDATE kész: {tid} cid={cid}")

        elif rtype == "update_error":
            status = res.get("status")
            err = res.get("error")
            cid = (err or {}).get("correlation_id") if isinstance(err, dict) else None
            if status == 404 and tid:
                t = prepare_new_task_for_id(tid)
                if t and t.get("finals") and valid_external(t["finals"][0]) and valid_external(t["finals"][1]):
                    tip_payload = _build_tip_payload_from_task(t)
                    update_payload = _build_update_payload_from_task(t)
                    dispatcher.enqueue_save({
                        "id": t["id"],
                        "tip_payload": tip_payload,
                        "update_payload": update_payload,
                        "state_info": {
                            "odds1": tip_payload["odds1"],
                            "odds2": tip_payload["odds2"],
                            "profit_percent": tip_payload["profit_percent"],
                        },
                        "finals": t.get("finals"),
                    })
                    log(f"↩️ UPDATE 404 → újra SAVE sorba téve: {tid}")
            else:
                warn(f"⚠️ UPDATE hiba id={tid} status={status} err={err} cid={cid}")

        elif rtype == "delete_ok":
            if tid in active_ids:
                active_ids.remove(tid); save_active_all(active_ids)
            last_sent_state.pop(tid, None)
            last_update_ts.pop(tid, None)
            last_update_attempt_ts.pop(tid, None)
            last_seen_ts.pop(tid, None)
            if tid in seen:
                seen.remove(tid); remove_seen_line(tid)
            pending_delete_ids.discard(tid)
            resp = res.get("resp", {})
            cid = resp.get("correlation_id")
            log(f"❌ DELETE kész: {tid} cid={cid}")

        elif rtype == "delete_error":
            err = res.get("error")
            cid = (err or {}).get("correlation_id") if isinstance(err, dict) else None
            warn(f"⚠️ DELETE hiba id={tid} status={res.get('status')} err={err} cid={cid}")
            pending_delete_ids.discard(tid)


def flush_pending_updates():
    """Elküldi az elbufferelt UPDATE payloadokat a dispatchernek."""
    global _pending_update_buffer
    if not _pending_update_buffer:
        return
    for payload in _pending_update_buffer:
        try:
            dispatcher.enqueue_update(payload)
        except Exception as e:
            warn(f"⚠️ UPDATE enqueue hiba (flush): {e}")
    _pending_update_buffer = []

def flush_pending_deletes():
    """Elküldi az elbufferelt DELETE ID-kat a dispatchernek."""
    global _pending_delete_buffer
    if not _pending_delete_buffer:
        return
    for gid in _pending_delete_buffer:
        try:
            dispatcher.enqueue_delete(gid)
        except Exception as e:
            warn(f"⚠️ DELETE enqueue hiba (flush): {e}")
    _pending_delete_buffer = []

def maybe_flush_immediate():
    """
    Ha összesen legalább 10 UPDATE+DELETE összegyűlt,
    azonnal flush-oljuk (nem várunk a ciklus végéig).
    """
    total = len(_pending_update_buffer) + len(_pending_delete_buffer)
    threshold = min(UPDATE_IMMEDIATE_FLUSH_THRESHOLD, DELETE_IMMEDIATE_FLUSH_THRESHOLD)
    if threshold > 0 and total >= threshold:
        flush_pending_updates()
        flush_pending_deletes()

def schedule_delete(gid: str):
    """
    DELETE-ek gyűjtése:
    - pending_delete_ids: jelzi, hogy már jelöltük törlésre
    - _pending_delete_buffer: amik még nem mentek el a dispatcherhez
    """
    # 🔒 BOOTSTRAP alatt nem törlünk Supabase-ben – előbb épüljön fel
    # az összes main/group/next oldal és a "valós" tbody lista.
    if in_bootstrap_phase():
        return

    if gid in pending_delete_ids:
        return
    pending_delete_ids.add(gid)
    _pending_delete_buffer.append(gid)
    maybe_flush_immediate()



# --- CDP-BASED TBODY READING -------------------------------------------

def _get_tbody_ids_via_cdp_for_target(target_id: str) -> list[str] | None:
    """
    CDP Runtime.evaluate használatával lekérdezi a tbody ID-ket egy adott target-ből.
    
    Args:
        target_id: CDP target ID
    
    Returns:
        List of tbody IDs or None if CDP fails
    """
    if not USE_CDP_FOR_TBODY_READING:
        return None  # Feature kikapcsolva
    
    try:
        # JavaScript kifejezés: összes tbody[data-id] vagy tbody[dataid] elem ID-jének gyűjtése
        js_expression = """
        (function() {
            const tbodys = Array.from(document.querySelectorAll('tbody.surebet_record'));
            const ids = [];
            for (const tbody of tbodys) {
                const id = tbody.getAttribute('data-id') || tbody.getAttribute('dataid');
                if (id) {
                    ids.push(id);
                }
            }
            return ids;
        })();
        """
        
        result = driver.execute_cdp_cmd("Runtime.evaluate", {
            "expression": js_expression,
            "returnByValue": True,
            "awaitPromise": False
        })
        
        if result and 'result' in result and 'value' in result['result']:
            tbody_ids = result['result']['value']
            if isinstance(tbody_ids, list):
                DIAG_LOGGER.log_cdp_lifecycle("TBODY_READ_SUCCESS", url=f"target={target_id[:12]}...", details=f"count={len(tbody_ids)}")
                return tbody_ids
        
        # Sikertelen CDP válasz
        return None
        
    except Exception as e:
        # CDP hiba, fallback-re kell váltani
        DIAG_LOGGER.log_cdp_lifecycle("TBODY_READ_ERROR", url=f"target={target_id[:12]}...", error=True, details=str(e)[:80])
        return None


def _get_tbody_ids_via_cdp_for_window(handle: str) -> list[str] | None:
    """
    CDP-vel lekérdezi a tbody ID-ket egy window handle-höz tartozó target-ből.
    
    Args:
        handle: Window handle
    
    Returns:
        List of tbody IDs or None if CDP fails
    """
    if not USE_CDP_FOR_TBODY_READING:
        return None  # Feature kikapcsolva
    
    try:
        # Először meg kell találni a window handle-höz tartozó target ID-t
        targets_info = _safe_cdp_cmd("Target.getTargets", {}, label="get tbody targets")
        if not targets_info or 'targetInfos' not in targets_info:
            return None
        
        # Keresünk egy page target-et ami ehhez a handle-höz tartozik
        target_id = None
        for target in targets_info['targetInfos']:
            if target.get('type') == 'page':
                # Nem tudjuk direkt módon match-elni a handle-t a targetId-vel CDP-ből,
                # de megpróbálhatjuk az URL vagy egyéb információk alapján
                # Egyszerűbb megoldás: próbálkozunk Runtime.evaluate-tel minden page target-en
                target_id = target.get('targetId')
                if target_id:
                    # Próbálkozás ezzel a target-tel
                    tbody_ids = _get_tbody_ids_via_cdp_for_target(target_id)
                    if tbody_ids is not None:
                        return tbody_ids
        
        return None
        
    except Exception as e:
        return None


def _scan_window_cdp_with_fallback(handle: str, source_label: str) -> set[str]:
    """
    Megpróbálja CDP-vel beolvasni a tbody ID-ket, ha elbukik, Selenium fallback.
    
    Args:
        handle: Window handle
        source_label: Forrás címke (main/group/next)
    
    Returns:
        Set of tbody IDs
    """
    ids_set = set()
    now_ts = time.time()
    
    # 1. Próbálkozás CDP-vel
    if USE_CDP_FOR_TBODY_READING:
        try:
            tbody_ids = _get_tbody_ids_via_cdp_for_window(handle)
            if tbody_ids is not None:
                # CDP siker!
                for tid in tbody_ids:
                    if tid:
                        ids_set.add(tid)
                        last_seen_ts[tid] = now_ts
                        if tid not in id_source:
                            id_source[tid] = source_label
                return ids_set
        except Exception:
            pass  # Fallback-re váltunk
    
    # 2. Selenium fallback (eredeti implementáció)
    try:
        driver.switch_to.window(handle)
        tbodys = driver.find_elements(By.CSS_SELECTOR, "tbody.surebet_record")
        for tbody in tbodys:
            try:
                tid = tbody.get_attribute("data-id") or tbody.get_attribute("dataid")
            except Exception:
                tid = None
            if tid:
                ids_set.add(tid)
                last_seen_ts[tid] = now_ts
                if tid not in id_source:
                    id_source[tid] = source_label
    except Exception:
        pass  # Hiba, üres set marad
    
    return ids_set


# --- TAB-ALAPÚ RESYNC (ÚJ LOGIKA) -----------------------------------------

def collect_live_ids_from_open_tabs() -> set[str]:
    """
    Összegyűjti az összes élő tbody ID-t a JELENLEG NYITOTT tabokból:

      - MAIN_HANDLE (főoldal)
      - group_tabs
      - next_tabs

    Közben frissíti:
      - last_seen_ts[tid]
      - id_source[tid]
    
    CDP-ALAPÚ MEGKÖZELÍTÉS (USE_CDP_FOR_TBODY_READING = True):
      - CDP Runtime.evaluate használata minden tabon
      - Automatikus Selenium fallback hiba esetén
      - Gyorsabb, kevesebb "no such window" hiba
    
    SELENIUM FALLBACK (USE_CDP_FOR_TBODY_READING = False vagy CDP hiba):
      - Eredeti driver.switch_to.window + find_elements megközelítés
      - 100% backward compatibility
    """
    live_ids = set()
    
    # 1) MAIN
    try:
        if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
            ids = _scan_window_cdp_with_fallback(MAIN_HANDLE, "main")
            live_ids.update(ids)
    except Exception as e:
        warn(f"collect_live_ids_from_open_tabs: MAIN_HANDLE hiba: {e}")

    # 2) GROUP tabok
    for url, info in list(group_tabs.items()):
        handle = info.get("handle")
        if not handle or handle not in driver.window_handles:
            continue
        try:
            ids = _scan_window_cdp_with_fallback(handle, "group")
            live_ids.update(ids)
        except Exception as e:
            warn(f"collect_live_ids_from_open_tabs: group tab hiba {url}: {e}")

    # 3) NEXT tabok
    for url, info in list(next_tabs.items()):
        handle = info.get("handle")
        if not handle or handle not in driver.window_handles:
            continue
        try:
            ids = _scan_window_cdp_with_fallback(handle, "next")
            live_ids.update(ids)
        except Exception as e:
            warn(f"collect_live_ids_from_open_tabs: next tab hiba {url}: {e}")

    # próbáljunk visszamenni a MAIN-re
    try:
        if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
            driver.switch_to.window(MAIN_HANDLE)
    except Exception:
        pass

    mode_str = "CDP+fallback" if USE_CDP_FOR_TBODY_READING else "Selenium"
    log(f"collect_live_ids_from_open_tabs ({mode_str}): {len(live_ids)} élő tbody ID a nyitott tabokból")
    return live_ids


def post_bootstrap_cleanup():
    """
    BOOTSTRAP fázis után automatikusan lefutó cleanup:
    - összegyűjti az élő ID-kat a nyitott main/group/next tabokból
    - összehasonlítja az active_ids fájllal
    - ami nem látható a weboldalon, törli az active_ids fájlból
    - és küldi a delete-tip-et a szervernek is
    
    Ez minden induláskor lefut, akár user váltásnál is.
    """
    global active_ids, BOOTSTRAP_CLEANUP_DONE
    
    if BOOTSTRAP_CLEANUP_DONE:
        return  # már lefutott, ne csináljuk újra
    
    log("🧹 POST-BOOTSTRAP CLEANUP indul: ID-k összehasonlítása active_ids fájllal...")
    DIAG_LOGGER.log_milestone("POST_BOOTSTRAP_CLEANUP_START")
    
    try:
        # Összegyűjtjük az élő ID-kat a nyitott tabokból
        live_ids = collect_live_ids_from_open_tabs()
    except Exception as e:
        warn(f"POST-BOOTSTRAP CLEANUP: hiba az élő ID-k gyűjtésekor: {e}")
        live_ids = set()
    
    # Azonosítjuk a stale ID-kat (amik az active_ids-ben vannak, de nem látszanak)
    stale_ids = [tid for tid in list(active_ids) if tid not in live_ids]
    
    if stale_ids:
        log(f"🗑️ POST-BOOTSTRAP CLEANUP: {len(stale_ids)} ID nem látható → törlés active_ids fájlból és szerverről")
        
        # Törlés az active_ids-ből
        for tid in stale_ids:
            active_ids.discard(tid)
        
        # Mentés az active_ids fájlba
        save_active_all(active_ids)
        
        # DELETE küldése a szervernek (dispatcher-en keresztül)
        for tid in stale_ids:
            try:
                dispatcher.enqueue_delete(tid)
            except Exception as e:
                warn(f"⚠️ DELETE enqueue hiba (post-bootstrap): {e}")
        
        # Azonnal kiküldjük a DELETE-eket
        try:
            process_dispatcher_results(max_items=2000)
        except Exception as e:
            warn(f"⚠️ POST-BOOTSTRAP CLEANUP: dispatcher results hiba: {e}")
        
        log(f"✅ POST-BOOTSTRAP CLEANUP: {len(stale_ids)} ID törölve")
    else:
        log("✨ POST-BOOTSTRAP CLEANUP: nincs törlendő ID – minden élő ID megtalálható a tabokon")
    
    BOOTSTRAP_CLEANUP_DONE = True
    log("🏁 POST-BOOTSTRAP CLEANUP kész – normál működés folytatódik")
    DIAG_LOGGER.log_milestone(f"POST_BOOTSTRAP_CLEANUP_COMPLETE (stale_removed={len(stale_ids) if stale_ids else 0})")


def query_tips_ids_from_database():
    """
    Lekérdezi az összes aktív tip ID-t közvetlenül az adatbázisból Supabase SDK használatával.
    
    Returns:
        set: Az aktív tip ID-k halmaza, vagy üres halmaz hiba esetén
    """
    if not SUPABASE_SDK_AVAILABLE:
        warn("⚠️ Supabase SDK nem elérhető - használd: pip install supabase")
        return None
    
    try:
        log("📊 Supabase SDK használata közvetlen adatbázis lekérdezéshez...")
        
        # Supabase kliens létrehozása SERVICE ROLE KEY-jel (teljes hozzáférés)
        supabase = create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)
        
        # Tips tábla lekérdezése - csak az ID oszlop
        response = supabase.table("tips").select("id").execute()
        
        # ID-k kinyerése
        if response.data:
            db_ids = set(row["id"] for row in response.data if "id" in row)
            log(f"✅ Supabase SDK: {len(db_ids)} ID sikeresen lekérdezve")
            return db_ids
        else:
            log("ℹ️ Supabase SDK: üres válasz, nincs tip az adatbázisban")
            return set()
            
    except Exception as e:
        warn(f"⚠️ Supabase SDK hiba: {e}")
        return None


def reconcile_database_with_active_ids():
    """
    🔄 ADATBÁZIS RECONCILIATION - Extrém biztonságos megoldás crash recovery-re
    
    PROBLÉMA:
    - Script crash esetén az adatbázisban maradnak orphan rekordok
    - active_ids.txt a helyes állapotot tükrözi
    - Adatbázisban sokkal több rekord van mint kellene
    
    MEGOLDÁS:
    - Induláskor lekérdezzük az összes adatbázis ID-t
    - Összehasonlítjuk az active_ids.txt tartalmával
    - Töröljük azt, ami NINCS az active_ids.txt-ben (orphan rekordok)
    
    BIZTONSÁGI FUNKCIÓK:
    1. Maximum törlési limit (DB_RECONCILE_MAX_DELETES)
    2. Részletes logolás minden műveletről
    3. Történet mentése fájlba
    4. Csak egyszer fut induláskor
    5. active_ids.txt = source of truth (a legbiztonságosabb)
    
    Ez minden indításkor lefut, beleértve crash után újraindítást is.
    """
    global active_ids, DB_RECONCILE_DONE
    
    if DB_RECONCILE_DONE:
        return  # már lefutott egyszer
    
    if not DB_RECONCILE_ENABLED:
        log("ℹ️ DB RECONCILIATION kikapcsolva (DB_RECONCILE_ENABLED=False)")
        DB_RECONCILE_DONE = True
        return
    
    log("🔄 DATABASE RECONCILIATION indul: active_ids.txt és adatbázis szinkronizálása...")
    DIAG_LOGGER.log_milestone("DB_RECONCILIATION_START")
    
    try:
        # 1. Lekérjük az összes adatbázisbeli ID-t
        log("📊 Adatbázis ID-k lekérdezése...")
        
        # Először próbáljuk a Supabase SDK-t (közvetlen adatbázis lekérdezés)
        db_ids = query_tips_ids_from_database()
        
        # Ha SDK nem működött, próbáljuk az API endpoint-ot (fallback)
        if db_ids is None:
            log("🔄 Fallback: API endpoint használata...")
            status, data = http_post(LIST_ACTIVE_TIPS_URL, {}, timeout=30)
            
            if status != 200 or not isinstance(data, dict):
                warn(f"⚠️ DB RECONCILIATION: sem SDK, sem API endpoint nem működött (status={status})")
                DB_RECONCILE_DONE = True
                return
            
            db_ids = set(data.get("ids", []))
        
        log(f"📊 Adatbázisban {len(db_ids)} aktív tip ID található")
        
        # 2. Betöltjük az active_ids.txt tartalmát (source of truth)
        file_ids = set(active_ids) if active_ids else set()
        log(f"📄 active_ids.txt-ben {len(file_ids)} ID található")
        
        # 3. Azonosítjuk az orphan ID-kat (adatbázisban van, de a fájlban nincs)
        orphan_ids = db_ids - file_ids
        
        if not orphan_ids:
            log("✨ DB RECONCILIATION: nincs orphan ID – adatbázis és active_ids.txt szinkronban van")
            DB_RECONCILE_DONE = True
            DIAG_LOGGER.log_milestone("DB_RECONCILIATION_COMPLETE (orphans=0)")
            return
        
        log(f"🗑️ DB RECONCILIATION: {len(orphan_ids)} orphan ID azonosítva (adatbázisban van, de active_ids.txt-ben nincs)")
        
        # 4. Biztonsági limit ellenőrzése
        if len(orphan_ids) > DB_RECONCILE_MAX_DELETES:
            warn(f"⚠️ DB RECONCILIATION: BIZTONSÁGI LIMIT! {len(orphan_ids)} orphan ID > limit ({DB_RECONCILE_MAX_DELETES})")
            warn(f"⚠️ Csak az első {DB_RECONCILE_MAX_DELETES} ID-t töröljük biztonsági okokból")
            orphan_ids = set(list(orphan_ids)[:DB_RECONCILE_MAX_DELETES])
        
        # 5. Orphan ID-k törlése az adatbázisból
        log(f"🗑️ {len(orphan_ids)} orphan ID törlése az adatbázisból...")
        deleted_count = 0
        failed_count = 0
        
        for orphan_id in orphan_ids:
            try:
                dispatcher.enqueue_delete(orphan_id)
                deleted_count += 1
            except Exception as e:
                warn(f"⚠️ DB RECONCILIATION: DELETE enqueue hiba ({orphan_id}): {e}")
                failed_count += 1
        
        # 6. Azonnal kiküldjük a DELETE-eket
        try:
            flush_pending_deletes()
            process_dispatcher_results(max_items=2000)
        except Exception as e:
            warn(f"⚠️ DB RECONCILIATION: dispatcher results hiba: {e}")
        
        # 7. Történet logolása
        try:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            with open(DB_RECONCILE_HISTORY_FILE, "a", encoding="utf-8") as f:
                f.write(f"{timestamp} | Orphans deleted: {deleted_count} | Failed: {failed_count} | DB IDs: {len(db_ids)} | File IDs: {len(file_ids)}\n")
        except Exception as e:
            warn(f"⚠️ DB RECONCILIATION: történet mentés hiba: {e}")
        
        log(f"✅ DB RECONCILIATION kész: {deleted_count} orphan ID törölve, {failed_count} hiba")
        DIAG_LOGGER.log_milestone(f"DB_RECONCILIATION_COMPLETE (orphans_deleted={deleted_count}, failed={failed_count})")
        
    except Exception as e:
        warn(f"⚠️ DB RECONCILIATION: általános hiba: {e}")
        DIAG_LOGGER.log_event("DB_RECONCILIATION", f"Error: {str(e)[:100]}", "ERROR")
    finally:
        DB_RECONCILE_DONE = True


def run_dynamic_bootstrap():
    """
    Dinamikus BOOTSTRAP fázis:
    1. MAIN + rekurzív NEXT oldalak megnyitása (max 20s)
    2. GROUP linkek gyűjtése + párhuzamos megnyitás
    3. 10s várakozás GROUP oldalak betöltésére
    4. Max 5 perc az egész folyamatra
    """
    global BOOTSTRAP_COMPLETED, MAIN_HANDLE
    
    DIAG_LOGGER.log_milestone("BOOTSTRAP_START")
    
    bootstrap_start = time.time()
    MAX_BOOTSTRAP_TIME = 300  # 5 perc
    NEXT_PHASE_TIMEOUT = 20   # 20s MAIN + NEXT oldalakra
    GROUP_LOAD_WAIT = 10      # 10s GROUP oldalak betöltésére
    
    log("🚀 DINAMIKUS BOOTSTRAP indul: MAIN + NEXT oldalak rekurzív feltérképezése")
    
    try:
        # === FÁZIS 1: MAIN + rekurzív NEXT oldalak (max 20s) ===
        phase1_start = time.time()
        next_urls_to_open = []
        
        # MAIN oldal szkennelése NEXT linkekért
        try:
            if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
                driver.switch_to.window(MAIN_HANDLE)
                next_link = find_next_page_link()
                if next_link and next_link not in next_tabs:
                    next_urls_to_open.append(next_link)
                    log(f"📄 MAIN-ról talált NEXT: {next_link}")
        except Exception as e:
            warn(f"⚠️ MAIN scan hiba (NEXT linkek): {e}")
        
        # Rekurzívan nyitjuk a NEXT oldalakat és keressük a további NEXT linkeket
        opened_next = set()
        while next_urls_to_open and (time.time() - phase1_start) < NEXT_PHASE_TIMEOUT:
            next_url = next_urls_to_open.pop(0)
            if next_url in opened_next or next_url in next_tabs:
                continue
            
            try:
                # BOOTSTRAP: szinkron nyitás hogy biztosan megnyíljon mielőtt szkenneljük
                _open_next_tab_sync(next_url)
                opened_next.add(next_url)
                
                # Scan az újonnan megnyitott NEXT oldalon további NEXT linkekért
                if next_url in next_tabs:
                    info = next_tabs[next_url]
                    handle = info.get("handle")
                    if handle and handle in driver.window_handles:
                        driver.switch_to.window(handle)
                        further_next = find_next_page_link()
                        if further_next and further_next not in opened_next and further_next not in next_tabs:
                            next_urls_to_open.append(further_next)
                            log(f"📄 NEXT-ről talált újabb NEXT: {further_next}")
            except Exception as e:
                warn(f"⚠️ NEXT oldal megnyitás hiba ({next_url}): {e}")
        
        phase1_elapsed = time.time() - phase1_start
        log(f"✅ FÁZIS 1 kész: {len(opened_next)} NEXT oldal megnyitva ({phase1_elapsed:.1f}s)")
        
        # === FÁZIS 2: GROUP linkek gyűjtése + megnyitás ===
        if (time.time() - bootstrap_start) >= MAX_BOOTSTRAP_TIME:
            log("⏰ 5 perces timeout – BOOTSTRAP befejezése GROUP fázis nélkül")
            BOOTSTRAP_COMPLETED = True
            return
        
        log("📦 FÁZIS 2: GROUP linkek gyűjtése MAIN + NEXT oldalakról")
        group_urls_to_open = set()
        
        # MAIN oldalról GROUP linkek
        try:
            if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
                driver.switch_to.window(MAIN_HANDLE)
                tbodys = driver.find_elements(By.CSS_SELECTOR, "tbody.surebet_record")
                for tbody in tbodys:
                    try:
                        group_link = find_group_link_in_tbody(tbody)
                        if group_link and group_link not in group_tabs:
                            group_urls_to_open.add(group_link)
                    except Exception:
                        pass
        except Exception as e:
            warn(f"⚠️ MAIN GROUP linkek gyűjtése hiba: {e}")
        
        # NEXT oldalakról GROUP linkek
        for next_url, info in list(next_tabs.items()):
            try:
                handle = info.get("handle")
                if handle and handle in driver.window_handles:
                    driver.switch_to.window(handle)
                    tbodys = driver.find_elements(By.CSS_SELECTOR, "tbody.surebet_record")
                    for tbody in tbodys:
                        try:
                            group_link = find_group_link_in_tbody(tbody)
                            if group_link and group_link not in group_tabs:
                                group_urls_to_open.add(group_link)
                        except Exception:
                            pass
            except Exception as e:
                warn(f"⚠️ NEXT ({next_url}) GROUP linkek gyűjtése hiba: {e}")
        
        # === FÁZIS 2b: GROUP oldalak párhuzamos megnyitása ===
        group_count = len(group_urls_to_open)
        log(f"🔍 {group_count} GROUP oldal nyitása...")
        
        # BOOTSTRAP: szinkron nyitás hogy biztosan megnyíljanak
        for group_url in group_urls_to_open:
            if (time.time() - bootstrap_start) >= MAX_BOOTSTRAP_TIME:
                log("⏰ 5 perces timeout – BOOTSTRAP befejezése")
                break
            try:
                _open_group_tab_sync(group_url)
            except Exception as e:
                warn(f"⚠️ GROUP oldal megnyitás hiba ({group_url}): {e}")
        
        # Ellenőrizzük hány GROUP oldal nyílt meg ténylegesen
        opened_group_count = len([url for url in group_urls_to_open if url in group_tabs])
        log(f"✅ {opened_group_count}/{group_count} GROUP oldal megnyílt")
        
        # === FÁZIS 3: Várakozás GROUP oldalak betöltésére ===
        if (time.time() - bootstrap_start) < MAX_BOOTSTRAP_TIME:
            log(f"⏳ {GROUP_LOAD_WAIT}s várakozás GROUP oldalak betöltésére...")
            time.sleep(GROUP_LOAD_WAIT)
        
        total_time = time.time() - bootstrap_start
        log(f"✅ DINAMIKUS BOOTSTRAP befejezve: {len(opened_next)} NEXT + {len(group_tabs)} GROUP oldal ({total_time:.1f}s)")
        DIAG_LOGGER.log_milestone(f"BOOTSTRAP_PHASE_COMPLETE (next={len(opened_next)}, group={len(group_tabs)}, time={total_time:.1f}s)")
        
    except Exception as e:
        warn(f"⚠️ DINAMIKUS BOOTSTRAP hiba: {e}")
        DIAG_LOGGER.log_event("BOOTSTRAP", f"Error: {str(e)[:100]}", "ERROR")
    finally:
        BOOTSTRAP_COMPLETED = True
        log("🏁 BOOTSTRAP_COMPLETED = True – normál működés indul")
        DIAG_LOGGER.log_milestone("BOOTSTRAP_COMPLETED")


def full_resync_and_cleanup(max_groups=None):
    """
    ÚJ: TAB-ALAPÚ RESYNC

    - NEM mászkál driver.get-tel oldalról oldalra
    - CSAK a már nyitott tabokat nézi végig (MAIN + GROUP + NEXT)
    - Ami active_ids-ben van, de sehol nem látszik → DELETE (Supabase + TXT)
    """
    global active_ids

    log("🔄 TAB-RESYNC indul (nyitott MAIN/GROUP/NEXT tabok alapján)…")

    try:
        live_ids = collect_live_ids_from_open_tabs()
    except Exception as e:
        warn(f"TAB-RESYNC: hiba az élő ID-k gyűjtésekor: {e}")
        live_ids = set()

    if not live_ids:
        log("ℹ️ TAB-RESYNC: nincs élő tbody ID a nyitott tabok alapján (friss indulásnál ez normális lehet).")

    stale = [tid for tid in list(active_ids) if tid not in live_ids]

    if stale:
        log(f"🗑️ TAB-RESYNC: {len(stale)} ID már nem él → törlés Supabase + txt")
        for tid in stale:
            schedule_delete(tid)

        # ami itt összegyűlt, azonnal küldjük is ki
        flush_pending_updates()
        flush_pending_deletes()
        process_dispatcher_results(max_items=2000)
    else:
        log("✨ TAB-RESYNC: nincs törlendő ID – minden élő a NYITOTT tabok szerint.")

    log("🔁 TAB-RESYNC kész.")


# ---------- ACCOUNT ROTATION / RESTART ----------

def check_tbody_for_surebet_com(tbody_element) -> bool:
    """
    Ellenőrzi, hogy a tbody elemben szerepel-e a 'surebet.com' szöveg (case-insensitive).
    Ha igen, akkor account váltás szükséges.
    """
    try:
        tbody_text = tbody_element.text.lower() if tbody_element else ""
        return "surebet.com" in tbody_text
    except Exception:
        return False

def get_next_account_key(current: str) -> str:
    """
    Következő account kulcs:
    - acc1 -> acc2
    - acc2 -> acc3
    - acc3 -> acc1
    - minden más -> acc1
    """
    if current == "acc1":
        return "acc2"
    if current == "acc2":
        return "acc3"
    if current == "acc3":
        return "acc1"
    return "acc1"


def restart_with_account(next_key: str):
    warn(f"♻️ Account váltás: {ACTIVE_ACCOUNT_KEY} → {next_key} – Chrome + script újraindítás...")
    
    # Pause before rotation for bot-proofing
    warn(f"⏸️ Pausing {ACCOUNT_ROTATION_PAUSE_SEC} seconds before account rotation...")
    time.sleep(ACCOUNT_ROTATION_PAUSE_SEC)

    # Mentjük az account információt ÉS NULLÁZZUK a futásidőt
    try:
        # Account váltáskor NULLÁZZUK a runtime-ot, hogy a következő account
        # friss 0 perccel induljon és ismét 32 percet futhasson
        runtime_state["accumulated_minutes"] = 0.0
        runtime_state["last_session_start"] = None
        
        # PERZISZTENS ACCOUNT INFORMÁCIÓ - ez a fő újdonság!
        runtime_state["current_account"] = ACTIVE_ACCOUNT_KEY
        runtime_state["next_account"] = next_key
        runtime_state["account_rotation_pending"] = True
        
        save_runtime_state(runtime_state)
        warn(f"💾 Account info mentve: current={ACTIVE_ACCOUNT_KEY}, next={next_key}, pending=True, time=0.0")
    except Exception as e:
        warn(f"⚠️ Runtime state mentés hiba: {e}")

    # Itt MOST NEM hívunk TAB-RESYNC-et.
    # A folyamatos futás alatt a DISAPPEAR_GRACE_SEC alapú törlés már szépen
    # karbantartotta az active_ids-t, nem akarunk egy utolsó, részleges nézeten alapuló
    # „globális takarítást” rárúgni.

    # 1) Minden pending mentés/törlés flush-olása
    try:
        flush_pending_updates()
        flush_pending_deletes()
        process_dispatcher_results(max_items=2000)
        dispatcher.stop()
    except Exception:
        pass

    # 2) Chrome lezárása
    try:
        driver.quit()
    except Exception:
        pass

    # 4) Script újraindítása új accounttal (proper path handling for spaces in filename)
    script_path = os.path.abspath(__file__)
    restart_command = [sys.executable, script_path, f"--acc={next_key}"]
    
    # Detailed logging for debugging
    warn(f"🔄 Restart command: {restart_command}")
    warn(f"📂 Script path: {script_path}")
    warn(f"🐍 Python executable: {sys.executable}")
    warn(f"🎯 Target account: {next_key}")
    warn(f"💡 TIP: Ha újraindítod manuálisan, már nem kell --acc paraméter!")
    
    os.execv(
        sys.executable,
        restart_command
    )


# ---------- fő program ----------
seen = load_seen()
active_ids = load_active()
last_sent_state = {}
last_update_ts = {}
last_update_attempt_ts = {}
link_cache = load_link_cache()

# Perzisztens futásidő állapot betöltése
runtime_state = load_runtime_state()
accumulated_runtime_minutes = runtime_state.get("accumulated_minutes", 0.0)

# NOTE: A futtatáskor a login() hívás indít. Ha csak importálod, ne fusson automatikusan.
if __name__ == "__main__":
    SESSION_START_TIME = time.time()
    RUN_STARTED_AT = SESSION_START_TIME
    
    # Ha ez nem az első indítás, és az előző session start van mentve,
    # azt is figyelembe vesszük (ha nem múlt el túl sok idő - pl. max 1 óra)
    last_session_start = runtime_state.get("last_session_start")
    if last_session_start and (SESSION_START_TIME - last_session_start) < 3600:
        # Az előző session időt is hozzáadjuk
        log(f"📊 Előző akkumulált futásidő: {accumulated_runtime_minutes:.1f} perc")
    
    # Frissítjük az állapotot az új session kezdetével
    runtime_state["last_session_start"] = SESSION_START_TIME
    
    # FRISSÍTJÜK AZ ACCOUNT ÁLLAPOTOT - sikeres indítás után
    # Ha account rotation volt folyamatban, most már kész
    if runtime_state.get("account_rotation_pending"):
        log(f"✅ Account rotation sikeres volt: {runtime_state.get('current_account')} → {ACTIVE_ACCOUNT_KEY}")
    
    runtime_state["current_account"] = ACTIVE_ACCOUNT_KEY
    runtime_state["account_rotation_pending"] = False
    # next_account-ot nem töröljük, csak info célból marad
    
    save_runtime_state(runtime_state)
    log(f"💾 Account állapot frissítve: current={ACTIVE_ACCOUNT_KEY}, pending=False")
    
    # ============================================================================
    # 🛡️ KRITIKUS ELLENŐRZÉS: SZERVER ELÉRHETŐ-E? (LEGELSŐ!)
    # ============================================================================
    # Ezt MINDENNEL ELŐBB kell csinálni, mert:
    # - Ha szerver nem elérhető, úgysem lehet bejelentkezni
    # - Nem pazaroljuk az időt Chrome inicializálással és login-nal
    # - Azonnal látható ha szerver leáll
    log("")
    log("=" * 80)
    log("🔍 KRITIKUS ELLENŐRZÉS: surebet.com szerver elérhető-e?")
    log("=" * 80)
    
    if not is_surebet_server_available():
        log("❌ Szerver nem elérhető - nem lehet bejelentkezni!")
        wait_for_server_recovery()
    
    log("✅ Szerver elérhető, folytatás...")
    log("=" * 80)
    log("")
    
    # Initialize server check timer (for 10-minute throttling)
    last_server_check_time = time.time()
    
    login()

    log("🚀 DINAMIKUS BOOTSTRAP fázis: rekurzív MAIN + NEXT + GROUP oldalak megnyitása")

    try:
        MAIN_HANDLE = driver.current_window_handle
    except Exception:
        MAIN_HANDLE = None

    # GROUP/NEXT tab-nyitó háttér worker - BOOTSTRAP ELŐTT indul!
    groupnext_thread = threading.Thread(target=group_next_opener_worker, daemon=True)
    groupnext_thread.start()
    log("🚀 Group/NEXT opener worker elindítva (BOOTSTRAP előtt)")

    # Dinamikus BOOTSTRAP futtatása
    run_dynamic_bootstrap()

    # NAV worker: csak BOOTSTRAP UTÁN indul
    nav_thread = None
    nav_started = False

    # Időszakos TAB cleanup worker
    tab_cleanup_thread = threading.Thread(target=tab_cleanup_worker, daemon=True)
    tab_cleanup_thread.start()
    log("🧹 TAB cleanup worker elindítva")

    # Autoupdate indítása Shift+P-vel, ha kell
    ensure_main_autoupdate()
    prev_ids_main = set()

    def scan_next_tabs_evented(curr_ids_main: set):
        next_all_curr_ids = set()
        pending_deletes = []
        to_close = []
        open_requests = []

        # 🔄 Collect tabs for parallel JSON update
        tabs_to_update = []

        items = list(next_tabs.items())
        for url, info in items:
            handle = info["handle"]
            if not validate_and_switch_tab(handle, next_tabs, url, "NEXT"):
                to_close.append(url)
                continue

            try:
                if maybe_refresh_next_tab(url, info):
                    pass
            except Exception:
                pass

            # 🔄 JSON AUTO-UPDATE: Collect tabs that need updating
            try:
                if ENABLE_JSON_AUTO_UPDATE:
                    now = time.time()
                    last_update = info.get('last_json_update', 0)
                    if now - last_update >= JSON_UPDATE_INTERVAL:
                        tabs_to_update.append((driver, "NEXT", info))
            except Exception as e:
                log(f"[AUTO-UPDATE] Error checking update time: {e}")

            if info.get("needs_scan", False):
                curr_ids_tab, pend_del, should_close, found_next = next_scan_tab(url, info, curr_ids_main)
                next_all_curr_ids.update(curr_ids_tab)
                pending_deletes.extend(pend_del)
                if found_next:
                    open_requests.append(found_next)
                if should_close:
                    to_close.append(url)

        # 🔄 Update all NEXT tabs in parallel
        if tabs_to_update:
            try:
                if len(tabs_to_update) > 1:
                    # Use parallel for multiple tabs
                    tab_info = [(drv, ptype) for drv, ptype, _ in tabs_to_update]
                    results = inject_json_updates_parallel(tab_info)
                    # Update timestamps
                    now = time.time()
                    for _, _, info in tabs_to_update:
                        info['last_json_update'] = now
                    log(f"[JSON-UPDATE] NEXT: Updated {len(tabs_to_update)} tabs in parallel")
                else:
                    # Use sequential for single tab
                    drv, ptype, info = tabs_to_update[0]
                    count = inject_json_updates_to_page(drv, ptype)
                    info['last_json_update'] = time.time()
                    if count > 0:
                        log(f"[JSON-UPDATE] NEXT auto-updated: {count} surebets")
            except Exception as e:
                log(f"[JSON-UPDATE] Error updating NEXT tabs: {e}")

        return next_all_curr_ids, pending_deletes, to_close, open_requests

    def scan_group_tabs_evented(curr_ids_main: set, higher_ids: set):
        group_all_curr_ids = set()
        pending_deletes = []
        to_close = []

        # 🔄 Collect tabs for parallel JSON update
        tabs_to_update = []

        items = list(group_tabs.items())
        for url, info in items:
            handle = info["handle"]
            if not validate_and_switch_tab(handle, group_tabs, url, "GROUP"):
                to_close.append(url)
                continue

            try:
                if maybe_refresh_group_tab(url, info):
                    pass
            except Exception:
                pass

            # 🔄 JSON AUTO-UPDATE: Collect tabs that need updating
            try:
                if ENABLE_JSON_AUTO_UPDATE:
                    now = time.time()
                    last_update = info.get('last_json_update', 0)
                    if now - last_update >= JSON_UPDATE_INTERVAL:
                        tabs_to_update.append((driver, "GROUP", info))
            except Exception as e:
                log(f"[AUTO-UPDATE] Error checking update time: {e}")

            if info.get("needs_scan", False):
                curr_ids_tab, pend_del, should_close = group_scan_tab(url, info, higher_ids)
                group_all_curr_ids.update(curr_ids_tab)
                pending_deletes.extend(pend_del)
                if should_close:
                    to_close.append(url)

        # 🔄 Update all GROUP tabs in parallel
        if tabs_to_update:
            try:
                if len(tabs_to_update) > 1:
                    # Use parallel for multiple tabs
                    tab_info = [(drv, ptype) for drv, ptype, _ in tabs_to_update]
                    results = inject_json_updates_parallel(tab_info)
                    # Update timestamps
                    now = time.time()
                    for _, _, info in tabs_to_update:
                        info['last_json_update'] = now
                    log(f"[JSON-UPDATE] GROUP: Updated {len(tabs_to_update)} tabs in parallel")
                else:
                    # Use sequential for single tab
                    drv, ptype, info = tabs_to_update[0]
                    count = inject_json_updates_to_page(drv, ptype)
                    info['last_json_update'] = time.time()
                    if count > 0:
                        log(f"[JSON-UPDATE] GROUP auto-updated: {count} surebets")
            except Exception as e:
                log(f"[JSON-UPDATE] Error updating GROUP tabs: {e}")

        return group_all_curr_ids, pending_deletes, to_close

    try:
        while True:
            loop_start_time = time.time()
            
            # 💀 Ha a WebDriver meghalt, ne kínlódjunk tovább – lépjünk ki a fő loopból
            if DRIVER_DEAD:
                warn("💀 WebDriver kapcsolat meghalt (DRIVER_DEAD=True) – kilépek a fő ciklusból.")
                DIAG_LOGGER.log_crash_context(Exception("DRIVER_DEAD"), "DRIVER_DEATH")
                break

            # 🛡️ PERIODIKUS szerver check (10 percenként, nem minden loop-ban!)
            # Ez csökkenti a bot detection kockázatát
            current_time = time.time()
            if current_time - last_server_check_time > SERVER_CHECK_INTERVAL:
                minutes_since_last = int((current_time - last_server_check_time) / 60)
                log(f"🔍 Periodikus szerver check (utolsó: {minutes_since_last} perc)")
                
                if not is_surebet_server_available():
                    wait_for_server_recovery()
                
                last_server_check_time = current_time
            # else: Skip check - not time yet (reduces bot detection risk)

            # 🏥 Session health check: gyors window_handles check
            try:
                handles = driver.window_handles
                # Diagnostic: periodic health logging
                try:
                    info = _safe_cdp_cmd("Target.getTargets", {}, label="health_check")
                    target_count = len(info.get("targetInfos", [])) if info else 0
                except Exception:
                    target_count = None
                DIAG_LOGGER.log_session_health(len(handles), target_count)
            except WebDriverException as e:
                msg_lower = str(e).lower()
                if "invalid session" in msg_lower or "chrome not reachable" in msg_lower:
                    warn(f"❌ Session health check failed: {e}")
                    DIAG_LOGGER.log_crash_context(e, "SESSION_LOSS")
                    restart_application()
                # Egyéb WebDriverException - log de folytatjuk
                warn(f"⚠️ Session health check warning: {e}")
                DIAG_LOGGER.log_event("HEALTH", f"Warning: {str(e)[:100]}", "WARN")
            except Exception as e:
                # Váratlan hiba - log de folytatjuk
                warn(f"⚠️ Session health check unexpected error: {e}")
                DIAG_LOGGER.log_event("HEALTH", f"Unexpected: {str(e)[:100]}", "ERROR")

            bootstrap = in_bootstrap_phase()
            
            # 🧹 MEMORY LEAK PREVENTION: Periodic cleanup of old tracking data (every 5 minutes)
            # Töröljük a régi (>1 óra) tracking bejegyzéseket hogy ne növekedjen a folyamat a memória
            current_minute = int(time.time() / 60)
            if current_minute % 5 == 0:  # Minden 5. percben
                try:
                    cleanup_old_tracking_data()
                except Exception as e:
                    warn(f"⚠️ Memory cleanup hiba: {e}")
                
                # 📡 NETWORK LOGGING: Save network logs for API discovery (every 5 minutes)
                try:
                    save_network_logs_to_file()
                except Exception as e:
                    warn(f"⚠️ Network logging hiba: {e}")
                
                # 📊 CONTENT HASH STATISTICS - Log every 5 minutes
                if ENABLE_CONTENT_HASH_CHECKING:
                    try:
                        log_content_hash_metrics()
                    except Exception as e:
                        warn(f"⚠️ Hash metrics logging hiba: {e}")

            # 🧹 POST-BOOTSTRAP CLEANUP – csak egyszer, amikor a bootstrap vége van
            if not bootstrap and not BOOTSTRAP_CLEANUP_DONE:
                post_bootstrap_cleanup()
            
            # 🔄 DATABASE RECONCILIATION – adatbázis és active_ids.txt szinkronizálása
            # Ez a post_bootstrap_cleanup UTÁN fut, amikor már biztos hogy minden tab megnyílt
            if not bootstrap and BOOTSTRAP_CLEANUP_DONE and not DB_RECONCILE_DONE:
                reconcile_database_with_active_ids()

            # --- SUPABASE dispatcher eredmények ---
            if not bootstrap:
                process_dispatcher_results(max_items=400)

            now_ts = time.time()
            maybe_refresh_main_page()

            # --- MAIN tab életben tartása + újranyitása, ha kell ---
            try:
                # Ha nincs MAIN_HANDLE, vagy a handle már nincs a window_handles-ben → újranyitjuk
                if not MAIN_HANDLE or MAIN_HANDLE not in driver.window_handles:
                    log("⚠️ MAIN_HANDLE eltűnt, új főoldalt nyitok...")

                    # új tab + MAIN_URL betöltése
                    driver.switch_to.new_window("tab")
                    driver.get(MAIN_URL)
                    MAIN_HANDLE = driver.current_window_handle
                    handle_birth[MAIN_HANDLE] = time.time()

                    _inject_disable_animations()
                    _wait_main_container(timeout=12)
                    ensure_main_autoupdate()
                    time.sleep(3)

                # biztosan MAIN-en vagyunk
                driver.switch_to.window(MAIN_HANDLE)

                # időnként pici keepalive mozgás, hogy ne haljon el a tab
                if now_ts - last_keepalive_ping_ts >= 90:
                    tiny_keepalive_ping()
                    last_keepalive_ping_ts = now_ts

                tbodys_main = driver.find_elements(By.CSS_SELECTOR, "tbody.surebet_record")

            except Exception as e:
                warn(f"Főoldal scan hiba: {e}")
                time.sleep(CHECK_INTERVAL)
                continue

            ensure_main_autoupdate()


            curr_ids_main = set()
            new_ids_main = []

            # Optimization #3: Batch collect all tbody IDs using execute_script for faster DOM access
            try:
                tbody_data = driver.execute_script("""
                    const tbodys = document.querySelectorAll('tbody.surebet_record');
                    return Array.from(tbodys).map(tb => ({
                        id: tb.getAttribute('data-id') || tb.getAttribute('dataid'),
                        text: (tb.textContent || '').toLowerCase(),
                        element: tb
                    })).filter(item => item.id);
                """)
            except Exception:
                tbody_data = []

            # If batch failed, fallback to old method
            if not tbody_data:
                for tbody in tbodys_main:
                    try:
                        tbody_id = tbody.get_attribute("data-id") or tbody.get_attribute("dataid")
                    except Exception:
                        tbody_id = None
                    if not tbody_id:
                        continue
                    
                    # Ellenőrizzük, hogy van-e "surebet.com" a tbody szövegében
                    if check_tbody_for_surebet_com(tbody):
                        next_key = get_next_account_key(ACTIVE_ACCOUNT_KEY)
                        warn(f"🚨 'surebet.com' szöveg detektálva tbody-ban (id={tbody_id}) → Account váltás: {ACTIVE_ACCOUNT_KEY} → {next_key}")
                        restart_with_account(next_key)

                    curr_ids_main.add(tbody_id)
                    last_seen_ts[tbody_id] = now_ts
                    id_source[tbody_id] = 'main'

                    # GROUP linkek folyamatos keresése + tabnyitás (BOOTSTRAP alatt is)
                    try:
                        group_url = find_group_link_in_tbody(tbody)
                        if group_url:
                            open_group_tab_if_needed(group_url)
                    except Exception:
                        pass

                    # BOOTSTRAP alatt is megkülönböztetjük, mi seen, mi új,
                    # de a SAVE/UPDATE úgyis no-op lesz a gating miatt.
                    if tbody_id in seen:
                        handle_update_for_id(tbody_id)
                    else:
                        new_ids_main.append(tbody_id)
            else:
                # Fast path: process batched data
                for item in tbody_data:
                    tbody_id = item.get('id')
                    if not tbody_id:
                        continue
                    
                    # Ellenőrizzük, hogy van-e "surebet.com" a tbody szövegében
                    tbody_text = item.get('text', '')
                    if 'surebet.com' in tbody_text:
                        next_key = get_next_account_key(ACTIVE_ACCOUNT_KEY)
                        warn(f"🚨 'surebet.com' szöveg detektálva tbody-ban (id={tbody_id}) → Account váltás: {ACTIVE_ACCOUNT_KEY} → {next_key}")
                        restart_with_account(next_key)
                    
                    curr_ids_main.add(tbody_id)
                    last_seen_ts[tbody_id] = now_ts
                    id_source[tbody_id] = 'main'

                    # GROUP linkek - still need element reference
                    tbody_elem = item.get('element')
                    if tbody_elem:
                        try:
                            group_url = find_group_link_in_tbody(tbody_elem)
                            if group_url:
                                open_group_tab_if_needed(group_url)
                        except Exception:
                            pass

                    # BOOTSTRAP alatt is megkülönböztetjük, mi seen, mi új
                    if tbody_id in seen:
                        handle_update_for_id(tbody_id)
                    else:
                        new_ids_main.append(tbody_id)

            # Új ID-k NAV-queue-be (BOOTSTRAP alatt csak "előkészül", de nem küldünk)
            batch_save_new_ids(new_ids_main)

            # NEXT paginálás + első NEXT tab nyitása
            try:
                maybe_refresh_main_paginate_and_try_open_next(len_tbodys_main=len(tbodys_main))
            except Exception:
                pass

            # --- NEXT tabok scan ---
            next_all_curr_ids, next_pending_deletes, next_to_close, next_open_requests = scan_next_tabs_evented(curr_ids_main)

            # új NEXT URL-ek nyitása (BOOTSTRAP alatt is)
            for nurl in next_open_requests:
                try:
                    open_next_tab_if_needed(nurl)
                except Exception:
                    pass

            # --- GROUP tabok scan ---
            higher_ids = curr_ids_main | next_all_curr_ids
            group_all_curr_ids, group_pending_deletes, group_to_close = scan_group_tabs_evented(curr_ids_main, higher_ids)

            curr_ids_all_now = curr_ids_main | next_all_curr_ids | group_all_curr_ids
            now2 = time.time()

            # Eltűnt ID-k jelölése – a valódi DELETE a schedule_delete-ben BOOTSTRAP alatt még no-op
            # NEXT oldalon eltűnt ID-k
            for url, gid in next_pending_deletes:
                if gid in curr_ids_all_now:
                    continue
                last_ts = last_seen_ts.get(gid, 0.0)
                if (now2 - last_ts) >= DISAPPEAR_GRACE_SEC:
                    schedule_delete(gid)

            # GROUP oldalon eltűnt ID-k
            for url, gid in group_pending_deletes:
                if gid in curr_ids_all_now:
                    continue
                last_ts = last_seen_ts.get(gid, 0.0)
                if (now2 - last_ts) >= DISAPPEAR_GRACE_SEC:
                    schedule_delete(gid)

            # MAIN-en eltűnt ID-k
            maybe_gone_main = [aid for aid in list(active_ids) if id_source.get(aid) == 'main' and aid not in curr_ids_main]
            now_ts2 = time.time()
            gone_main_ids = set()
            for gid in maybe_gone_main:
                last_ts = last_seen_ts.get(gid, 0.0)
                if (now_ts2 - last_ts) >= DISAPPEAR_GRACE_SEC:
                    schedule_delete(gid)
                    gone_main_ids.add(gid)
            
            # Eltűnt ID-k eltávolítása az OPEN_TASKS sorból
            if gone_main_ids:
                remove_gone_ids_from_open_tasks(gone_main_ids)

            # TABOK BEZÁRÁSA
            for url in next_to_close:
                info = next_tabs.get(url)
                handle = info.get("handle") if info else None
                if handle:
                    CLOSING_HANDLES.add(handle)  # Jelzés, hogy bezárás alatt van
                try:
                    if info and handle and handle in driver.window_handles:
                        driver.switch_to.window(handle)
                        driver.close()
                except Exception:
                    pass
                finally:
                    next_tabs.pop(url, None)
                    if handle:
                        CLOSING_HANDLES.discard(handle)  # Eltávolítás bezárás után
                    try:
                        if MAIN_HANDLE and MAIN_HANDLE in driver.window_handles:
                            driver.switch_to.window(MAIN_HANDLE)
                    except Exception:
                        pass

            for url in group_to_close:
                close_group_tab(url)
                block_group_url(url, GROUP_REOPEN_BACKOFF_SEC, "empty(<=1)-close")

            # Ciklus végén mindig flush-oljuk, ami a threshold alatt maradt
            # (BOOTSTRAP alatt ezek üresek, mert UPDATE/DELETE gatingel)
            flush_pending_updates()
            flush_pending_deletes()

            # Periodikusan mentjük a runtime state-et (minden 60 mp-ben)
            try:
                if not hasattr(process_dispatcher_results, '_last_state_save'):
                    process_dispatcher_results._last_state_save = time.time()
                
                if (time.time() - process_dispatcher_results._last_state_save) >= 60:
                    current_session_minutes = (time.time() - SESSION_START_TIME) / 60.0
                    total_runtime = accumulated_runtime_minutes + current_session_minutes
                    runtime_state["accumulated_minutes"] = accumulated_runtime_minutes
                    runtime_state["last_session_start"] = SESSION_START_TIME
                    save_runtime_state(runtime_state)
                    process_dispatcher_results._last_state_save = time.time()
            except Exception:
                pass

            # ✅ ACCOUNT ROTÁCIÓ: perzisztens időzítéssel
            if ACCOUNT_ROTATE_MIN > 0:
                # Jelenlegi session futásideje percben
                current_session_minutes = (time.time() - SESSION_START_TIME) / 60.0
                # Összes akkumulált futásidő
                total_runtime_minutes = accumulated_runtime_minutes + current_session_minutes
                
                if total_runtime_minutes >= ACCOUNT_ROTATE_MIN:
                    next_key = get_next_account_key(ACTIVE_ACCOUNT_KEY)
                    log(f"♻️ {total_runtime_minutes:.1f} perc akkumulált futásidő (limit: {ACCOUNT_ROTATE_MIN:.1f}) → váltás {ACTIVE_ACCOUNT_KEY} → {next_key}")
                    
                    # A restart_with_account() fogja nullázni a számlálót
                    restart_with_account(next_key)

            # 🔴 NAV worker indítása – CSAK BOOTSTRAP UTÁN
            if not nav_started and not bootstrap:
                nav_thread = threading.Thread(target=background_nav_worker, daemon=True)
                nav_thread.start()
                log("🚀 NAV háttér worker elindítva (BOOTSTRAP után)")
                nav_started = True

            prev_ids_main = curr_ids_main
            
            # 📊 Loop timing diagnostic
            loop_duration = time.time() - loop_start_time
            DIAG_LOGGER.log_loop_timing(loop_duration)
            
            # 📈 Queue status periodic logging
            if DIAG_LOGGER.loop_iteration % 20 == 0:  # Minden 20. iterációnál
                try:
                    open_tasks_len = len(OPEN_TASKS) if OPEN_TASKS else 0
                    active_ids_count = len(active_ids) if active_ids else 0
                    DIAG_LOGGER.log_queue_status(open_tasks=open_tasks_len)
                    log(f"📊 Active IDs: {active_ids_count}")
                except Exception:
                    pass
            
            time.sleep(CHECK_INTERVAL)

    except KeyboardInterrupt:
        warn("🛑 Leállítva.")
        DIAG_LOGGER.log_milestone("Application stopped by user (KeyboardInterrupt)")
    except WebDriverException as e:
        msg_lower = str(e).lower()
        if "invalid session" in msg_lower or "chrome not reachable" in msg_lower:
            warn(f"❌ Critical WebDriver error in main loop: {e}")
            DIAG_LOGGER.log_crash_context(e, "WEBDRIVER_CRASH")
            restart_application()
        else:
            warn(f"⚠️ WebDriverException in main loop (not restarting): {e}")
            DIAG_LOGGER.log_event("ERROR", f"WebDriverException (not critical): {str(e)[:150]}", "WARN")
    finally:
        try:
            flush_pending_updates()
            flush_pending_deletes()
            process_dispatcher_results(max_items=1000)
            dispatcher.stop()
        except Exception:
            pass
        try:
            driver.quit()
        except Exception:
            pass

        driver = None