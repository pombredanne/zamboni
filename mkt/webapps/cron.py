import datetime
import os
import shutil
import stat
import time

from django.conf import settings
from django.db.models import Count

import commonware.log
import cronjobs
from celery.task.sets import TaskSet
from lib.es.utils import raise_if_reindex_in_progress

import amo
from amo.utils import chunked

from .models import Installed, Webapp
from .tasks import update_trending, webapp_update_weekly_downloads

log = commonware.log.getLogger('z.cron')


@cronjobs.register
def update_weekly_downloads():
    """Update the weekly "downloads" from the users_install table."""
    raise_if_reindex_in_progress()
    interval = datetime.datetime.today() - datetime.timedelta(days=7)
    counts = (Installed.objects.values('addon')
                               .filter(created__gte=interval,
                                       addon__type=amo.ADDON_WEBAPP)
                               .annotate(count=Count('addon')))

    ts = [webapp_update_weekly_downloads.subtask(args=[chunk])
          for chunk in chunked(counts, 1000)]
    TaskSet(ts).apply_async()


@cronjobs.register
def clean_old_signed(seconds=60 * 60):
    """Clean out apps signed for reviewers."""
    log.info('Removing old apps signed for reviewers')
    root = settings.SIGNED_APPS_REVIEWER_PATH
    for path in os.listdir(root):
        full = os.path.join(root, path)
        age = time.time() - os.stat(full)[stat.ST_ATIME]
        if age > seconds:
            log.debug('Removing signed app: %s, %dsecs old.' % (full, age))
            shutil.rmtree(full)


@cronjobs.register
def update_app_trending():
    """
    Update trending for all apps.

    In testing on the server, each calculation takes about 2.5s. A chunk size
    of 50 means each task will take about 2 minutes.
    """
    chunk_size = 50
    all_ids = list(Webapp.objects.values_list('id', flat=True))

    for ids in chunked(all_ids, chunk_size):
        update_trending.delay(ids)
