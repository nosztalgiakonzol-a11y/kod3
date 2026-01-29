# API Specification: list-active-tips Endpoint

## Overview

This document describes the required API endpoint for the Database Reconciliation feature in the ArbifyBeta application.

## Problem Statement

The application needs to clean up orphaned records in the database after crashes. To do this, it needs to:
1. Get a list of ALL active tip IDs currently in the database
2. Compare with the local source of truth (`active_ids.txt` file)
3. Delete any IDs that exist in the database but not in the local file

## Required Endpoint

### Endpoint Details

- **URL:** `POST /functions/v1/list-active-tips`
- **Base URL:** `https://sonudgyyvxncdcganppl.supabase.co`
- **Full URL:** `https://sonudgyyvxncdcganppl.supabase.co/functions/v1/list-active-tips`
- **Method:** POST
- **Timeout:** 30 seconds

### Request

#### Headers
```http
Content-Type: application/json
apikey: {SUPABASE_ANON_KEY}
Authorization: Bearer {SUPABASE_ANON_KEY}
X-Correlation-Id: {uuid}
```

#### Body
```json
{}
```

**Note:** Empty JSON body. No parameters required.

### Response

#### Success Response (HTTP 200)

```json
{
  "ok": true,
  "ids": [
    "JGsEIiqTLxc",
    "KnJpGip3WlI",
    "II3dFCqBibs",
    "..."
  ],
  "correlation_id": "4ab30334-0667-4745-999d-d3ca9fb26ffc"
}
```

**Fields:**
- `ok` (boolean, required): Success indicator. Must be `true` for successful response.
- `ids` (array of strings, required): Array of all active tip IDs in the database. Can be empty array if no tips exist.
- `correlation_id` (string, optional): For tracking/debugging. If not provided, client will use the X-Correlation-Id from request.

#### Error Response (HTTP 4xx/5xx)

```json
{
  "ok": false,
  "error": "error_code",
  "message": "Human readable error message",
  "correlation_id": "4ab30334-0667-4745-999d-d3ca9fb26ffc"
}
```

### Database Query Logic

The endpoint should query the tips table and return IDs for tips that are:
- Currently active (not deleted)
- Have `type != "gone"` or similar status field
- Are considered "live" records

**Example SQL (adjust based on your schema):**
```sql
SELECT id FROM tips 
WHERE deleted_at IS NULL 
  AND status = 'active'
ORDER BY created_at DESC;
```

## Usage in Application

### When It's Called

The endpoint is called **once per application startup**, specifically:
1. After bootstrap phase completes
2. After post-bootstrap cleanup runs
3. Before normal operations begin

### What Happens With The Data

```python
# 1. Call API
status, data = http_post(LIST_ACTIVE_TIPS_URL, {}, timeout=30)

# 2. Extract IDs
db_ids = set(data.get("ids", []))

# 3. Load local file
file_ids = set(active_ids)  # from active_ids.txt

# 4. Find orphans
orphan_ids = db_ids - file_ids

# 5. Delete orphans
for orphan_id in orphan_ids:
    dispatcher.enqueue_delete(orphan_id)
```

### Error Handling

If the endpoint returns non-200 or invalid data:
- Warning is logged: `⚠️ DB RECONCILIATION: nem sikerült lekérni az adatbázis ID-kat (status={status})`
- Reconciliation is skipped
- Application continues normal operation
- **No crash or failure** - gracefully handles missing endpoint

## Example Scenarios

### Scenario 1: No Orphans

**Database has:** `["id1", "id2", "id3"]`  
**File has:** `["id1", "id2", "id3"]`  
**Result:** `✨ DB RECONCILIATION: nincs orphan ID – adatbázis és active_ids.txt szinkronban van`

### Scenario 2: Orphans Found

