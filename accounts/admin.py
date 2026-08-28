"""
Django admin registration for the custom user model.

Registered explicitly because swapping `AUTH_USER_MODEL` unregisters the
built-in `UserAdmin`, and an admin site with no way to look at users is a
support problem waiting to happen.

`OAuthIdentity` is shown inline and read-only. Being able to see which
providers an account is connected to answers most "I cannot sign in" questions
immediately; being able to *edit* the rows would let an administrator
hand-write a link between a user and a provider account nobody has proved they
own, which is the one thing the whole linking policy exists to prevent.
"""

from django.contrib import admin
from django.contrib.auth.admin import UserAdmin as DjangoUserAdmin

from .models import OAuthIdentity, User


class OAuthIdentityInline(admin.TabularInline):
    model = OAuthIdentity
    extra = 0
    can_delete = True
    fields = ["provider", "subject", "email", "created_at", "last_login_at"]
    readonly_fields = fields

    def has_add_permission(self, request, obj=None) -> bool:
        return False


@admin.register(User)
class UserAdmin(DjangoUserAdmin):
    inlines = [OAuthIdentityInline]
    list_display = ["username", "email", "is_active", "is_staff", "date_joined"]
    list_filter = ["is_active", "is_staff", "is_superuser", "date_joined"]
    search_fields = ["username", "email", "first_name", "last_name"]
    ordering = ["-date_joined"]

    # The two extra fields the custom model adds, appended to Django's stock
    # fieldsets rather than replacing them, so a future Django release that
    # adds a field to the base does not silently drop it from this form.
    fieldsets = DjangoUserAdmin.fieldsets + (
        ("Profile", {"fields": ("avatar_url",)}),
    )
    add_fieldsets = DjangoUserAdmin.add_fieldsets + (
        ("Contact", {"fields": ("email",)}),
    )


@admin.register(OAuthIdentity)
class OAuthIdentityAdmin(admin.ModelAdmin):
    list_display = ["provider", "user", "email", "created_at", "last_login_at"]
    list_filter = ["provider"]
    search_fields = ["user__username", "user__email", "email", "subject"]
    readonly_fields = ["provider", "subject", "email", "created_at", "last_login_at"]

    def has_add_permission(self, request) -> bool:
        return False
