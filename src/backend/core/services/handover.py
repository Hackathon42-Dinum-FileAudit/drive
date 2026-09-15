"""
Service for auditing and transferring items during user offboarding/handover.
Ensures continuity of public records when an agent leaves an administration.
"""

from django.db.models import Count, Q, Sum

from lasuite.drf.models.choices import PRIVILEGED_ROLES, RoleChoices

from core import models

# TODO: DRF and Throttling
"""
## DRF Serializers (core/api/serializers.py) - for swagger / frontend types genration

Define standard DRF serializers for request documentation and response formatting:

1. HandoverAuditItemSerializer:
    • Fields: id, title, type (file or folder), size, is_sole_owner, is_flagged_personal, updated_at.
2. HandoverAuditSummarySerializer:
    • Fields: total_items, total_bytes, sole_owner_count, shared_count, personal_flagged_count.
3. HandoverAuditResponseSerializer:
    • Fields:
        • departing_user: nested light user representation (UserLightSerializer).
        • summary: nested HandoverAuditSummarySerializer.
        • items: list of HandoverAuditItemSerializer.
        • handover_token: string (the signed challenge token required for the transfer endpoint).


## Throttling (core/api/throttling.py) - minimal security

Handover audits are rare administrative events. Protect against brute-force enumeration:

Create a throttle class inheriting from rest_framework.throttling.UserRateThrottle:
    • Set scope = "handover_audit" (configured in settings.py e.g., "10/hour"), or set a default rate = "10/hour".
"""


def get_departing_user_administered_items(departing_user):
    """
    Query non-deleted shared items administered by the departing user.

    Files with no other access, including sole-owner files, are intentionally
    excluded for now;
    TODO: decide later whether they should be deleted or archived or handled differently.
    """
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
            total_owners_count=Count(
                "accesses",
                filter=Q(accesses__role=RoleChoices.OWNER),
                distinct=True,
            ),
            # Count other colleagues or teams with any access
            other_accesses_count=Count(
                "accesses",
                filter=~Q(accesses__user=departing_user) | Q(accesses__team__gt=""),
                distinct=True,
            ),
        )
        # Sole-owner files are deferred until the delete-versus-archive decision is made.
        .filter(other_accesses_count__gt=0)
        .distinct()
        .order_by("-updated_at")
    )


def build_user_handover_audit(departing_user):
    """
    Compile the audit data and summary metrics for the line manager.
    """
    items_qs = get_departing_user_administered_items(departing_user)

    # Storage quota in Drive is charged to creator
    total_bytes = (
        models.Item.objects.filter(
            creator=departing_user,
            deleted_at__isnull=True,
        ).aggregate(total=Sum("size"))["total"]
        or 0
    )

    items_list = []
    sole_owner_count = 0
    shared_count = 0

    for item in items_qs:
        # Sole owner: departing agent is an owner and total owner count is <= 1
        is_sole_owner = item.total_owners_count <= 1
        if is_sole_owner:
            sole_owner_count += 1

        is_shared = item.other_accesses_count > 0
        if is_shared:
            shared_count += 1

        items_list.append(
            {
                "id": str(item.id),
                "title": item.title,
                "type": item.type,
                "size": item.size,
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
        },
        "items": items_list,
    }
