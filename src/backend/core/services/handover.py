"""
Service for auditing and transferring items during user offboarding/handover.
Ensures continuity of public records when an agent leaves an administration.
"""

from django.core.exceptions import ValidationError
from django.db import transaction
from django.db.models import Count, OuterRef, Q, Subquery, Sum, Value
from django.db.models.functions import Coalesce

from lasuite.drf.models.choices import PRIVILEGED_ROLES, RoleChoices

from core import models
from core.entitlements import get_entitlements_backend
from core.services.accesses import synchronize_descendants_accesses
from core.storage import get_storage_compute_backend
from core.storage.cache import invalidate_storage_used_cache

# TODO: DRF and Throttling
"""
## DRF Serializers (core/api/serializers.py) - for swagger / frontend types generation

Define standard DRF serializers for request documentation and response formatting:

1. HandoverAuditItemSerializer:
    • Fields: id, title, type, size, depth, parent_id, parent_title, breadcrumbs,
      is_sole_owner, is_shared, other_members_count, is_flagged_personal, updated_at.
2. HandoverAuditSummarySerializer:
    • Fields: total_items, total_bytes, sole_owner_count, shared_count, personal_flagged_count.
3. HandoverAuditResponseSerializer:
    • Fields:
        • departing_user: nested light user representation (UserLightSerializer).
        • summary: nested HandoverAuditSummarySerializer.
        • items: list of HandoverAuditItemSerializer.
        • handover_token: string (the signed challenge token required for transfer).


## Throttling (core/api/throttling.py) - minimal security

Handover audits are rare administrative events. Protect against brute-force enumeration:

Create a throttle class inheriting from rest_framework.throttling.UserRateThrottle:
    • Set scope = "handover_audit" (configured in settings.py e.g., "10/hour"),
      or set a default rate = "10/hour".
"""


def _get_accesses_count_subqueries(departing_user):
    """Return subqueries for computing other_members_count and total_owners_count."""
    other_members_subquery = Coalesce(
        Subquery(
            models.ItemAccess.objects.filter(item=OuterRef("pk"))
            .exclude(user=departing_user)
            .values("item")
            .annotate(cnt=Count("id"))
            .values("cnt")[:1]
        ),
        Value(0),
    )
    total_owners_subquery = Coalesce(
        Subquery(
            models.ItemAccess.objects.filter(item=OuterRef("pk"), role=RoleChoices.OWNER)
            .values("item")
            .annotate(cnt=Count("id"))
            .values("cnt")[:1]
        ),
        Value(0),
    )
    return other_members_subquery, total_owners_subquery


def get_departing_user_administered_items(departing_user):
    """
    Query non-deleted shared items administered by the departing user.

    Files with no other access, including sole-owner files, are intentionally
    excluded for now;
    TODO: decide later whether they should be deleted or archived or handled differently.
    """
    other_members_subquery, total_owners_subquery = _get_accesses_count_subqueries(departing_user)

    return (
        models.Item.objects.filter(
            # 1. Active items only (not in trash)
            deleted_at__isnull=True,
            ancestors_deleted_at__isnull=True,
        )
        .filter(
            # 2. Items created by user OR where user holds an owner/admin access
            Q(creator=departing_user)
            | Q(accesses__user=departing_user, accesses__role__in=PRIVILEGED_ROLES)
        )
        .annotate(
            # Count total owners on the item
            total_owners_count=total_owners_subquery,
            # Count other colleagues or teams with any access
            other_accesses_count=other_members_subquery,
        )
        # Sole-owner files are deferred until the delete-versus-archive decision is made.
        .filter(other_accesses_count__gt=0)
        .distinct()
        .order_by("-updated_at")
    )


