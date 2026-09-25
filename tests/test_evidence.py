"""The authoritative view must discard the generator's decoy scenario.

Every case ships one authoritative scenario plus one decoy injected at a shifted
timestamp. These tests pin the three anchors that separate them.
"""

from __future__ import annotations

from decimal import Decimal

from student_agent.evidence import build_view

ORDER = {
    "order_id": "order-1",
    "customer_id": "customer-1",
    "order_status": "delivered",
    "order_purchase_timestamp": "2018-02-19T09:00:00-03:00",
    "order_approved_at": "2018-02-19T10:00:00-03:00",
    "order_delivered_carrier_date": "2018-02-26T09:00:00-03:00",
    "order_delivered_customer_date": "2018-03-05T09:00:00-03:00",
    "order_estimated_delivery_date": "2018-03-01T09:00:00-03:00",
}

ITEMS = [
    {
        "order_item_id": "item-1",
        "seller_id": "seller-1",
        "shipping_limit_date": "2018-02-22T09:00:00-03:00",
        "price": "79.00",
        "freight_value": "18.00",
    },
    {
        # Decoy: a second row for the same item with a shifted limit.
        "order_item_id": "item-1",
        "seller_id": "seller-1",
        "shipping_limit_date": "2018-03-12T09:00:00-03:00",
        "price": "79.00",
        "freight_value": "10.00",
    },
]

TIMELINE = {
    "events": [
        {"event_at": "2018-02-19T10:00:00-03:00", "event_type": "captured",
         "amount_brl": "18.00", "status": "confirmed"},
        # Decoy: captured three weeks off the approval date.
        {"event_at": "2018-03-09T10:00:00-03:00", "event_type": "captured",
         "amount_brl": "79.00", "status": "confirmed"},
    ]
}

PAYMENTS = [
    {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "18.00"},
    {"payment_sequential": "1", "payment_type": "credit_card", "payment_value": "79.00"},
]

SHIPMENT = {
    "events": [
        {"event_at": "2018-03-05T09:00:00-03:00", "event_type": "delivered_late",
         "actor": "seller", "status": "confirmed"},
        # Decoy: an event that does not match the authoritative delivery date.
        {"event_at": "2018-05-25T09:00:00-03:00", "event_type": "delivered_late",
         "actor": "logistics_provider", "status": "confirmed"},
    ]
}


def _view(**overrides):
    payload = {
        "order": ORDER,
        "items": ITEMS,
        "payments": PAYMENTS,
        "payment_timeline": TIMELINE,
        "refund_timeline": None,
        "shipment": SHIPMENT,
        "sellers": [],
    }
    payload.update(overrides)
    return build_view(**payload)


def test_item_row_is_anchored_to_purchase_date() -> None:
    view = _view()
    assert view.order_total == Decimal("97.00")
    assert view.shipping_limit_at.isoformat().startswith("2018-02-22")


def test_capture_off_the_approval_date_is_discarded() -> None:
    view = _view()
    assert [event["amount_brl"] for event in view.captures] == ["18.00"]
    assert view.paid_total == Decimal("18.00")
    assert view.payment_references == ["order-1-1"]


def test_shipment_event_must_match_the_delivery_timestamp() -> None:
    view = _view()
    assert [event["actor"] for event in view.late_events] == ["seller"]
    assert view.delivered_late is True
    assert view.seller_missed_handoff is True


def test_refund_is_kept_only_when_it_settles_an_authoritative_capture() -> None:
    refunds = {
        "events": [
            {"event_at": "2018-03-10T09:00:00-03:00", "event_type": "refund_requested",
             "amount_brl": "18.00", "status": "failed"},
            # Decoy: settles the discarded 79.00 capture.
            {"event_at": "2018-03-20T09:00:00-03:00", "event_type": "refund_requested",
             "amount_brl": "79.00", "status": "pending"},
        ]
    }
    view = _view(refund_timeline=refunds)
    assert [event["status"] for event in view.refunds] == ["failed"]


def test_every_discarded_source_is_reported_as_a_conflict() -> None:
    view = _view()
    fields = {conflict.field for conflict in view.conflicts}
    assert "order_items.shipping_limit_date" in fields
    assert "payment_timeline.events.event_at" in fields
    assert "shipment.events.event_at" in fields
    assert all(len(conflict.sources) >= 2 for conflict in view.conflicts)


def test_missing_order_yields_an_empty_view() -> None:
    view = build_view(
        order=None, items=None, payments=None, payment_timeline=None,
        refund_timeline=None, shipment=None, sellers=None,
    )
    assert view.order == {}
    assert view.captures == []
