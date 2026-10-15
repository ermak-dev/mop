"""Узкий PostgreSQL/runtime gate; общий controller gate остаётся полным."""
import sys
from controller import main


if __name__ == '__main__':
    sys.exit(main([
        '_controller_cases.TransportDeadline',
        'controller_boundary.Materialization',
        'controller_boundary.Boundary.test_uncertain_commit_is_not_advertised_as_a_retryable_rollback',
        'controller_boundary.Boundary.test_orm_inputs_do_not_cross_the_thread_boundary',
        'controller_boundary.Boundary.test_lazy_values_do_not_cross_await',
        'controller_boundary.Boundary.test_real_asgi_server_and_worker_harness_own_lifecycle',
        '_controller_cases.Foundation.test_boundary_rejects_lazy_models_and_closes_connections',
        '_controller_cases.Foundation.test_cancellation_does_not_leak_connection_or_free_a_running_slot',
        '_controller_cases.Foundation.test_bounded_executor_and_async_adapter_ownership',
        '_controller_cases.Foundation.test_orm_boundary_does_not_block_heartbeat',
        '_controller_cases.Foundation.test_short_transaction_rolls_back_before_over_budget_commit',
        '_controller_cases.Foundation.test_observed_threads_and_database_connections_are_bounded_and_drained',
    ]))