def build_user_handover_audit(departing_user):
    """
    Compile the audit data, hierarchy relations, and summary metrics for the line manager.
    """
    items_qs = get_departing_user_administered_items(departing_user)
    items = list(items_qs)

    # 1. Collect all unique ancestor IDs from item paths
    ancestor_ids = set()
    for item in items:
        if len(item.path) > 1:
            ancestor_ids.update(str(p_id) for p_id in item.path[:-1])

    # 2. Batch fetch folder titles in ONE query (avoids 100+ SQL queries)
    folder_map = {}
    if ancestor_ids:
        for folder in (
            models.Item.objects.filter(id__in=ancestor_ids)
            .only("id", "title")
            .iterator()
        ):
            folder_map[str(folder.id)] = folder.title

    # 3. Storage quota in Drive is charged to creator
    total_bytes = (
        models.Item.objects.filter(
            creator=departing_user,
            deleted_at__isnull=True,
        ).aggregate(total=Sum("size"))["total"]
        or 0
    )

    other_members_subquery, _ = _get_accesses_count_subqueries(departing_user)

    # 4. Count skipped unshared items (administered by user with zero collaborators)
    unshared_qs = (
        models.Item.objects.filter(
            deleted_at__isnull=True,
            ancestors_deleted_at__isnull=True,
        )
        .filter(
            Q(creator=departing_user)
            | Q(accesses__user=departing_user, accesses__role__in=PRIVILEGED_ROLES)
        )
        .annotate(other_accesses_count=other_members_subquery)
        .filter(other_accesses_count=0)
        .distinct()
    )
    unshared_agg = unshared_qs.aggregate(
        count=Count("id"),
        total_bytes=Sum("size"),
    )
    skipped_unshared_count = unshared_agg["count"] or 0
    skipped_unshared_bytes = unshared_agg["total_bytes"] or 0

    # 5. Count skipped soft-deleted items (in trashbin)
    trash_qs = (
        models.Item.objects.filter(
            Q(deleted_at__isnull=False) | Q(ancestors_deleted_at__isnull=False)
        )
        .filter(
            Q(creator=departing_user)
            | Q(accesses__user=departing_user, accesses__role__in=PRIVILEGED_ROLES)
        )
        .distinct()
    )
    trash_agg = trash_qs.aggregate(
        count=Count("id"),
        total_bytes=Sum("size"),
    )
    skipped_trash_count = trash_agg["count"] or 0
    skipped_trash_bytes = trash_agg["total_bytes"] or 0

    items_list = []
    sole_owner_count = 0
    shared_count = 0

    for item in items:
        # Sole owner: departing agent is an owner and total owner count is <= 1
        is_sole_owner = item.total_owners_count <= 1
        if is_sole_owner:
            sole_owner_count += 1

        is_shared = item.other_accesses_count > 0
        if is_shared:
            shared_count += 1

        # Ancestor folder IDs from root down to direct parent
        ancestor_path_ids = [str(p_id) for p_id in item.path[:-1]]

        # Direct parent: last element in ancestor path (or None if item is at root)
        parent_id = ancestor_path_ids[-1] if ancestor_path_ids else None
        parent_title = folder_map.get(parent_id) if parent_id else None

        # Breadcrumbs list: [{ id, title }, ...]
        breadcrumbs = [
            {"id": p_id, "title": folder_map.get(p_id, "")}
            for p_id in ancestor_path_ids
        ]

        items_list.append(
            {
                "id": str(item.id),
                "title": item.title,
                "type": item.type,
                "size": item.size,
                "depth": len(item.path),
                "parent_id": parent_id,
                "parent_title": parent_title,
                "breadcrumbs": breadcrumbs,
                "is_sole_owner": is_sole_owner,
                "is_shared": is_shared,
                "other_members_count": item.other_accesses_count,
                "created_at": item.created_at,
                "updated_at": item.updated_at,
            }
        )

    return {
        "summary": {
            "total_items": len(items_list),
            "total_bytes": total_bytes,
            "sole_owner_count": sole_owner_count,
            "shared_count": shared_count,
            "skipped_unshared_count": skipped_unshared_count,
            "skipped_unshared_bytes": skipped_unshared_bytes,
            "skipped_trash_count": skipped_trash_count,
            "skipped_trash_bytes": skipped_trash_bytes,
        },
        "items": items_list,
    }


