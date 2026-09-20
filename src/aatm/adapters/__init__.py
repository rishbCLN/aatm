"""Tool adapters + deterministic mock world."""

from .base import BaseAdapter, MockWorldState, ToolAdapter
from .failures import CrashSignal, FailureInjector, FailureRule, InjectedFailure
from .mock_crm import CRMAdapter
from .mock_email import EmailAdapter
from .mock_payment import PaymentAdapter
from .mock_travel import TravelAdapter
from .registry import AdapterRegistry, default_registry

__all__ = [
    "ToolAdapter",
    "BaseAdapter",
    "MockWorldState",
    "CrashSignal",
    "FailureInjector",
    "FailureRule",
    "InjectedFailure",
    "TravelAdapter",
    "PaymentAdapter",
    "EmailAdapter",
    "CRMAdapter",
    "AdapterRegistry",
    "default_registry",
]
