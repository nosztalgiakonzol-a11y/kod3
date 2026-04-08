# Database Reconciliation API - Magyar Összefoglaló

## Probléma

Az ArbifyBeta alkalmazás a következő hibát adja induláskor:
```
⚠️ DB RECONCILIATION: nem sikerült lekérni az adatbázis ID-kat (status=404)
```

## Mit csinál a Database Reconciliation funkció?

### Probléma amit megold:
1. Ha a script crashel, az adatbázisban maradnak "árva" (orphan) rekordok
2. A helyi `active_ids.txt` fájl tartalmazza a helyes állapotot
3. Az adatbázisban sokkal több rekord van, mint kellene

### Megoldás:
- Minden indításkor lekérdezi az összes adatbázisbeli tip ID-t
- Összehasonlítja az `active_ids.txt` fájllal
- Törli azokat az ID-kat, amik az adatbázisban vannak, de a fájlban nincsenek

## Mi kell a backend oldalon?

### Új Endpoint létrehozása

**URL:** `POST /functions/v1/list-active-tips`

**Teljes cím:**
```
https://sonudgyyvxncdcganppl.supabase.co/functions/v1/list-active-tips
```

### Kérés (Request)

**Body:** Üres JSON
```json
{}
```

**Headers:** (ugyanaz mint más endpoint-oknál)
```
Content-Type: application/json
apikey: {SUPABASE_ANON_KEY}
Authorization: Bearer {SUPABASE_ANON_KEY}
```

### Válasz (Response)

**Sikeres válasz (HTTP 200):**
```json
{
  "ok": true,
  "ids": ["JGsEIiqTLxc", "KnJpGip3WlI", "II3dFCqBibs", "..."],
  "correlation_id": "4ab30334-0667-4745-999d-d3ca9fb26ffc"
}
```

**Mezők:**
- `ok`: boolean, kötelező, legyen `true`
- `ids`: string tömb, kötelező, az összes aktív tip ID
- `correlation_id`: string, opcionális, debug célra

### Mit kell visszaadni?

Az endpoint-nak vissza kell adnia **az összes aktív tip ID-t** az adatbázisból.

**Példa SQL query (igazítsd a sémához):**
```sql
SELECT id FROM tips 
WHERE deleted_at IS NULL 
  AND status = 'active'
ORDER BY created_at DESC;
```

## Példa használat

### 1. Alkalmazás indítása után

**Lekérés:**
```
POST /functions/v1/list-active-tips
Body: {}
```

**Válasz:**
```json
{
  "ok": true,
  "ids": ["id1", "id2", "id3", "id4", "id5"]
}
```

### 2. Összehasonlítás

- **Adatbázisban:** `["id1", "id2", "id3", "id4", "id5"]`
- **active_ids.txt-ben:** `["id1", "id2", "id3"]`
- **Árva (orphan) ID-k:** `["id4", "id5"]`

### 3. Törlés

Az alkalmazás automatikusan törli `id4` és `id5` rekordokat a meglévő `delete-tip` endpoint-tal.

### 4. Eredmény

```
✅ DB RECONCILIATION kész: 2 orphan ID törölve, 0 hiba
```

## Mi történik jelenleg (404 hiba esetén)?

- Figyelmeztetés kerül a logba
- A reconciliation ki van hagyva
- Az alkalmazás normálisan folytatja a működést
- **Nincs crash vagy hiba** - csak árva rekordok maradnak

## Mi történik majd az endpoint megléte után?

- Automatikus takarítás minden indításnál
- Az adatbázis tiszta és szinkronban marad
- Nincs szükség manuális tisztításra
- Crash után automatikus helyreállítás

## Biztonsági szempontok

### Authentikáció
- Ugyanaz mint a többi endpoint (save-tip, delete-tip, stb.)
- apikey + Bearer token

### Authorizáció
- Csak az adott user/account tip-jeit adja vissza
- Ugyanaz a logika mint más tip endpoint-oknál

## Teljesítmény

### Várható terhelés
- Hívási gyakoriság: ~1x / 30-60 perc (indításkor)
- Válasz méret: ~10-50 KB tipikusan (100-1000 ID)
- Válaszidő: < 1 másodperc ideális
- Timeout: 30 másodperc (kliens oldal)

## Tesztelés

### Manuális teszt cURL-lel:

```bash
curl -X POST \
  https://sonudgyyvxncdcganppl.supabase.co/functions/v1/list-active-tips \
  -H "Content-Type: application/json" \
  -H "apikey: YOUR_API_KEY" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{}'
```

### Teszt esetek:

1. **Üres adatbázis** → `{"ok": true, "ids": []}`
2. **1 rekord** → `{"ok": true, "ids": ["id1"]}`
3. **5 rekord** → `{"ok": true, "ids": ["id1", "id2", "id3", "id4", "id5"]}`
4. **Törölt rekordokkal** → csak az aktívakat adja vissza

## Implementációs checklist

- [ ] Supabase Edge Function létrehozása `/functions/v1/list-active-tips` útvonalon
- [ ] Adatbázis query implementálása (összes aktív tip ID)
- [ ] JSON válasz formázása (`ok`, `ids`, `correlation_id` mezőkkel)
- [ ] Auth/authorization ellenőrzés
- [ ] Tesztelés üres adatbázissal
- [ ] Tesztelés mintaadatokkal
- [ ] Tesztelés nagy adathalmazral (1000+ rekord)
- [ ] Deploy production-re
- [ ] Ellenőrzés a frontend alkalmazásból

## További információ

Részletes angol nyelvű specifikáció: **API_SPECIFICATION.md** fájl a repository-ban.

## Kapcsolódó endpoint-ok (referencia)

Meglévő tip endpoint-ok:
- `POST /functions/v1/save-tip` - Új tip létrehozása
- `POST /functions/v1/update-tip` - Tip frissítése
- `POST /functions/v1/delete-tip` - Tip törlése
- `POST /functions/v1/update-tips-batch` - Batch frissítés
- `POST /functions/v1/delete-tips-batch` - Batch törlés

Az új `list-active-tips` endpoint ugyanazt a mintát kövesse (auth, error handling, stb.)

## Kérdések?

Ha bármi nem világos, kérdezz!

---

**Dokumentum verzió:** 1.0  
**Utoljára frissítve:** 2026-01-29
