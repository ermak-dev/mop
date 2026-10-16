"""Имена непересекающихся/parent proxy-счётчиков публичного DTO v1.

total — отдельная метрика, не сумма всех остальных метрик. Clients сохраняют
nullable values и coverage; source completeness не выводится из пустой очереди.
"""
COUNTERS = ('total', 'unclassified', 'input', 'input_uncached', 'cache_read',
            'cache_write', 'output', 'output_non_reasoning', 'reasoning')
