"""PayPal REST (Orders API v2) gateway helper.

PayPal does not support OMR or AED — every charge is placed in USD.
Booking amounts stay in their native currency everywhere else (the
Booking doctype, Payment Intent, ledger); conversion to USD happens
only at the point we talk to PayPal, using fixed pegs (both OMR and
AED are hard-pegged to USD, so a fixed rate is accurate, not an
approximation, and needs no live FX API call).

Credentials come from site_config only (never hardcoded):
  bench set-config -c <site> paypal_client_id "..."
  bench set-config -c <site> paypal_client_secret "..."
  bench set-config -c <site> paypal_sandbox 1   # 0 once live
"""

import frappe
from frappe.utils import get_url

# Fixed currency pegs (both are official, government-set pegs, not
# floating market rates — safe to hardcode).
# 1 OMR = 2.6008 USD (Central Bank of Oman peg)
# 1 AED = 1 / 3.6725 USD (UAE Central Bank peg)
FX_TO_USD = {
    "OMR": 2.6008,
    "AED": 1 / 3.6725,
    "USD": 1.0,
}


class PayPalError(Exception):
    pass


def to_usd(amount, currency):
    rate = FX_TO_USD.get((currency or "").upper())
    if not rate:
        frappe.throw(f"No USD conversion rate configured for currency '{currency}'.")
    return round(float(amount) * rate, 2)


def _base_url():
    return ("https://api-m.sandbox.paypal.com"
            if frappe.conf.get("paypal_sandbox") else "https://api-m.paypal.com")


def _get_access_token():
    client_id = frappe.conf.get("paypal_client_id")
    client_secret = frappe.conf.get("paypal_client_secret")
    if not client_id or not client_secret:
        raise PayPalError("PayPal credentials are not configured on this site.")

    import requests
    resp = requests.post(
        f"{_base_url()}/v1/oauth2/token",
        headers={"Accept": "application/json"},
        auth=(client_id, client_secret),
        data={"grant_type": "client_credentials"},
        timeout=20,
    )
    if resp.status_code != 200:
        frappe.log_error(resp.text, "PayPal OAuth token request failed")
        raise PayPalError("Could not authenticate with PayPal.")
    return resp.json()["access_token"]


def create_order(intent):
    """Creates a PayPal Order for this Payment Intent, converting its
    native-currency amount to USD. Returns (order_id, approval_url)."""
    import requests

    usd_amount = to_usd(intent.amount, intent.currency)
    token = _get_access_token()

    return_url = get_url(
        f"/car-marketplace/paypal-return?intent={intent.name}")
    cancel_url = get_url(
        f"/car-marketplace/paypal-return?intent={intent.name}&cancelled=1")

    body = {
        "intent": "CAPTURE",
        "purchase_units": [{
            "reference_id": intent.name,
            "custom_id": intent.booking,
            "amount": {
                "currency_code": "USD",
                "value": f"{usd_amount:.2f}",
            },
        }],
        "application_context": {
            "return_url": return_url,
            "cancel_url": cancel_url,
            "user_action": "PAY_NOW",
        },
    }

    resp = requests.post(
        f"{_base_url()}/v2/checkout/orders",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        json=body, timeout=20,
    )
    if resp.status_code not in (200, 201):
        frappe.log_error(resp.text, "PayPal create order failed")
        raise PayPalError("Could not start PayPal checkout.")

    data = resp.json()
    order_id = data["id"]
    approval_url = next(
        (l["href"] for l in data.get("links", []) if l.get("rel") == "approve"),
        None)
    if not approval_url:
        frappe.log_error(str(data), "PayPal order missing approval link")
        raise PayPalError("PayPal did not return a checkout link.")

    return order_id, approval_url


def get_order(order_id):
    """Fetches an order's current status directly from PayPal."""
    import requests

    token = _get_access_token()
    resp = requests.get(
        f"{_base_url()}/v2/checkout/orders/{order_id}",
        headers={"Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if resp.status_code != 200:
        frappe.log_error(resp.text, f"PayPal get order failed for {order_id}")
        raise PayPalError("Could not look up PayPal order.")
    return resp.json()


def capture_order(order_id):
    """Captures a previously-approved PayPal order. Returns the raw
    PayPal response dict. Caller must check status == 'COMPLETED'
    before treating it as a real payment — never trust the redirect
    alone.

    Idempotent: if this order was already captured on an earlier call
    (e.g. the return page loaded twice — browser reload, back button),
    PayPal rejects the second capture with ORDER_ALREADY_CAPTURED. In
    that case we look the order up directly instead of treating it as
    a failure — it genuinely succeeded, just not on this call."""
    import requests

    token = _get_access_token()
    resp = requests.post(
        f"{_base_url()}/v2/checkout/orders/{order_id}/capture",
        headers={"Content-Type": "application/json",
                 "Authorization": f"Bearer {token}"},
        timeout=20,
    )
    if resp.status_code in (200, 201):
        return resp.json()

    body_text = resp.text
    if "ORDER_ALREADY_CAPTURED" in body_text:
        return get_order(order_id)

    frappe.log_error(body_text, f"PayPal capture failed for order {order_id}")
    raise PayPalError("PayPal payment capture failed.")
