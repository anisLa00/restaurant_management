from . import models


def post_init_hook(env):
    env["discuss.channel"].sudo()._sync_restaurant_management_conversations()
