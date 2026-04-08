# Teljesítmény Optimalizálás Sok Tab Kezelésére (40-72 tab)

## 🎯 Cél
A script jól fusson amikor 40-50-72 tab is nyitva van, **ANÉLKÜL** hogy tab-okat zárnánk be vagy korlátozná a tab számot.

---

## 🚀 Chrome/Selenium Optimalizációk

### 1. Chrome Command-Line Argumentumok Optimalizálása

**Memória és teljesítmény javítása:**

```python
chrome_options.add_argument("--disable-dev-shm-usage")  # Shared memory probléma elkerülése
chrome_options.add_argument("--disable-gpu")  # GPU acceleráció kikapcsolása (nem kell)
chrome_options.add_argument("--no-sandbox")  # Sandbox kikapcsolása (gyorsabb)
chrome_options.add_argument("--disable-web-security")  # Gyorsabb betöltés
chrome_options.add_argument("--disable-features=IsolateOrigins,site-per-process")  # Kevesebb process
chrome_options.add_argument("--disable-blink-features=AutomationControlled")  # Már van
chrome_options.add_argument("--disable-setuid-sandbox")  # További sandbox kikapcsolás
```

**Háttér tab optimalizálás:**
```python
chrome_options.add_argument("--aggressive-cache-discard")  # Agresszív cache tisztítás
chrome_options.add_argument("--aggressive-tab-discard")  # Háttérben levő tabok memória felszabadítás
chrome_options.add_argument("--disable-background-timer-throttling")  # Timer throttling ki
chrome_options.add_argument("--disable-backgrounding-occluded-windows")  # Háttér optimalizálás
chrome_options.add_argument("--disable-renderer-backgrounding")  # Renderer optimalizálás
```

**Network és resource optimalizálás:**
```python
chrome_options.add_argument("--disable-extensions")  # Nincs extension overhead
chrome_options.add_argument("--disable-plugins")  # Flash, PDF stb. ki
chrome_options.add_argument("--disable-images")  # Képek betöltés kikapcsolása (csak ha nem kell)
chrome_options.add_argument("--blink-settings=imagesEnabled=false")  # Képek kikapcsolása másik módszer
```

**Memory limit növelés:**
```python
chrome_options.add_argument("--max-old-space-size=4096")  # 4GB heap size JavaScript-nek
chrome_options.add_argument("--js-flags=--max-old-space-size=4096")  # V8 heap size
```

### 2. Chrome Preferences Optimalizálása

```python
prefs = {
    # Letöltések kikapcsolása
    "download.default_directory": "/dev/null",
    "download.prompt_for_download": False,
    "download_restrictions": 3,
    
    # Notifikációk és pop-upok
    "profile.default_content_setting_values.notifications": 2,
    "profile.default_content_setting_values.popups": 2,
    
    # Automatikus frissítések kikapcsolása
    "credentials_enable_service": False,
    "profile.password_manager_enabled": False,
    
    # Háttér alkalmazások
    "background_mode.enabled": False,
    
    # Prefetch kikapcsolása (memória spórolás)
    "dns_prefetching.enabled": False,
    "prefetch.enabled": False,
    
    # Hardware acceleration ki
    "hardware_acceleration_mode.enabled": False,
}
chrome_options.add_experimental_option("prefs", prefs)
```

---

## 💾 Operációs Rendszer Szintű Optimalizációk

### Windows Specifikus

**1. Virtual Memory (Pagefile) Növelése:**
```
1. System Properties → Advanced → Performance Settings
2. Advanced → Virtual Memory → Change
3. Állítsd be: Initial size: 8192 MB, Maximum: 16384 MB
4. Újraindítás
```

**2. Process Priority Növelése:**
```python
# A script elejére
import psutil
import os

# Python process priority növelése
p = psutil.Process(os.getpid())
p.nice(psutil.HIGH_PRIORITY_CLASS)  # Windows
# vagy
p.nice(-10)  # Linux (minél kisebb, annál magasabb prioritás)
```

**3. Chrome Process Priority Növelése:**
```python
import subprocess
import time

def set_chrome_priority():
    """Chrome processek prioritásának növelése"""
    try:
        # Találd meg az összes chrome process-t
        for proc in psutil.process_iter(['pid', 'name']):
            if 'chrome' in proc.info['name'].lower():
                p = psutil.Process(proc.info['pid'])
                p.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)  # Windows
    except Exception as e:
        print(f"Priority beállítás hiba: {e}")

# Hívd meg a login után
time.sleep(5)  # Várj amíg Chrome elindul
set_chrome_priority()
```

**4. Power Plan Módosítása:**
```
Control Panel → Power Options → High Performance
```

### Linux Specifikus

**1. Swap Space Növelése:**
```bash
# Ellenőrzés
free -h

# Swap file létrehozása (8GB)
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Permanens
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**2. File Descriptor Limit Növelése:**
```bash
# Ideiglenes
ulimit -n 65536

# Permanens: /etc/security/limits.conf
* soft nofile 65536
* hard nofile 65536
```

**3. System Resource Limits:**
```bash
# /etc/sysctl.conf
fs.file-max = 2097152
vm.max_map_count = 262144
vm.swappiness = 10
```

---

## 🐍 Python Kód Szintű Optimalizációk

### 1. Selenium Timeout-ok Növelése

```python
# Jelenleg használt értékek növelése
RESOLVE_TIMEOUT = 3.0  # 1.5 helyett
CDP_POLL_INTERVAL = 0.5  # 0.4 helyett (kevesebb polling)
PAIR_TIMEOUT_SEC = 25  # 17 helyett

