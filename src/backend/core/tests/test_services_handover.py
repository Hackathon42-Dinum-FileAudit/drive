"""Tests for the handover service and handover audit API."""

from django.utils import timezone

import pytest
from rest_framework.test import APIClient

from core import factories, models
from core.services import handover

pytestmark = pytest.mark.django_db


def test_build_user_handover_audit_summary_metrics():
    """Verify that build_user_handover_audit correctly compiles shared and skipped metrics."""
    departing_user = factories.UserFactory()
    colleague = factories.UserFactory()

    # 1. Shared item (creator=departing_user, colleague has access) -> INCLUDED in audit
    shared_item = factories.ItemFactory(
        creator=departing_user,
        size=1000,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    # 2. Unshared item (creator=departing_user, no other users) -> SKIPPED (unshared)
    factories.ItemFactory(
        creator=departing_user,
        size=5000,
        users=[(departing_user, models.RoleChoices.OWNER)],
    )

    # 3. Trashbin item (soft-deleted) -> SKIPPED (trash)
    factories.ItemFactory(
        creator=departing_user,
        size=2000,
        deleted_at=timezone.now(),
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    audit = handover.build_user_handover_audit(departing_user)
    summary = audit["summary"]

    assert summary["total_items"] == 1
    assert summary["total_bytes"] == 6000  # Sum of non-deleted created items (1000 + 5000)
    assert summary["shared_count"] == 1
    assert summary["sole_owner_count"] == 1
    assert summary["skipped_unshared_count"] == 1
    assert summary["skipped_unshared_bytes"] == 5000
    assert summary["skipped_trash_count"] == 1
    assert summary["skipped_trash_bytes"] == 2000

    assert len(audit["items"]) == 1
    assert audit["items"][0]["id"] == str(shared_item.id)


def test_build_user_handover_audit_multiowner_and_sole_owner():
    """Verify that multi-owner and admin-only items have is_sole_owner=False."""
    departing_user = factories.UserFactory()
    colleague = factories.UserFactory()
    successor_2 = factories.UserFactory()

    # 1. Sole owner: departing_user is OWNER, colleague is READER
    sole_item = factories.ItemFactory(
        creator=departing_user,
        size=1000,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    # 2. Multi-owner: departing_user is OWNER, colleague is also OWNER
    co_owned_item = factories.ItemFactory(
        creator=departing_user,
        size=2000,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.OWNER),
        ],
    )

    # 3. Multi-owner (trio): departing_user is OWNER, colleague and successor_2 are OWNERS
    trio_owned_item = factories.ItemFactory(
        creator=departing_user,
        size=3000,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.OWNER),
            (successor_2, models.RoleChoices.OWNER),
        ],
    )

    # 4. Colleague is OWNER, departing_user is ADMINISTRATOR (not owner)
    admin_item = factories.ItemFactory(
        creator=colleague,
        size=4000,
        users=[
            (colleague, models.RoleChoices.OWNER),
            (departing_user, models.RoleChoices.ADMIN),
        ],
    )

    audit = handover.build_user_handover_audit(departing_user)
    items_by_id = {it["id"]: it for it in audit["items"]}

    assert audit["summary"]["total_items"] == 4
    assert audit["summary"]["sole_owner_count"] == 1
    assert audit["summary"]["shared_count"] == 4

    assert items_by_id[str(sole_item.id)]["is_sole_owner"] is True
    assert items_by_id[str(co_owned_item.id)]["is_sole_owner"] is False
    assert items_by_id[str(trio_owned_item.id)]["is_sole_owner"] is False
    assert items_by_id[str(admin_item.id)]["is_sole_owner"] is False


