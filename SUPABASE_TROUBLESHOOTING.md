# Supabase SDK Troubleshooting Guide

## Problem: Import Error Despite Installation

If you see this error despite having Supabase installed:
```
⚠️ Supabase SDK import failed: [error message]
```

This guide will help you fix it.

---

## Quick Diagnosis

Run the script and look at the **actual error message** shown. The new version shows exactly what's wrong.

---

## Common Issues & Solutions

### 1. "No module named 'supabase'"

**Cause:** Supabase not installed for the Python version you're using.

**Solution:**
```bash
# Use the SAME Python version as the script
py -3.11 -m pip install supabase

# Or if using python directly:
python3.11 -m pip install supabase
```

**Verify:**
```bash
py -3.11 -c "from supabase import create_client; print('Success!')"
```

---

### 2. "No module named 'httpx'" or other dependency

**Cause:** Supabase installed but dependencies are broken.

**Solution:**
```bash
# Uninstall completely
py -3.11 -m pip uninstall supabase -y

# Reinstall fresh with all dependencies
py -3.11 -m pip install supabase

# Or force reinstall:
py -3.11 -m pip install --force-reinstall supabase
```

---

### 3. "cannot import name 'create_client'"

**Cause:** Old version of Supabase installed.

**Solution:**
```bash
# Upgrade to latest version
py -3.11 -m pip install --upgrade supabase

# Or install specific version:
py -3.11 -m pip install "supabase>=2.0.0"
```

**Check version:**
```bash
py -3.11 -m pip show supabase | grep Version
```

---

### 4. Multiple Python Installations

**Cause:** Supabase installed to different Python than script uses.

**Diagnosis:**
```bash
# List all Python versions
py --list

# Check where supabase is installed
py -3.11 -m pip show supabase
```

**Solution:**
```bash
# Install to EXACT Python version used by script
py -3.11 -m pip install supabase

# If script uses different version, adjust accordingly
py -3.10 -m pip install supabase  # Example for 3.10
```

---

### 5. Permission Issues (Windows)

**Cause:** Installing to protected location without admin rights.

**Solution:**
```bash
# Install for current user only
py -3.11 -m pip install --user supabase
```

---

### 6. Corporate Proxy/Firewall

**Cause:** Network restrictions blocking pip.

**Solution:**
```bash
# Use proxy
py -3.11 -m pip install --proxy http://proxy:port supabase

# Or use trusted host
py -3.11 -m pip install --trusted-host pypi.org --trusted-host files.pythonhosted.org supabase
```

---

## Verification Steps

### Step 1: Check Installation
```bash
py -3.11 -m pip show supabase
```

**Expected output:**
```
Name: supabase
Version: 2.x.x
Location: [path]
Requires: httpx, python-dotenv, ...
```

### Step 2: Test Import
```bash
py -3.11 -c "from supabase import create_client; print('✅ Supabase SDK working!')"
```

**Expected output:**
```
✅ Supabase SDK working!
```

### Step 3: Run Script
```bash
py -3.11 ArbifyBeta.py
```

**Expected output:**
```
✅ Supabase SDK loaded successfully
```

---

## Still Not Working?

### Collect Debug Information

1. **Python version:**
   ```bash
   py -3.11 --version
   ```

2. **Pip version:**
   ```bash
   py -3.11 -m pip --version
   ```

3. **Installed packages:**
   ```bash
   py -3.11 -m pip list | grep supabase
   ```

4. **Full error from script:**
   ```bash
   py -3.11 ArbifyBeta.py 2>&1 | head -20
   ```

5. **Test import directly:**
   ```bash
   py -3.11 -c "import sys; print(sys.path); from supabase import create_client"
   ```

Share this information for further help.

---

## Alternative: Manual Installation

If pip isn't working, download and install manually:

```bash
# Download supabase and dependencies
git clone https://github.com/supabase-community/supabase-py
cd supabase-py
py -3.11 setup.py install
```

---

## Fallback: Use Without Supabase

The script works without Supabase SDK! It will use HTTP endpoints instead:

```
⚠️ Supabase SDK import failed: ...
   Database reconciliation will use fallback method.
```

**Impact:**
- Database reconciliation uses API endpoint (404 if not implemented)
- Everything else works normally
- Slightly slower but functional

---

## Summary

| Issue | Command | Expected Result |
|-------|---------|-----------------|
| Not installed | `py -3.11 -m pip install supabase` | ✅ Installed |
| Broken dependencies | `py -3.11 -m pip install --force-reinstall supabase` | ✅ Fixed |
| Old version | `py -3.11 -m pip install --upgrade supabase` | ✅ Updated |
| Wrong Python | `py -3.11 -m pip install supabase` | ✅ Correct version |
| Verify | `py -3.11 -c "from supabase import create_client"` | ✅ No error |

---

**After fixing, you should see: `✅ Supabase SDK loaded successfully`** 🎉
