#!/usr/bin/env python3
"""Unit tests for weather_model config handling in Open-Meteo callers."""

import asyncio
import configparser
from unittest.mock import Mock

from modules.commands.alternatives.wx_international import GlobalWxCommand
from modules.service_plugins.weather_service import WeatherService


def _build_bot(mock_logger, config):
    bot = Mock()
    bot.logger = mock_logger
    bot.config = config
    bot.db_manager = Mock()
    return bot


def _openmeteo_payload():
    return {
        "current": {
            "temperature_2m": 70,
            "weather_code": 1,
            "wind_speed_10m": 5,
            "wind_direction_10m": 180,
            "apparent_temperature": 70,
            "relative_humidity_2m": 50,
        },
        "daily": {
            "time": ["2026-03-29", "2026-03-30"],
            "weather_code": [1, 2],
            "temperature_2m_max": [72, 73],
            "temperature_2m_min": [58, 59],
        },
    }


def test_gwx_blank_weather_model_omits_models_param(mock_logger, monkeypatch):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "weather_model", "")

    cmd = GlobalWxCommand(_build_bot(mock_logger, config))
    captured = {}

    def _fake_get(_url, params=None, timeout=0):
        captured["params"] = params
        response = Mock()
        response.ok = True
        response.json.return_value = _openmeteo_payload()
        return response

    monkeypatch.setattr("modules.commands.alternatives.wx_international.requests.get", _fake_get)

    cmd.get_open_meteo_weather(47.6, -122.3, forecast_type="tomorrow")
    assert "models" not in captured["params"]


def test_gwx_unset_weather_model_uses_best_match(mock_logger, monkeypatch):
    config = configparser.ConfigParser()
    config.add_section("Weather")

    cmd = GlobalWxCommand(_build_bot(mock_logger, config))
    captured = {}

    def _fake_get(_url, params=None, timeout=0):
        captured["params"] = params
        response = Mock()
        response.ok = True
        response.json.return_value = _openmeteo_payload()
        return response

    monkeypatch.setattr("modules.commands.alternatives.wx_international.requests.get", _fake_get)

    cmd.get_open_meteo_weather(47.6, -122.3, forecast_type="tomorrow")
    assert captured["params"]["models"] == "best_match"


def test_weather_service_blank_weather_model_omits_models_param(mock_logger):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.set("Weather", "weather_model", "")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "47.6")
    config.set("Weather_Service", "my_position_lon", "-122.3")

    service = WeatherService(_build_bot(mock_logger, config))
    captured = {}

    def _fake_get(_url, params=None, timeout=0):
        captured["params"] = params
        response = Mock()
        response.ok = True
        response.json.return_value = _openmeteo_payload()
        return response

    service.api_session = Mock()
    service.api_session.get = _fake_get

    asyncio.run(service._get_weather_forecast())
    assert "models" not in captured["params"]


def test_weather_service_unset_weather_model_uses_best_match(mock_logger):
    config = configparser.ConfigParser()
    config.add_section("Weather")
    config.add_section("Weather_Service")
    config.set("Weather_Service", "my_position_lat", "47.6")
    config.set("Weather_Service", "my_position_lon", "-122.3")

    service = WeatherService(_build_bot(mock_logger, config))
    captured = {}

    def _fake_get(_url, params=None, timeout=0):
        captured["params"] = params
        response = Mock()
        response.ok = True
        response.json.return_value = _openmeteo_payload()
        return response

    service.api_session = Mock()
    service.api_session.get = _fake_get

    asyncio.run(service._get_weather_forecast())
    assert captured["params"]["models"] == "best_match"
