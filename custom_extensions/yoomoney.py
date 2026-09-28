"""YooMoney payment provider declaration.

The transport client remains in app.services.payments; this extension advertises
its capabilities to the registry without copying secrets into extension state.
"""
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class YooMoneyProvider:
    code: str = "yoomoney"
    title: str = "YooMoney"
    category: str = "custom"
    supports_create: bool = True
    supports_polling: bool = True
    supports_webhook: bool = False


def setup(registry):
    registry.register_payment_providers("yoomoney", YooMoneyProvider())
