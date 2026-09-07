import logging
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import main


class MainTest(unittest.TestCase):
    def test_logging_rotates_daily_and_keeps_seven_backups(self):
        with tempfile.TemporaryDirectory() as directory:
            with mock.patch.object(main, "LOG_DIR", directory), \
                    mock.patch.object(
                        main.logging, "basicConfig"
                    ) as basic_config:
                main.setup_logging()

            handlers = basic_config.call_args.kwargs["handlers"]
            file_handler = next(
                handler for handler in handlers
                if isinstance(
                    handler, logging.handlers.TimedRotatingFileHandler
                )
            )
            self.assertEqual("MIDNIGHT", file_handler.when)
            self.assertEqual(7, file_handler.backupCount)
            for handler in handlers:
                handler.close()


if __name__ == "__main__":
    unittest.main()
