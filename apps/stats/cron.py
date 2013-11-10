import datetime

from django.core.management import call_command
from django.db.models import Sum, Max

import commonware.log
from celery.task.sets import TaskSet
import cronjobs

from amo.utils import chunked
from addons.models import Addon
from .models import (AddonCollectionCount, CollectionCount,
                     UpdateCount)
from . import tasks
from lib.es.utils import raise_if_reindex_in_progress

task_log = commonware.log.getLogger('z.task')
cron_log = commonware.log.getLogger('z.cron')


@cronjobs.register
def update_addons_collections_downloads():
    """Update addons+collections download totals."""
    raise_if_reindex_in_progress()

    d = (AddonCollectionCount.objects.values('addon', 'collection')
         .annotate(sum=Sum('count')))

    ts = [tasks.update_addons_collections_downloads.subtask(args=[chunk])
          for chunk in chunked(d, 100)]
    TaskSet(ts).apply_async()


@cronjobs.register
def update_collections_total():
    """Update collections downloads totals."""

    d = (CollectionCount.objects.values('collection_id')
                                .annotate(sum=Sum('count')))

    ts = [tasks.update_collections_total.subtask(args=[chunk])
          for chunk in chunked(d, 50)]
    TaskSet(ts).apply_async()


@cronjobs.register
def update_global_totals(date=None):
    """Update global statistics totals."""
    raise_if_reindex_in_progress()

    if date:
        date = datetime.datetime.strptime(date, '%Y-%m-%d').date()
    today = date or datetime.date.today()
    today_jobs = [dict(job=job, date=today) for job in
                  tasks._get_daily_jobs(date)]

    max_update = date or UpdateCount.objects.aggregate(max=Max('date'))['max']
    metrics_jobs = [dict(job=job, date=max_update) for job in
                    tasks._get_metrics_jobs(date)]

    ts = [tasks.update_global_totals.subtask(kwargs=kw)
          for kw in today_jobs + metrics_jobs]
    TaskSet(ts).apply_async()


@cronjobs.register
def update_monolith_stats(date=None):
    """Update monolith statistics."""
    if date:
        date = datetime.datetime.strptime(date, '%Y-%m-%d').date()
    today = date or datetime.date.today()
    jobs = [{'metric': metric,
             'date': today} for metric in tasks._get_monolith_jobs(date)]

    ts = [tasks.update_monolith_stats.subtask(kwargs=kw) for kw in jobs]
    TaskSet(ts).apply_async()


@cronjobs.register
def update_google_analytics(date=None):
    """
    Update stats from Google Analytics.
    """
    if date:
        date = datetime.datetime.strptime(date, '%Y-%m-%d').date()
    else:
        # Assume that we want to populate yesterday's stats by default.
        date = datetime.date.today() - datetime.timedelta(days=1)
    tasks.update_google_analytics.delay(date=date)


@cronjobs.register
def addon_total_contributions():
    addons = Addon.objects.values_list('id', flat=True)
    ts = [tasks.addon_total_contributions.subtask(args=chunk)
          for chunk in chunked(addons, 100)]
    TaskSet(ts).apply_async()


@cronjobs.register
def index_latest_stats(index=None, aliased=True):
    raise_if_reindex_in_progress()
    latest = UpdateCount.search(index).order_by('-date').values_dict()
    if latest:
        latest = latest[0]['date']
    else:
        latest = datetime.date.today() - datetime.timedelta(days=1)
    fmt = lambda d: d.strftime('%Y-%m-%d')
    date_range = '%s:%s' % (fmt(latest), fmt(datetime.date.today()))
    cron_log.info('index_stats --date=%s' % date_range)
    call_command('index_stats', addons=None, date=date_range)
