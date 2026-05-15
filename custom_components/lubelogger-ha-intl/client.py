"""Client for interacting with LubeLogger API."""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Any

import aiohttp
from homeassistant.util import dt as dt_util

from .const import (
    API_ROOT,
    API_ADJUSTED_ODOMETER,
    API_GAS_RECORD,
    API_ODOMETER,
    API_PLAN,
    API_REMINDER,
    API_REPAIR_RECORD,
    API_SERVICE_RECORD,
    API_SUPPLY_RECORD,
    API_TAX,
    API_UPGRADE_RECORD,
    API_VEHICLES,
    API_EQUIPMENT_RECORD,
)

_LOGGER = logging.getLogger(__name__)


def parse_date_string(date_str: str) -> datetime | None:
    """Parse a date string in multiple formats and return timezone-aware datetime."""
    if not date_str:
        return None

    # Try ISO format first (handles timezone-aware strings)
    try:
        if date_str.endswith("Z"):
            date_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(date_str)
        # Ensure timezone-aware - use UTC if no timezone info
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_util.UTC)
        return dt
    except (ValueError, AttributeError):
        pass

    # Try European formats first, then US formats
    formats = [
        "%d/%m/%Y",           # European format: "28/02/2027"
        "%d/%m/%Y %H:%M:%S",  # European with time
        "%m/%d/%Y",           # US format: "12/17/2025"
        "%m/%d/%Y %H:%M:%S",  # US with time
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d",
    ]

    for fmt in formats:
        try:
            dt = datetime.strptime(date_str, fmt)
            # Make timezone-aware (assume local timezone)
            if dt.tzinfo is None:
                dt = dt_util.as_local(dt)
            return dt
        except (ValueError, AttributeError):
            continue

    return None




def get_latest_record_sort_key(
    rec: dict[str, Any],
    date_fields: tuple[str, ...],
) -> tuple[int, datetime, int]:
    """Return a stable sort key for latest record selection.

    Prefers records with a parseable date, then by that date, then by numeric ID.
    """
    for field in date_fields:
        date_str = rec.get(field)
        if date_str:
            dt = parse_date_string(date_str)
            if dt:
                rec_id = rec.get("id") or rec.get("Id")
                try:
                    rec_id_int = int(rec_id) if rec_id is not None else 0
                except (TypeError, ValueError):
                    rec_id_int = 0
                return (1, dt, rec_id_int)

    rec_id = rec.get("id") or rec.get("Id")
    try:
        rec_id_int = int(rec_id) if rec_id is not None else 0
    except (TypeError, ValueError):
        rec_id_int = 0

    return (0, datetime.min.replace(tzinfo=dt_util.UTC), rec_id_int)


def calculate_reminder_priority(reminder: dict[str, Any]) -> tuple:
    """Calculate priority for reminder sorting.
    
    Returns (priority_value, days, distance) where:
    - Lower priority_value = higher priority
    - Priority order: PastDue (negative) > Urgent (0) > NotUrgent (positive)
    - For same urgency: use dueDays or dueDistance
    """
    # Get urgency
    urgency = reminder.get("urgency", "")
    
    # Get values as numbers
    due_days_str = reminder.get("dueDays", "")
    due_distance_str = reminder.get("dueDistance", "")
    
    try:
        due_days = int(due_days_str) if due_days_str not in [None, "", "null"] else 999999
    except (ValueError, TypeError):
        due_days = 999999
    
    try:
        due_distance = float(due_distance_str) if due_distance_str not in [None, "", "null"] else 999999
    except (ValueError, TypeError):
        due_distance = 999999
    
    # Base priority by urgency
    if urgency == "PastDue":
        base_priority = -10000  # Highest priority
    elif urgency == "Urgent":
        base_priority = 0
    else:  # "NotUrgent" or unknown
        base_priority = 10000
    
    # Get metric type
    metric = reminder.get("metric", "")
    
    # Calculate specific priority based on metric
    if "Date" in metric and "Odometer" not in metric:
        # Pure date reminder - use due days
        if due_days < 0:  # Overdue
            specific_priority = abs(due_days) * 10  # More overdue = higher priority
        else:
            specific_priority = due_days
    elif "Odometer" in metric and "Date" not in metric:
        # Pure odometer reminder
        if due_distance < 0:  # Overdue
            specific_priority = abs(due_distance) * 10
        else:
            specific_priority = due_distance
    else:
        # Both or unknown - use whichever is closer
        if due_days < 0 or due_distance < 0:  # Overdue in at least one
            specific_priority = min(abs(due_days), abs(due_distance)) * 10
        else:
            specific_priority = min(due_days, due_distance)
    
    # Combine base priority and specific priority
    priority = base_priority + specific_priority
    
    return (priority, due_days, due_distance)


