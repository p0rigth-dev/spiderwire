"""SpiderFarmer GSS Modbus protocol library.

Shared between the ``gss-ctrl`` CLI and the SpiderFarmer Home Assistant
integration. See ``docs/protocol-analysis.md`` and ``docs/device-map.md``
for the bus reference.
"""

from importlib.metadata import PackageNotFoundError, version

from .bus import (
    DEFAULT_ACTUATOR_INTERVAL,
    DEFAULT_FAST_INTERVAL,
    DEFAULT_HEARTBEAT_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    BusMaster,
)
from .protocol import (
    CRCError,
    ExceptionResponse,
    ModbusError,
    ModbusTimeoutError,
)
from .registers import (
    BlowerData,
    CO2SensorData,
    DeviceData,
    DeviceHeader,
    DeviceType,
    FanControllerData,
    SensorHubData,
    parse_device_data,
)
from .transport import RS485Transport

try:
    __version__ = version("spiderwire")
except PackageNotFoundError:
    __version__ = "0.0.0+unknown"

__all__ = [
    "BlowerData",
    "BusMaster",
    "CO2SensorData",
    "CRCError",
    "DEFAULT_ACTUATOR_INTERVAL",
    "DEFAULT_FAST_INTERVAL",
    "DEFAULT_HEARTBEAT_INTERVAL",
    "DEFAULT_SCAN_INTERVAL",
    "DeviceData",
    "DeviceHeader",
    "DeviceType",
    "ExceptionResponse",
    "FanControllerData",
    "ModbusError",
    "ModbusTimeoutError",
    "RS485Transport",
    "SensorHubData",
    "__version__",
    "parse_device_data",
]
