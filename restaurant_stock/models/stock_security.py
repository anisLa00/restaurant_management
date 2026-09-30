from odoo.exceptions import AccessError


STOCKKEEPER_GROUP = "restaurant_core.group_restaurant_stockkeeper"
CENTRAL_STOREKEEPER_GROUP = "restaurant_core.group_restaurant_central_storekeeper"
MANAGER_GROUP = "restaurant_core.group_restaurant_branch_manager"
OPERATIONS_GROUP = "restaurant_core.group_restaurant_operations_manager"
OWNER_GROUP = "restaurant_core.group_restaurant_owner"
PURCHASING_GROUP = "restaurant_core.group_restaurant_purchasing"


def require_role(env, group):
    if not env.su and not env.user.has_group(group):
        raise AccessError(env._("Your role does not allow this operation."))


def has_all_branch_stock_access(env):
    return (
        env.su
        or env.user.has_group(CENTRAL_STOREKEEPER_GROUP)
        or env.user.has_group(OPERATIONS_GROUP)
        or env.user.has_group(OWNER_GROUP)
    )


def require_assigned_branches(branches):
    if not branches:
        return

    branches.check_access("read")

    if branches.company_id - branches.env.companies:
        raise AccessError(
            branches.env._("Select an authorized company for this branch.")
        )

    if not has_all_branch_stock_access(branches.env):
        assigned = branches.sudo().search([
            ("id", "in", branches.ids),
            ("user_ids", "in", [branches.env.uid]),
        ])

        if branches - assigned:
            raise AccessError(
                branches.env._(
                    "You are not assigned to this restaurant branch."
                )
            )


def lock_records(records, operation="write"):
    records.check_access(operation)
    records.lock_for_update()
    records.invalidate_recordset(["state"])
