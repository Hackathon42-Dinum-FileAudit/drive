"""create_handover_demo management command.

Creates deterministic users, permissions, and file scenarios for testing the
Handover / Offboarding Audit API in La Suite Drive.
All names, titles, and comments are in English for open source consistency.

How to run:
    Via Makefile (recommended):
        make handover-demo

    Via Docker Compose:
        docker compose exec app-dev python manage.py create_handover_demo

    Directly in local shell:
        python manage.py create_handover_demo [-f]

Testing the seeded audit endpoint:
    1. Authenticate as manager:
        curl -c cookies.txt -X POST http://localhost:8071/api/v1.0/e2e/user-auth/ \
            -H "Content-Type: application/json" -d '{"email": "manager@example.com"}'

    2. Query audit endpoint for departing user:
        SUB_ID="2c6c1f9f-9b0a-46b9-be85-d63983d1a750"
        curl -b cookies.txt http://localhost:8071/api/v1.0/users/$SUB_ID/handover/audit/
"""

from datetime import timedelta
from io import BytesIO

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from core import factories, models

HANDOVER_USERS = {
    "manager": {
        "id": "021d6063-a251-472a-919e-325565b35c49",
        "email": "manager@example.com",
        "full_name": "Line Manager (Auditor)",
        "short_name": "Manager",
        "is_staff": True,
    },
    "subordinate": {
        "id": "2c6c1f9f-9b0a-46b9-be85-d63983d1a750",
        "email": "subordinate@example.com",
        "full_name": "John Doe (Departing Employee)",
        "short_name": "John",
        "is_staff": False,
    },
    # 3 Successor / Recipient colleagues for UI testing:
    "colleague": {  # Recipient 1 (Alice / existing drive dev user)
        "id": "2d915b4b-a763-4190-83a9-7380982d561e",
        "email": "drive@drive.world",
        "full_name": "Alice Martin (Successor 1)",
        "short_name": "Alice",
        "is_staff": False,
    },
    "recipient_2": {  # Recipient 2 (Bob)
        "id": "2d915b4b-a763-4190-83a9-7380982d562e",
        "email": "bob.colleague@example.com",
        "full_name": "Bob Dupont (Successor 2)",
        "short_name": "Bob",
        "is_staff": False,
    },
    "recipient_3": {  # Recipient 3 (Charlie)
        "id": "2d915b4b-a763-4190-83a9-7380982d563e",
        "email": "charlie.colleague@example.com",
        "full_name": "Charlie Leroy (Successor 3)",
        "short_name": "Charlie",
        "is_staff": False,
    },
    "outsider": {  # External user not belonging to manager's team
        "id": "2d915b4b-a763-4190-83a9-7380982d569e",
        "email": "outsider@example.com",
        "full_name": "Outsider User",
        "short_name": "Outsider",
        "is_staff": False,
    },
}

DEMO_ITEM_PREFIX = "a0000000-0000-0000-0000-"


def _reset_or_create_user(user_data):
    """Ensure user exists with fixed deterministic UUID and credentials, reset to clean state."""
    user = models.User.objects.filter(email=user_data["email"]).first()
    target_is_staff = user_data.get("is_staff", False)
    if user:
        user.is_staff = target_is_staff
        user.full_name = user_data["full_name"]
        user.short_name = user_data["short_name"]
        user.is_superuser = False
        user.is_active = True
        user.password = make_password("drive")
        if "id" in user_data:
            user.sub = str(user_data["id"])
        user.save(
            update_fields=[
                "is_staff",
                "full_name",
                "short_name",
                "is_superuser",
                "is_active",
                "password",
                "sub",
            ]
        )
        return user

    kwargs = {
        "admin_email": user_data["email"],
        "email": user_data["email"],
        "sub": str(user_data["id"]) if "id" in user_data else user_data["email"],
        "full_name": user_data["full_name"],
        "short_name": user_data["short_name"],
        "password": make_password("drive"),
        "is_superuser": False,
        "is_active": True,
        "is_staff": target_is_staff,
    }
    if "id" in user_data:
        kwargs["id"] = user_data["id"]

    return models.User.objects.create(**kwargs)