def simulate_handover_transfer(  # noqa: PLR0912
    departing_user,
    recipient,
    item_ids,
    reallocate_storage_quota=False,
    departing_user_action="revoke",
):
    """
    Perform a dry-run simulation of the handover transfer.

    Validates recipient existence, quota constraints, item eligibility, and
    calculates impact metrics without committing any changes to the database.
    """
    errors = []
    warnings = []

    # 1. Recipient validations
    if recipient.id == departing_user.id:
        errors.append(
            {
                "code": "invalid_recipient",
                "message": "Cannot transfer items to the departing user themselves.",
            }
        )

    if not recipient.is_active:
        errors.append(
            {
                "code": "inactive_recipient",
                "message": f"Recipient '{recipient.email}' is inactive.",
            }
        )

    # 2. Collect candidate items (mandatory selection)
    if not item_ids:
        errors.append(
            {
                "code": "missing_item_ids",
                "message": "At least one item ID must be explicitly selected for transfer.",
            }
        )
        candidate_items = []
    else:
        all_administered_qs = get_departing_user_administered_items(departing_user)
        selected_folders = all_administered_qs.filter(
            id__in=item_ids, type=models.ItemTypeChoices.FOLDER
        )
        descendant_filter = Q(id__in=item_ids)
        for folder in selected_folders:
            descendant_filter |= Q(path__descendants=folder.path)
        candidate_items = list(all_administered_qs.filter(descendant_filter))

        found_ids = {str(item.id) for item in candidate_items}
        missing_ids = [str(i_id) for i_id in item_ids if str(i_id) not in found_ids]
        if missing_ids:
            warnings.append(
                {
                    "code": "items_not_found_or_unshared",
                    "message": (
                        f"{len(missing_ids)} requested item(s) are either unshared, "
                        "already deleted, or not administered by the departing user."
                    ),
                    "item_ids": missing_ids,
                }
            )

    # 3. Quota simulation
    bytes_to_reallocate = 0
    if reallocate_storage_quota:
        bytes_to_reallocate = sum(
            item.size or 0 for item in candidate_items if item.creator_id == departing_user.id
        )

    backend = get_entitlements_backend()
    if hasattr(backend, "get_storage_limit"):
        storage_limit = backend.get_storage_limit(recipient)
    else:
        storage_limit = getattr(recipient, "storage_limit_override", None)

    if hasattr(backend, "get_storage_used"):
        current_storage_used = backend.get_storage_used(recipient)
    else:
        current_storage_used = (
            get_storage_compute_backend().compute_storage_used([recipient]) or 0
        )

    projected_storage_used = current_storage_used + bytes_to_reallocate
    quota_exceeded = False

    if storage_limit is not None and projected_storage_used > storage_limit:
        quota_exceeded = True
        overflow = projected_storage_used - storage_limit
        errors.append(
            {
                "code": "recipient_quota_overflow",
                "message": (
                    f"Recipient '{recipient.email}' will exceed storage quota by "
                    f"{overflow} bytes."
                ),
                "overflow_bytes": overflow,
            }
        )

    # 4. Check existing accesses on candidate items
    recipient_accesses = dict(
        models.ItemAccess.objects.filter(
            item__in=candidate_items, user=recipient
        ).values_list("item_id", "role")
    )

    for item in candidate_items:
        existing_role = recipient_accesses.get(item.id)
        if existing_role:
            if existing_role == RoleChoices.OWNER:
                warnings.append(
                    {
                        "code": "already_owner",
                        "item_id": str(item.id),
                        "item_title": item.title,
                        "message": f"Recipient is already OWNER of '{item.title}'.",
                    }
                )
            else:
                warnings.append(
                    {
                        "code": "role_upgrade",
                        "item_id": str(item.id),
                        "item_title": item.title,
                        "message": (
                            f"Recipient's role on '{item.title}' will be upgraded "
                            f"from {existing_role.upper()} to OWNER."
                        ),
                    }
                )

    can_execute = len(errors) == 0

    return {
        "dry_run": True,
        "can_execute": can_execute,
        "summary": {
            "items_count": len(candidate_items),
            "total_bytes_reallocated": bytes_to_reallocate,
            "reallocate_storage_quota": reallocate_storage_quota,
            "departing_user_action": departing_user_action,
        },
        "recipient": {
            "id": str(recipient.id),
            "email": recipient.email,
            "current_storage_used": current_storage_used,
            "storage_limit": storage_limit,
            "projected_storage_used": projected_storage_used,
            "quota_exceeded": quota_exceeded,
        },
        "warnings": warnings,
        "errors": errors,
        "items": [
            {
                "id": str(item.id),
                "title": item.title,
                "type": item.type,
                "size": item.size,
                "depth": len(item.path),
            }
            for item in candidate_items
        ],
    }


