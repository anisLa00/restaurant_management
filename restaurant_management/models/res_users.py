from odoo import api, models


class ResUsers(models.Model):
    _inherit = "res.users"

    def _schedule_restaurant_management_sync(self):
        cron = self.env.ref(
            "restaurant_management.ir_cron_sync_management_conversations",
            raise_if_not_found=False,
        )
        if cron:
            cron.sudo()._trigger()

    @api.model_create_multi
    def create(self, vals_list):
        users = super().create(vals_list)
        users._schedule_restaurant_management_sync()
        return users

    def write(self, vals):
        result = super().write(vals)
        if {
            "active",
            "group_ids",
            "company_ids",
            "restaurant_branch_ids",
        } & set(vals):
            self._schedule_restaurant_management_sync()
        return result
