"""Начальный ready-evidence query: настоящий PostgreSQL и verified HTTPS (#749)."""
import sys
import _lib  # noqa: F401
import hermetic  # noqa: F401
from _controller_fixture import main

if __name__ == '__main__':
    sys.exit(main(['_ready_evidence_cases.ReadyEvidence',
        '_create_cases.CreateMachineSteps', '_create_cases.CreateActivation',
        '_initial_create_cases.InitialCreatePhases', '_management_context_cases.ActivatedManagementContext']))
