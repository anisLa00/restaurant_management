from . import models


def post_init_hook(env):
    env["restaurant.accounting.daily.sales"].sudo()._sync_confirmed_closings()
