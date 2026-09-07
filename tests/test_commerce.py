import pytest
from app.modules.commerce.service import CommerceService

def test_razorpay_hmac_signature():
    order_id = "order_EKwxwAgItmmXdp"
    payment_id = "pay_29QQoUBcxQtvFt"
    secret = "enEnvironmentSecretKey123"

    import hmac
    import hashlib
    expected = hmac.new(secret.encode("utf-8"), f"{order_id}|{payment_id}".encode("utf-8"), hashlib.sha256).hexdigest()

    is_valid = CommerceService.verify_razorpay_signature(order_id, payment_id, expected, secret)
    assert is_valid is True

    is_invalid = CommerceService.verify_razorpay_signature(order_id, payment_id, "bogus_signature", secret)
    assert is_invalid is False
