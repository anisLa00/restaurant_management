from datetime import datetime, time, timedelta

import pytz

from odoo import fields
from odoo.exceptions import AccessError


STOCKKEEPER_GROUP = "restaurant_core.group_restaurant_stockkeeper"
CENTRAL_STOREKEEPER_GROUP = "restaurant_core.group_restaurant_central_storekeeper"
MANAGER_GROUP = "restaurant_core.group_restaurant_branch_manager"
OPERATIONS_GROUP = "restaurant_core.group_restaurant_operations_manager"
OWNER_GROUP = "restaurant_core.group_restaurant_owner"
PURCHASING_GROUP = "restaurant_core.group_restaurant_purchasing"


def get_company_business_timezone(company):
    """Return the configured company timezone used for stock reporting."""
    return pytz.timezone(company.tz or "UTC")


def get_company_business_date(company, timestamp=None):
    """Convert a stored UTC timestamp to the company's local date."""
    value = fields.Datetime.to_datetime(timestamp or fields.Datetime.now())
    if value.tzinfo is None:
        value = pytz.UTC.localize(value)
    else:
        value = value.astimezone(pytz.UTC)
    return value.astimezone(get_company_business_timezone(company)).date()


def get_company_day_utc_range(company, business_date):
    """Return UTC-naive bounds for one company-local calendar day."""
    business_date = fields.Date.to_date(business_date)
    timezone = get_company_business_timezone(company)
    local_start = timezone.localize(datetime.combine(business_date, time.min))
    local_end = timezone.localize(
        datetime.combine(business_date + timedelta(days=1), time.min)
    )
    return (
        local_start.astimezone(pytz.UTC).replace(tzinfo=None),
        local_end.astimezone(pytz.UTC).replace(tzinfo=None),
    )


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
