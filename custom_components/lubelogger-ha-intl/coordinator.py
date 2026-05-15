"""Data update coordinator for LubeLogger."""
from __future__ import annotations

import logging
from datetime import timedelta

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .client import LubeLoggerClient
from .const import (
    CONF_PASSWORD,
    CONF_URL,
    CONF_USERNAME,
    DEFAULT_UPDATE_INTERVAL,
    DOMAIN,
)

_LOGGER = logging.getLogger(__name__)


class LubeLoggerDataUpdateCoordinator(DataUpdateCoordinator):
    """Class to manage fetching LubeLogger data."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        """Initialize the coordinator."""
        self.entry = entry
        self.client = LubeLoggerClient(
            url=entry.data[CONF_URL],
            username=entry.data[CONF_USERNAME],
            password=entry.data[CONF_PASSWORD],
        )

        update_interval = timedelta(
            seconds=entry.options.get("update_interval", DEFAULT_UPDATE_INTERVAL)
        )

        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=update_interval,
        )

    async def _async_update_data(self) -> dict:
        """Fetch data from LubeLogger, organized by vehicle."""
        data: dict = {"vehicles": []}

        # Get all vehicles
        try:
            vehicles = await self.client.async_get_vehicles()
        except Exception as err:
            _LOGGER.warning("Error fetching vehicles: %s", err)
            return data

        # For each vehicle, fetch its specific data
        for vehicle in vehicles:
            vehicle_id = vehicle.get("Id") or vehicle.get("id")
            if not vehicle_id:
                continue

            # Build device name from Make, Model, Year
            make = vehicle.get("Make") or vehicle.get("make") or ""
            model = vehicle.get("Model") or vehicle.get("model") or ""
            year_val = vehicle.get("Year") or vehicle.get("year")
            year = str(year_val) if year_val else ""
            
            name_parts = [part for part in [year, make, model] if part]
            device_name = " ".join(name_parts) if name_parts else vehicle.get("Name") or vehicle.get("name") or f"Vehicle {vehicle_id}"

            vehicle_data = {
                "id": vehicle_id,
                "name": device_name,
                "vehicle_info": vehicle,
            }

            # Vehicle info (includes aggregate fields from /api/vehicle/info)
            try:
                vehicle_info = await self.client.async_get_vehicle_info(vehicle_id)
                if vehicle_info:
                    merged_info = dict(vehicle)
                    merged_info.update(vehicle_info)
                    vehicle_data["vehicle_info"] = merged_info
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching vehicle info for vehicle %s: %s", vehicle_id, err
                )

            # Latest odometer for this vehicle
            try:
                vehicle_data["latest_odometer"] = await self.client.async_get_latest_odometer(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest odometer for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_odometer"] = None

            # Full odometer history for this vehicle
            try:
                vehicle_data["odometer_records"] = await self.client.async_get_odometer_records(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching odometer records for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["odometer_records"] = []

            # Next planned item for this vehicle
            try:
                vehicle_data["next_plan"] = await self.client.async_get_next_plan(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching next plan for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["next_plan"] = None

            # Latest tax for this vehicle
            try:
                vehicle_data["latest_tax"] = await self.client.async_get_latest_tax(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest tax for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_tax"] = None

            # Latest service record for this vehicle
            try:
                vehicle_data["latest_service"] = await self.client.async_get_latest_service(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest service for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_service"] = None

            # Latest repair record for this vehicle
            try:
                vehicle_data["latest_repair"] = await self.client.async_get_latest_repair(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest repair for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_repair"] = None

            # Latest upgrade record for this vehicle
            try:
                vehicle_data["latest_upgrade"] = await self.client.async_get_latest_upgrade(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest upgrade for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_upgrade"] = None

            # Latest supply record for this vehicle
            try:
                vehicle_data["latest_supply"] = await self.client.async_get_latest_supply(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest supply for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_supply"] = None

            # Latest gas/fuel record for this vehicle
            try:
                vehicle_data["latest_gas"] = await self.client.async_get_latest_gas(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest gas for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_gas"] = None

            # Full gas records for aggregate fallbacks
            try:
                vehicle_data["gas_records"] = await self.client.async_get_gas_records(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching gas records for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["gas_records"] = []

            # Full maintenance records for aggregate fallbacks
            try:
                vehicle_data["service_records"] = await self.client.async_get_service_records(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching service records for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["service_records"] = []
            try:
                vehicle_data["repair_records"] = await self.client.async_get_repair_records(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching repair records for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["repair_records"] = []
            try:
                vehicle_data["upgrade_records"] = await self.client.async_get_upgrade_records(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching upgrade records for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["upgrade_records"] = []
            try:
                vehicle_data["supply_records"] = await self.client.async_get_supply_records(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching supply records for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["supply_records"] = []

            # Next reminder for this vehicle
            try:
                vehicle_data["next_reminder"] = await self.client.async_get_next_reminder(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching next reminder for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["next_reminder"] = None

            # Latest equipment record for this vehicle
            try:
                vehicle_data["latest_equipment"] = await self.client.async_get_latest_equipment(
                    vehicle_id
                )
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching latest equipment for vehicle %s: %s", vehicle_id, err
                )
                vehicle_data["latest_equipment"] = None

            # Equipment records
            try:
                equipment_records = await self.client.async_get_equipment_records(vehicle_id)
            except Exception as err:
                _LOGGER.warning(
                    "Error fetching equipment for vehicle %s: %s", vehicle_id, err
                )
                equipment_records = []

            vehicle_data["equipment_records"] = equipment_records

            data["vehicles"].append(vehicle_data)

        return data
