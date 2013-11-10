import logging
import os
import shutil

from django.conf import settings

from celeryutils import task

import amo
from amo.decorators import set_modified_on
from lib.video import library
import waffle

log = logging.getLogger('z.devhub.task')
time_limits = settings.CELERY_TIME_LIMITS['lib.video.tasks.resize_video']


# Video decoding can take a while, so let's increase these limits.
@task(time_limit=time_limits['hard'], soft_time_limit=time_limits['soft'])
@set_modified_on
def resize_video(src, instance, user=None, **kw):
    """Try and resize a video and cope if it fails."""
    try:
        result = _resize_video(src, instance, **kw)
    except Exception, err:
        log.error('Error on processing video: %s' % err)
        _resize_error(src, instance, user)
        raise

    if not result:
        log.error('Error on processing video, _resize_video not True.')
        _resize_error(src, instance, user)

    log.info('Video resize complete.')
    return


def _resize_error(src, instance, user):
    """An error occurred in processing the video, deal with that approp."""
    amo.log(amo.LOG.VIDEO_ERROR, instance, user=user)
    instance.delete()


def _resize_video(src, instance, **kw):
    """
    Given a preview object and a file somewhere: encode into the full
    preview size and generate a thumbnail.
    """
    log.info('[1@None] Encoding video %s' % instance.pk)
    lib = library
    if not lib:
        log.info('Video library not available for %s' % instance.pk)
        return

    video = lib(src)
    video.get_meta()
    if not video.is_valid():
        log.info('Video is not valid for %s' % instance.pk)
        return

    if waffle.switch_is_active('video-encode'):
        # Do the video encoding.
        try:
            video_file = video.get_encoded(amo.ADDON_PREVIEW_SIZES[1])
        except Exception:
            log.info('Error encoding video for %s, %s' %
                     (instance.pk, video.meta), exc_info=True)
            return

    # Do the thumbnail next, this will be the signal that the
    # encoding has finished.
    try:
        thumbnail_file = video.get_screenshot(amo.ADDON_PREVIEW_SIZES[0])
    except Exception:
        # We'll have this file floating around because the video
        # encoded successfully, or something has gone wrong in which case
        # we don't want the file around anyway.
        if waffle.switch_is_active('video-encode'):
            os.remove(video_file)
        log.info('Error making thumbnail for %s' % instance.pk)
        return

    for path in (instance.thumbnail_path, instance.image_path):
        dirs = os.path.dirname(path)
        if not os.path.exists(dirs):
            os.makedirs(dirs)

    shutil.move(thumbnail_file, instance.thumbnail_path)
    if waffle.switch_is_active('video-encode'):
        # Move the file over, removing the temp file.
        shutil.move(video_file, instance.image_path)
    else:
        # We didn't re-encode the file.
        shutil.copyfile(src, instance.image_path)

    instance.sizes = {'thumbnail': amo.ADDON_PREVIEW_SIZES[0],
                      'image': amo.ADDON_PREVIEW_SIZES[1]}
    instance.save()
    log.info('Completed encoding video: %s' % instance.pk)
    return True
