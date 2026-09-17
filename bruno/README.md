# Bruno Collection: Drive Handover API

This directory contains the Git-native **Bruno** API collection for testing and validating the **File and Document Handover API** in La Suite Drive.

---

## 1. Quick Start

### Step 1: Start Backend Services
From `drive_forks/` (or repository root):
```bash
docker compose up -d
```

### Step 2: Seed Handover Demo Data
Run the idempotent handover seeding command:
```bash
make handover-demo
# or inside the app container:
# docker compose exec app-dev python manage.py create_handover_demo
```

### Step 3: Open in Bruno Client or Run in CLI

#### Option A: Bruno Desktop App
1. Open the [Bruno](https://www.usebruno.com/) desktop client.
2. Select **Open Collection** and choose the `drive_forks/bruno` folder.
3. Select the **Local Dev** environment from the top-right environment selector.

### Step 3: Open in Bruno Client or Run in CLI

#### Option A: Bruno Desktop App
1. Open the [Bruno](https://www.usebruno.com/) desktop client.
2. Select **Open Collection** and choose the `drive_forks/bruno` folder.
3. Select the **Local Dev** environment from the top-right environment selector.

#### Option B: Bruno CLI (Headless / Automated Tests)
Run from `drive_forks/bruno`:
```bash
# Run all 18 requests across all folders
npx @usebruno/cli run --env "Local Dev"
```

---

## 2. Test Personas

| Persona | Email | Fixed UUID | `is_staff` | Role / Privileges |
| :--- | :--- | :--- | :---: | :--- |
| **Manager** | `manager@example.com` | `021d6063-a251-472a-919e-325565b35c49` | `True` | Staff user with supervisory rights (`IsManagerOf`). |
| **Subordinate** | `subordinate@example.com` | `2c6c1f9f-9b0a-46b9-be85-d63983d1a750` | `False` | Departing user whose items are audited and transferred. |
| **Recipient 1 (Alice)** | `drive@drive.world` | `2d915b4b-a763-4190-83a9-7380982d561e` | `False` | Successor colleague 1. |
| **Recipient 2 (Bob)** | `bob.colleague@example.com` | `2d915b4b-a763-4190-83a9-7380982d562e` | `False` | Successor colleague 2. |
| **Recipient 3 (Charlie)**| `charlie.colleague@example.com` | `2d915b4b-a763-4190-83a9-7380982d563e` | `False` | Successor colleague 3. |

---

## 3. Test Requests & Workflow

### Folder `1-Auth/`
- **`1.1 Login as Manager`**: Authenticates as `manager@example.com` via `/api/v1.0/e2e/user-auth/`. Captures session ID and CSRF token and sets `cookie_header`, `session_cookie`, and `csrf_token`.

### Folder `2-Audit/`
- **`2.1 Handover Audit (as Manager) [200 OK]`**:
  - `GET /api/v1.0/users/{{subordinate_id}}/handover/audit/`
  - Returns `200 OK` with summary metrics (`total_items`, `sole_owner_count`, `total_bytes`, `skipped_unshared_count`, `skipped_trash_count`) and full item tree with breadcrumbs and staggered timestamps.
- **`2.2 Anti-Self Audit Attempt [403 Forbidden]`**:
  - `GET /api/v1.0/users/{{manager_id}}/handover/audit/`
  - Returns `403 Forbidden` (`IsManagerOf` rejects managers auditing themselves).

### Folder `3-Inspect-Items/`
- **`3.1 List Subordinate Live Items`**: Queries live items visible to subordinate.
- **`3.2 List Subordinate Trashbin`**: Queries soft-deleted items in trashbin.

### Folder `4-Transfer/`
Covers the transfer and dry-run workflow:
- **`4.1 Transfer Dry-Run (Preview) [200 OK]`**:
  - `POST /api/v1.0/users/{{subordinate_id}}/handover/transfer/` with `dry_run: true` and `reallocate_storage_quota: true`.
  - Simulates the transfer without DB commit, returning projected quota, warnings (`role_upgrade`, `already_owner`), and `can_execute: true`.
- **`4.2 Anti-Self Transfer Dry-Run [200 OK - Blocked]`**:
  - Simulates transfer where `recipient_id == subordinate_id`. Returns `200 OK` with `can_execute: false` and error `invalid_recipient`.
- **`4.3 Anti-Self Transfer Execution [400 Bad Request]`**:
  - Tries to execute transfer where `recipient_id == subordinate_id` with `dry_run: false`. Rejects with `400 Bad Request`.
- **`4.4 Missing Recipient Attempt [400 Bad Request]`**:
  - Omits `recipient_id`. Rejects with `400 Bad Request`.
- **`4.5 Missing Item IDs Attempt [400 Bad Request]`**:
  - Omits `item_ids`. Rejects with `400 Bad Request`.
- **`4.6 Execute Transfer [200 OK]`**:
  - Real transfer execution (`dry_run: false`) reallocating quota and transferring ownership to Alice.
- **`4.7 Handover Audit Post-Transfer [200 OK]`**:
  - Re-audits the subordinate to verify that `sole_owner_count` is updated.

### Folder `5-Personas/`
- **`5.1 Login as Subordinate`**: Authenticates as `subordinate@example.com`.
- **`5.2 Subordinate Audit Attempt [403 Forbidden]`**: Verifies that a subordinate cannot audit themselves.
- **`5.3 Subordinate Transfer Attempt [403 Forbidden]`**: Verifies that non-staff users cannot execute transfers.
- **`5.4 Login as Recipient 1 (Alice)`**: Authenticates as `drive@drive.world`.
- **`5.5 Login as Recipient 2 (Bob)`**: Authenticates as `bob.colleague@example.com`.
- **`5.6 Login as Recipient 3 (Charlie)`**: Authenticates as `charlie.colleague@example.com`.

---

## 4. Key Notes & Conventions

- **CSRF & Cookies**: In Django, authenticated POST requests require both the session cookie and the CSRF token cookie (`Cookie: drive_sessionid=...; csrftoken=...`) along with the header `X-CSRFToken: <token>`.
- **Idempotency**: Always run `make handover-demo` to ensure a fresh, consistent dataset.