# WebDriverWait timeout-ok
WebDriverWait(driver, 30)  # 10-15 helyett
```

### 2. Tab State Management Optimalizálása

```python
# Tab state cache
_tab_state_cache = {}
_cache_ttl = 5  # másodperc

def get_tab_state_cached(tab_id):
    """Cached tab state lekérdezése"""
    now = time.time()
    if tab_id in _tab_state_cache:
        state, timestamp = _tab_state_cache[tab_id]
        if now - timestamp < _cache_ttl:
            return state
    
    # Csak ha kell, kérdezd le
    state = get_tab_state(tab_id)
    _tab_state_cache[tab_id] = (state, now)
    return state
```

### 3. Batch Operations Optimalizálása

```python
# Növeld a batch méreteket
UPDATE_BATCH_MAX = 100  # 50 helyett
DELETE_BATCH_MAX = 100  # 50 helyett

# Csökkentsd a flush időt
UPDATE_BATCH_FLUSH_SEC = 2.0  # 1.2 helyett
DELETE_BATCH_FLUSH_SEC = 2.5  # 1.5 helyett
```

### 4. CDP Communication Optimalizálás

```python
# Lazy loading - csak akkor kommunikálj CDP-n ha muszáj
def get_tbody_ids_lazy(tab_handles):
    """Csak akkor kérdezd le a tbody-kat ha változás van"""
    # Implementálj change detection-t
    # Például: hash-eld a tab URL-jét, csak ha változik, kérdezd le újra
    pass

# Batch CDP parancsok
def execute_cdp_batch(commands):
    """Több CDP parancs egyben"""
    results = []
    for cmd in commands:
        results.append(driver.execute_cdp_cmd(cmd['method'], cmd['params']))
    return results
```

### 5. Memory Management

```python
import gc

def periodic_garbage_collection():
    """Rendszeres garbage collection"""
    gc.collect()  # Manual GC hívás

# Hívd meg időnként (pl. minden 1000 művelet után)
operation_counter = 0
if operation_counter % 1000 == 0:
    periodic_garbage_collection()
```

---

## 🔧 Chrome Driver Optimalizációk

### 1. Keep-Alive Connection

```python
# HTTP keep-alive használata
chrome_options.add_argument("--enable-features=NetworkService,NetworkServiceInProcess")
```

### 2. Page Load Strategy

```python
# Gyorsabb page load
chrome_options.page_load_strategy = 'eager'  # vagy 'none'
# 'none' = nem vár a teljes betöltésre
# 'eager' = vár a DOM-ra, de nem a resource-okra
# 'normal' = vár mindenre (default, lassú)
```

### 3. Headless Mode (opcionális)

```python
# Ha nem kell látni a böngészőt
chrome_options.add_argument("--headless=new")  # Új headless mode
chrome_options.add_argument("--window-size=1920,1080")
```

---

## 📊 Monitoring és Diagnosztika

### 1. Memory Monitoring

```python
import psutil

def log_memory_usage():
    """Memória használat logolása"""
    process = psutil.Process()
    mem_info = process.memory_info()
    
    print(f"📊 Memory: {mem_info.rss / 1024 / 1024:.0f} MB")
    print(f"📊 Threads: {process.num_threads()}")
    print(f"📊 Open files: {len(process.open_files())}")

# Hívd meg rendszeresen
if time.time() % 60 < 1:  # Minden percben
    log_memory_usage()
```

### 2. Tab Statistics

```python
def log_tab_statistics():
    """Tab statisztikák logolása"""
    try:
        all_handles = driver.window_handles
        print(f"🔢 Összes tab: {len(all_handles)}")
        
        # Memória per tab
        for handle in all_handles:
            driver.switch_to.window(handle)
            # Log info...
    except Exception as e:
        print(f"Tab stats hiba: {e}")
```

---

## ⚡ További Tippek

### 1. SSD Használata
- A profile directory és cache SSD-n legyen
- Swap is SSD-n (ha lehetséges)

### 2. RAM Növelés
- Ideális: 16-32 GB RAM
- Minimum: 8 GB RAM + nagy swap

### 3. Multi-Core CPU
- Chrome sok process-t használ
- 4+ core CPU ajánlott

### 4. Network Optimalizálás
```python
# Timeout-ok növelése
chrome_options.add_argument("--disk-cache-size=0")  # No disk cache
chrome_options.add_argument("--media-cache-size=0")  # No media cache
```

### 5. Stable Network Connection
- Használj Ethernet kábelt WiFi helyett
- Kerüld a network timeout-okat

---

## 🎯 Összefoglalás - Gyors Checklist

**Chrome Optimalizációk:**
- [ ] `--disable-dev-shm-usage`
- [ ] `--aggressive-tab-discard`
- [ ] `--max-old-space-size=4096`
- [ ] `page_load_strategy = 'eager'`

**System Optimalizációk:**
- [ ] Virtual memory/swap növelés
- [ ] Process priority növelés
- [ ] High Performance power plan

**Kód Optimalizációk:**
- [ ] Timeout-ok növelése
- [ ] Batch sizes növelése
- [ ] Cache használata tab state-re
- [ ] Periodic GC

**Hardware:**
- [ ] SSD használata
- [ ] 16+ GB RAM
- [ ] 4+ core CPU

---

## 📝 Megjegyzések

- Nem minden optimalizáció kell egyszerre
- Kezd a Chrome argumentumokkal
- Növeld fokozatosan a timeout-okat
- Monitor-ozd a memory használatot
- Tesztelj különböző konfigurációkkal

**Fontos:** Ezek a módszerek **NÖVELIK** a rendszer kapacitását tab kezelésre, **ANÉLKÜL** hogy tab-okat zárnánk be vagy korlátoznánk a számukat.

