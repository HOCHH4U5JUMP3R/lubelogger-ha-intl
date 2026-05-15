"""Sensor platform for LubeLogger integration."""
from __future__ import annotations

from datetime import datetime
from typing import Any
import json
import os
import logging
import re

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity
from homeassistant.util import dt as dt_util

from .const import DOMAIN
from .coordinator import LubeLoggerDataUpdateCoordinator

_LOGGER = logging.getLogger(__name__)


def parse_date(date_str: str | None) -> datetime | None:
    """Parse a date string from LubeLogger API and return timezone-aware datetime."""
    if not date_str:
        return None

    # Try ISO format first
    try:
        if date_str.endswith("Z"):
            date_str = date_str.replace("Z", "+00:00")
        dt = datetime.fromisoformat(date_str.replace("Z", "+00:00"))
        # Ensure timezone-aware
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=dt_util.UTC)
        return dt
    except (ValueError, AttributeError):
        pass

    # US and EU date and time format
    formats = [
        "%d.%m.%Y",           # EU dotted format: "18.03.2026"
        "%d.%m.%Y %H:%M:%S",  # EU dotted format with time
        "%d/%m/%Y",           # EU format: "28/02/2027"
        "%d/%m/%Y %H:%M:%S",  # EU format with time
        "%m/%d/%Y",           # US format (fallback)
        "%m/%d/%Y %H:%M:%S",  # US format with time (fallback)
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


def convert_number_string(number_str: Any) -> float | int | str | None:
    """Convert a number string to a number, handling both European and International formats.
    
    European format: 1.234,56 -> 1234.56
    International format: 1,234.56 -> 1234.56
    """
    if number_str is None or number_str == "":
        return None
    
    if isinstance(number_str, (int, float)):
        return number_str
    
    if isinstance(number_str, str):
        original = number_str
        # Remove common currency symbols and trim
        number_str = number_str.replace('€', '').replace('$', '').replace('£', '').strip()
        
        # Helper to check if a part is likely a thousands group (exactly 3 digits)
        def is_thousands_part(part: str) -> bool:
            return part.isdigit() and len(part) == 3
        
        # Count separators
        comma_count = number_str.count(',')
        dot_count = number_str.count('.')
        
        # Case 1: Only one type of separator
        if comma_count == 1 and dot_count == 0:
            # e.g., "1234,56" or "1,234"
            parts = number_str.split(',')
            if len(parts) == 2 and not is_thousands_part(parts[1]):
                # Single comma with non-3-digit right part -> decimal comma
                number_str = number_str.replace(',', '.')
            else:
                # Could be a thousands comma (e.g., "1,234") -> remove it
                number_str = number_str.replace(',', '')
        
        elif dot_count == 1 and comma_count == 0:
            # e.g., "1234.56" or "1.234"
            parts = number_str.split('.')
            if len(parts) == 2 and not is_thousands_part(parts[1]):
                # Single dot with non-3-digit right part -> decimal dot, keep as is
                pass
            else:
                # Likely a thousands dot (e.g., "1.234") -> remove it
                number_str = number_str.replace('.', '')
        
        # Case 2: Both separators present (e.g., "1.234,56" or "1,234.56")
        elif comma_count > 0 and dot_count > 0:
            last_comma = number_str.rfind(',')
            last_dot = number_str.rfind('.')
            
            # Assume the LAST separator is the decimal point
            if last_comma > last_dot:
                # European: last separator is comma -> dot is thousands
                number_str = number_str.replace('.', '').replace(',', '.')
            else:
                # International: last separator is dot -> comma is thousands
                number_str = number_str.replace(',', '')
                # Dot remains as decimal
        
        # Case 3: Multiple separators of the same type (thousands)
        elif comma_count > 1:
            # e.g., "1,234,567"
            number_str = number_str.replace(',', '')
        elif dot_count > 1:
            # e.g., "1.234.567"
            number_str = number_str.replace('.', '')
        
        # Final conversion
        try:
            result = float(number_str)
            return int(result) if result.is_integer() else result
        except (ValueError, TypeError):
            # If conversion fails, return the cleaned original string
            return original.strip()
    
    return number_str


def _should_convert_numeric_string(value: Any) -> bool:
    """Return True when a value is likely numeric (and not a date/time)."""
    if not isinstance(value, str):
        return False
    text = value.strip()
    if not text:
        return False

    # Avoid converting date/time-like values
    if parse_date(text) is not None:
        return False
    if re.search(r"\d{1,2}[./-]\d{1,2}[./-]\d{2,4}", text):
        return False

    # Numeric-like pattern with optional sign/separators/currency
    return bool(re.fullmatch(r"[€$£]?\s*[-+]?\d[\d.,\s]*", text))


def _get_record_value(record: dict[str, Any], *keys: str) -> Any:
    """Get value by trying exact keys and then case-insensitive key matching."""
    for key in keys:
        if key in record:
            return record.get(key)
    lowered = {str(k).lower(): v for k, v in record.items()}
    for key in keys:
        value = lowered.get(key.lower())
        if value is not None:
            return value
    return None


def _get_record_datetime(record: dict[str, Any], fields: tuple[str, ...]) -> datetime | None:
    """Return first parseable datetime from candidate fields."""
    for field in fields:
        dt = parse_date(_get_record_value(record, field))
        if dt:
            return dt
    return None


def _get_extra_field_value(record: dict[str, Any], field_name: str) -> Any:
    """Return value from ExtraFields-style arrays by field name."""
    for container_key in ("ExtraFields", "extraFields", "extrafields"):
        items = record.get(container_key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or item.get("Name") or "").strip().lower()
            if name == field_name.strip().lower():
                return item.get("value") or item.get("Value")
    return None


def convert_fuel_consumption(value: Any) -> float | str:
    """Convert fuel consumption from l/100km to km/l with 2 decimals."""
    if value is None or value == "":
        return None
    
    # If it's already a number
    if isinstance(value, (int, float)):
        num_value = float(value)
    # If it's a string, convert European format
    elif isinstance(value, str):
        # Replace comma with dot for European format
        value_clean = value.replace(',', '.')
        try:
            num_value = float(value_clean)
        except (ValueError, TypeError):
            # If it cannot be converted, return the original string
            return value
    else:
        return value
    
    # Conversion l/100km → km/l
    # Realistic consumption: l/100km are typically between 3 and 20
    # km/l are typically between 5 and 33
    if 2 < num_value < 30:  # Likely l/100km
        num_value = 100 / num_value
    
    # Round to 2 decimal places
    return round(num_value, 2)


def _to_float(value: Any) -> float | None:
    """Best-effort float conversion for numeric-like values."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        converted = convert_number_string(value)
        if isinstance(converted, (int, float)):
            return float(converted)
        try:
            return float(value.replace(",", "."))
        except (ValueError, TypeError):
            return None
    return None


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up LubeLogger sensors from a config entry."""
    coordinator: LubeLoggerDataUpdateCoordinator = hass.data[DOMAIN] [
        entry.entry_id
    ]

    sensors: list[SensorEntity] = []
    vehicles = coordinator.data.get("vehicles", [])

    for vehicle in vehicles:
        vehicle_id = vehicle.get("id")
        vehicle_name = vehicle.get("name", f"Vehicle {vehicle_id}")
        vehicle_info = vehicle.get("vehicle_info", {})

        # Only create sensors if data exists (visible/tabs requirement)
        if vehicle.get("latest_odometer"):
            sensors.append(
                LubeLoggerLatestOdometerSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("next_plan"):
            sensors.append(
                LubeLoggerNextPlanSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("latest_tax"):
            sensors.append(
                LubeLoggerLatestTaxSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("latest_service"):
            sensors.append(
                LubeLoggerLatestServiceSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("latest_repair"):
            sensors.append(
                LubeLoggerLatestRepairSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("latest_upgrade"):
            sensors.append(
                LubeLoggerLatestUpgradeSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("latest_supply"):
            sensors.append(
                LubeLoggerLatestSupplySensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("latest_gas"):
            sensors.append(
                LubeLoggerLatestGasSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
            sensors.extend(
                [
                    LubeLoggerVehicleAggregateSensor(
                        coordinator, vehicle_id, vehicle_name, vehicle_info,
                        "Total Distance", "total_distance", "km",
                        ("totalDistance", "TotalDistance", "distanceTotal", "DistanceTotal", "odometer"),
                    ),
                    LubeLoggerVehicleAggregateSensor(
                        coordinator, vehicle_id, vehicle_name, vehicle_info,
                        "Total Fuel", "total_fuel", "L",
                        ("totalFuel", "TotalFuel", "fuelTotal", "FuelTotal", "totalLiters", "TotalLiters"),
                    ),
                    LubeLoggerVehicleAggregateSensor(
                        coordinator, vehicle_id, vehicle_name, vehicle_info,
                        "Total Fuel Cost", "total_fuel_cost", "EUR",
                        ("totalFuelCost", "TotalFuelCost", "fuelCostTotal", "FuelCostTotal"),
                    ),
                    LubeLoggerVehicleAggregateSensor(
                        coordinator, vehicle_id, vehicle_name, vehicle_info,
                        "Total Average Fuel Economy", "total_average_fuel_economy", "km/l",
                        ("averageFuelEconomy", "AverageFuelEconomy", "avgFuelEconomy", "AvgFuelEconomy", "averageConsumption", "AverageConsumption"),
                    ),
                    LubeLoggerVehicleAggregateSensor(
                        coordinator, vehicle_id, vehicle_name, vehicle_info,
                        "Total Service Cost", "total_service_cost", "EUR",
                        ("totalServiceCost", "TotalServiceCost", "maintenanceCostTotal", "MaintenanceCostTotal"),
                    ),
                ]
            )
        if vehicle.get("next_reminder"):
            sensors.append(
                LubeLoggerNextReminderSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("latest_equipment"):
            sensors.append(
                LubeLoggerLatestEquipmentSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )
        if vehicle.get("equipment_records") is not None:
            sensors.append(
                LubeLoggerEquipmentListSensor(coordinator, vehicle_id, vehicle_name, vehicle_info)
            )

        equipment_list = vehicle.get("equipment_records", [])
        equipment_name_counts: dict[str, int] = {}

        for equipment in equipment_list:
            base_name = (
                equipment.get("description")
                or equipment.get("Description")
                or equipment.get("name")
                or equipment.get("Name")
                or f"Equipment {equipment.get('id') or equipment.get('Id') or 'unknown'}"
            )
            count = equipment_name_counts.get(base_name, 0) + 1
            equipment_name_counts[base_name] = count
            display_name = f"{base_name} {count}" if count > 1 else base_name
            sensors.append(
                LubeLoggerEquipmentSensor(
                    coordinator, vehicle_id, vehicle_name, vehicle_info, equipment, display_name
                )
            )

    async_add_entities(sensors)


class BaseLubeLoggerSensor(CoordinatorEntity, SensorEntity):
    """Base sensor that reads a key from coordinator data for a specific vehicle."""

    # This tells HA to generate the entity name using the device name + translation
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
        key: str,
        translation_key: str,
        unique_id_suffix: str,
        device_class: SensorDeviceClass | None = None,
        state_class: SensorStateClass | None = None,
        unit: str | None = None,
    ) -> None:
        super().__init__(coordinator)
        self._vehicle_id = vehicle_id
        self._vehicle_name = vehicle_name
        self._key = key
        self._attr_translation_key = translation_key
        self._attr_unique_id = f"lubelogger_{vehicle_id}_{unique_id_suffix}"
        self._attr_device_class = device_class
        self._attr_state_class = state_class
        self._attr_native_unit_of_measurement = unit
        
        # Extract make/model/year from vehicle info for device info
        make = vehicle_info.get("Make") or vehicle_info.get("make") or ""
        model = vehicle_info.get("Model") or vehicle_info.get("model") or ""
        year = str(vehicle_info.get("Year") or vehicle_info.get("year") or "")
        
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(vehicle_id))},
            name=vehicle_name,
            manufacturer=make or "LubeLogger",
            model=model or vehicle_name,
            sw_version=year,
        )

    @property
    def _record(self) -> dict | None:
        data = self.coordinator.data or {}
        vehicles = data.get("vehicles", [])
        for vehicle in vehicles:
            if vehicle.get("id") == self._vehicle_id:
                rec = vehicle.get(self._key)
                return rec if isinstance(rec, dict) else None
        return None

    @property
    def available(self) -> bool:
        """Return if sensor is available."""
        return self._record is not None
    
    def _load_translations(self) -> dict:
        """Load translations for the current language."""
        if not hasattr(self, 'hass') or not self.hass:
            return {}
        
        # Get current language (handle locales like it_IT -> it)
        try:
            lang = self.hass.config.language
            if '_' in lang:
                lang = lang.split('_')[0]
            elif '-' in lang:
                lang = lang.split('-')[0]
        except:
            lang = 'en'
        
        # Try to load translation file
        translations = {}
        try:
            # Get the path to this integration
            current_dir = os.path.dirname(os.path.abspath(__file__))
            translations_dir = os.path.join(current_dir, "translations")
            translation_file = os.path.join(translations_dir, f"{lang}.json")
            
            if os.path.exists(translation_file):
                with open(translation_file, 'r', encoding='utf-8') as f:
                    translations = json.load(f)
            else:
                # Fallback to English
                en_file = os.path.join(translations_dir, "en.json")
                if os.path.exists(en_file):
                    with open(en_file, 'r', encoding='utf-8') as f:
                        translations = json.load(f)
        except Exception as e:
            _LOGGER.debug("Error loading translations for %s: %s", lang, e)
        
        return translations
    
    def _translate_value(self, translation_key: str, default: str = None) -> str:
        """Translate a value using the translation files.
        
        Args:
            translation_key: Dot-separated key (e.g., "state_attributes.sensor.next_reminder.urgency.NotUrgent")
            default: Default value if translation not found
            
        Returns:
            Translated string or default value
        """
        translations = self._load_translations()
        
        # Navigate through the translation dictionary
        keys = translation_key.split('.')
        current = translations
        
        for key in keys:
            if isinstance(current, dict) and key in current:
                current = current[key]
            else:
                return default or keys[-1]
        
        if isinstance(current, str):
            return current
        
        return default or keys[-1]
    
    def _translate_attribute(self, entity_type: str, attribute_name: str, value: str) -> str:
        """Translate an attribute value.
        
        Args:
            entity_type: Type of entity (e.g., "next_reminder")
            attribute_name: Name of attribute (e.g., "urgency")
            value: Value to translate (e.g., "NotUrgent")
            
        Returns:
            Translated value or original if not found
        """
        # First try the full path
        translation_key = f"state_attributes.sensor.{entity_type}.{attribute_name}.{value}"
        translated = self._translate_value(translation_key, value)
        
        # If not found, try a simpler path
        if translated == value:
            translation_key = f"{attribute_name}.{value}"
            translated = self._translate_value(translation_key, value)
        
        return translated


class LubeLoggerVehicleAggregateSensor(CoordinatorEntity, SensorEntity):
    """Generic vehicle aggregate metric sensor."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
        name: str,
        unique_suffix: str,
        unit: str,
        keys: tuple[str, ...],
    ) -> None:
        super().__init__(coordinator)
        self._vehicle_id = vehicle_id
        self._keys = keys
        self._attr_name = name
        self._attr_unique_id = f"lubelogger_{vehicle_id}_{unique_suffix}"
        self._attr_native_unit_of_measurement = unit
        make = vehicle_info.get("Make") or vehicle_info.get("make") or ""
        model = vehicle_info.get("Model") or vehicle_info.get("model") or ""
        year = str(vehicle_info.get("Year") or vehicle_info.get("year") or "")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(vehicle_id))},
            name=vehicle_name,
            manufacturer=make or "LubeLogger",
            model=model or vehicle_name,
            sw_version=year,
        )

    @property
    def native_value(self) -> Any:
        data = self.coordinator.data or {}
        for vehicle in data.get("vehicles", []):
            if vehicle.get("id") != self._vehicle_id:
                continue
            source = vehicle.get("vehicle_info") or {}
            value = _get_record_value(source, *self._keys)
            if value in (None, ""):
                # Fallback to latest_gas extra fields
                latest_gas = vehicle.get("latest_gas") or {}
                for key in self._keys:
                    value = _get_extra_field_value(latest_gas, key)
                    if value not in (None, ""):
                        break
            if self._attr_native_unit_of_measurement == "km/l":
                num = _to_float(value)
                if num and num > 0:
                    return round(100 / num, 2) if num < 50 else round(num, 2)
                return None
            num = _to_float(value)
            return num if num is not None else value
        return None


class LubeLoggerLatestOdometerSensor(BaseLubeLoggerSensor):
    """Sensor for latest odometer value."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_odometer",
            translation_key="latest_odometer",
            unique_id_suffix="latest_odometer",
            device_class=SensorDeviceClass.DISTANCE,
            state_class=SensorStateClass.MEASUREMENT,
            unit="km",
        )

    @property
    def native_value(self) -> Any:
        rec = self._record
        if not rec:
            return None
        
        if rec.get("adjusted"):
            odometer = rec.get("odometer")
        else:
            odometer = rec.get("odometer") or rec.get("Odometer")
        
        if odometer:
            return convert_number_string(odometer)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add the date in a readable format
        if "date" in attrs:
            try:
                dt = parse_date(attrs["date"])
                if dt:
                    attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
            except (ValueError, TypeError):
                pass

        # Include complete odometer history for HA post-hoc history alignment
        data = self.coordinator.data or {}
        vehicles = data.get("vehicles", [])
        for vehicle in vehicles:
            if vehicle.get("id") == self._vehicle_id:
                attrs["odometer_history"] = vehicle.get("odometer_records") or []
                break
        
        return attrs


class LubeLoggerNextPlanSensor(BaseLubeLoggerSensor):
    """Sensor for next planned item from Plan endpoint."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="next_plan",
            translation_key="next_plan",
            unique_id_suffix="next_plan",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None

        for field in ("dateCreated", "dateModified", "Date", "date"):
            dt = parse_date(rec.get(field))
            if dt:
                return dt
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add the date in a readable format
        date_fields = ["dateCreated", "dateModified", "Date", "date"]
        for field in date_fields:
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs[f"{field}_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break
        
        return attrs


class LubeLoggerLatestTaxSensor(BaseLubeLoggerSensor):
    """Sensor for latest tax record."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_tax",
            translation_key="latest_tax",
            unique_id_suffix="latest_tax",
            device_class=SensorDeviceClass.MONETARY,
            state_class=None,
            unit="EUR",
        )

    @property
    def native_value(self) -> Any:
        rec = self._record
        if not rec:
            return None
        
        cost = rec.get("cost") or rec.get("Cost")
        if cost:
            return convert_number_string(cost)
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add date in readable format
        date_fields = ["date", "Date", "taxDate"]
        for field in date_fields:
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break
        
        return attrs


class LubeLoggerLatestServiceSensor(BaseLubeLoggerSensor):
    """Sensor for latest service record."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_service",
            translation_key="latest_service",
            unique_id_suffix="latest_service",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None
        return _get_record_datetime(rec, ("date", "Date", "serviceDate", "ServiceDate"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add date in readable format
        date_fields = ["date", "Date", "serviceDate", "ServiceDate"]
        for field in date_fields:
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break
        
        return attrs


class LubeLoggerLatestRepairSensor(BaseLubeLoggerSensor):
    """Sensor for latest repair record."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_repair",
            translation_key="latest_repair",
            unique_id_suffix="latest_repair",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None

        return _get_record_datetime(rec, ("date", "Date", "repairDate", "RepairDate"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add date in readable format
        date_fields = ["date", "Date", "repairDate", "RepairDate"]
        for field in date_fields:
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break
        
        return attrs


class LubeLoggerLatestUpgradeSensor(BaseLubeLoggerSensor):
    """Sensor for latest upgrade record."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_upgrade",
            translation_key="latest_upgrade",
            unique_id_suffix="latest_upgrade",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None

        return _get_record_datetime(rec, ("date", "Date", "upgradeDate", "UpgradeDate"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add date in readable format
        date_fields = ["date", "Date", "upgradeDate", "UpgradeDate"]
        for field in date_fields:
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break
        
        return attrs


class LubeLoggerLatestSupplySensor(BaseLubeLoggerSensor):
    """Sensor for latest supply record."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_supply",
            translation_key="latest_supply",
            unique_id_suffix="latest_supply",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None

        return _get_record_datetime(rec, ("date", "Date", "supplyDate", "SupplyDate"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add date in readable format
        date_fields = ["date", "Date", "supplyDate", "SupplyDate"]
        for field in date_fields:
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break
        
        return attrs


class LubeLoggerLatestGasSensor(BaseLubeLoggerSensor):
    """Sensor for latest gas/fuel record."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_gas",
            translation_key="latest_gas",
            unique_id_suffix="latest_gas",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None

        return _get_record_datetime(rec, ("date", "Date", "fuelDate", "FuelDate"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value

        # Resolve fuel economy robustly from direct fields, variants, ExtraFields, or fallback calculation
        fuel_raw = _get_record_value(self._record, "fuelEconomy", "FuelEconomy", "averageConsumption", "AverageConsumption")
        if fuel_raw in (None, ""):
            fuel_raw = _get_extra_field_value(self._record, "fuelEconomy")
        if fuel_raw in (None, ""):
            fuel_raw = _get_extra_field_value(self._record, "averageConsumption")
        if fuel_raw in (None, ""):
            fuel_raw = _get_extra_field_value(self._record, "Fuel Economy")
        if fuel_raw in (None, ""):
            fuel_raw = _get_extra_field_value(self._record, "Average Consumption")

        fuel_value_num = _to_float(fuel_raw)

        # Last fallback: compute from distance/liters when available
        if fuel_value_num is None:
            distance = _to_float(_get_record_value(self._record, "distance", "Distance", "tripDistance", "TripDistance"))
            liters = _to_float(_get_record_value(self._record, "liters", "Liters", "fuelAmount", "FuelAmount", "volume", "Volume"))
            if liters and distance and liters > 0 and distance > 0:
                # l/100km
                fuel_value_num = (liters / distance) * 100

        if fuel_value_num is not None and fuel_value_num > 0:
            # normalize to km/l for HA attributes
            attrs["fuelEconomy"] = round(100 / fuel_value_num, 2)
            attrs["fuelEconomy_unit"] = "km/l"
        
        # Add date in readable format
        date_fields = ["date", "Date", "FuelDate"]
        for field in date_fields:
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break
        
        return attrs


class LubeLoggerNextReminderSensor(BaseLubeLoggerSensor):
    """Sensor for next reminder."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="next_reminder",
            translation_key="next_reminder",
            unique_id_suffix="next_reminder",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None

        # API uses dueDate for reminders
        for field in ("dueDate", "DueDate", "Date", "date"):
            dt = parse_date(rec.get(field))
            if dt:
                return dt
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None
        
        attrs = {}
        # Process ALL fields in the record to make them interoperable
        for key, value in self._record.items():
            # Convert any value that looks like a number
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value
        
        # Add due date in readable format
        if "dueDate" in attrs:
            try:
                dt = parse_date(attrs["dueDate"])
                if dt:
                    attrs["due_date_formatted"] = dt.strftime("%d/%m/%Y")
            except (ValueError, TypeError):
                pass
        
        # Get reminder values
        due_distance = attrs.get("dueDistance")
        due_days = attrs.get("dueDays")
        metric = attrs.get("metric", "")
        urgency = attrs.get("urgency", "")
        
        # Determine if it's overdue
        is_overdue = False
        overdue_by = None
        
        if urgency == "PastDue":
            is_overdue = True
        elif due_distance is not None and due_distance < 0:
            is_overdue = True
            overdue_by = f"{-due_distance} km"
        elif due_days is not None and due_days < 0:
            is_overdue = True
            overdue_by = f"{-due_days} days"
        
        attrs["overdue"] = is_overdue
        if overdue_by:
            attrs["overdue_by"] = overdue_by
        
        # Determina che tipo di reminder è in base alla metrica
        metric_lower = metric.lower() if metric else ""
        show_days = False
        show_distance = False
        
        # Logica corretta per interpretare la metrica
        if metric_lower == "date":
            show_days = True
            show_distance = False
        elif metric_lower == "odometer":
            show_days = False
            show_distance = True
        elif metric_lower in ["both", "dateandodometer"]:
            show_days = True
            show_distance = True
        else:
            # Fallback: controlla se contiene le parole
            if "date" in metric_lower:
                show_days = True
            if "odometer" in metric_lower:
                show_distance = True
        
        # Create display attributes for dueDays and dueDistance - solo se rilevanti
        combined_parts = []
        
        if show_days and due_days is not None:
            if due_days < 0:
                combined_parts.append(f"Overdue by {-due_days} days")
            elif due_days == 0:
                combined_parts.append("Due today")
            else:
                combined_parts.append(f"In {due_days} days")
        
        if show_distance and due_distance is not None:
            if due_distance < 0:
                combined_parts.append(f"Overdue by {-due_distance} km")
            elif due_distance == 0:
                combined_parts.append("Due now")
            else:
                combined_parts.append(f"In {due_distance} km")
        
        # Tradurre le parti individuali
        translated_parts = []
        for part in combined_parts:
            if "Overdue by" in part and "days" in part:
                match = re.search(r'Overdue by (\d+) days', part)
                if match:
                    translated = self._translate_attribute("next_reminder", "status", "overdue_days")
                    translated_parts.append(translated.replace("{value}", match.group(1)))
                else:
                    translated_parts.append(part)
            elif "In" in part and "days" in part:
                match = re.search(r'In (\d+) days', part)
                if match:
                    translated = self._translate_attribute("next_reminder", "status", "future_days")
                    translated_parts.append(translated.replace("{value}", match.group(1)))
                else:
                    translated_parts.append(part)
            elif "Overdue by" in part and "km" in part:
                match = re.search(r'Overdue by (\d+) km', part)
                if match:
                    translated = self._translate_attribute("next_reminder", "status", "overdue_distance")
                    translated_parts.append(translated.replace("{value}", match.group(1)))
                else:
                    translated_parts.append(part)
            elif "In" in part and "km" in part:
                match = re.search(r'In (\d+) km', part)
                if match:
                    translated = self._translate_attribute("next_reminder", "status", "future_distance")
                    translated_parts.append(translated.replace("{value}", match.group(1)))
                else:
                    translated_parts.append(part)
            elif "Due today" in part:
                translated_parts.append(self._translate_attribute("next_reminder", "status", "due_today"))
            elif "Due now" in part:
                translated_parts.append(self._translate_attribute("next_reminder", "status", "due_now"))
            else:
                translated_parts.append(part)
        
        # Ora combina le parti tradotte
        if translated_parts:
            if len(translated_parts) == 2:
                and_conjunction = self._translate_attribute("next_reminder", "and_conjunction", " and ")
                attrs["combined_display"] = f"{translated_parts[0]}{and_conjunction}{translated_parts[1]}"
            else:
                attrs["combined_display"] = translated_parts[0]
        else:
            attrs["combined_display"] = ""
        
        # Aggiorna anche i singoli display per coerenza
        if show_days and due_days is not None:
            if due_days < 0:
                attrs["dueDays_display"] = f"Overdue by {-due_days} days"
            elif due_days == 0:
                attrs["dueDays_display"] = "Due today"
            else:
                attrs["dueDays_display"] = f"In {due_days} days"
            
            # Traduci anche i singoli display
            if due_days < 0:
                translated = self._translate_attribute("next_reminder", "status", "overdue_days")
                attrs["dueDays_display"] = translated.replace("{value}", str(-due_days))
            elif due_days == 0:
                attrs["dueDays_display"] = self._translate_attribute("next_reminder", "status", "due_today")
            else:
                translated = self._translate_attribute("next_reminder", "status", "future_days")
                attrs["dueDays_display"] = translated.replace("{value}", str(due_days))
        else:
            attrs["dueDays_display"] = ""
        
        if show_distance and due_distance is not None:
            if due_distance < 0:
                attrs["dueDistance_display"] = f"Overdue by {-due_distance} km"
            elif due_distance == 0:
                attrs["dueDistance_display"] = "Due now"
            else:
                attrs["dueDistance_display"] = f"In {due_distance} km"
            
            # Traduci anche i singoli display
            if due_distance < 0:
                translated = self._translate_attribute("next_reminder", "status", "overdue_distance")
                attrs["dueDistance_display"] = translated.replace("{value}", str(-due_distance))
            elif due_distance == 0:
                attrs["dueDistance_display"] = self._translate_attribute("next_reminder", "status", "due_now")
            else:
                translated = self._translate_attribute("next_reminder", "status", "future_distance")
                attrs["dueDistance_display"] = translated.replace("{value}", str(due_distance))
        else:
            attrs["dueDistance_display"] = ""
        
        # Determine reminder type
        if metric_lower == "date":
            reminder_type = "By time"
        elif metric_lower == "odometer":
            reminder_type = "By distance"
        elif metric_lower in ["both", "dateandodometer"]:
            reminder_type = "Both"
        else:
            reminder_type = "Mixed"
        
        # Create status message (will be translated later)
        status_parts = []
        
        if show_days and due_days is not None:
            if due_days < 0:
                status_parts.append(f"Overdue by {-due_days} days")
            elif due_days == 0:
                status_parts.append("Due today")
            else:
                status_parts.append(f"In {due_days} days")
        
        if show_distance and due_distance is not None:
            if due_distance < 0:
                status_parts.append(f"Overdue by {-due_distance} km")
            elif due_distance == 0:
                status_parts.append("Due now")
            else:
                status_parts.append(f"In {due_distance} km")
        
        # Combine status
        if status_parts:
            if len(status_parts) == 2:
                status = f"{status_parts[0]} and {status_parts[1]}"
            else:
                status = status_parts[0]
        else:
            status = ""
        
        attrs["reminder_type"] = reminder_type
        attrs["status"] = status
        
        # Translate attributes using JSON files
        if "urgency" in attrs:
            attrs["urgency"] = self._translate_attribute("next_reminder", "urgency", attrs["urgency"])
        
        if "reminder_type" in attrs:
            attrs["reminder_type"] = self._translate_attribute("next_reminder", "reminder_type", attrs["reminder_type"])
        
        # Translate status message
        if "status" in attrs and attrs["status"]:
            status = attrs["status"]
            
            # Determine status type for translation
            if "Overdue by" in status:
                if "days" in status:
                    status_key = "overdue_days"
                    value = abs(due_days) if due_days is not None else 0
                else:  # km
                    status_key = "overdue_distance"
                    value = abs(due_distance) if due_distance is not None else 0
            elif "In " in status:
                if "days" in status:
                    status_key = "future_days"
                    value = due_days if due_days is not None else 0
                else:  # km
                    status_key = "future_distance"
                    value = due_distance if due_distance is not None else 0
            elif "Due today" in status:
                status_key = "due_today"
                value = 0
            elif "Due now" in status:
                status_key = "due_now"
                value = 0
            else:
                status_key = "generic"
                value = 0
            
            # Get translation template
            translation_key = f"state_attributes.sensor.next_reminder.status.{status_key}"
            template = self._translate_value(translation_key, status)
            
            # Replace placeholder with actual value
            if "{value}" in template and value is not None:
                attrs["status"] = template.replace("{value}", str(value))
        
        return attrs

class LubeLoggerLatestEquipmentSensor(BaseLubeLoggerSensor):
    """Sensor for latest equipment record."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_equipment",
            translation_key="latest_equipment",
            unique_id_suffix="latest_equipment",
            device_class=SensorDeviceClass.TIMESTAMP,
        )

    @property
    def native_value(self) -> datetime | None:
        rec = self._record
        if not rec:
            return None

        purchase_date = _get_extra_field_value(rec, "PurchaseDate")
        dt = parse_date(purchase_date)
        if dt:
            return dt

        return _get_record_datetime(rec, ("date", "Date", "equipmentDate", "EquipmentDate"))

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        if not self._record:
            return None

        attrs = {}

        # Same normalization logic used everywhere else
        for key, value in self._record.items():
            if _should_convert_numeric_string(value):
                attrs[key] = convert_number_string(value)
            else:
                attrs[key] = value

        # Add formatted date
        for field in ("date", "Date", "equipmentDate", "EquipmentDate"):
            if field in attrs:
                try:
                    dt = parse_date(attrs[field])
                    if dt:
                        attrs["date_formatted"] = dt.strftime("%d/%m/%Y")
                except (ValueError, TypeError):
                    pass
                break

        return attrs

class LubeLoggerEquipmentListSensor(BaseLubeLoggerSensor):
    """Sensor exposing all equipment records as attributes."""

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
    ) -> None:
        super().__init__(
            coordinator=coordinator,
            vehicle_id=vehicle_id,
            vehicle_name=vehicle_name,
            vehicle_info=vehicle_info,
            key="latest_equipment",
            translation_key="equipment_list",
            unique_id_suffix="equipment_list",
        )

    @property
    def native_value(self) -> int:
        """Return total number of equipment entries."""
        data = self.coordinator.data or {}
        vehicles = data.get("vehicles", [])
        for vehicle in vehicles:
            if vehicle.get("id") == self._vehicle_id:
                equipment_records = vehicle.get("equipment_records") or []
                return len(equipment_records)
        return 0

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return all equipment entries."""
        data = self.coordinator.data or {}
        vehicles = data.get("vehicles", [])
        for vehicle in vehicles:
            if vehicle.get("id") == self._vehicle_id:
                equipment_records = vehicle.get("equipment_records") or []
                return {
                    "equipment_entries": equipment_records,
                    "vehicle_id": self._vehicle_id,
                }

        return {"equipment_entries": [], "vehicle_id": self._vehicle_id}


class LubeLoggerEquipmentSensor(CoordinatorEntity, SensorEntity):
    """Entity for a single equipment item."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: LubeLoggerDataUpdateCoordinator,
        vehicle_id: int,
        vehicle_name: str,
        vehicle_info: dict,
        equipment: dict[str, Any],
        display_name: str,
    ) -> None:
        super().__init__(coordinator)

        self._vehicle_id = vehicle_id
        self._equipment = equipment
        self._equipment_id = equipment.get("id") or equipment.get("Id")
        equipment_name = display_name

        self._attr_unique_id = f"lubelogger_equipment_{vehicle_id}_{self._equipment_id}"
        self._attr_translation_key = "equipment_item"
        self._attr_name = equipment_name

        make = vehicle_info.get("Make") or vehicle_info.get("make") or ""
        model = vehicle_info.get("Model") or vehicle_info.get("model") or ""
        year = str(vehicle_info.get("Year") or vehicle_info.get("year") or "")
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, str(vehicle_id))},
            name=vehicle_name,
            manufacturer=make or "LubeLogger",
            model=model or vehicle_name,
            sw_version=year,
        )

    @property
    def native_value(self) -> bool | None:
        value = _get_record_value(self._equipment, "isactive", "IsActive", "active", "Active")

        if value is None:
            value = _get_extra_field_value(self._equipment, "Active")

        # Some payloads provide single key/value with name metadata
        if value is None:
            item_name = str(
                self._equipment.get("name")
                or self._equipment.get("Name")
                or self._equipment.get("-name")
                or self._equipment.get("_name")
                or ""
            ).strip().lower()
            if item_name == "active":
                value = (
                    self._equipment.get("value")
                    or self._equipment.get("Value")
                    or self._equipment.get("-value")
                )

        # Some payloads provide dynamic name/value pairs
        if value is None:
            for key in ("fields", "Fields", "values", "Values", "attributes", "Attributes"):
                items = self._equipment.get(key)
                if isinstance(items, list):
                    for item in items:
                        if not isinstance(item, dict):
                            continue
                        item_name = str(
                            item.get("name")
                            or item.get("Name")
                            or item.get("-name")
                            or item.get("_name")
                            or ""
                        ).strip().lower()
                        if item_name == "active":
                            value = item.get("value") or item.get("Value") or item.get("-value")
                            break
                    if value is not None:
                        break

        if isinstance(value, bool):
            return value
        if isinstance(value, (int, float)):
            return bool(value)
        if isinstance(value, str):
            return value.strip().lower() in ("true", "1", "yes", "on")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        return self._equipment
