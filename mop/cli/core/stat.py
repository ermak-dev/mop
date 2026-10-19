"""mop stat [--days N | --start <date> --end <date>] [--group-by day|model|key]

Query controller-owned proxy usage only. The UTC end date is exclusive; default
is 14 days including today, bounded to 366 days. Output preserves source coverage
and nullable counters. total is a parent metric, not a sum of all categories.
There is no transcript read, node scan, invented billing or missing-source zero.

Shared proxy keys cannot attribute usage to users, projects or puppets. --users,
--puppets and those groupings refuse explicitly before any query. Model/key/day
breakdowns retain the server's source provenance and incomplete coverage.
The explicit HTTPS origin/CA and transient Django token follow mop health.
"""
from mop.cli import _control

MCP = {"annotations": "readonly",
       "actions": ["execution.view_usage"], "args": [
    {"name": "days", "type": "integer", "flag": "--days", "help": "bounded UTC window, default 14 days"},
    {"name": "start", "type": "string", "flag": "--start", "help": "inclusive ISO date"},
    {"name": "end", "type": "string", "flag": "--end", "help": "exclusive ISO date"},
    {"name": "group_by", "type": "string", "flag": "--group-by", "help": "day, model or key"},
    {"name": "users", "type": "boolean", "flag": "--users", "help": "unavailable for shared proxy keys"},
    {"name": "puppets", "type": "boolean", "flag": "--puppets", "help": "unavailable for shared proxy keys"}]}


def main(argv):
    return _control.invoke('stat', argv)
