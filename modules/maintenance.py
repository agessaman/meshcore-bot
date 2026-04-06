#!/usr/bin/env python3
"""Periodic maintenance: data retention, nightly digest email, log rotation, DB backups."""

from __future__ import annotations

import datetime
import json
import os
import sqlite3
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    pass


def _utc_now() -> datetime.datetime:
    """Current time in UTC (timezone-aware; replaces deprecated utcnow())."""
    return datetime.datetime.now(datetime.UTC)


def _iso_week_key_from_ran_at(ran_at: str) -> str:
    """Derive YYYY-Www from maint.status db_backup_ran_at ISO string for weekly dedup."""
    if not ran_at:
        return ''
    try:
        s = ran_at.strip()
        if s.endswith('Z'):
            s = s[:-1] + '+00:00'
        dt = datetime.datetime.fromisoformat(s)
        if dt.tzinfo is not None:
            dt = dt.replace(tzinfo=None)
        y, w, _ = dt.isocalendar()
        return f'{y}-W{w}'
    except (ValueError, TypeError):
        return ''


def _row_n(cur: sqlite3.Cursor) -> int:
    row = cur.fetchone()
    if row is None:
        return 0
    if isinstance(row, dict):
        return int(row['n'])
    try:
        return int(dict(row)['n'])
    except (KeyError, TypeError, ValueError):
        return int(row['n'])


def _count_log_errors_last_24h(log_path: Path) -> tuple[int | str, int | str]:
    """Count ERROR / CRITICAL log lines from the last 24 hours.

    Supports default text format (`YYYY-MM-DD HH:MM:SS - name - LEVEL - msg`) and
    JSON lines from json_logging (`_JsonFormatter` in core).
    """
    cutoff_local = datetime.datetime.now() - datetime.timedelta(hours=24)
    cutoff_utc = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=24)
    err = crit = 0
    try:
        with open(log_path, encoding='utf-8', errors='replace') as fh:
            for raw in fh:
                ln = raw.rstrip('\n')
                if not ln:
                    continue
                if ln.startswith('{'):
                    try:
                        obj = json.loads(ln)
                    except json.JSONDecodeError:
                        continue
                    level = str(obj.get('level', ''))
                    ts = obj.get('timestamp', '')
                    if not ts:
                        continue
                    ts_s = str(ts).replace('Z', '+00:00')
                    try:
                        line_dt = datetime.datetime.fromisoformat(ts_s)
                    except ValueError:
                        continue
                    if line_dt.tzinfo is None:
                        line_dt = line_dt.replace(tzinfo=datetime.UTC)
                    else:
                        line_dt = line_dt.astimezone(datetime.UTC)
                    if line_dt < cutoff_utc:
                        continue
                    if level == 'ERROR':
                        err += 1
                    elif level == 'CRITICAL':
                        crit += 1
                    continue

                if len(ln) < 19:
                    continue
                try:
                    line_dt = datetime.datetime.strptime(ln[:19], '%Y-%m-%d %H:%M:%S')
                except ValueError:
                    continue
                if line_dt < cutoff_local:
                    continue
                if ' - CRITICAL - ' in ln:
                    crit += 1
                elif ' - ERROR - ' in ln:
                    err += 1
        return err, crit
    except OSError:
        return 'n/a', 'n/a'


