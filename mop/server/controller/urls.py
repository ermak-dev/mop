"""Только стандартный Admin: предметные endpoints добавляют их владельцы."""
from django.contrib import admin
from django.urls import path

# Стандартный token admin раскрывает bearer key; общий Admin его не публикует.
from rest_framework.authtoken.models import TokenProxy
if admin.site.is_registered(TokenProxy):
    admin.site.unregister(TokenProxy)
from .views import Health

from django.views.decorators.cache import never_cache
from . import authviews as auth
from . import machineviews as machine

urlpatterns = [path('admin/', admin.site.urls), path('api/v1/health', Health.as_view()),
    path('api/v1/auth/csrf', auth.csrf),
    path('api/v1/auth/login', never_cache(auth.Login.as_view())),
    path('api/v1/auth/logout', never_cache(auth.Logout.as_view())),
    path('api/v1/auth/token', auth.IssueToken.as_view()),
    path('api/v1/auth/token/rotate', auth.RotateToken.as_view()),
    path('api/v1/auth/token/revoke', auth.RevokeToken.as_view()),
    path('api/v1/machine/work', machine.Work.as_view()),
    path('api/v1/machine/work/claim', machine.Claim.as_view()),
    path('api/v1/machine/work/result', machine.Result.as_view()),
    path('api/v1/machine/executor/restarted', machine.Restarted.as_view()),
    path('api/v1/machine/facts', machine.Facts.as_view()),
    path('api/v1/identity', auth.Identity.as_view()),
    path('api/v1/projects', auth.Projects.as_view()),
    path('api/v1/projects/<uuid:project_id>', auth.Project.as_view()),
]
