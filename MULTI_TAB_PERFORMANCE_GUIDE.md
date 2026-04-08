# Performance Optimization Guide for Many Tabs (40-72 tabs)

## 🎯 Goal
Make the script run well with 40-50-72 tabs open, **WITHOUT** closing tabs or limiting tab count.

---

## 🚀 Chrome/Selenium Optimizations

### 1. Chrome Command-Line Arguments

**Memory and Performance:**

```python
chrome_options.add_argument("--disable-dev-shm-usage")  # Avoid shared memory issues
chrome_options.add_argument("--disable-gpu")  # Disable GPU acceleration
chrome_options.add_argument("--no-sandbox")  # Disable sandbox (faster)
chrome_options.add_argument("--disable-web-security")  # Faster loading
chrome_options.add_argument("--disable-features=IsolateOrigins,site-per-process")  # Fewer processes
chrome_options.add_argument("--disable-setuid-sandbox")  # Additional sandbox disable
```

**Background Tab Optimization:**
```python
chrome_options.add_argument("--aggressive-cache-discard")  # Aggressive cache cleanup
chrome_options.add_argument("--aggressive-tab-discard")  # Background tab memory release
chrome_options.add_argument("--disable-background-timer-throttling")  # Disable timer throttling
chrome_options.add_argument("--disable-backgrounding-occluded-windows")  # Background optimization
chrome_options.add_argument("--disable-renderer-backgrounding")  # Renderer optimization
```

**Network and Resource Optimization:**
```python
chrome_options.add_argument("--disable-extensions")  # No extension overhead
chrome_options.add_argument("--disable-plugins")  # Disable Flash, PDF, etc.
chrome_options.add_argument("--disable-images")  # Disable image loading (if not needed)
chrome_options.add_argument("--blink-settings=imagesEnabled=false")  # Alternative image disable
```

**Memory Limit Increase:**
```python
chrome_options.add_argument("--max-old-space-size=4096")  # 4GB heap for JavaScript
chrome_options.add_argument("--js-flags=--max-old-space-size=4096")  # V8 heap size
```

### 2. Chrome Preferences Optimization

```python
prefs = {
    # Disable downloads
    "download.default_directory": "/dev/null",
    "download.prompt_for_download": False,
    "download_restrictions": 3,
    
    # Notifications and popups
    "profile.default_content_setting_values.notifications": 2,
    "profile.default_content_setting_values.popups": 2,
    
    # Disable auto-updates
    "credentials_enable_service": False,
    "profile.password_manager_enabled": False,
    
    # Background apps
    "background_mode.enabled": False,
    
    # Disable prefetch (memory saving)
    "dns_prefetching.enabled": False,
    "prefetch.enabled": False,
    
    # Hardware acceleration off
    "hardware_acceleration_mode.enabled": False,
}
chrome_options.add_experimental_option("prefs", prefs)
```

---

## 💾 Operating System Level Optimizations

### Windows Specific

**1. Increase Virtual Memory (Pagefile):**
```
1. System Properties → Advanced → Performance Settings
2. Advanced → Virtual Memory → Change
3. Set: Initial size: 8192 MB, Maximum: 16384 MB
4. Restart
```

**2. Increase Process Priority:**
```python
# Add to script beginning
import psutil
import os

# Increase Python process priority
p = psutil.Process(os.getpid())
p.nice(psutil.HIGH_PRIORITY_CLASS)  # Windows
# or
p.nice(-10)  # Linux (lower = higher priority)
```

**3. Increase Chrome Process Priority:**
```python
import subprocess
import time

def set_chrome_priority():
    """Increase Chrome process priority"""
    try:
        # Find all chrome processes
        for proc in psutil.process_iter(['pid', 'name']):
            if 'chrome' in proc.info['name'].lower():
                p = psutil.Process(proc.info['pid'])
                p.nice(psutil.ABOVE_NORMAL_PRIORITY_CLASS)  # Windows
    except Exception as e:
        print(f"Priority setting error: {e}")

# Call after login
time.sleep(5)  # Wait for Chrome to start
set_chrome_priority()
```

**4. Power Plan:**
```
Control Panel → Power Options → High Performance
```

### Linux Specific

**1. Increase Swap Space:**
```bash
# Check current
free -h

# Create swap file (8GB)
sudo fallocate -l 8G /swapfile
sudo chmod 600 /swapfile
sudo mkswap /swapfile
sudo swapon /swapfile

# Make permanent
echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
```

**2. Increase File Descriptor Limit:**
```bash
# Temporary
ulimit -n 65536

# Permanent: /etc/security/limits.conf
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

## 🐍 Python Code Level Optimizations

### 1. Increase Selenium Timeouts

```python
# Increase current values
RESOLVE_TIMEOUT = 3.0  # Instead of 1.5
CDP_POLL_INTERVAL = 0.5  # Instead of 0.4 (less polling)
PAIR_TIMEOUT_SEC = 25  # Instead of 17