def test_api_users_handover_audit_as_staff_manager():
    """Staff manager should successfully query subordinate handover audit."""
    manager = factories.UserFactory(is_staff=True)
    subordinate = factories.UserFactory()
    colleague = factories.UserFactory()

    factories.ItemFactory(
        creator=subordinate,
        size=1024,
        users=[
            (subordinate, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    client = APIClient()
    client.force_login(manager)

    response = client.get(f"/api/v1.0/users/{subordinate.id}/handover/audit/")
    assert response.status_code == 200
    data = response.json()

    assert data["departing_user"]["id"] == str(subordinate.id)
    assert data["summary"]["total_items"] == 1
    assert data["summary"]["skipped_unshared_count"] == 0
    assert data["summary"]["skipped_trash_count"] == 0
    assert len(data["items"]) == 1


def test_api_users_handover_audit_anti_self_forbidden():
    """Users cannot audit or initiate a handover for themselves."""
    user = factories.UserFactory(is_staff=True)

    client = APIClient()
    client.force_login(user)

    response = client.get(f"/api/v1.0/users/{user.id}/handover/audit/")
    assert response.status_code == 403


def test_api_users_handover_audit_anonymous_unauthorized():
    """Anonymous callers cannot access the handover audit endpoint."""
    user = factories.UserFactory()

    client = APIClient()
    response = client.get(f"/api/v1.0/users/{user.id}/handover/audit/")
    assert response.status_code == 401


def test_simulate_handover_transfer_requires_item_ids():
    """Simulation requires a non-empty list of item IDs."""
    departing_user = factories.UserFactory()
    recipient = factories.UserFactory()

    simulation = handover.simulate_handover_transfer(
        departing_user=departing_user,
        recipient=recipient,
        item_ids=[],
    )
    assert simulation["can_execute"] is False
    assert any(err["code"] == "missing_item_ids" for err in simulation["errors"])


def test_simulate_handover_transfer_selective_and_folder_cascading():
    """Simulation includes selected items and automatically cascades to folder descendants."""
    departing_user = factories.UserFactory()
    colleague = factories.UserFactory()
    recipient = factories.UserFactory()

    # Item 1: Root folder with 1 child file
    folder_1 = factories.ItemFactory(
        creator=departing_user,
        type=models.ItemTypeChoices.FOLDER,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )
    child_file = factories.ItemFactory(
        parent=folder_1,
        creator=departing_user,
        type=models.ItemTypeChoices.FILE,
        size=1024,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    # Item 2: Unrelated root file (not selected)
    unrelated_file = factories.ItemFactory(
        creator=departing_user,
        type=models.ItemTypeChoices.FILE,
        size=2048,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    # Manager selects ONLY folder_1
    simulation = handover.simulate_handover_transfer(
        departing_user=departing_user,
        recipient=recipient,
        item_ids=[str(folder_1.id)],
    )

    assert simulation["can_execute"] is True
    # Both folder_1 and its child_file are included in simulation
    assert simulation["summary"]["items_count"] == 2
    item_ids_in_sim = {item["id"] for item in simulation["items"]}
    assert str(folder_1.id) in item_ids_in_sim
    assert str(child_file.id) in item_ids_in_sim
    assert str(unrelated_file.id) not in item_ids_in_sim


def test_execute_handover_transfer_selective_and_untouched_items():
    """Execution only transfers chosen item IDs; unselected items remain untouched."""
    departing_user = factories.UserFactory()
    colleague = factories.UserFactory()
    recipient = factories.UserFactory()

    item_selected = factories.ItemFactory(
        creator=departing_user,
        size=1024,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )
    item_unselected = factories.ItemFactory(
        creator=departing_user,
        size=2048,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    result = handover.execute_handover_transfer(
        departing_user=departing_user,
        recipient=recipient,
        item_ids=[str(item_selected.id)],
        reallocate_storage_quota=False,
        departing_user_action="revoke",
    )

    assert result["status"] == "completed"
    assert result["summary"]["items_transferred"] == 1

    # Selected item was transferred
    assert models.ItemAccess.objects.filter(
        item=item_selected, user=recipient, role=models.RoleChoices.OWNER
    ).exists()
    assert not models.ItemAccess.objects.filter(item=item_selected, user=departing_user).exists()

    # Unselected item was completely untouched
    assert not models.ItemAccess.objects.filter(item=item_unselected, user=recipient).exists()
    assert models.ItemAccess.objects.filter(
        item=item_unselected, user=departing_user, role=models.RoleChoices.OWNER
    ).exists()


def test_execute_handover_transfer_folder_cascades_to_children():
    """Transferring a folder transfers the folder and its descendant files to recipient."""
    departing_user = factories.UserFactory()
    colleague = factories.UserFactory()
    recipient = factories.UserFactory()

    parent_folder = factories.ItemFactory(
        creator=departing_user,
        type=models.ItemTypeChoices.FOLDER,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )
    child_file = factories.ItemFactory(
        parent=parent_folder,
        creator=departing_user,
        type=models.ItemTypeChoices.FILE,
        size=4096,
        users=[
            (departing_user, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    result = handover.execute_handover_transfer(
        departing_user=departing_user,
        recipient=recipient,
        item_ids=[str(parent_folder.id)],
        reallocate_storage_quota=True,
        departing_user_action="revoke",
    )

    assert result["status"] == "completed"
    assert result["summary"]["items_transferred"] == 2
    assert result["summary"]["bytes_reallocated"] == 4096

    # Recipient is OWNER on parent folder, cascading full control to child
    assert models.ItemAccess.objects.filter(
        item=parent_folder, user=recipient, role=models.RoleChoices.OWNER
    ).exists()
    assert child_file.get_abilities(recipient)["accesses_manage"] is True

    # Departing user access is revoked on both
    assert not models.ItemAccess.objects.filter(item=parent_folder, user=departing_user).exists()
    assert not models.ItemAccess.objects.filter(item=child_file, user=departing_user).exists()

    # Both parent and child have creator updated to recipient
    parent_folder.refresh_from_db()
    child_file.refresh_from_db()
    assert parent_folder.creator_id == recipient.id
    assert child_file.creator_id == recipient.id


def test_api_users_handover_transfer_mandatory_item_ids_validation():
    """POST /handover/transfer/ returns 400 when item_ids is missing or empty."""
    manager = factories.UserFactory(is_staff=True)
    subordinate = factories.UserFactory()
    recipient = factories.UserFactory()

    client = APIClient()
    client.force_login(manager)

    # 1. Missing item_ids
    res1 = client.post(
        f"/api/v1.0/users/{subordinate.id}/handover/transfer/",
        data={"recipient_id": str(recipient.id)},
        format="json",
    )
    assert res1.status_code == 400
    assert "item_ids" in res1.json()["errors"][0]["attr"]

    # 2. Empty list item_ids
    res2 = client.post(
        f"/api/v1.0/users/{subordinate.id}/handover/transfer/",
        data={"recipient_id": str(recipient.id), "item_ids": []},
        format="json",
    )
    assert res2.status_code == 400
    assert "item_ids" in res2.json()["errors"][0]["attr"]


def test_api_users_handover_transfer_selective_execution():
    """Manager transfers only the selected item_ids via API."""
    manager = factories.UserFactory(is_staff=True)
    subordinate = factories.UserFactory()
    colleague = factories.UserFactory()
    recipient = factories.UserFactory()

    item_to_transfer = factories.ItemFactory(
        creator=subordinate,
        size=1024,
        users=[
            (subordinate, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )
    item_to_leave = factories.ItemFactory(
        creator=subordinate,
        size=2048,
        users=[
            (subordinate, models.RoleChoices.OWNER),
            (colleague, models.RoleChoices.READER),
        ],
    )

    client = APIClient()
    client.force_login(manager)

    response = client.post(
        f"/api/v1.0/users/{subordinate.id}/handover/transfer/",
        data={
            "recipient_id": str(recipient.id),
            "item_ids": [str(item_to_transfer.id)],
            "keep_departing_access": True,
        },
        format="json",
    )

    assert response.status_code == 200
    data = response.json()
    assert data["status"] == "completed"
    assert data["summary"]["items_transferred"] == 1

    # item_to_transfer transferred
    assert models.ItemAccess.objects.filter(
        item=item_to_transfer, user=recipient, role=models.RoleChoices.OWNER
    ).exists()
    assert models.ItemAccess.objects.filter(
        item=item_to_transfer, user=subordinate, role=models.RoleChoices.READER
    ).exists()

    # item_to_leave untouched
    assert not models.ItemAccess.objects.filter(item=item_to_leave, user=recipient).exists()
    assert models.ItemAccess.objects.filter(
        item=item_to_leave, user=subordinate, role=models.RoleChoices.OWNER
    ).exists()


def test_is_user_in_manager_team_service():
    """Verify is_user_in_manager_team behavior across roles and environments."""
    # 1. Staff and Superuser always allowed
    staff_manager = factories.UserFactory(is_staff=True)
    superuser_manager = factories.UserFactory(is_superuser=True)
    random_user = factories.UserFactory()

    assert handover.is_user_in_manager_team(staff_manager, random_user) is True
    assert handover.is_user_in_manager_team(superuser_manager, random_user) is True

    # 2. None / Self-check
    assert handover.is_user_in_manager_team(None, random_user) is True
    assert handover.is_user_in_manager_team(random_user, None) is True
    assert handover.is_user_in_manager_team(random_user, random_user) is True

    # 3. Test override attribute
    regular_manager = factories.UserFactory(is_staff=False, is_superuser=False)
    authorized_sub = factories.UserFactory()
    authorized_sub.is_in_manager_team = True
    unauthorized_sub = factories.UserFactory()
    unauthorized_sub.is_in_manager_team = False

    assert handover.is_user_in_manager_team(regular_manager, authorized_sub) is True
    assert handover.is_user_in_manager_team(regular_manager, unauthorized_sub) is False

    # 4. Demo environment check
    demo_manager = factories.UserFactory(
        email="manager@example.com", is_staff=False, is_superuser=False
    )
    demo_member = factories.UserFactory(email="subordinate@example.com")
    outsider = factories.UserFactory(email="outsider@example.com")

    assert handover.is_user_in_manager_team(demo_manager, demo_member) is True
    assert handover.is_user_in_manager_team(demo_manager, outsider) is False


def test_api_users_handover_transfer_unauthorized_recipient_rejected():
    """Transfer API rejects recipients who are not in the manager's team."""
    demo_manager = factories.UserFactory(
        email="manager@example.com", is_staff=False, is_superuser=False
    )
    subordinate = factories.UserFactory(email="subordinate@example.com")
    outsider_recipient = factories.UserFactory(email="outsider@example.com")

    item = factories.ItemFactory(
        creator=subordinate,
        size=1024,
        users=[(subordinate, models.RoleChoices.OWNER)],
    )

    client = APIClient()
    client.force_login(demo_manager)

    response = client.post(
        f"/api/v1.0/users/{subordinate.id}/handover/transfer/",
        data={
            "recipient_id": str(outsider_recipient.id),
            "item_ids": [str(item.id)],
        },
        format="json",
    )

    assert response.status_code == 400
    res_data = response.json()
    assert "recipient_id" in res_data["errors"][0]["attr"]
    assert "not a member of the manager's team" in res_data["errors"][0]["detail"]
    assert outsider_recipient.email in res_data["errors"][0]["detail"]


