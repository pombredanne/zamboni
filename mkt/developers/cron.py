from celery.task.sets import TaskSet
import cronjobs

from amo.utils import chunked

from mkt.developers.tasks import region_email, region_exclude
from mkt.webapps.models import AddonExcludedRegion, Webapp


def _region_email(ids, regions):
    ts = [region_email.subtask(args=[chunk, regions])
          for chunk in chunked(ids, 100)]
    TaskSet(ts).apply_async()


@cronjobs.register
def send_new_region_emails(regions):
    """Email app developers notifying them of new regions added."""
    excluded = (AddonExcludedRegion.objects
                .filter(region__in=[r.id for r in regions])
                .values_list('addon', flat=True))
    ids = (Webapp.objects.exclude(id__in=excluded)
           .filter(enable_new_regions=True)
           .values_list('id', flat=True))
    _region_email(ids, regions)


def _region_exclude(ids, regions):
    ts = [region_exclude.subtask(args=[chunk, regions])
          for chunk in chunked(ids, 100)]
    TaskSet(ts).apply_async()


@cronjobs.register
def exclude_new_region(regions):
    """
    Update regional blacklist based on a list of regions to exclude.
    """
    excluded = (AddonExcludedRegion.objects
                .filter(region__in=[r.id for r in regions])
                .values_list('addon', flat=True))
    ids = (Webapp.objects.exclude(id__in=excluded)
           .filter(enable_new_regions=False)
           .values_list('id', flat=True))
    _region_exclude(ids, regions)
