"""Load integration modules without starting Home Assistant in unit tests."""
import sys
from pathlib import Path
from types import ModuleType

integration_directory = Path(__file__).resolve().parents[1] / "custom_components" / "telegram_access_registrar"
if "telegram_access_registrar" not in sys.modules:
    package = ModuleType("telegram_access_registrar")
    package.__path__ = [str(integration_directory)]
    sys.modules[package.__name__] = package
