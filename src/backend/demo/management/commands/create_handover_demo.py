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

from io import BytesIO

from django.conf import settings
from django.contrib.auth.hashers import make_password
from django.core.files.storage import default_storage
from django.core.management.base import BaseCommand, CommandError

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
    "colleague": {
        "id": "2d915b4b-a763-4190-83a9-7380982d561e",
        "email": "drive@drive.world",
        "full_name": "Drive Collaborator",
        "short_name": "Drive",
        "is_staff": True,
    },
}

DEMO_ITEM_PREFIX = "a0000000-0000-0000-0000-"


def _get_or_create_user(user_data):
    """Ensure user exists with fixed deterministic UUID and credentials."""
    user = models.User.objects.filter(email=user_data["email"]).first()
    if user:
        update_fields = []
        if user_data.get("is_staff") and not user.is_staff:
            user.is_staff = True
            update_fields.append("is_staff")
        if user.full_name != user_data["full_name"]:
            user.full_name = user_data["full_name"]
            user.short_name = user_data["short_name"]
            update_fields.extend(["full_name", "short_name"])
        if update_fields:
            user.save(update_fields=update_fields)
        return user

    kwargs = {
        "admin_email": user_data["email"],
        "email": user_data["email"],
        "sub": user_data["email"],
        "full_name": user_data["full_name"],
        "short_name": user_data["short_name"],
        "password": make_password("drive"),
        "is_superuser": False,
        "is_active": True,
        "is_staff": user_data.get("is_staff", False),
    }
    if "id" in user_data:
        kwargs["id"] = user_data["id"]

    return models.User.objects.create(**kwargs)


def _make_file(
    title,
    filename,
    mimetype,
    size,
    parent=None,
    creator=None,
    users=None,
    item_id=None,
):
    """Helper to create a ready file with storage entry."""
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
    )
    content = f"Dummy content for {filename}".encode("utf-8")
    default_storage.save(file_item.file_key, BytesIO(content))
    return file_item


def create_handover_demo(stdout=None):
    """Seed handover demo data for frontend and API testing."""
    log = stdout.write if stdout else print

    log(" [1/3] Provisioning deterministic handover users in DB...")
    manager = _get_or_create_user(HANDOVER_USERS["manager"])
    subordinate = _get_or_create_user(HANDOVER_USERS["subordinate"])
    colleague = _get_or_create_user(HANDOVER_USERS["colleague"])
    log(f"  [✓] Manager:     {manager.email} (ID: {manager.id})")
    log(f"  [✓] Subordinate: {subordinate.email} (ID: {subordinate.id})")
    log(f"  [✓] Colleague:   {colleague.email} (ID: {colleague.id})")

    log(" [2/3] Resetting previous test items for idempotency...")
    # Delete previous test items matching the demo prefix as well as legacy IDs
    legacy_ids = [
        "11111111-1111-1111-1111-111111111111",
        "11111111-1111-1111-1111-222222222222",
        "22222222-2222-2222-2222-111111111111",
        "33333333-3333-3333-3333-111111111111",
        "44444444-4444-4444-4444-111111111111",
        "55555555-5555-5555-5555-111111111111",
        "66666666-6666-6666-6666-111111111111",
    ]
    models.Item.objects.filter(id__in=legacy_ids).delete()
    models.Item.objects.filter(id__startswith=DEMO_ITEM_PREFIX).delete()

    log(" [3/3] Creating rich folder & file hierarchy (in English)...")

    # Shared with colleague as reader (subordinate is sole owner)
    shared_reader = [
        (subordinate, models.RoleChoices.OWNER),
        (colleague, models.RoleChoices.READER),
    ]
    # Co-owned with colleague (subordinate is NOT sole owner)
    co_owned = [
        (subordinate, models.RoleChoices.OWNER),
        (colleague, models.RoleChoices.OWNER),
    ]
    # Subordinate admin, colleague owner
    sub_admin = [
        (colleague, models.RoleChoices.OWNER),
        (subordinate, models.RoleChoices.ADMIN),
    ]

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
    )

    # --- CASE 1: Empty Folder (Depth 1) ---
    factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000010",
        title="1_Empty_Folder",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )

    # --- CASE 2: Diverse & Long filenames (Depth 1 + children) ---
    folder_2 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000020",
        title="2_Diverse_And_Long_Filenames",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
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
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000022",
        title="financial_forecast_2026.csv",
        filename="financial_forecast_2026.csv",
        mimetype="text/csv",
        size=154000,
        parent=folder_2,
        creator=subordinate,
        users=shared_reader,
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000023",
        title="organization_brand_logo.png",
        filename="organization_brand_logo.png",
        mimetype="image/png",
        size=450000,
        parent=folder_2,
        creator=subordinate,
        users=shared_reader,
    )

    # --- CASE 3: Deep Tree (Depth 1 -> 2 -> 3 -> 4) ---
    folder_3 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000030",
        title="3_Deep_Folder_Hierarchy",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    subfolder_3a = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000031",
        title="Subfolder_A",
        parent=folder_3,
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
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
    )
    subfolder_3b = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000033",
        title="Subfolder_B",
        parent=folder_3,
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    confidential_archives = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000034",
        title="Confidential_Archives",
        parent=subfolder_3b,
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
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
    )

    # --- CASE 4: Co-Owned Projects (is_sole_owner=False) ---
    folder_4 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000040",
        title="4_Ongoing_Projects",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=co_owned,
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
    )

    # --- CASE 5: Subordinate is Admin (Colleague Owner) ---
    folder_5 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000050",
        title="5_Design_Resources",
        type=models.ItemTypeChoices.FOLDER,
        creator=colleague,
        users=sub_admin,
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
    )

    # --- CASE 6: Empty Archive Folder ---
    factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000060",
        title="6_Empty_Archive_Folder",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )

    # --- CASE 7: Personal Records (Privacy test scenario) ---
    folder_7 = factories.ItemFactory(
        id=f"{DEMO_ITEM_PREFIX}000000000070",
        title="7_PERSONAL_Pay_Slips_And_Private_Records",
        type=models.ItemTypeChoices.FOLDER,
        creator=subordinate,
        users=shared_reader,
    )
    _make_file(
        item_id=f"{DEMO_ITEM_PREFIX}000000000071",
        title="january_2026_salary_statement.pdf",
        filename="january_2026_salary_statement.pdf",
        mimetype="application/pdf",
        size=120000,
        parent=folder_7,
        creator=subordinate,
        users=shared_reader,
    )

    log("  [✓] All folder structures and files generated successfully in English.")
    log(f"\nAudit Endpoint : /api/v1.0/users/{subordinate.id}/handover/audit/")


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
