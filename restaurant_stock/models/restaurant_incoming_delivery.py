from odoo import fields, models, tools


class RestaurantIncomingDelivery(models.Model):
    """Read-only index of every delivery that a branch can receive."""

    _name = "restaurant.incoming.delivery"
    _description = "Restaurant Incoming Delivery"
    _auto = False
    _order = "delivery_date desc, id desc"

    name = fields.Char(string="Reference", readonly=True)
    delivery_type = fields.Selection(
        [
            ("request", "Stock Request"),
            ("distribution", "Direct Distribution"),
        ],
        string="Origin",
        readonly=True,
    )
    delivery_date = fields.Date(string="Date", readonly=True)
    branch_id = fields.Many2one("restaurant.branch", readonly=True)
    company_id = fields.Many2one("res.company", readonly=True)
    delivery_status = fields.Selection(
        [
            ("requested", "Requested"),
            ("confirmed", "Confirmed"),
            ("delivered", "Delivered"),
            ("received", "Received"),
            ("rejected", "Rejected"),
            ("cancelled", "Cancelled"),
        ],
        string="Status",
        readonly=True,
    )
    request_id = fields.Many2one("restaurant.stock.request", readonly=True)
    distribution_id = fields.Many2one(
        "restaurant.stock.distribution",
        readonly=True,
    )

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE VIEW restaurant_incoming_delivery AS (
                SELECT
                    request.id * 2 AS id,
                    request.name AS name,
                    'request'::varchar AS delivery_type,
                    request.request_date AS delivery_date,
                    request.branch_id AS branch_id,
                    request.company_id AS company_id,
                    request.branch_status AS delivery_status,
                    request.id AS request_id,
                    NULL::integer AS distribution_id
                FROM restaurant_stock_request request

                UNION ALL

                SELECT
                    distribution.id * 2 + 1 AS id,
                    distribution.name AS name,
                    'distribution'::varchar AS delivery_type,
                    distribution.distribution_date AS delivery_date,
                    distribution.branch_id AS branch_id,
                    distribution.company_id AS company_id,
                    CASE distribution.state
                        WHEN 'draft' THEN 'confirmed'
                        WHEN 'dispatched' THEN 'delivered'
                        WHEN 'received' THEN 'received'
                        WHEN 'cancelled' THEN 'cancelled'
                    END::varchar AS delivery_status,
                    NULL::integer AS request_id,
                    distribution.id AS distribution_id
                FROM restaurant_stock_distribution distribution
            )
        """)

    def action_open_delivery(self):
        self.ensure_one()

        if self.delivery_type == "request":
            source = self.request_id
            view = self.env.ref(
                "restaurant_stock.restaurant_stock_incoming_view_form"
            )
        else:
            source = self.distribution_id
            view = self.env.ref(
                "restaurant_stock.restaurant_stock_distribution_view_form"
            )

        source.check_access("read")
        return {
            "type": "ir.actions.act_window",
            "name": self.name,
            "res_model": source._name,
            "res_id": source.id,
            "view_mode": "form",
            "views": [(view.id, "form")],
            "target": "current",
            "context": {"create": False},
        }
