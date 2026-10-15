"""Только стандартный Admin: предметные endpoints добавляют их владельцы."""
from django.contrib import admin
from django.urls import path

# Стандартный token admin раскрывает bearer key; общий Admin его не публикует.
from rest_framework.authtoken.models import TokenProxy
if admin.site.is_registered(TokenProxy):
    admin.site.unregister(TokenProxy)
from .views import Health

urlpatterns = [path('admin/', admin.site.urls), path('api/v1/health', Health.as_view())]
