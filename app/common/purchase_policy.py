"""Shared purchase policy for public offers, orders, and booking responses."""
PURCHASE_POLICY_VERSION = "2026-10-21"
PURCHASE_TERMS = "Progress Rooms does not currently process refunds for classes, class packages, or membership passes."
PURCHASE_SUPPORT = "You may arrange a refund independently using your own payment or bank account. Progress Rooms does not approve, manage, or process these requests at present. You can choose whether to include your refund policy in the class or membership description shown to students."
BOOKING_CANCELLATION_TERMS = "Bookings can be cancelled at least 2 hours before class start. Unused sessions remain available within the pass validity."


def purchase_policy():
    # Public offers and checkout expose booking terms only. Refund guidance belongs to staff authoring.
    return {"version": PURCHASE_POLICY_VERSION, "terms": BOOKING_CANCELLATION_TERMS}
