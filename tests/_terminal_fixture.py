"""Отдельный interpreter gateway fence; framework включается только в worker."""
import os
import time


def claim(bootstrap, actor, target, incarnation, start):
    os.environ['MOP_CONTROLLER_BOOTSTRAP']=bootstrap
    from mop.server.controller.runtime import configure, close_pools
    configure()
    from mop.server.sessions.terminal_control import Terminals, Conflict
    while time.time()<start:
        time.sleep(.01)
    try:
        try:
            return Terminals().open(actor,target_id=target,incarnation_id=incarnation,mode='rw',cols=100,rows=30)['id']
        except Conflict:
            return None
    finally:
        close_pools()