class MaintenanceRunner:
    """Runs data retention, nightly email, log rotation hot-apply, and DB backups."""

    def __init__(self, bot: Any, get_current_time: Callable[[], datetime.datetime]) -> None:
        self.bot = bot
        self.logger = bot.logger
        self._get_current_time = get_current_time
        self._last_retention_stats: dict[str, Any] = {}
        self._last_db_backup_stats: dict[str, Any] = {}
        self._last_log_rotation_applied: dict[str, str] = {}

    @property
    def last_retention_stats(self) -> dict[str, Any]:
        return self._last_retention_stats

    def get_notif(self, key: str) -> str:
        try:
            val = self.bot.db_manager.get_metadata(f'notif.{key}')
            return val if val is not None else ''
        except Exception:
            return ''

    def get_maint(self, key: str) -> str:
        try:
            val = self.bot.db_manager.get_metadata(f'maint.{key}')
            return val if val is not None else ''
        except Exception:
            return ''

    def run_data_retention(self) -> None:
        """Run data retention cleanup: packet_stream, repeater tables, stats, caches, mesh_connections."""
        import asyncio

        def get_retention_days(section: str, key: str, default: int) -> int:
            try:
                if self.bot.config.has_section(section) and self.bot.config.has_option(section, key):
                    return self.bot.config.getint(section, key)
            except Exception:
                pass
            return default

        packet_stream_days = get_retention_days('Data_Retention', 'packet_stream_retention_days', 3)
        purging_log_days = get_retention_days('Data_Retention', 'purging_log_retention_days', 90)
        daily_stats_days = get_retention_days('Data_Retention', 'daily_stats_retention_days', 90)
        observed_paths_days = get_retention_days('Data_Retention', 'observed_paths_retention_days', 90)
        mesh_connections_days = get_retention_days('Data_Retention', 'mesh_connections_retention_days', 7)
        stats_days = get_retention_days('Stats_Command', 'data_retention_days', 7)

        try:
            if hasattr(self.bot, 'web_viewer_integration') and self.bot.web_viewer_integration:
                bi = getattr(self.bot.web_viewer_integration, 'bot_integration', None)
                if bi and hasattr(bi, 'cleanup_old_data'):
                    bi.cleanup_old_data(packet_stream_days)

            if hasattr(self.bot, 'repeater_manager') and self.bot.repeater_manager:
                if hasattr(self.bot, 'main_event_loop') and self.bot.main_event_loop and self.bot.main_event_loop.is_running():
                    future = asyncio.run_coroutine_threadsafe(
                        self.bot.repeater_manager.cleanup_database(purging_log_days),
                        self.bot.main_event_loop
                    )
                    try:
                        future.result(timeout=60)
                    except Exception as e:
                        self.logger.error(f"Error in repeater_manager.cleanup_database: {e}")
                else:
                    try:
                        loop = asyncio.get_event_loop()
                    except RuntimeError:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                    loop.run_until_complete(self.bot.repeater_manager.cleanup_database(purging_log_days))
                if hasattr(self.bot.repeater_manager, 'cleanup_repeater_retention'):
                    self.bot.repeater_manager.cleanup_repeater_retention(
                        daily_stats_days=daily_stats_days,
                        observed_paths_days=observed_paths_days
                    )

            if hasattr(self.bot, 'command_manager') and self.bot.command_manager:
                stats_cmd = self.bot.command_manager.commands.get('stats') if getattr(self.bot.command_manager, 'commands', None) else None
                if stats_cmd and hasattr(stats_cmd, 'cleanup_old_stats'):
                    stats_cmd.cleanup_old_stats(stats_days)

            if hasattr(self.bot, 'db_manager') and self.bot.db_manager and hasattr(self.bot.db_manager, 'cleanup_expired_cache'):
                self.bot.db_manager.cleanup_expired_cache()

            if hasattr(self.bot, 'mesh_graph') and self.bot.mesh_graph and hasattr(self.bot.mesh_graph, 'delete_expired_edges_from_db'):
                self.bot.mesh_graph.delete_expired_edges_from_db(mesh_connections_days)

            ran_at = _utc_now().isoformat()
            self._last_retention_stats['ran_at'] = ran_at
            try:
                self.bot.db_manager.set_metadata('maint.status.data_retention_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.data_retention_outcome', 'ok')
            except Exception:
                pass

        except Exception as e:
            self.logger.exception(f"Error during data retention cleanup: {e}")
            self._last_retention_stats['error'] = str(e)
            try:
                ran_at = _utc_now().isoformat()
                self.bot.db_manager.set_metadata('maint.status.data_retention_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.data_retention_outcome', f'error: {e}')
            except Exception:
                pass

    def collect_email_stats(self) -> dict[str, Any]:
        """Gather summary stats for the nightly digest."""
        stats: dict[str, Any] = {}

        try:
            start = getattr(self.bot, 'connection_time', None)
            if start:
                delta = datetime.timedelta(seconds=int(time.time() - start))
                hours, rem = divmod(delta.seconds, 3600)
                minutes = rem // 60
                parts = []
                if delta.days:
                    parts.append(f"{delta.days}d")
                parts.append(f"{hours}h {minutes}m")
                stats['uptime'] = ' '.join(parts)
            else:
                stats['uptime'] = 'unknown'
        except Exception:
            stats['uptime'] = 'unknown'

        try:
            with self.bot.db_manager.connection() as conn:
                cur = conn.cursor()
                cur.execute("SELECT COUNT(*) AS n FROM complete_contact_tracking")
                stats['contacts_total'] = _row_n(cur)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM complete_contact_tracking "
                    "WHERE last_heard >= datetime('now', '-1 day')"
                )
                stats['contacts_24h'] = _row_n(cur)
                cur.execute(
                    "SELECT COUNT(*) AS n FROM complete_contact_tracking "
                    "WHERE first_heard >= datetime('now', '-1 day')"
                )
                stats['contacts_new_24h'] = _row_n(cur)
        except Exception as e:
            stats['contacts_error'] = str(e)

        try:
            db_path = str(self.bot.db_manager.db_path)
            size_bytes = os.path.getsize(db_path)
            stats['db_size_mb'] = f'{size_bytes / 1_048_576:.1f}'
            stats['db_path'] = db_path
        except Exception:
            stats['db_size_mb'] = 'unknown'

        try:
            log_file = self.bot.config.get('Logging', 'log_file', fallback='').strip()
            if log_file:
                log_path = Path(log_file)
                stats['log_file'] = str(log_path)
                if log_path.exists():
                    stats['log_size_mb'] = f'{log_path.stat().st_size / 1_048_576:.1f}'
                    err_ct, crit_ct = _count_log_errors_last_24h(log_path)
                    stats['errors_24h'] = err_ct
                    stats['criticals_24h'] = crit_ct
                    backup = Path(str(log_path) + '.1')
                    if backup.exists() and (time.time() - backup.stat().st_mtime) < 86400:
                        stats['log_rotated_24h'] = True
                        stats['log_backup_size_mb'] = f'{backup.stat().st_size / 1_048_576:.1f}'
                    else:
                        stats['log_rotated_24h'] = False
        except Exception:
            pass

        stats['retention'] = self._last_retention_stats.copy()
        return stats

    def format_email_body(self, stats: dict[str, Any], period_start: str, period_end: str) -> str:
        lines = [
            'MeshCore Bot — Nightly Maintenance Report',
            '=' * 44,
            f'Period : {period_start} → {period_end}',
            '',
            'BOT STATUS',
            '─' * 30,
            f"  Uptime    : {stats.get('uptime', 'unknown')}",
            f"  Connected : {'yes' if getattr(self.bot, 'connected', False) else 'no'}",
            '',
            'NETWORK ACTIVITY (past 24 h)',
            '─' * 30,
            f"  Active contacts  : {stats.get('contacts_24h', 'n/a')}",
            f"  New contacts     : {stats.get('contacts_new_24h', 'n/a')}",
            f"  Total tracked    : {stats.get('contacts_total', 'n/a')}",
            '',
            'DATABASE',
            '─' * 30,
            f"  Size : {stats.get('db_size_mb', 'n/a')} MB",
        ]
        if self._last_retention_stats.get('ran_at'):
            lines.append(f"  Last retention run : {self._last_retention_stats['ran_at']} UTC")
        if self._last_retention_stats.get('error'):
            lines.append(f"  Retention error    : {self._last_retention_stats['error']}")

        lines += [
            '',
            'ERRORS (past 24 h, log file)',
            '─' * 30,
            f"  ERROR    : {stats.get('errors_24h', 'n/a')}",
            f"  CRITICAL : {stats.get('criticals_24h', 'n/a')}",
        ]
        if stats.get('log_file'):
            lines += [
                '',
                'LOG FILES',
                '─' * 30,
                f"  Current : {stats.get('log_file')} ({stats.get('log_size_mb', '?')} MB)",
            ]
            if stats.get('log_rotated_24h'):
                lines.append(
                    f"  Rotated : yes — backup is {stats.get('log_backup_size_mb', '?')} MB"
                )
            else:
                lines.append('  Rotated : no')

        lines += [
            '',
            '─' * 44,
            'Manage notification settings: /config',
        ]
        return '\n'.join(lines)

    def send_nightly_email(self) -> None:
        """Build and dispatch the nightly maintenance digest if enabled."""
        import smtplib
        import ssl as _ssl
        from email.message import EmailMessage

        if self.get_notif('nightly_enabled') != 'true':
            return

        smtp_host = self.get_notif('smtp_host')
        smtp_security = self.get_notif('smtp_security') or 'starttls'
        smtp_user = self.get_notif('smtp_user')
        smtp_password = self.get_notif('smtp_password')
        from_name = self.get_notif('from_name') or 'MeshCore Bot'
        from_email = self.get_notif('from_email')
        recipients = [r.strip() for r in self.get_notif('recipients').split(',') if r.strip()]

        if not smtp_host or not from_email or not recipients:
            self.logger.warning(
                "Nightly email enabled but SMTP settings incomplete "
                f"(host={smtp_host!r}, from={from_email!r}, recipients={recipients})"
            )
            return

        try:
            smtp_port = int(self.get_notif('smtp_port') or (465 if smtp_security == 'ssl' else 587))
        except ValueError:
            smtp_port = 587

        now_utc = _utc_now()
        yesterday = now_utc - datetime.timedelta(days=1)
        period_start = yesterday.strftime('%Y-%m-%d %H:%M UTC')
        period_end = now_utc.strftime('%Y-%m-%d %H:%M UTC')

        try:
            stats = self.collect_email_stats()
            body = self.format_email_body(stats, period_start, period_end)

            msg = EmailMessage()
            msg['Subject'] = f'MeshCore Bot — Nightly Report {now_utc.strftime("%Y-%m-%d")}'
            msg['From'] = f'{from_name} <{from_email}>'
            msg['To'] = ', '.join(recipients)
            msg.set_content(body)

            if self.get_maint('email_attach_log') == 'true':
                log_file = self.bot.config.get('Logging', 'log_file', fallback='').strip()
                if log_file:
                    log_path = Path(log_file)
                    max_attach = 5 * 1024 * 1024
                    if log_path.exists() and log_path.stat().st_size <= max_attach:
                        try:
                            with open(log_path, 'rb') as fh:
                                msg.add_attachment(fh.read(), maintype='text', subtype='plain',
                                                   filename=log_path.name)
                        except Exception as attach_err:
                            self.logger.warning(f"Could not attach log file to nightly email: {attach_err}")

            context = _ssl.create_default_context()
            _smtp_timeout = 30
            if smtp_security == 'ssl':
                with smtplib.SMTP_SSL(smtp_host, smtp_port, context=context, timeout=_smtp_timeout) as s:
                    if smtp_user and smtp_password:
                        s.login(smtp_user, smtp_password)
                    s.send_message(msg)
            else:
                with smtplib.SMTP(smtp_host, smtp_port, timeout=_smtp_timeout) as s:
                    if smtp_security == 'starttls':
                        s.ehlo()
                        s.starttls(context=context)
                        s.ehlo()
                    if smtp_user and smtp_password:
                        s.login(smtp_user, smtp_password)
                    s.send_message(msg)

            self.logger.info(
                f"Nightly maintenance email sent to {recipients} "
                f"(contacts_24h={stats.get('contacts_24h')}, "
                f"errors={stats.get('errors_24h')})"
            )
            try:
                ran_at = _utc_now().isoformat()
                self.bot.db_manager.set_metadata('maint.status.nightly_email_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.nightly_email_outcome', 'ok')
            except Exception:
                pass

        except Exception as e:
            self.logger.error(f"Failed to send nightly maintenance email: {e}")
            try:
                ran_at = _utc_now().isoformat()
                self.bot.db_manager.set_metadata('maint.status.nightly_email_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.nightly_email_outcome', f'error: {e}')
            except Exception:
                pass

    def apply_log_rotation_config(self) -> None:
        """Check bot_metadata for log rotation settings and replace the RotatingFileHandler if changed."""
        from logging.handlers import RotatingFileHandler as _RFH

        max_bytes_str = self.get_maint('log_max_bytes')
        backup_count_str = self.get_maint('log_backup_count')

        if not max_bytes_str and not backup_count_str:
            return

        new_cfg = {'max_bytes': max_bytes_str, 'backup_count': backup_count_str}
        if new_cfg == self._last_log_rotation_applied:
            return

        try:
            max_bytes = int(max_bytes_str) if max_bytes_str else 5 * 1024 * 1024
            backup_count = int(backup_count_str) if backup_count_str else 3
        except ValueError:
            self.logger.warning(f"Invalid log rotation config in bot_metadata: {new_cfg}")
            return

        logger = self.bot.logger
        for i, handler in enumerate(logger.handlers):
            if isinstance(handler, _RFH):
                log_path = handler.baseFilename
                formatter = handler.formatter
                level = handler.level
                handler.close()
                new_handler = _RFH(log_path, maxBytes=max_bytes, backupCount=backup_count, encoding='utf-8')
                new_handler.setFormatter(formatter)
                new_handler.setLevel(level)
                logger.handlers[i] = new_handler
                self._last_log_rotation_applied = new_cfg
                self.logger.info(f"Log rotation config applied: maxBytes={max_bytes}, backupCount={backup_count}")
                try:
                    ran_at = _utc_now().isoformat()
                    self.bot.db_manager.set_metadata('maint.status.log_rotation_applied_at', ran_at)
                except Exception:
                    pass
                break

    def maybe_run_db_backup(self) -> None:
        """Check if a scheduled DB backup is due and run it."""
        if self.get_maint('db_backup_enabled') != 'true':
            return

        sched = self.get_maint('db_backup_schedule') or 'daily'
        if sched == 'manual':
            return

        backup_time_str = self.get_maint('db_backup_time') or '02:00'
        now = self._get_current_time()
        try:
            bh, bm = [int(x) for x in backup_time_str.split(':')]
        except Exception:
            bh, bm = 2, 0

        scheduled_today = now.replace(hour=bh, minute=bm, second=0, microsecond=0)
        fire_window_end = scheduled_today + datetime.timedelta(minutes=2)
        if now < scheduled_today or now > fire_window_end:
            return

        if sched == 'weekly' and now.weekday() != 0:
            return

        if not self._last_db_backup_stats:
            try:
                db_ran_at = self.bot.db_manager.get_metadata('maint.status.db_backup_ran_at') or ''
                if db_ran_at:
                    self._last_db_backup_stats['ran_at'] = db_ran_at
                    wk = _iso_week_key_from_ran_at(db_ran_at)
                    if wk:
                        self._last_db_backup_stats['week_key'] = wk
            except Exception:
                pass

        date_key = now.strftime('%Y-%m-%d')
        week_key = f"{now.year}-W{now.isocalendar()[1]}"
        last_ran = self._last_db_backup_stats.get('ran_at', '')
        if sched == 'daily' and last_ran.startswith(date_key):
            return
        if sched == 'weekly':
            seeded_week = self._last_db_backup_stats.get('week_key', '')
            if seeded_week == week_key:
                return

        self.run_db_backup()
        if sched == 'weekly':
            self._last_db_backup_stats['week_key'] = week_key

    def run_db_backup(self) -> None:
        """Backup the SQLite database using sqlite3.Connection.backup(), then prune old backups."""
        import sqlite3 as _sqlite3

        backup_dir_str = self.get_maint('db_backup_dir') or '/data/backups'
        try:
            retention_count = int(self.get_maint('db_backup_retention_count') or '7')
        except ValueError:
            retention_count = 7

        backup_dir = Path(backup_dir_str)
        ran_at = _utc_now().isoformat()

        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            self.logger.error(f"DB backup: cannot create backup directory {backup_dir}: {e}")
            wk = _iso_week_key_from_ran_at(ran_at)
            self._last_db_backup_stats = {'ran_at': ran_at, 'error': str(e), **({'week_key': wk} if wk else {})}
            try:
                self.bot.db_manager.set_metadata('maint.status.db_backup_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.db_backup_outcome', f'error: {e}')
            except Exception:
                pass
            return

        db_path = Path(str(self.bot.db_manager.db_path))
        ts = _utc_now().strftime('%Y%m%dT%H%M%S')
        backup_path = backup_dir / f"{db_path.stem}_{ts}.db"

        try:
            src = _sqlite3.connect(str(db_path), check_same_thread=False)
            dst = _sqlite3.connect(str(backup_path))
            try:
                src.backup(dst, pages=200)
            finally:
                dst.close()
                src.close()

            size_mb = backup_path.stat().st_size / 1_048_576
            self.logger.info(f"DB backup created: {backup_path} ({size_mb:.1f} MB)")

            stem = db_path.stem
            backups = sorted(backup_dir.glob(f"{stem}_*.db"), key=lambda p: p.stat().st_mtime)
            while len(backups) > retention_count:
                oldest = backups.pop(0)
                try:
                    oldest.unlink()
                    self.logger.info(f"DB backup pruned: {oldest}")
                except OSError:
                    pass

            ran_at = _utc_now().isoformat()
            wk = _iso_week_key_from_ran_at(ran_at)
            self._last_db_backup_stats = {
                'ran_at': ran_at,
                'path': str(backup_path),
                'size_mb': f'{size_mb:.1f}',
                **({'week_key': wk} if wk else {}),
            }
            try:
                self.bot.db_manager.set_metadata('maint.status.db_backup_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.db_backup_outcome', 'ok')
                self.bot.db_manager.set_metadata('maint.status.db_backup_path', str(backup_path))
            except Exception:
                pass

        except Exception as e:
            self.logger.error(f"DB backup failed: {e}")
            wk = _iso_week_key_from_ran_at(ran_at)
            self._last_db_backup_stats = {'ran_at': ran_at, 'error': str(e), **({'week_key': wk} if wk else {})}
            try:
                self.bot.db_manager.set_metadata('maint.status.db_backup_ran_at', ran_at)
                self.bot.db_manager.set_metadata('maint.status.db_backup_outcome', f'error: {e}')
            except Exception:
                pass
