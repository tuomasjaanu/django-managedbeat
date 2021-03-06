import os
import pickle
import socket
import threading
import traceback
import logging

from datetime import timedelta

import uuid

from django.conf import settings
from django.core.cache import caches
from django.core.cache.backends.locmem import LocMemCache
from django.core.exceptions import ImproperlyConfigured
from django.core.management import BaseCommand
from django.utils import timezone
import time
from djcelery.app import app

logger = logging.getLogger(__name__)

class Command(BaseCommand):
    help = 'Run managed celerybeat process instance'

    def call_celerybeat(self):
        """
        This method will run celerybeat via djcelery.app.

        It clears celerybeat PID file before running celerybeat.

        It will return as and if celerybeat returns; in which case there was usually an error
        :return: None
        """
        try:
            os.unlink("/tmp/celerybeat.pid")
        except:
            pass
        try:
            os.chdir("/")
        except:
            logger.error("Unable to chdir to /")
        try:
            app.start(['celery','beat','--pidfile=/tmp/celerybeat.pid'])
        except:
            logger.error("celery beat throws: %s",traceback.format_exc())

    def cache_sanity_check(self, cache):
        """
        Check if cache is properly configured

        :return:
        """
        tmp_key = uuid.uuid4()
        tmp_value = uuid.uuid4()
        try:
            cache.set(tmp_key, tmp_value)
            if cache.get(tmp_key) != tmp_value:
                raise ImproperlyConfigured("Managedbeat requires reliable cache framework")
        finally:
            cache.delete(tmp_key)

        if type(cache) is LocMemCache:
            logger.warning("Managedbeat should not be used with LocMemCache in multi-instance configuration; it will simply not work")

    def handle(self, *args, **options):
        """
        Entry point for Django admin command managedbeat

        :param args: No arguments
        :param options: No options
        :return: None
        """

        # Try to get settings
        managedbeat_settings = getattr(settings, "MANAGEDBEAT", {})

        cache_key = managedbeat_settings.get("cache_key", "managedbeat_status")
        leader_timeout = managedbeat_settings.get("leader_expire", 60)
        status_poll_interval = managedbeat_settings.get("status_poll_interval", 15)

        cache_name = managedbeat_settings.get("cache", "default")

        cache = caches[cache_name]

        # Perform a quick sanity check
        self.cache_sanity_check(cache)

        # generate a unique identifier for this instance
        unique_id = uuid.uuid4()

        # first, sleep for status_poll_interval to prevent restarting multiple times if leader is disputed
        try:
            time.sleep(status_poll_interval)
        except:
            # in case of interrupted sleep or any unexpected exception, kill everything
            #   NOTE: supervisord or similar should then attempt to restart the command on this instance
            os._exit(255)

        def get_leader():
            """
            Get current leader information. Leader is the instance running celerybeat

            :return: Leader's unique ID or None if no leader has been active for 1 minute
            """
            jstatus = cache.get(cache_key)
            try:
                status = pickle.loads(jstatus)
                now = timezone.now()

                if (now - status['timestamp']) > timedelta(seconds=leader_timeout):
                    return None
                return status['unique_id']
            except:
                return None

        def set_leader():
            """
            Set leader information.

            :return: None
            """
            status = {
                "timestamp": timezone.now(),
                "peer": socket.getfqdn(),
                "unique_id": unique_id
            }
            logger.debug("set_leader(%s)", status)
            cache.set(cache_key, pickle.dumps(status))

        def reset_leader():
            """
            Clear leader information

            :return: None
            """
            cache.delete(cache_key)

        while(True):
            # Get leader information
            leader = get_leader()

            if leader:
                # if we have a leader that is active, try sleeping for 15 seconds before checking again
                try:
                    time.sleep(status_poll_interval)
                except:
                    # in case of interrupted sleep or any unexpected exception, kill everything
                    #   NOTE: supervisord or similar should then attempt to restart the command on this instance
                    os._exit(255)
                continue

            # If there was no active leader, make this instance the leader
            set_leader()

            # Run celerybeat in thread
            thr = threading.Thread(target=self.call_celerybeat)
            thr.start()

            # While celerybeat thread is alive
            while(thr.is_alive()):
                # check for leader status
                if get_leader() != unique_id:

                    # and if another instance becomes the leader, stop our copy of celerybeat
                    logger.error("another celerybeat already running, exiting this one")
                    os._exit(255)

                # refresh leader information
                set_leader()

                # wait for the thread to exit
                try:
                    thr.join(status_poll_interval)
                except:
                    reset_leader()
                    os._exit(255)

            # Celerybeat thread is no longer alive, reset leader status
            reset_leader()
