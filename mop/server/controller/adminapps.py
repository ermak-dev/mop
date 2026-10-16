"""Admin config отделён: controller auto-discovery должен видеть один default."""
from django.contrib.admin import apps as admin_apps


class ControllerAdminConfig(admin_apps.AdminConfig):
    default_site = 'mop.server.controller.adminsite.AccessAdminSite'