def execute_handover_transfer(  # noqa: PLR0912, PLR0915
    departing_user,
    recipient,
    item_ids,
    reallocate_storage_quota=False,
    departing_user_action="revoke",
):
    """
    Execute the handover transfer atomically.

    Reassigns item creator, grants OWNER role to recipient, cleans up
    redundant permissions, updates departing user access, and invalidates
    storage and permission caches.
    """
    if not item_ids:
        raise ValidationError(
            {"item_ids": "At least one item ID must be explicitly selected for transfer."}
        )

    # 1. Run simulation first; if blocking errors exist, abort
    simulation = simulate_handover_transfer(
        departing_user=departing_user,
        recipient=recipient,
        item_ids=item_ids,
        reallocate_storage_quota=reallocate_storage_quota,
        departing_user_action=departing_user_action,
    )
    if not simulation["can_execute"]:
        raise ValidationError(simulation["errors"])

    all_administered_qs = get_departing_user_administered_items(departing_user)
    selected_folders = all_administered_qs.filter(
        id__in=item_ids, type=models.ItemTypeChoices.FOLDER
    )
    descendant_filter = Q(id__in=item_ids)
    for folder in selected_folders:
        descendant_filter |= Q(path__descendants=folder.path)
    candidate_qs = all_administered_qs.filter(descendant_filter)

    candidate_ids = list(candidate_qs.values_list("id", flat=True))
    if not candidate_ids:
        return {
            "dry_run": False,
            "status": "completed",
            "summary": {
                "items_transferred": 0,
                "bytes_reallocated": 0,
                "reallocate_storage_quota": reallocate_storage_quota,
                "departing_user_action": departing_user_action,
            },
            "recipient": {
                "id": str(recipient.id),
                "email": recipient.email,
            },
            "items": [],
        }

    transferred_items = []
    total_bytes_reallocated = 0

    with transaction.atomic():
        # Acquire row-level locks on user rows in deterministic order to prevent deadlocks
        user_ids = sorted([departing_user.id, recipient.id])
        list(models.User.objects.select_for_update().filter(id__in=user_ids).order_by("id"))

        # Acquire row-level locks on all candidate items in deterministic order
        items = list(
            models.Item.objects.select_for_update()
            .filter(id__in=candidate_ids, deleted_at__isnull=True)
            .order_by("id")
        )

        # 2. Update Creator (storage quota) if requested
        if reallocate_storage_quota:
            items_to_update_creator = []
            for item in items:
                if item.creator_id == departing_user.id:
                    item.creator = recipient
                    items_to_update_creator.append(item)
                    total_bytes_reallocated += item.size or 0

            if items_to_update_creator:
                models.Item.objects.bulk_update(items_to_update_creator, ["creator"])

        # 3. Transfer / Upgrade Accesses for Recipient
        existing_accesses = {
            access.item_id: access
            for access in models.ItemAccess.objects.filter(item__in=items, user=recipient)
        }

        accesses_to_create = []
        accesses_to_update = []

        for item in items:
            access = existing_accesses.get(item.id)
            if access:
                if access.role != RoleChoices.OWNER:
                    access.role = RoleChoices.OWNER
                    accesses_to_update.append(access)
            else:
                accesses_to_create.append(
                    models.ItemAccess(item=item, user=recipient, role=RoleChoices.OWNER)
                )

        if accesses_to_create:
            models.ItemAccess.objects.bulk_create(accesses_to_create)

        if accesses_to_update:
            models.ItemAccess.objects.bulk_update(accesses_to_update, ["role"])

        # 4. Prune redundant / shadowed lower roles on child items
        all_recipient_accesses = models.ItemAccess.objects.filter(item__in=items, user=recipient)
        for access in all_recipient_accesses:
            synchronize_descendants_accesses(access.item, access)

        # 5. Departing user access action
        departing_accesses = models.ItemAccess.objects.filter(item__in=items, user=departing_user)
        if departing_user_action == "revoke":
            departing_accesses.delete()
        elif departing_user_action == "keep_reader":
            departing_accesses.update(role=RoleChoices.READER)

        # 6. Invalidate caches
        for item in items:
            item.invalidate_nb_accesses_cache()

        affected_user_ids = [departing_user.id, recipient.id]
        transaction.on_commit(lambda: invalidate_storage_used_cache(affected_user_ids))

        transferred_items = [
            {
                "id": str(item.id),
                "title": item.title,
                "type": item.type,
                "size": item.size,
                "depth": len(item.path),
            }
            for item in items
        ]

    return {
        "dry_run": False,
        "status": "completed",
        "summary": {
            "items_transferred": len(transferred_items),
            "bytes_reallocated": total_bytes_reallocated,
            "reallocate_storage_quota": reallocate_storage_quota,
            "departing_user_action": departing_user_action,
        },
        "recipient": {
            "id": str(recipient.id),
            "email": recipient.email,
        },
        "items": transferred_items,
    }