def _set_timestamps(item, created_at=None, updated_at=None):
    """Directly update created_at and updated_at to bypass auto_now/auto_now_add for demo."""
    update_kwargs = {}
    if created_at is not None:
        update_kwargs["created_at"] = created_at
    if updated_at is not None:
        update_kwargs["updated_at"] = updated_at
    if update_kwargs:
        models.Item.objects.filter(id=item.id).update(**update_kwargs)
        item.refresh_from_db()
    return item


def _make_file(  # noqa: PLR0913
    title,
    filename,
    mimetype,
    size,
    parent=None,
    creator=None,
    users=None,
    item_id=None,
    deleted_at=None,
    created_at=None,
    updated_at=None,
):
    """Helper to create a ready file with storage entry using ItemFactory."""
    file_item = factories.ItemFactory(
        id=item_id,
        parent=parent,
        title=title,
        filename=filename,
        type=models.ItemTypeChoices.FILE,
        creator=creator,
        mimetype=mimetype,
        update_upload_state=models.ItemUploadStateChoices.READY,
        size=size,
        users=users or [],
        deleted_at=deleted_at,
    )
    content = f"Dummy content for {filename}".encode("utf-8")
    default_storage.save(file_item.file_key, BytesIO(content))

    if created_at is not None or updated_at is not None:
        _set_timestamps(file_item, created_at=created_at, updated_at=updated_at)

    return file_item


