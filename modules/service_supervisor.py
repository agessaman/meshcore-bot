"""Service plugin supervision, mixed into MeshCoreBot.

Starting services at boot, restarting unhealthy ones with a backoff, and the
system health report the web viewer reads from the database."""

from __future__ import annotations

import asyncio
import time
from typing import Any


class ServiceSupervisorMixin:
    """Mixed into MeshCoreBot."""

    _service_restart_failures: Any
    _service_restarting: Any
    config: Any
    connected: Any
    db_manager: Any
    logger: Any
    meshcore: Any
    services: Any
    start_time: Any
    web_viewer_integration: Any

    async def _start_service_at_boot(self, name: str, service: Any, *, started: str, failed: str) -> None:
        """Start one service during startup; a failure or a declined start waits out the restart backoff."""
        try:
            await service.start()
        except Exception as e:
            self.logger.error(f"{failed}: {e}")
            self._service_restart_failures[name] = time.time()
            return
        if not self._note_if_service_not_running(name, service):
            self.logger.info(started)

    def _restart_unhealthy_services(self, now: float, backoff: float) -> None:
        """Health-loop step: start a restart task for each service that is due one."""
        for name, service in self.services.items():
            if not self._service_restart_due(name, service, now, backoff):
                continue
            self.logger.warning(f"Service '{name}' unhealthy, attempting restart...")
            asyncio.create_task(self._restart_service(name, service))

    def _service_restart_due(self, name: str, service: Any, now: float, backoff: float) -> bool:
        """Whether the health loop should restart *service* now."""
        if not getattr(service, 'enabled', True):
            return False
        try:
            if service.is_healthy():
                return False
        except Exception:
            pass
        if name in self._service_restarting:
            return False
        last_failure = self._service_restart_failures.get(name)
        return last_failure is None or now - last_failure >= backoff

    def _note_if_service_not_running(self, service_name: str, service_instance: Any) -> bool:
        """Back off a service whose start() returned without running; True if so.

        That happens for missing configuration (Discord or Telegram with no
        channels, the map uploader with no key) and for transient conditions
        (the radio not connected yet). Either way, restarting it on every
        health tick cannot help; after the backoff the health loop tries again,
        which recovers the transient case.
        """
        if not getattr(service_instance, 'enabled', True):
            return False  # disabled on purpose; the health loop skips it anyway
        try:
            running = service_instance.is_running()
        except Exception:
            running = True
        if running:
            return False
        backoff = self.config.getint('Bot', 'service_restart_backoff_seconds', fallback=300)
        self.logger.info(
            f"Service '{service_name}' did not start (check its configuration); "
            f"next attempt in {backoff}s"
        )
        self._service_restart_failures[service_name] = time.time()
        return True

    async def _restart_service(self, service_name: str, service_instance: Any) -> bool:
        """Stop and start a service. Used when is_healthy() is False.
        Returns True on success, False on failure. Exceptions are caught and logged.
        """
        self._service_restarting.add(service_name)
        try:
            await service_instance.stop()
            await service_instance.start()
            if self._note_if_service_not_running(service_name, service_instance):
                return False
            if not service_instance.is_healthy():
                # Restarted but still unhealthy: wait out the backoff before the
                # next attempt rather than restarting it on every health tick.
                self.logger.warning(f"Service '{service_name}' is still unhealthy after restart")
                self._service_restart_failures[service_name] = time.time()
                return False
            self._service_restart_failures.pop(service_name, None)
            return True
        except Exception as e:
            self.logger.error(f"Failed to restart service '{service_name}': {e}")
            self._service_restart_failures[service_name] = time.time()
            return False
        finally:
            self._service_restarting.discard(service_name)

    async def get_system_health(self) -> dict[str, Any]:
        """Aggregate health status from all components.

        Collects status information from the meshcore connection, database,
        services, and other components to provide a system health report.

        Returns:
            Dict[str, Any]: Dictionary containing overall health status and component details.
        """
        health = {
            'status': 'healthy',
            'timestamp': time.time(),
            'uptime_seconds': time.time() - self.start_time,
            'components': {}
        }

        # Check core connection
        health['components']['meshcore'] = {
            'healthy': self.connected and self.meshcore is not None,
            'message': 'Connected' if (self.connected and self.meshcore is not None) else 'Disconnected'
        }

        # Check database
        try:
            stats = self.db_manager.get_database_stats()
            health['components']['database'] = {
                'healthy': True,
                'entries': stats.get('geocoding_cache_entries', 0) + stats.get('generic_cache_entries', 0),
                'message': 'Operational'
            }
        except Exception as e:
            health['components']['database'] = {
                'healthy': False,
                'error': str(e),
                'message': f'Error: {str(e)}'
            }

        # Check services
        if hasattr(self, 'services') and self.services:
            for name, service in self.services.items():
                try:
                    is_healthy = service.is_healthy()
                    health['components'][f'service_{name}'] = {
                        'healthy': is_healthy,
                        'message': 'Running' if is_healthy else 'Stopped',
                        'enabled': getattr(service, 'enabled', True)
                    }
                except Exception as e:
                    health['components'][f'service_{name}'] = {
                        'healthy': False,
                        'error': str(e),
                        'message': f'Error: {str(e)}'
                    }

        # Check web viewer if available
        if hasattr(self, 'web_viewer_integration') and self.web_viewer_integration:
            try:
                is_healthy = self.web_viewer_integration.is_viewer_healthy() if hasattr(
                    self.web_viewer_integration, 'is_viewer_healthy'
                ) else True
                health['components']['web_viewer'] = {
                    'healthy': is_healthy,
                    'message': 'Operational' if is_healthy else 'Unhealthy'
                }
            except Exception as e:
                health['components']['web_viewer'] = {
                    'healthy': False,
                    'error': str(e),
                    'message': f'Error: {str(e)}'
                }

        # Determine overall status
        unhealthy = [
            k for k, v in health['components'].items()
            if not v.get('healthy', True)
        ]
        if unhealthy:
            if len(unhealthy) < len(health['components']):
                health['status'] = 'degraded'
            else:
                health['status'] = 'unhealthy'

        # Store health data in database for web viewer access
        try:
            self.db_manager.set_system_health(health)
        except Exception as e:
            self.logger.debug(f"Could not store system health in database: {e}")

        return health
