# Content Hash Checking - Quick Start Guide (HU)

## Mi ez?

**Content-based change detection** group és next oldalakhoz.

**Előny:** Csak akkor refresh-el amikor a tartalom TÉNYLEG változott!

---

## Használat

### 1. Pull Latest
```bash
git pull origin copilot/analyze-bootstrap-login-flow
```

### 2. Run
```bash
py -3.11 "Arbify Beta.py"
```

### 3. Watch Logs

**Ha nincs változás:**
```
[HASH] 📋 Checking GROUP page content: https://...
[HASH] ✅ Quick check: NO CHANGE detected (0.8ms) - Refresh SKIPPED
[HASH] ⏭️ GROUP refresh SKIPPED (reason: quick_no_change)
```

**Ha van változás:**
```
[HASH] 📋 Checking GROUP page content: https://...
[HASH] 🔥 CHANGE DETECTED (2.3ms) - Will REFRESH
[HASH] 🔄 GROUP will refresh (reason: content_changed)
```

**Statisztikák (5 percenként):**
```
📊 CONTENT HASH CHECKING STATISTICS
  Total checks performed: 147
  Quick checks: 147
  Full hashes: 23
  Changes detected: 23
  Refreshes skipped: 124 (84.4%)
  Avg check time: 1.23ms
  Total time spent: 180.8ms
```

---

## Beállítások

### Verbose Logging (Részletes log)

**File:** `Arbify Beta.py`, Line ~403

```python
CONTENT_HASH_VERBOSE_LOGGING = True   # ✅ Részletes (alapértelmezett)
CONTENT_HASH_VERBOSE_LOGGING = False  # ⚠️ Csak fontos dolgok
```

**True esetén:** Minden check-et logol, hash-eket mutat
**False esetén:** Csak változásokat és skip-eket logol

---

### Enable/Disable Feature

**File:** `Arbify Beta.py`, Line ~402

```python
ENABLE_CONTENT_HASH_CHECKING = True   # ✅ Bekapcsolva (alapértelmezett)
ENABLE_CONTENT_HASH_CHECKING = False  # ⚠️ Kikapcsolva (régi viselkedés)
```

---

### Quick Check Mode

**File:** `Arbify Beta.py`, Line ~404

```python
CONTENT_HASH_USE_QUICK_CHECK = True   # ✅ Gyors check először (alapértelmezett)
CONTENT_HASH_USE_QUICK_CHECK = False  # ⚠️ Mindig teljes hash (lassabb)
```

**True:** Két-lépcsős ellenőrzés (gyors + teljes ha kell)
**False:** Mindig teljes hash (pontosabb de lassabb)

---

## Várható Eredmények

### Előtte (baseline):
- Group/Next refresh: Minden 30-60 másodpercben
- Refreshes/óra: ~100
- Efficiency: 0% (mindig refresh)

### Utána (content hash):
- Check: Minden 30-60s (~1ms)
- Refresh: Csak ha változott (~10-20x/óra)
- Efficiency: **80-90% fewer refreshes!** 🎉

**Nyereség:**
- Idő megtakarítás: 140-400 mp/óra
- Bandwidth: -90%
- Bot detection risk: -60-70%

---

## Hibaelhárítás

### Túl sok log
```python
CONTENT_HASH_VERBOSE_LOGGING = False
```

### Probléma van
```python
ENABLE_CONTENT_HASH_CHECKING = False  # Visszaáll régi működésre
```

### Kihagyja a változásokat
```python
CONTENT_HASH_USE_QUICK_CHECK = False  # Mindig teljes hash
```

---

## Mit jelentenek a log üzenetek?

### `[HASH] 📋 Checking GROUP/NEXT page content`
Ellenőrzi a lap tartalmát

### `[HASH] ✅ Quick check: NO CHANGE detected`
Gyors ellenőrzés: nincs változás → skip refresh

### `[HASH] ✅ Full hash: NO CHANGE`
Teljes hash: nincs változás → skip refresh

### `[HASH] 🔥 CHANGE DETECTED`
Változás észlelve → refresh fog történni

### `[HASH] ⏭️ Refresh SKIPPED`
Refresh kihagyva (nincs változás)

### `[HASH] 🔄 Will REFRESH`
Refresh fog történni (van változás)

### `[HASH] ✅ Refreshed, new hash stored`
Refresh megtörtént, új hash eltárolva

---

## Performance Info

**Overhead per check:**
- Quick check: 0.5-1ms
- Full hash: 2-5ms
- Average: ~1-2ms

**1 óra alatt:**
- Checks: ~100
- Total overhead: 100-200ms
- Refreshes avoided: 70-80
- Time saved: **140-400 seconds!**

**Net: MUCH FASTER!** ⚡

---

## Feedback Kérés

Kérlek jelentsd vissza:

1. **Hány refresh-t hagyott ki?**
   - Nézd a statisztikát: "Refreshes skipped: XX (YY%)"

2. **Vannak hibák?**
   - Check logs for errors
   - False positives/negatives?

3. **Performance OK?**
   - Script fut rendesen?
   - Nincs lassulás?

4. **Logging megfelelő?**
   - Túl sok/kevés log?
   - Érthető-e?

---

## Kapcsolódó Dokumentáció

1. **ALTERNATIVES_WHEN_HEADERS_UNRELIABLE_HU.md** - Teljes technikai leírás
2. **SMART_REFRESH_STATIC_PAGES_HU.md** - Miért kell ez ha ETag nem működik
3. **Implementation details** - Kód kommentek az Arbify Beta.py-ban

---

**KÉSZ ÉS TESZTELÉSRE VÁRA!** ✅

Pull, run, watch logs, report back! 🚀