**Database has:** `["id1", "id2", "id3", "id4", "id5"]`  
**File has:** `["id1", "id2", "id3"]`  
**Orphans:** `["id4", "id5"]`  
**Action:** Delete `id4` and `id5` using existing `delete-tip` endpoint  
**Result:** `✅ DB RECONCILIATION kész: 2 orphan ID törölve, 0 hiba`

### Scenario 3: After Crash

1. Application running with 100 tips in database and file
2. Application crashes, 20 tips disappear from file but remain in database
3. User restarts application
4. Reconciliation runs:
   - Gets 100 IDs from database
   - Loads 80 IDs from file
   - Finds 20 orphans
   - Deletes 20 orphaned records
5. Database and file are back in sync

## Security Considerations

### Authentication
- Uses existing Supabase authentication (apikey + Bearer token)
- Same auth as other endpoints (save-tip, delete-tip, etc.)

### Authorization
- Should only return tips for the authenticated user/account
- Follow same authorization logic as other tip endpoints

### Rate Limiting
- Called only once per application startup
- Low frequency (~1 call per 30-60 minutes)
- Should not trigger rate limits

## Performance Considerations

### Expected Load
- ~100-1000 tip IDs per response (typical)
- Could be up to several thousand in edge cases
- Response size: ~10-50 KB typically

### Query Performance
- Should be fast (< 1 second)
- Index on `id`, `status`, `deleted_at` fields recommended
- Consider pagination if returning > 10,000 IDs (unlikely)

### Timeout
- Client timeout: 30 seconds
- Endpoint should respond within 5-10 seconds ideally

## Testing

### Test Cases

#### Test 1: Empty Database
**Request:** `POST /functions/v1/list-active-tips` with `{}`  
**Expected:** `{"ok": true, "ids": []}`

#### Test 2: Single Record
**Setup:** One active tip in database  
**Request:** `POST /functions/v1/list-active-tips` with `{}`  
**Expected:** `{"ok": true, "ids": ["tip_id_123"]}`

#### Test 3: Multiple Records
**Setup:** 5 active tips in database  
**Request:** `POST /functions/v1/list-active-tips` with `{}`  
**Expected:** `{"ok": true, "ids": ["id1", "id2", "id3", "id4", "id5"]}`

#### Test 4: With Deleted Records
**Setup:** 3 active tips, 2 deleted tips in database  
**Request:** `POST /functions/v1/list-active-tips` with `{}`  
**Expected:** `{"ok": true, "ids": ["id1", "id2", "id3"]}` (only active)

### Manual Testing

```bash
curl -X POST \
  https://sonudgyyvxncdcganppl.supabase.co/functions/v1/list-active-tips \
  -H "Content-Type: application/json" \
  -H "apikey: YOUR_API_KEY" \
  -H "Authorization: Bearer YOUR_API_KEY" \
  -d '{}'
```

## Implementation Checklist

- [ ] Create Supabase Edge Function at `/functions/v1/list-active-tips`
- [ ] Implement database query to get all active tip IDs
- [ ] Return response in correct JSON format with `ok` and `ids` fields
- [ ] Add authentication/authorization checks
- [ ] Test with empty database
- [ ] Test with sample data
- [ ] Test with large dataset (1000+ records)
- [ ] Deploy to production
- [ ] Verify from frontend application

## Related Endpoints

For reference, here are the other tip-related endpoints that already exist:

- `POST /functions/v1/save-tip` - Create new tip
- `POST /functions/v1/update-tip` - Update existing tip
- `POST /functions/v1/delete-tip` - Delete tip (soft or hard delete)
- `POST /functions/v1/update-tips-batch` - Batch update tips
- `POST /functions/v1/delete-tips-batch` - Batch delete tips

The new `list-active-tips` endpoint should follow similar patterns for auth, error handling, and response format.

## Questions?

If you need clarification on:
- Database schema details
- Tip ID format/structure
- Authentication flow
- Any other aspect

Please reach out to the frontend development team.

---

**Document Version:** 1.0  
**Last Updated:** 2026-01-29  
**Contact:** Frontend Development Team
