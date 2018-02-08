import logging
import os
import sys
import datetime


class FileLogger:
    def __init__(self, log_level=10, log_to_file=True):
        self._log_level = log_level
        self._log_to_file = log_to_file

        # Formatter string for logging
        self._formatter_str = "%(asctime)s - %(levelname)s - %(name)s - %(message)s"
        self._date_format = "%y%m%d"

        # Folder name for logfiles
        self._log_dir = "log"

        # Do not use the logger directly. Use function 'log(msg, severity)'
        logging.basicConfig(level=self._log_level, format=self._formatter_str)
        self._logger = logging.getLogger()

        # Current date for logging
        self._date = datetime.datetime.now().strftime(self._date_format)

        # Add a file handlers to the logger if enabled
        if self._log_to_file:
            # If log directory doesn't exist, create it
            if not os.path.exists(self._log_dir):
                os.makedirs(self._log_dir)

            self._update_file_handler()

    def _update_file_handler(self):
        # Create a file handler for logging
        logfile_path = os.path.join(self._log_dir, self._date + ".log")
        handler = logging.FileHandler(logfile_path, encoding="utf-8")
        handler.setLevel(self._log_level)

        # Format file handler
        formatter = logging.Formatter(self._formatter_str)
        handler.setFormatter(formatter)

        # Add file handler to logger
        self._logger.addHandler(handler)

        # Redirect all uncaught exceptions to logfile
        sys.stderr = open(logfile_path, "w")

    # Log an event and save it in a file with current date as name
    def log(self, severity, msg, *args, **kwargs):
        # Add file handler to logger if enabled
        if self._log_to_file:
            now = datetime.datetime.now().strftime(self._date_format)

            # If current date not the same as initial one, create new FileHandler
            if str(now) != str(self._date):
                self._date = now
                # Remove old handlers
                self._logger.handlers = []
                sys.stderr.close()

                self._update_file_handler()

        self._logger.log(severity, msg, *args, **kwargs)

    def debug(self, msg, *args, **kwargs):
        self.log(logging.DEBUG, msg, *args, **kwargs)

    def info(self, msg, *args, **kwargs):
        self.log(logging.INFO, msg, *args, **kwargs)

    def warning(self, msg, *args, **kwargs):
        self.log(logging.WARNING, msg, *args, **kwargs)

    def error(self, msg, *args, **kwargs):
        self.log(logging.ERROR, msg, *args, **kwargs)

    def exception(self, msg, *args, exc_info=True, **kwargs):
        self.error(msg, *args, exc_info=exc_info, **kwargs)

    def critical(self, msg, *args, **kwargs):
        self.log(logging.CRITICAL, msg, *args, **kwargs)