def create_handover_demo(stdout=None):  # noqa: PLR0915
    """Seed handover demo data for frontend and API testing."""
    log = stdout.write if stdout else print

    log(" [1/3] Resetting previous handover demo data (files & users)...")
    # Delete all items matching demo prefix
    models.Item.objects.filter(id__startswith=DEMO_ITEM_PREFIX).delete()
    # Clean up any residual items created by handover users
    user_emails = [u["email"] for u in HANDOVER_USERS.values()]
    models.Item.objects.filter(creator__email__in=user_emails).delete()

    # Reset all users to pristine state
    users = {}
    for key, user_data in HANDOVER_USERS.items():
        users[key] = _reset_or_create_user(user_data)
        log(f"  [✓] {user_data['full_name']:<30} {user_data['email']:<30} (ID: {users[key].id})")

    subordinate = users["subordinate"]
    colleague = users["colleague"]
    recipient_2 = users["recipient_2"]
    recipient_3 = users["recipient_3"]

    log(" [2/3] Creating rich folder & file hierarchy (in English)...")

    # Shared with Recipient 1 (Alice) as reader (subordinate is sole owner)
    shared_reader = [
        (subordinate, models.RoleChoices.OWNER),
        (colleague, models.RoleChoices.READER),
    ]
    # Shared with Recipient 2 (Bob)
    shared_bob_reader = [
        (subordinate, models.RoleChoices.OWNER),
        (recipient_2, models.RoleChoices.READER),
    ]
    # Shared with all 3 successor candidates
    shared_team = [
        (subordinate, models.RoleChoices.OWNER),
        (colleague, models.RoleChoices.READER),
        (recipient_2, models.RoleChoices.READER),
        (recipient_3, models.RoleChoices.READER),
    ]
    # Co-owned with colleague Alice (subordinate is NOT sole owner)
    co_owned = [
        (subordinate, models.RoleChoices.OWNER),
        (colleague, models.RoleChoices.OWNER),
    ]
    # Co-owned with Recipient 2 Bob (subordinate is NOT sole owner)
    co_owned_bob = [
        (subordinate, models.RoleChoices.OWNER),
        (recipient_2, models.RoleChoices.OWNER),
    ]
    # Co-owned with Alice and Bob (3 co-owners)
    co_owned_trio = [
        (subordinate, models.RoleChoices.OWNER),
        (colleague, models.RoleChoices.OWNER),
        (recipient_2, models.RoleChoices.OWNER),
    ]
    # Co-owned with all successors (4 co-owners)
    co_owned_team = [
        (subordinate, models.RoleChoices.OWNER),
        (colleague, models.RoleChoices.OWNER),
        (recipient_2, models.RoleChoices.OWNER),
        (recipient_3, models.RoleChoices.OWNER),
    ]
    # Subordinate admin, colleague owner
    sub_admin = [
        (colleague, models.RoleChoices.OWNER),
        (subordinate, models.RoleChoices.ADMIN),
    ]

    now = timezone.now()

    # --- CASE 0: File at ROOT level (Depth 1, No parent) ---
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000001",
        title="root_notes.txt",
        filename="root_notes.txt",
        mimetype="text/plain",
        size=2048,
        parent=None,
        creator=subordinate,
        users=shared_reader,
        created_at=now - timedelta(days=2),
        updated_at=now - timedelta(hours=3),
    )

    # --- CASE 1: Empty Folder (Depth 1) ---
    folder_1 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000010",
        title="1_Empty_Folder",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    _set_timestamps(
        folder_1,
        created_at=now - timedelta(days=140),
        updated_at=now - timedelta(days=140),
    )

    # --- CASE 2: Diverse & Long filenames (Depth 1 + children) ---
    folder_2 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000020",
        title="2_Diverse_And_Long_Filenames",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    _set_timestamps(
        folder_2,
        created_at=now - timedelta(days=90),
        updated_at=now - timedelta(days=2),
    )
    long_name = (
        "annual_review_meeting_minutes_with_an_intentionally_long_filename"
        "_to_test_ui_table_cell_truncation.pdf"
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000021",
        title=long_name,
        filename=long_name,
        mimetype="application/pdf",
        size=2450000,
        parent=folder_2,
        creator=subordinate,
        users=shared_reader,
        created_at=now - timedelta(days=90),
        updated_at=now - timedelta(days=30),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000022",
        title="financial_forecast_2026.csv",
        filename="financial_forecast_2026.csv",
        mimetype="text/csv",
        size=154000,
        parent=folder_2,
        creator=subordinate,
        users=shared_bob_reader,
        created_at=now - timedelta(days=14),
        updated_at=now - timedelta(days=2),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000023",
        title="organization_brand_logo.png",
        filename="organization_brand_logo.png",
        mimetype="image/png",
        size=450000,
        parent=folder_2,
        creator=subordinate,
        users=shared_team,
        created_at=now - timedelta(days=240),
        updated_at=now - timedelta(days=240),
    )

    # --- CASE 3: Deep Tree (Depth 1 -> 2 -> 3 -> 4) ---
    folder_3 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000030",
        title="3_Deep_Folder_Hierarchy",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    _set_timestamps(
        folder_3,
        created_at=now - timedelta(days=365),
        updated_at=now - timedelta(days=4),
    )
    subfolder_3a = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000031",
        title="Subfolder_A",
        parent=folder_3,
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    _set_timestamps(
        subfolder_3a,
        created_at=now - timedelta(days=300),
        updated_at=now - timedelta(days=4),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000032",
        title="client_pitch_deck.pptx",
        filename="client_pitch_deck.pptx",
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size=18500000,
        parent=subfolder_3a,
        creator=subordinate,
        users=shared_reader,
        created_at=now - timedelta(days=300),
        updated_at=now - timedelta(days=4),
    )
    subfolder_3b = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000033",
        title="Subfolder_B",
        parent=folder_3,
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    _set_timestamps(
        subfolder_3b,
        created_at=now - timedelta(days=365),
        updated_at=now - timedelta(days=150),
    )
    confidential_archives = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000034",
        title="Confidential_Archives",
        parent=subfolder_3b,
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    _set_timestamps(
        confidential_archives,
        created_at=now - timedelta(days=365),
        updated_at=now - timedelta(days=150),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000035",
        title="infrastructure_database_backup_v1.zip",
        filename="infrastructure_database_backup_v1.zip",
        mimetype="application/zip",
        size=125000000,
        parent=confidential_archives,
        creator=subordinate,
        users=shared_reader,
        created_at=now - timedelta(days=365),
        updated_at=now - timedelta(days=365),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000036",
        title="executive_committee_audio_recording.mp3",
        filename="executive_committee_audio_recording.mp3",
        mimetype="audio/mpeg",
        size=35200000,
        parent=confidential_archives,
        creator=subordinate,
        users=shared_reader,
        created_at=now - timedelta(days=180),
        updated_at=now - timedelta(days=150),
    )

    # --- CASE 4: Co-Owned Projects (is_sole_owner=False) ---
    folder_4 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000040",
        title="4_Ongoing_Projects",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=co_owned,
    )
    _set_timestamps(
        folder_4,
        created_at=now - timedelta(days=21),
        updated_at=now - timedelta(hours=6),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000041",
        title="project_alpha_planning_tracker.xlsx",
        filename="project_alpha_planning_tracker.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=780000,
        parent=folder_4,
        creator=subordinate,
        users=co_owned,
        created_at=now - timedelta(days=21),
        updated_at=now - timedelta(days=1),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000042",
        title="project_beta_technical_specifications.docx",
        filename="project_beta_technical_specifications.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=1100000,
        parent=folder_4,
        creator=subordinate,
        users=co_owned,
        created_at=now - timedelta(days=7),
        updated_at=now - timedelta(hours=6),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000043",
        title="inter_ministerial_roadmap_2026.pptx",
        filename="inter_ministerial_roadmap_2026.pptx",
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size=3400000,
        parent=folder_4,
        creator=subordinate,
        users=co_owned_bob,
        created_at=now - timedelta(days=14),
        updated_at=now - timedelta(days=2),
    )
    subfolder_4a = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000044",
        title="Shared_Deliverables",
        parent=folder_4,
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=co_owned_trio,
    )
    _set_timestamps(
        subfolder_4a,
        created_at=now - timedelta(days=18),
        updated_at=now - timedelta(days=3),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000045",
        title="sprint_deliverables_signoff.pdf",
        filename="sprint_deliverables_signoff.pdf",
        mimetype="application/pdf",
        size=1200000,
        parent=subfolder_4a,
        creator=subordinate,
        users=co_owned_trio,
        created_at=now - timedelta(days=18),
        updated_at=now - timedelta(days=3),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000046",
        title="joint_procurement_contract.docx",
        filename="joint_procurement_contract.docx",
        mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        size=2400000,
        parent=subfolder_4a,
        creator=subordinate,
        users=co_owned_team,
        created_at=now - timedelta(days=10),
        updated_at=now - timedelta(days=1),
    )

    # --- CASE 5: Subordinate is Admin (Colleague Owner) ---
    folder_5 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000050",
        title="5_Design_Resources",
        type=models.ItemTypeChoices.FOLDER,
        creator=colleague,
        users=sub_admin,
    )
    _set_timestamps(
        folder_5,
        created_at=now - timedelta(days=120),
        updated_at=now - timedelta(days=14),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000051",
        title="web_portal_ui_mockup_v2.svg",
        filename="web_portal_ui_mockup_v2.svg",
        mimetype="image/svg+xml",
        size=85000,
        parent=folder_5,
        creator=colleague,
        users=sub_admin,
        created_at=now - timedelta(days=120),
        updated_at=now - timedelta(days=14),
    )

    # --- CASE 6: Unshared Personal Folder (Subordinate sole owner, zero collaborators) ---
    folder_6 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000060",
        title="6_Unshared_Personal_Folder",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=[(subordinate, models.RoleChoices.OWNER)],
    )
    _set_timestamps(
        folder_6,
        created_at=now - timedelta(days=270),
        updated_at=now - timedelta(days=240),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000061",
        title="personal_expenses_receipts.pdf",
        filename="personal_expenses_receipts.pdf",
        mimetype="application/pdf",
        size=180000,
        parent=folder_6,
        creator=subordinate,
        users=[(subordinate, models.RoleChoices.OWNER)],
        created_at=now - timedelta(days=270),
        updated_at=now - timedelta(days=240),
    )

    # --- CASE 7: Soft-Deleted Folder in Trashbin (Archival retention scenario) ---
    deleted_date = now - timedelta(days=7)
    folder_7 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000070",
        title="7_Old_Drafts_In_Trashbin",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        deleted_at=deleted_date,
        users=shared_reader,
    )
    _set_timestamps(
        folder_7,
        created_at=now - timedelta(days=730),
        updated_at=deleted_date,
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000071",
        title="discarded_meeting_notes_2025.odt",
        filename="discarded_meeting_notes_2025.odt",
        mimetype="application/vnd.oasis.opendocument.text",
        size=320000,
        parent=folder_7,
        creator=subordinate,
        users=shared_reader,
        deleted_at=deleted_date,
        created_at=now - timedelta(days=730),
        updated_at=deleted_date,
    )

    # --- CASE 8: Multi-Owner Department Workspace (4 co-owners + diverse subfiles) ---
    folder_8 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000080",
        title="8_CoOwned_Department_Workspace",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=co_owned_team,
    )
    _set_timestamps(
        folder_8,
        created_at=now - timedelta(days=60),
        updated_at=now - timedelta(days=3),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000081",
        title="department_annual_budget_2026.xlsx",
        filename="department_annual_budget_2026.xlsx",
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        size=1850000,
        parent=folder_8,
        creator=subordinate,
        users=co_owned,
        created_at=now - timedelta(days=60),
        updated_at=now - timedelta(days=5),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000082",
        title="vendor_service_level_agreement_sla.pdf",
        filename="vendor_service_level_agreement_sla.pdf",
        mimetype="application/pdf",
        size=3200000,
        parent=folder_8,
        creator=subordinate,
        users=co_owned_bob,
        created_at=now - timedelta(days=45),
        updated_at=now - timedelta(days=12),
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000083",
        title="cross_team_strategic_initiatives.pptx",
        filename="cross_team_strategic_initiatives.pptx",
        mimetype="application/vnd.openxmlformats-officedocument.presentationml.presentation",
        size=8900000,
        parent=folder_8,
        creator=subordinate,
        users=co_owned_trio,
        created_at=now - timedelta(days=30),
        updated_at=now - timedelta(days=3),
    )

    log("  [✓] All folder structures and files generated successfully in English.")
    log(f"\nAudit Endpoint : /api/v1.0/users/{subordinate.id}/handover/audit/")
    log("\nFrontend Personas & Login Credentials (password for all is 'drive'):")
    log("  - Manager:     manager@example.com           (Line Manager / Auditor)")
    log("  - Subordinate: subordinate@example.com       (Departing User)")
    log("  - Recipient 1: drive@drive.world             (Alice Martin / Successor 1)")
    log("  - Recipient 2: bob.colleague@example.com     (Bob Dupont / Successor 2)")
    log("  - Recipient 3: charlie.colleague@example.com (Charlie Leroy / Successor 3)")
    log("\nQuick Frontend Login:")
    log("  In your browser console at http://localhost:3000 (or http://localhost:8071):")
    log("    await fetch('http://localhost:8071/api/v1.0/e2e/user-auth/', {")
    log("      method: 'POST',")
    log("      headers: {'Content-Type': 'application/json'},")
    log("      body: JSON.stringify({email: 'manager@example.com'}),")
    log("      credentials: 'include'")
    log("    }); location.reload();\n")


class Command(BaseCommand):
    """A management command to create handover demo data."""

    help = __doc__

    def add_arguments(self, parser):
        parser.add_argument(
            "-f",
            "--force",
            action="store_true",
            default=False,
            help="Force command execution despite DEBUG is set to False",
        )

    def handle(self, *args, **options):
        if not settings.DEBUG and not options["force"]:
            raise CommandError(
                "This command is only meant for development environments. Use -f to force."
            )
        create_handover_demo(self.stdout)
