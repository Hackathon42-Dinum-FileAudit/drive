"""Permission handlers for the drive core app."""

from django.core import exceptions
from django.http import Http404

from lasuite.drf.models.choices import PRIVILEGED_ROLES
from rest_framework import permissions

from core.models import RoleChoices, get_trashbin_cutoff

ACTION_FOR_METHOD_TO_PERMISSION = {
    "versions_detail": {"DELETE": "versions_destroy", "GET": "versions_retrieve"},
    "children": {"GET": "children_list", "POST": "children_create"},
    "batch_share": {"POST": "accesses_manage"},
}


class IsAuthenticated(permissions.BasePermission):
    """
    Allows access only to authenticated users. Alternative method checking the presence
    of the auth token to avoid hitting the database.
    """

    def has_permission(self, request, view):
        return bool(request.auth) or request.user.is_authenticated


class IsAuthenticatedOrSafe(IsAuthenticated):
    """Allows access to authenticated users (or anonymous users but only on safe methods)."""

    def has_permission(self, request, view):
        if request.method in permissions.SAFE_METHODS:
            return True
        return super().has_permission(request, view)


class IsSelf(IsAuthenticated):
    """
    Allows access only to authenticated users. Alternative method checking the presence
    of the auth token to avoid hitting the database.
    """

    def has_object_permission(self, request, view, obj):
        """Write permissions are only allowed to the user itself."""
        return obj == request.user


class IsOwnedOrPublic(IsAuthenticated):
    """
    Allows access to authenticated users only for objects that are owned or not related
    to any user via the "owner" field.
    """

    def has_object_permission(self, request, view, obj):
        """Unsafe permissions are only allowed for the owner of the object."""
        if obj.owner == request.user:
            return True

        if request.method in permissions.SAFE_METHODS and obj.owner is None:
            return True

        try:
            return obj.user == request.user
        except exceptions.ObjectDoesNotExist:
            return False


class CreateWithPriviliegedRolesMixin:
    """
    Implement a common has_permission method checking that
    the user has privileged role on the item in order to
    perform the create action.
    This mixin must be used with the IsAuthenticated permission class
    """

    resources = None

    def has_permission(self, request, view):
        """Check the current user has privileged roles on the related item."""
        if super().has_permission(request, view) is False:
            return False

        if view.action == "create":
            role = getattr(view, view.resource_field_name).get_role(request.user)
            if role not in PRIVILEGED_ROLES:
                raise exceptions.PermissionDenied(
                    f"You are not allowed to manage {self.resources} for this resource."
                )

        return True


class InvitationPermission(CreateWithPriviliegedRolesMixin, IsAuthenticated):
    """A permission class for the InvitationViewset."""

    resources = "invitations"

    def has_object_permission(self, request, view, obj):
        """Check permission for a given object."""
        abilities = obj.get_abilities(request.user)
        return abilities.get(view.action, False)


class ItemAccessPermission(CreateWithPriviliegedRolesMixin, IsAuthenticated):
    """Permission class for the ItemAccessViewSet."""

    resources = "accesses"

    def has_object_permission(self, request, view, obj):
        """Check permission for a given object."""
        abilities = obj.get_abilities(request.user)

        requested_role = request.data.get("role")
        if requested_role and requested_role not in abilities.get("set_role_to", []):
            return False

        return abilities.get(view.action, False)


class ItemPermission(permissions.BasePermission):
    """Subclass to handle soft deletion specificities."""

    def has_permission(self, request, view):
        return request.user.is_authenticated or view.action not in [
            "create",
            "trashbin",
            "search",
        ]

    def has_object_permission(self, request, view, obj):
        """
        Return a 404 on deleted items
        - for which the trashbin cutoff is past
        - for which the current user is not owner of the item or one of its ancestors
        """
        if (deleted_at := obj.ancestors_deleted_at) and deleted_at < get_trashbin_cutoff():
            raise Http404

        abilities = obj.get_abilities(request.user)
        action = view.action
        try:
            action = ACTION_FOR_METHOD_TO_PERMISSION[view.action][request.method]
        except KeyError:
            pass

        has_permission = abilities.get(action, False)

        if obj.ancestors_deleted_at and not RoleChoices.OWNER in obj.user_roles:
            raise Http404

        return has_permission


class IsManagerOf(IsAuthenticated):
    """
    Permission class ensuring that the requesting user has managerial
    authority over the targeted user for handover and offboarding operations.
    """

    def has_permission(self, request, view):
        """
        Initial gate check before fetching the object.
        """
        # Base check: user must be authenticated
        if not super().has_permission(request, view):
            return False

        # -------------------------------------------------------------
        # [SECURITY TODO - STEP-UP AUTHENTICATION]
        # In production, check recent MFA / WebAuthn authentication:
        # auth_time = request.auth.get("auth_time") if request.auth else None
        # if not auth_time or (timezone.now().timestamp() - auth_time) > 900:
        #     raise exceptions.AuthenticationFailed("MFA re-authentication required.")
        # -------------------------------------------------------------
        return True

    def has_object_permission(self, request, view, obj):
        """
        Object-level check where `obj` is the departing User instance.
        """
        manager = request.user
        subordinate = obj

        # Rule 1: Anti-Self Handover
        # A user cannot perform a manager handover audit on themselves.
        if manager.id == subordinate.id:
            return False

        # Rule 2: Staff / Superuser bypass for development and administrative ease
        if manager.is_staff or manager.is_superuser:
            return True

        # -------------------------------------------------------------
        # [SECURITY TODO - MULTI-TENANT SIRET ISOLATION]
        # Ensure the manager and subordinate belong to the exact same organization:
        # manager_siret = manager.claims.get("siret")
        # subordinae_siret = subordinate.claims.get("siret")
        # if not manager_siret or manager_siret != subordinate_siret:
        #     return False
        # -------------------------------------------------------------

        # Rule 3: Manager relationship verification (Mocked for now)
        return self._check_is_manager_of(manager, subordinate)

    def _check_is_manager_of(self, manager, subordinate):
        """
        Verify that `manager` has authority over `subordinate`.
        Mocked for local development without external 'People' dependencies.
        """
        # -------------------------------------------------------------
        # [INTEGRATION TODO - PEOPLE / LA RÉGIE API / ACCOUNTS ]
        # In production:
        # 1. Obtain an inter-service JWT token.
        # 2. Call People/ACCOUNTS API: GET /api/v1.0/teams/?manager={manager.id}&member={subordinate.id}
        # 3. Cache the relationship in Redis (e.g. 5 min TTL).
        # -------------------------------------------------------------

        # MOCK IMPLEMENTATION FOR DEVELOPMENT:
        # Option A: Simple allow-all for authenticated users in dev:
        # return True

        # Option B: Mock via team match or simple email convention:
        # e.g., allow if manager shares any team with subordinate:
        # return bool(set(manager.teams) & set(subordinate.teams))

        return True
