from odoo.exceptions import AccessError, ValidationError


RECEPTION_GROUP = "restaurant_core.group_restaurant_reception"
MANAGER_GROUP = "restaurant_core.group_restaurant_branch_manager"
HR_GROUP = "restaurant_core.group_restaurant_hr"
OPERATIONS_GROUP = "restaurant_core.group_restaurant_operations_manager"
OWNER_GROUP = "restaurant_core.group_restaurant_owner"


def require_role(env, group):
    if not env.su and not env.user.has_group(group):
        raise AccessError(
            env._("Your role does not allow this operation.")
        )


def has_all_branch_access(env):
    """Owner and Operations Manager can operate across all branches."""
    return (
        env.su
        or env.user.has_group(OWNER_GROUP)
        or env.user.has_group(OPERATIONS_GROUP)
    )


def require_assigned_branches(branches):
    if not branches:
        return

    branches.check_access("read")

    if branches.company_id - branches.env.companies:
        raise AccessError(
            branches.env._(
                "Select an authorized company for this branch."
            )
        )

    # Owner / Operations Manager are not required to be manually
    # assigned to every restaurant branch.
    if not has_all_branch_access(branches.env):
        assigned = branches.sudo().search([
            ("id", "in", branches.ids),
            ("user_ids", "in", [branches.env.uid]),
        ])

        if branches - assigned:
            raise AccessError(
                branches.env._(
                    "You must be assigned to this restaurant branch."
                )
            )

    if any(not branch.active for branch in branches):
        raise ValidationError(
            branches.env._(
                "Choose an active restaurant branch."
            )
        )


def check_employee_company(records):
    for record in records:
        employee = record.env["hr.employee.public"].browse(
            record.employee_id.id
        )
        employee.check_access("read")

        if employee.company_id != record.company_id:
            raise ValidationError(
                record.env._(
                    "The employee and branch must belong to the same company."
                )
            )


def lock_records(records, operation="write"):
    records.check_access(operation)
    records.lock_for_update()
    records.invalidate_recordset(["state"])


def require_draft(records):
    if any(record.state != "draft" for record in records):
        raise AccessError(
            records.env._(
                "Only draft records can be edited or deleted."
            )
        )