class LubeLoggerClient:
    """Client for LubeLogger API."""

    def __init__(
        self,
        url: str,
        username: str,
        password: str,
        session: aiohttp.ClientSession | None = None,
    ) -> None:
        """Initialize the client."""
        self._url = url.rstrip("/")
        self._username = username
        self._password = password
        self._session = session
        self._auth = aiohttp.BasicAuth(username, password)

    async def async_get_vehicles(self) -> list[dict[str, Any]]:
        """Get all vehicles from LubeLogger."""
        vehicles = await self._async_request(API_VEHICLES)
        if not isinstance(vehicles, list):
            return []
        return vehicles

    async def async_get_latest_odometer(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest odometer record for a vehicle."""
        if vehicle_id:
            try:
                endpoint = f"{API_ADJUSTED_ODOMETER}?vehicleId={vehicle_id}"
                adjusted = await self._async_request(endpoint)
                if adjusted and isinstance(adjusted, dict):
                    _LOGGER.debug("Using adjusted odometer for vehicle %s: %s", vehicle_id, adjusted)
                    return {"odometer": adjusted, "adjusted": True}
            except Exception as err:
                _LOGGER.debug("Adjusted odometer not available for vehicle %s: %s", vehicle_id, err)
        
        endpoint = f"{API_ODOMETER}?vehicleId={vehicle_id}" if vehicle_id else API_ODOMETER
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No odometer records found for vehicle %s", vehicle_id)
            return None

        def odometer_value(rec: dict[str, Any]) -> float:
            value = rec.get("odometer") or rec.get("Odometer")
            if value is None:
                return float("-inf")
            if isinstance(value, (int, float)):
                return float(value)
            try:
                return float(str(value).replace(",", "."))
            except (TypeError, ValueError):
                return float("-inf")

        latest = max(
            records,
            key=lambda rec: (
                odometer_value(rec),
                get_latest_record_sort_key(rec, ("date", "Date", "odometerDate", "OdometerDate")),
            ),
        )
        _LOGGER.debug("Latest odometer for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_next_plan(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the next upcoming plan item for a vehicle."""
        endpoint = f"{API_PLAN}?vehicleId={vehicle_id}" if vehicle_id else API_PLAN
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No plan records found for vehicle %s", vehicle_id)
            return None
        
        def sort_key(rec: dict[str, Any]) -> Any:
            date_str = rec.get("dateCreated") or rec.get("dateModified") or rec.get("Date") or rec.get("date")
            if date_str:
                dt = parse_date_string(date_str)
                if dt:
                    return dt
            return datetime.max
        
        sorted_records = sorted([r for r in records if sort_key(r) != datetime.max], key=sort_key)
        if sorted_records:
            _LOGGER.debug("Next plan for vehicle %s: %s", vehicle_id, sorted_records[0])
            return sorted_records[0]
        return None

    async def async_get_latest_tax(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest tax record for a vehicle."""
        endpoint = f"{API_TAX}?vehicleId={vehicle_id}" if vehicle_id else API_TAX
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No tax records found for vehicle %s", vehicle_id)
            return None

        sorted_records = sorted(records, key=lambda rec: get_latest_record_sort_key(rec, ("date", "Date", "taxDate")))
        latest = sorted_records[-1] if sorted_records else None
        _LOGGER.debug("Latest tax for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_latest_service(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest service record for a vehicle."""
        endpoint = f"{API_SERVICE_RECORD}?vehicleId={vehicle_id}" if vehicle_id else API_SERVICE_RECORD
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No service records found for vehicle %s", vehicle_id)
            return None

        sorted_records = sorted(records, key=lambda rec: get_latest_record_sort_key(rec, ("date", "Date", "serviceDate")))
        latest = sorted_records[-1] if sorted_records else None
        _LOGGER.debug("Latest service for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_latest_repair(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest repair record for a vehicle."""
        endpoint = f"{API_REPAIR_RECORD}?vehicleId={vehicle_id}" if vehicle_id else API_REPAIR_RECORD
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No repair records found for vehicle %s", vehicle_id)
            return None

        sorted_records = sorted(records, key=lambda rec: get_latest_record_sort_key(rec, ("date", "Date", "repairDate")))
        latest = sorted_records[-1] if sorted_records else None
        _LOGGER.debug("Latest repair for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_latest_upgrade(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest upgrade record for a vehicle."""
        endpoint = f"{API_UPGRADE_RECORD}?vehicleId={vehicle_id}" if vehicle_id else API_UPGRADE_RECORD
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No upgrade records found for vehicle %s", vehicle_id)
            return None

        sorted_records = sorted(records, key=lambda rec: get_latest_record_sort_key(rec, ("date", "Date", "upgradeDate")))
        latest = sorted_records[-1] if sorted_records else None
        _LOGGER.debug("Latest upgrade for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_latest_supply(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest supply record for a vehicle."""
        endpoint = f"{API_SUPPLY_RECORD}?vehicleId={vehicle_id}" if vehicle_id else API_SUPPLY_RECORD
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No supply records found for vehicle %s", vehicle_id)
            return None

        sorted_records = sorted(records, key=lambda rec: get_latest_record_sort_key(rec, ("date", "Date", "supplyDate")))
        latest = sorted_records[-1] if sorted_records else None
        _LOGGER.debug("Latest supply for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_latest_gas(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest gas/fuel record for a vehicle."""
        endpoint = f"{API_GAS_RECORD}?vehicleId={vehicle_id}" if vehicle_id else API_GAS_RECORD
        records = await self._async_request(endpoint)
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No gas records found for vehicle %s", vehicle_id)
            return None

        sorted_records = sorted(records, key=lambda rec: get_latest_record_sort_key(rec, ("date", "Date", "fuelDate", "FuelDate")))
        latest = sorted_records[-1] if sorted_records else None
        _LOGGER.debug("Latest gas for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_next_reminder(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the next upcoming reminder for a vehicle."""
        endpoint = f"{API_REMINDER}?vehicleId={vehicle_id}" if vehicle_id else API_REMINDER
        records = await self._async_request(endpoint)
        
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No reminders found for vehicle %s", vehicle_id)
            return None
        
        valid_records = []
        for record in records:
            if isinstance(record, dict) and record:
                valid_records.append(record)
        
        if not valid_records:
            _LOGGER.debug("No valid reminder records for vehicle %s", vehicle_id)
            return None
        
        sorted_records = sorted(valid_records, key=calculate_reminder_priority)
        
        if sorted_records:
            next_reminder = sorted_records[0]
            _LOGGER.debug("Selected next reminder for vehicle %s: %s", vehicle_id, next_reminder)
            return next_reminder
        
        return None

    async def async_get_latest_equipment(
        self, vehicle_id: int | None = None
    ) -> dict[str, Any] | None:
        """Get the latest equipment record for a vehicle."""
        endpoint = f"{API_EQUIPMENT_RECORD}?vehicleId={vehicle_id}" if vehicle_id else API_EQUIPMENT_RECORD
        records = await self._async_request(endpoint)
        
        if not isinstance(records, list) or not records:
            _LOGGER.debug("No equipment records found for vehicle %s", vehicle_id)
            return None

        sorted_records = sorted(records, key=lambda rec: get_latest_record_sort_key(rec, ("date", "Date", "equipmentDate", "EquipmentDate")))
        latest = sorted_records[-1] if sorted_records else None

        _LOGGER.debug("Latest equipment for vehicle %s: %s", vehicle_id, latest)
        return latest

    async def async_get_equipment_records(
        self, vehicle_id: int
    ) -> list[dict[str, Any]]:
        """Get all equipment records for a vehicle."""
        endpoint = f"{API_EQUIPMENT_RECORD}?vehicleId={vehicle_id}" if vehicle_id else API_EQUIPMENT_RECORD
        records = await self._async_request(endpoint)

        if not isinstance(records, list) or not records:
            _LOGGER.debug("No equipment records found for vehicle %s", vehicle_id)
            return []

        return records

    async def _async_request(
        self, endpoint: str, method: str = "GET", **kwargs: Any
    ) -> Any:
        """Make an async request to the LubeLogger API."""
        url = f"{self._url}{endpoint}"
        session = self._session or aiohttp.ClientSession()

        try:
            async with session.request(
                method,
                url,
                auth=self._auth,
                timeout=aiohttp.ClientTimeout(total=10),
                **kwargs,
            ) as response:
                if response.status == 404:
                    _LOGGER.debug("Endpoint not found: %s", url)
                    return []
                response.raise_for_status()
                if response.content_type == "application/json":
                    return await response.json()
                return await response.text()
        except aiohttp.ClientError as err:
            _LOGGER.error("Error communicating with LubeLogger API: %s", err)
            raise
        finally:
            if not self._session:
                await session.close()
