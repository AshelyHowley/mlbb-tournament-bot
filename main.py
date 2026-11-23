from __future__ import annotations

import asyncio
import os
import logging
import sys

from app.config import PROJECT_ROOT, ConfigError, load_config
from app.utils.logging_setup import setup_logging


async def run() -> int:
    try:
        cfg = load_config()
    except ConfigError as e:
        print(f"CONFIGURATION ERROR: {e}\nFill in the .env file (see .env.example).", file=sys.stderr)
        return 2

    setup_logging(cfg.secrets(), PROJECT_ROOT / "logs")
    log = logging.getLogger("main")
    log.info("ACCESS_MODE=%s (%d extra allowed user(s) besides the admin)", cfg.access_mode, len(cfg.allowed_user_ids))
    if os.environ.get("RAILWAY_PROJECT_ID") and not os.environ.get("RAILWAY_VOLUME_MOUNT_PATH"):
        log.warning(
            "Railway without a Volume: the database lives inside the container and is WIPED on every deploy. "
            "Add a Volume (Settings -> Volumes, mount path /data) to keep registrations."
        )

    from app.bot.factory import build_bot, build_dispatcher, build_services
    from app.database.migrate import migrate
    from app.database.pool import WrongDatabaseError, create_pool

    try:
        pool = await create_pool(cfg.database_url)  # verifies current_database/current_user
    except WrongDatabaseError as e:
        log.critical("%s", e)
        return 3
    except Exception as e:
        log.critical("Cannot connect to database (%s)", type(e).__name__)
        return 3

    bot = build_bot(cfg)
    svc = build_services(cfg, pool, bot)
    backup = None
    storage = None
    try:
        applied = await migrate(pool)
        log.info("Migrations applied: %s", applied or "none")
        imported = await svc.archive.import_from_db(pool)
        log.info("Player archive: %s (%d new from DB, %d total)", svc.archive.path, imported, await svc.archive.count())
        stale = await svc.bc.recover()
        if stale:
            log.warning("%d interrupted broadcast(s) marked as 'interrupted'", stale)
        stale_rn = await svc.result_notify.recover()
        if stale_rn:
            log.warning("%d interrupted result notification(s) marked as 'interrupted'", stale_rn)
        me = await bot.get_me()
        log.info("Bot started as @%s", me.username)
        try:
            offered = await svc.bc.notify_interrupted()
            if offered:
                log.warning("offered to resume %d interrupted broadcast(s)", offered)
        except Exception:
            log.exception("could not offer to resume interrupted broadcasts")
        from app.services.fsm_storage import SQLiteStorage, fsm_path_for
        storage = SQLiteStorage(fsm_path_for(cfg.database_url))
        log.info("Dialog state storage: %s", storage.path)
        dp = build_dispatcher(svc, pool, storage)
        from app.services.db_backup import AutoBackup
        backup = AutoBackup(bot, cfg, svc.archive.path, cfg.backup_every_hours)
        backup.start()
        try:
            from app.services.db_backup import offer_restore_if_empty
            if await offer_restore_if_empty(bot, cfg):
                log.warning("Started with an EMPTY database; offered the admin to restore the pinned backup")
        except Exception:
            log.exception("restore offer failed")
        log.info("Auto backup: %s", f"every {cfg.backup_every_hours} h" if cfg.backup_every_hours else "off")
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
    finally:
        if backup is not None:
            await backup.shutdown()  # до закрытия сессии бота: копия уходит через Telegram
        await svc.bc.shutdown()
        await svc.result_notify.shutdown()
        await bot.session.close()
        if storage is not None:
            await storage.close()
        await pool.close()
        log.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        pass
