"""Domain exceptions with stable API error codes.
Never leak Python stack traces to customers — api.py maps these to
{"success": false, "error": {"code", "message"}}.
"""


class MarketplaceError(Exception):
    code = "MARKETPLACE_ERROR"
    http_status = 400
    default_message = "A marketplace error occurred."

    def __init__(self, message=None, **context):
        self.message = message or self.default_message
        self.context = context
        super().__init__(self.message)


class InvalidStateTransition(MarketplaceError):
    code = "INVALID_STATE_TRANSITION"
    default_message = "This action is not allowed in the current state."


class OfferExpired(MarketplaceError):
    code = "OFFER_EXPIRED"
    default_message = "The selected offer is no longer valid."


class TenderClosed(MarketplaceError):
    code = "TENDER_CLOSED"
    default_message = "This tender is closed."


class VehicleUnavailable(MarketplaceError):
    code = "VEHICLE_UNAVAILABLE"
    http_status = 409
    default_message = "The vehicle is no longer available for the selected period."


class SupplierNotEligible(MarketplaceError):
    code = "SUPPLIER_NOT_ELIGIBLE"
    http_status = 403
    default_message = "The supplier is not eligible for this tender."


class BookingNotFound(MarketplaceError):
    code = "BOOKING_NOT_FOUND"
    http_status = 404
    default_message = "Booking not found."


class UnauthorizedSupplierAccess(MarketplaceError):
    code = "UNAUTHORIZED_SUPPLIER_ACCESS"
    http_status = 403
    default_message = "You do not have access to this supplier resource."


class PaymentAlreadyProcessed(MarketplaceError):
    code = "PAYMENT_ALREADY_PROCESSED"
    default_message = "This payment has already been processed."


class RefundNotAllowed(MarketplaceError):
    code = "REFUND_NOT_ALLOWED"
    default_message = "A refund is not allowed for this booking."


class CancellationNotAllowed(MarketplaceError):
    code = "CANCELLATION_NOT_ALLOWED"
    default_message = "Cancellation is not allowed for this booking."


class IdempotencyConflict(MarketplaceError):
    code = "IDEMPOTENCY_CONFLICT"
    http_status = 409
    default_message = "The idempotency key was reused with a different payload."
