from __future__ import annotations

import asyncio
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
    try:
        applied = await migrate(pool)
        log.info("Migrations applied: %s", applied or "none")
        stale = await svc.bc.recover()
        if stale:
            log.warning("%d interrupted broadcast(s) marked as 'interrupted'", stale)
        stale_rn = await svc.result_notify.recover()
        if stale_rn:
            log.warning("%d interrupted result notification(s) marked as 'interrupted'", stale_rn)
        me = await bot.get_me()
        log.info("Bot started as @%s", me.username)
        dp = build_dispatcher(svc, pool)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types(), handle_signals=True)
    finally:
        await svc.bc.shutdown()
        await svc.result_notify.shutdown()
        await bot.session.close()
        await pool.close()
        log.info("Shutdown complete")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(asyncio.run(run()))
    except KeyboardInterrupt:
        pass