# WebDriverWait timeouts
WebDriverWait(driver, 30)  # Instead of 10-15
```

### 2. Optimize Tab State Management

```python
# Tab state cache
_tab_state_cache = {}
_cache_ttl = 5  # seconds

def get_tab_state_cached(tab_id):
    """Get cached tab state"""
    now = time.time()
    if tab_id in _tab_state_cache:
        state, timestamp = _tab_state_cache[tab_id]
        if now - timestamp < _cache_ttl:
            return state
    
    # Only query if needed
    state = get_tab_state(tab_id)
    _tab_state_cache[tab_id] = (state, now)
    return state
```

### 3. Optimize Batch Operations

```python
# Increase batch sizes
UPDATE_BATCH_MAX = 100  # Instead of 50
DELETE_BATCH_MAX = 100  # Instead of 50

# Increase flush time
UPDATE_BATCH_FLUSH_SEC = 2.0  # Instead of 1.2
DELETE_BATCH_FLUSH_SEC = 2.5  # Instead of 1.5
```

### 4. CDP Communication Optimization

```python
# Lazy loading - only communicate via CDP when necessary
def get_tbody_ids_lazy(tab_handles):
    """Only query tbody elements when changes detected"""
    # Implement change detection
    # Example: hash tab URL, only re-query if changed
    pass

# Batch CDP commands
def execute_cdp_batch(commands):
    """Execute multiple CDP commands at once"""
    results = []
    for cmd in commands:
        results.append(driver.execute_cdp_cmd(cmd['method'], cmd['params']))
    return results
```

### 5. Memory Management

```python
import gc

def periodic_garbage_collection():
    """Regular garbage collection"""
    gc.collect()  # Manual GC call

# Call periodically (e.g., every 1000 operations)
operation_counter = 0
if operation_counter % 1000 == 0:
    periodic_garbage_collection()
```

---

## 🔧 Chrome Driver Optimizations

### 1. Keep-Alive Connection

```python
# Use HTTP keep-alive
chrome_options.add_argument("--enable-features=NetworkService,NetworkServiceInProcess")
```

### 2. Page Load Strategy

```python
# Faster page load
chrome_options.page_load_strategy = 'eager'  # or 'none'
# 'none' = doesn't wait for full load
# 'eager' = waits for DOM but not resources
# 'normal' = waits for everything (default, slow)
```

### 3. Headless Mode (optional)

```python
# If you don't need to see the browser
chrome_options.add_argument("--headless=new")  # New headless mode
chrome_options.add_argument("--window-size=1920,1080")
```

---

## 📊 Monitoring and Diagnostics

### 1. Memory Monitoring

```python
import psutil

def log_memory_usage():
    """Log memory usage"""
    process = psutil.Process()
    mem_info = process.memory_info()
    
    print(f"📊 Memory: {mem_info.rss / 1024 / 1024:.0f} MB")
    print(f"📊 Threads: {process.num_threads()}")
    print(f"📊 Open files: {len(process.open_files())}")

# Call regularly
if time.time() % 60 < 1:  # Every minute
    log_memory_usage()
```

### 2. Tab Statistics

```python
def log_tab_statistics():
    """Log tab statistics"""
    try:
        all_handles = driver.window_handles
        print(f"🔢 Total tabs: {len(all_handles)}")
        
        # Memory per tab
        for handle in all_handles:
            driver.switch_to.window(handle)
            # Log info...
    except Exception as e:
        print(f"Tab stats error: {e}")
```

---

## ⚡ Additional Tips

### 1. Use SSD
- Profile directory and cache on SSD
- Swap also on SSD (if possible)

### 2. RAM Increase
- Ideal: 16-32 GB RAM
- Minimum: 8 GB RAM + large swap

### 3. Multi-Core CPU
- Chrome uses many processes
- 4+ core CPU recommended

### 4. Network Optimization
```python
# Increase timeouts
chrome_options.add_argument("--disk-cache-size=0")  # No disk cache
chrome_options.add_argument("--media-cache-size=0")  # No media cache
```

### 5. Stable Network Connection
- Use Ethernet cable instead of WiFi
- Avoid network timeouts

---

## 🎯 Summary - Quick Checklist

**Chrome Optimizations:**
- [ ] `--disable-dev-shm-usage`
- [ ] `--aggressive-tab-discard`
- [ ] `--max-old-space-size=4096`
- [ ] `page_load_strategy = 'eager'`

**System Optimizations:**
- [ ] Increase virtual memory/swap
- [ ] Increase process priority
- [ ] High Performance power plan

**Code Optimizations:**
- [ ] Increase timeouts
- [ ] Increase batch sizes
- [ ] Use cache for tab state
- [ ] Periodic GC

**Hardware:**
- [ ] Use SSD
- [ ] 16+ GB RAM
- [ ] 4+ core CPU

---

## 📝 Notes

- Not all optimizations are needed at once
- Start with Chrome arguments
- Gradually increase timeouts
- Monitor memory usage
- Test different configurations

**Important:** These methods **INCREASE** system capacity for tab handling **WITHOUT** closing tabs or limiting their count.
