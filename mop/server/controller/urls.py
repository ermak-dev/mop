"""Только стандартный Admin; предметные endpoints добавляют их владельцы."""
from django.contrib import admin
from django.urls import path, re_path

# Стандартный token admin раскрывает bearer key; общий Admin его не публикует.
from rest_framework.authtoken.models import TokenProxy
if admin.site.is_registered(TokenProxy):
    admin.site.unregister(TokenProxy)
from .views import Health
from . import dashboard

from django.views.decorators.cache import never_cache
from . import authviews as auth
from . import operationviews as operations
from . import machineviews as machine
from . import telemetryviews as telemetry
from . import terminalviews as terminal
from . import endpointviews as endpoint
from .buildviews import BuildProject
from . import projectionviews as projection
from .usageviews import Usage

from mop.server.gateway.views import gateway_view

urlpatterns = [
    path('llm/management.html', gateway_view),
    path('llm/raw/management.html', gateway_view),
    path('v8/management/<path:resource>', gateway_view),
    path('admin/', admin.site.urls), path('api/v1/health', Health.as_view()),
    path('api/v1/usage', Usage.as_view()),
    path('api/v1/auth/csrf', auth.csrf),
    path('api/v1/auth/login', never_cache(auth.Login.as_view())),
    path('api/v1/auth/logout', never_cache(auth.Logout.as_view())),
    path('api/v1/auth/token', auth.IssueToken.as_view()),
    path('api/v1/auth/token/rotate', auth.RotateToken.as_view()),
    path('api/v1/auth/token/revoke', auth.RevokeToken.as_view()),
    path('api/v1/machine/work', machine.Work.as_view()),
    path('api/v1/machine/work/input', machine.ActionInput.as_view()),
    path('api/v1/machine/executor', machine.ExecutorState.as_view()),
    path('api/v1/machine/work/claim', machine.Claim.as_view()),
    path('api/v1/machine/work/result', machine.Result.as_view()),
    path('api/v1/machine/executor/restarted', machine.Restarted.as_view()),
    path('api/v1/machine/facts', machine.Facts.as_view()),
    path('api/v1/machine/telemetry', telemetry.Ingest.as_view()),
    path('api/v1/sessions/telemetry', telemetry.Latest.as_view()),
    path('api/v1/sessions/history', telemetry.History.as_view()),
    path('api/v1/sessions/history/reset', telemetry.ResetHistory.as_view()),
    path('api/v1/terminal/attachments', terminal.Open.as_view()),
    path('api/v1/terminal/attachments/<uuid:attachment_id>', terminal.Close.as_view()),
    path('api/v1/endpoints', endpoint.Register.as_view()),
    path('api/v1/endpoints/<uuid:endpoint_id>', endpoint.Renew.as_view()),
    path('api/v1/endpoints/<uuid:endpoint_id>/renew', endpoint.Renew.as_view()),
    path('api/v1/addresses', endpoint.Addresses.as_view()),
    path('api/v1/addresses/<uuid:endpoint_id>', endpoint.Address.as_view()),
    path('api/v1/reports', endpoint.Reports.as_view()),
    path('api/v1/deliveries/<uuid:delivery_id>', endpoint.Delivery.as_view()),
    path('api/v1/deliveries/<uuid:delivery_id>/ack', endpoint.Ack.as_view()),
    path('api/v1/puppets', projection.PoolSnapshot.as_view()),
    path('api/v1/puppets/<uuid:target_id>', projection.PuppetDetail.as_view()),
    path('api/v1/snapshot', projection.PoolSnapshot.as_view()),
    path('api/v1/events', projection.ProjectionEvents.as_view()),
    path('api/v1/machine/observations', projection.Observations.as_view()),
    path('api/v1/machine/observation-producers', projection.Producers.as_view()),
    path('api/v1/machine/observation-producers/<uuid:target_id>/<str:kind>', projection.ProducerStatus.as_view()),
    path('api/v1/machine/observation-context', projection.ObservationContext.as_view()),
    path('api/v1/identity', auth.Identity.as_view()),
    path('api/v1/projects', auth.Projects.as_view()),
    path('api/v1/projects/<uuid:project_id>', auth.Project.as_view()),
    path('api/v1/projects/<uuid:project_id>/commands/build', BuildProject.as_view()),
    path('api/v1/nodes/<uuid:node_id>/commands/drain', operations.DrainNode.as_view()),
    path('api/v1/nodes/<uuid:node_id>/commands/enable', operations.EnableNode.as_view()),
    path('api/v1/puppets/<uuid:target_id>/commands/restart', operations.RestartPuppet.as_view()),
    path('api/v1/operations', operations.RequestLookup.as_view()),
    path('api/v1/operations/<uuid:operation_id>', operations.OperationStatus.as_view()),
    # Упакованный дашборд (#627/#644): тот же origin, что API; глубже
    # маршрутов страница сама разбирает hash-путь. Плоское имя -- в самой
    # отдаче, здесь только буквенно-цифровые с точкой и дефисом.
    path('', dashboard.index),
    re_path(r'^assets/(?P<name>[A-Za-z0-9][A-Za-z0-9._-]*)$', dashboard.asset),
]
