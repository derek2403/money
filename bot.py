import logging
import os
import sys

from dotenv import load_dotenv
from telegram.ext import Application, CommandHandler, MessageHandler, filters

import db
import handlers as h


def main() -> None:
    load_dotenv()
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not token:
        sys.exit("TELEGRAM_BOT_TOKEN is not set. Copy .env.example to .env and fill it in.")

    logging.basicConfig(
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        level=logging.INFO,
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)

    db.init_db()

    app = Application.builder().token(token).build()

    app.add_handler(h.build_init_conversation())
    app.add_handler(CommandHandler(["start", "help"], h.cmd_start))
    app.add_handler(CommandHandler("info", h.cmd_info))
    app.add_handler(CommandHandler("ledger", h.cmd_ledger))
    app.add_handler(CommandHandler("balance", h.cmd_balance))
    app.add_handler(CommandHandler("debt", h.cmd_debt))
    app.add_handler(CommandHandler("undo", h.cmd_undo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, h.on_text))

    app.run_polling(allowed_updates=["message"])


if __name__ == "__main__":
    main()
