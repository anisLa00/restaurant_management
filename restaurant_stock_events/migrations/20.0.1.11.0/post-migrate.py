import logging

from odoo import SUPERUSER_ID, api


_logger = logging.getLogger(__name__)


def migrate(cr, version):
    """Classify only active Daily Stock lines; closed history stays frozen."""
    env = api.Environment(cr, SUPERUSER_ID, {})
    Line = env["restaurant.stock.daily.line"]

    active_lines = Line.search([
        ("daily_state", "not in", ("closed", "cancelled")),
    ])
    active_lines._refresh_operation_mode_from_setup()

    _logger.info(
        "Daily Stock operation split classified %s active lines; closed and "
        "cancelled history remained in its legacy mode.",
        len(active_lines),
    )
