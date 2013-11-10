import json
import urllib
from datetime import datetime

from django.conf import settings
from django.core.cache import cache
from django.utils import translation
from django.utils.datastructures import SortedDict

import commonware.log
import waffle
from tower import ugettext_lazy as _lazy

import amo
from access import acl
from amo.helpers import absolutify
from amo.urlresolvers import reverse
from amo.utils import JSONEncoder, send_mail_jinja, to_language
from comm.utils import create_comm_thread, get_recipients
from editors.models import EscalationQueue, RereviewQueue, ReviewerScore
from files.models import File

from mkt.constants import comm
from mkt.constants.features import FeatureProfile
from mkt.site.helpers import product_as_dict
from mkt.webapps.models import Webapp


log = commonware.log.getLogger('z.mailer')


def send_mail(subject, template, context, emails, perm_setting=None, cc=None,
              attachments=None, reply_to=None):
    if not reply_to:
        reply_to = settings.MKT_REVIEWERS_EMAIL

    # Link to our newfangled "Account Settings" page.
    manage_url = absolutify('/settings') + '#notifications'
    send_mail_jinja(subject, template, context, recipient_list=emails,
                    from_email=settings.MKT_REVIEWERS_EMAIL,
                    use_blacklist=False, perm_setting=perm_setting,
                    manage_url=manage_url, headers={'Reply-To': reply_to},
                    cc=cc, attachments=attachments)


def send_note_emails(note):
    if not waffle.switch_is_active('comm-dashboard'):
        return

    recipients = get_recipients(note, False)
    name = note.thread.addon.name
    data = {
        'name': name,
        'sender': note.author.name,
        'comments': note.body,
        'thread_id': str(note.thread.id)
    }
    for email, tok in recipients:
        reply_to = '{0}{1}@{2}'.format(comm.REPLY_TO_PREFIX, tok,
                                       settings.POSTFIX_DOMAIN)
        subject = u'%s has been reviewed.' % name
        send_mail(subject, 'reviewers/emails/decisions/post.txt', data,
                  [email], perm_setting='app_reviewed', reply_to=reply_to)


class ReviewBase(object):

    def __init__(self, request, addon, version, review_type,
                 attachment_formset=None):
        self.request = request
        self.user = self.request.user
        self.addon = addon
        self.version = version
        self.review_type = review_type
        self.files = None
        self.comm_thread = None
        self.attachment_formset = attachment_formset
        self.in_pending = self.addon.status == amo.STATUS_PENDING
        self.in_rereview = RereviewQueue.objects.filter(
            addon=self.addon).exists()
        self.in_escalate = EscalationQueue.objects.filter(
            addon=self.addon).exists()

    def get_attachments(self):
        """
        Returns a list of triples suitable to be attached to an email.
        """
        try:
            num = int(self.attachment_formset.data['attachment-TOTAL_FORMS'])
        except (ValueError, TypeError):
            return []
        else:
            files = []
            for i in xrange(num):
                attachment_name = 'attachment-%d-attachment' % i
                attachment = self.request.FILES.get(attachment_name)
                if attachment:
                    attachment.open()
                    files.append((attachment.name, attachment.read(),
                                  attachment.content_type))
            return files

    def set_addon(self, **kw):
        """Alters addon and sets reviewed timestamp on version."""
        self.addon.update(_signal=False, **kw)
        self.version.update(_signal=False, reviewed=datetime.now())

    def set_files(self, status, files, copy_to_mirror=False,
                  hide_disabled_file=False):
        """Change the files to be the new status
        and copy, remove from the mirror as appropriate."""
        for file in files:
            file.update(_signal=False, datestatuschanged=datetime.now(),
                        reviewed=datetime.now(), status=status)
            if copy_to_mirror:
                file.copy_to_mirror()
            if hide_disabled_file:
                file.hide_disabled_file()

    def log_action(self, action):
        details = {'comments': self.data['comments'],
                   'reviewtype': self.review_type}
        if self.files:
            details['files'] = [f.id for f in self.files]

        amo.log(action, self.addon, self.version, user=self.user.get_profile(),
                created=datetime.now(), details=details,
                attachments=self.attachment_formset)

    def notify_email(self, template, subject, fresh_thread=False):
        """Notify the authors that their app has been reviewed."""
        data = self.data.copy()
        data.update(self.get_context_data())
        data['tested'] = ''
        dt, br = data.get('device_types'), data.get('browsers')
        if dt and br:
            data['tested'] = 'Tested on %s with %s' % (dt, br)
        elif dt and not br:
            data['tested'] = 'Tested on %s' % dt
        elif not dt and br:
            data['tested'] = 'Tested with %s' % br

        if self.comm_thread and waffle.switch_is_active('comm-dashboard'):
            recipients = get_recipients(self.comm_note)

            data['thread_id'] = str(self.comm_thread.id)
            for email, tok in recipients:
                subject = u'%s has been reviewed.' % data['name']
                reply_to = '{0}{1}@{2}'.format(comm.REPLY_TO_PREFIX, tok,
                                               settings.POSTFIX_DOMAIN)
                send_mail(subject,
                          'reviewers/emails/decisions/%s.txt' % template, data,
                          [email], perm_setting='app_reviewed',
                          attachments=self.get_attachments(),
                          reply_to=reply_to)
        else:
            emails = list(self.addon.authors.values_list('email', flat=True))
            cc_email = self.addon.mozilla_contact or None

            send_mail(subject % data['name'],
                      'reviewers/emails/decisions/%s.txt' % template, data,
                      emails, perm_setting='app_reviewed', cc=cc_email,
                      attachments=self.get_attachments())

    def get_context_data(self):
        # We need to display the name in some language that is relevant to the
        # recipient(s) instead of using the reviewer's. addon.default_locale
        # should work.
        if self.addon.name.locale != self.addon.default_locale:
            lang = to_language(self.addon.default_locale)
            with translation.override(lang):
                app = Webapp.objects.get(id=self.addon.id)
        else:
            app = self.addon
        return {'name': app.name,
                'reviewer': self.request.user.get_profile().name,
                'detail_url': absolutify(
                    app.get_url_path(add_prefix=False)),
                'review_url': absolutify(reverse('reviewers.apps.review',
                                                 args=[app.app_slug],
                                                 add_prefix=False)),
                'status_url': absolutify(app.get_dev_url('versions')),
                'comments': self.data['comments'],
                'MKT_SUPPORT_EMAIL': settings.MKT_SUPPORT_EMAIL,
                'SITE_URL': settings.SITE_URL}

    def request_information(self):
        """Send a request for information to the authors."""
        emails = list(self.addon.authors.values_list('email', flat=True))
        self.log_action(amo.LOG.REQUEST_INFORMATION)
        self.version.update(has_info_request=True)
        log.info(u'Sending request for information for %s to %s' %
                 (self.addon, emails))

        # Create thread.
        self.create_comm_thread(action='info')

        subject = u'Submission Update: %s'  # notify_email will format this.
        self.notify_email('info', subject)

    def send_escalate_mail(self):
        self.log_action(amo.LOG.ESCALATE_MANUAL)
        log.info(u'Escalated review requested for %s' % self.addon)
        data = self.get_context_data()

        if not waffle.switch_is_active('comm-dashboard'):
            send_mail(u'Escalated Review Requested: %s' % data['name'],
                      'reviewers/emails/super_review.txt',
                      data, [settings.MKT_SENIOR_EDITORS_EMAIL],
                      attachments=self.get_attachments())


class ReviewApp(ReviewBase):

    def set_data(self, data):
        self.data = data
        self.files = self.version.files.all()

    def create_comm_thread(self, **kwargs):
        res = create_comm_thread(action=kwargs['action'], addon=self.addon,
            comments=self.data['comments'],
            perms=self.data['action_visibility'],
            profile=self.request.amo_user,
            version=self.version)
        if res:
            self.comm_thread, self.comm_note = res

    def process_public(self):
        if self.addon.is_incomplete():
            # Failsafe.
            return

        # Hold onto the status before we change it.
        status = self.addon.status
        self.create_comm_thread(action='approve')
        if self.addon.make_public == amo.PUBLIC_IMMEDIATELY:
            self.process_public_immediately()
        else:
            self.process_public_waiting()

        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()

        # Assign reviewer incentive scores.
        return ReviewerScore.award_points(self.request.amo_user, self.addon, status)

    def process_public_waiting(self):
        """Make an app pending."""
        if self.addon.is_incomplete():
            # Failsafe.
            return

        self.addon.sign_if_packaged(self.version.pk)
        self.set_files(amo.STATUS_PUBLIC_WAITING, self.version.files.all())
        if self.addon.status != amo.STATUS_PUBLIC:
            self.set_addon(status=amo.STATUS_PUBLIC_WAITING,
                           highest_status=amo.STATUS_PUBLIC_WAITING)

        self.log_action(amo.LOG.APPROVE_VERSION_WAITING)
        self.notify_email('pending_to_public_waiting',
                          u'App Approved but unpublished: %s')

        log.info(u'Making %s public but pending' % self.addon)
        log.info(u'Sending email for %s' % self.addon)

    def process_public_immediately(self):
        """Approve an app."""
        if self.addon.is_incomplete():
            # Failsafe.
            return

        self.addon.sign_if_packaged(self.version.pk)
        # Save files first, because set_addon checks to make sure there
        # is at least one public file or it won't make the addon public.
        self.set_files(amo.STATUS_PUBLIC, self.version.files.all())
        if self.addon.status != amo.STATUS_PUBLIC:
            self.set_addon(status=amo.STATUS_PUBLIC,
                           highest_status=amo.STATUS_PUBLIC)

        # Call update_version, so various other bits of data update.
        self.addon.update_version(_signal=False)
        self.addon.update_name_from_package_manifest()
        self.addon.update_supported_locales()

        # Note: Post save signal happens in the view after the transaction is
        # committed to avoid multiple indexing tasks getting fired with stale
        # data.

        self.log_action(amo.LOG.APPROVE_VERSION)
        self.notify_email('pending_to_public', u'App Approved: %s')

        log.info(u'Making %s public' % self.addon)
        log.info(u'Sending email for %s' % self.addon)

    def process_sandbox(self):
        """Reject an app."""
        # Hold onto the status before we change it.
        status = self.addon.status

        self.set_files(amo.STATUS_OBSOLETE, self.version.files.all(),
                       hide_disabled_file=True)
        # If this app is not packaged (packaged apps can have multiple
        # versions) or if there aren't other versions with already reviewed
        # files, reject the app also.
        if (not self.addon.is_packaged or
            not self.addon.versions.exclude(id=self.version.id)
                .filter(files__status__in=amo.REVIEWED_STATUSES).exists()):
            self.set_addon(status=amo.STATUS_REJECTED)

        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()
        if self.in_rereview:
            RereviewQueue.objects.filter(addon=self.addon).delete()

        self.log_action(amo.LOG.REJECT_VERSION)
        self.create_comm_thread(action='reject')
        self.notify_email('pending_to_sandbox', u'Submission Update: %s')

        log.info(u'Making %s disabled' % self.addon)
        log.info(u'Sending email for %s' % self.addon)

        # Assign reviewer incentive scores.
        return ReviewerScore.award_points(self.request.amo_user, self.addon, status,
                                          in_rereview=self.in_rereview)

    def process_escalate(self):
        """Ask for escalation for an app."""
        self.create_comm_thread(action='escalate')
        EscalationQueue.objects.get_or_create(addon=self.addon)
        self.notify_email('author_super_review', u'Submission Update: %s')

        self.send_escalate_mail()

    def process_comment(self):
        self.version.update(has_editor_comment=True)
        self.log_action(amo.LOG.COMMENT_VERSION)
        self.create_comm_thread(action='comment')

    def process_clear_escalation(self):
        """Clear app from escalation queue."""
        EscalationQueue.objects.filter(addon=self.addon).delete()
        self.log_action(amo.LOG.ESCALATION_CLEARED)
        log.info(u'Escalation cleared for app: %s' % self.addon)

    def process_clear_rereview(self):
        """Clear app from re-review queue."""
        RereviewQueue.objects.filter(addon=self.addon).delete()
        self.log_action(amo.LOG.REREVIEW_CLEARED)
        log.info(u'Re-review cleared for app: %s' % self.addon)
        # Assign reviewer incentive scores.
        return ReviewerScore.award_points(self.request.amo_user, self.addon,
                                          self.addon.status, in_rereview=True)

    def process_disable(self):
        """Disables app."""
        if not acl.action_allowed(self.request, 'Addons', 'Edit'):
            return

        # Disable disables all files, not just those in this version.
        self.set_files(amo.STATUS_OBSOLETE,
                       File.objects.filter(version__addon=self.addon),
                       hide_disabled_file=True)
        self.addon.update(status=amo.STATUS_DISABLED)
        if self.in_escalate:
            EscalationQueue.objects.filter(addon=self.addon).delete()
        if self.in_rereview:
            RereviewQueue.objects.filter(addon=self.addon).delete()
        self.create_comm_thread(action='disable')
        subject = u'App disabled by reviewer: %s'
        self.notify_email('disabled', subject)

        self.log_action(amo.LOG.APP_DISABLED)
        log.info(u'App %s has been disabled by a reviewer.' % self.addon)


class ReviewHelper(object):
    """
    A class that builds enough to render the form back to the user and
    process off to the correct handler.
    """

    def __init__(self, request=None, addon=None, version=None,
                 attachment_formset=None):
        self.handler = None
        self.required = {}
        self.addon = addon
        self.version = version
        self.all_files = version and version.files.all()
        self.attachment_formset = attachment_formset
        self.get_review_type(request, addon, version)
        self.actions = self.get_actions()

    def set_data(self, data):
        self.handler.set_data(data)

    def get_review_type(self, request, addon, version):
        if EscalationQueue.objects.filter(addon=addon).exists():
            queue = 'escalated'
        elif RereviewQueue.objects.filter(addon=addon).exists():
            queue = 'rereview'
        else:
            queue = 'pending'
        self.review_type = queue
        self.handler = ReviewApp(request, addon, version, queue,
                                 attachment_formset=self.attachment_formset)

    def get_actions(self):
        public = {
            'method': self.handler.process_public,
            'minimal': False,
            'label': _lazy(u'Push to public'),
            'details': _lazy(u'This will approve the sandboxed app so it '
                             u'appears on the public side.')}
        reject = {
            'method': self.handler.process_sandbox,
            'label': _lazy(u'Reject'),
            'minimal': False,
            'details': _lazy(u'This will reject the app and remove it from '
                             u'the review queue.')}
        info = {
            'method': self.handler.request_information,
            'label': _lazy(u'Request more information'),
            'minimal': True,
            'details': _lazy(u'This will send the author(s) an email '
                             u'requesting more information.')}
        escalate = {
            'method': self.handler.process_escalate,
            'label': _lazy(u'Escalate'),
            'minimal': True,
            'details': _lazy(u'Flag this app for an admin to review.')}
        comment = {
            'method': self.handler.process_comment,
            'label': _lazy(u'Comment'),
            'minimal': True,
            'details': _lazy(u'Make a comment on this app.  The author won\'t '
                             u'be able to see this.')}
        clear_escalation = {
            'method': self.handler.process_clear_escalation,
            'label': _lazy(u'Clear Escalation'),
            'minimal': True,
            'details': _lazy(u'Clear this app from the escalation queue. The '
                             u'author will get no email or see comments '
                             u'here.')}
        clear_rereview = {
            'method': self.handler.process_clear_rereview,
            'label': _lazy(u'Clear Re-review'),
            'minimal': True,
            'details': _lazy(u'Clear this app from the re-review queue. The '
                             u'author will get no email or see comments '
                             u'here.')}
        disable = {
            'method': self.handler.process_disable,
            'label': _lazy(u'Disable app'),
            'minimal': True,
            'details': _lazy(u'Disable the app, removing it from public '
                             u'results. Sends comments to author.')}

        actions = SortedDict()

        if not self.version:
            # Return early if there is no version, this app is incomplete.
            actions['info'] = info
            actions['comment'] = comment
            return actions

        file_status = self.version.files.values_list('status', flat=True)
        multiple_versions = (File.objects.exclude(version=self.version)
                                         .filter(
                                             version__addon=self.addon,
                                             status__in=amo.REVIEWED_STATUSES)
                                         .exists())

        show_privileged = (not self.version.is_privileged
                           or acl.action_allowed(self.handler.request, 'Apps',
                                                 'ReviewPrivileged'))

        # Public.
        if ((self.addon.is_packaged and amo.STATUS_PUBLIC not in file_status
             and show_privileged)
            or (not self.addon.is_packaged and
                self.addon.status != amo.STATUS_PUBLIC)):
            actions['public'] = public

        # Reject.
        if self.addon.is_packaged and show_privileged:
            # Packaged apps reject the file only, or the app itself if there's
            # only a single version.
            if (not multiple_versions and
                self.addon.status not in [amo.STATUS_REJECTED,
                                          amo.STATUS_DISABLED]):
                actions['reject'] = reject
            elif multiple_versions and amo.STATUS_OBSOLETE not in file_status:
                actions['reject'] = reject
        elif not self.addon.is_packaged:
            # Hosted apps reject the app itself.
            if self.addon.status not in [amo.STATUS_REJECTED,
                                         amo.STATUS_DISABLED]:
                actions['reject'] = reject

        # Disable.
        if (acl.action_allowed(self.handler.request, 'Addons', 'Edit') and (
                self.addon.status != amo.STATUS_DISABLED or
                amo.STATUS_OBSOLETE not in file_status)):
            actions['disable'] = disable

        # Clear escalation.
        if self.handler.in_escalate:
            actions['clear_escalation'] = clear_escalation

        # Clear re-review.
        if self.handler.in_rereview:
            actions['clear_rereview'] = clear_rereview

        # Escalate.
        if not self.handler.in_escalate:
            actions['escalate'] = escalate

        # Request info and comment are always shown.
        actions['info'] = info
        actions['comment'] = comment

        return actions

    def process(self):
        action = self.handler.data.get('action', '')
        if not action:
            raise NotImplementedError
        return self.actions[action]['method']()


def clean_sort_param(request, date_sort='created'):
    """
    Handles empty and invalid values for sort and sort order
    'created' by ascending is the default ordering.
    """
    sort = request.GET.get('sort', date_sort)
    order = request.GET.get('order', 'asc')

    if sort not in ('name', 'created', 'nomination', 'num_abuse_reports'):
        sort = date_sort
    if order not in ('desc', 'asc'):
        order = 'asc'
    return sort, order


def create_sort_link(pretty_name, sort_field, get_params, sort, order):
    """Generate table header sort links.

    pretty_name -- name displayed on table header
    sort_field -- name of the sort_type GET parameter for the column
    get_params -- additional get_params to include in the sort_link
    sort -- the current sort type
    order -- the current sort order
    """
    get_params.append(('sort', sort_field))

    if sort == sort_field and order == 'asc':
        # Have link reverse sort order to desc if already sorting by desc.
        get_params.append(('order', 'desc'))
    else:
        # Default to ascending.
        get_params.append(('order', 'asc'))

    # Show little sorting sprite if sorting by this field.
    url_class = ''
    if sort == sort_field:
        url_class = ' class="sort-icon ed-sprite-sort-%s"' % order

    return u'<a href="?%s"%s>%s</a>' % (urllib.urlencode(get_params, True),
                                        url_class, pretty_name)


class AppsReviewing(object):
    """
    Class to manage the list of apps a reviewer is currently reviewing.

    Data is stored in memcache.
    """

    def __init__(self, request):
        self.request = request
        self.user_id = request.amo_user.id
        self.key = '%s:myapps:%s' % (settings.CACHE_PREFIX, self.user_id)

    def get_apps(self):
        ids = []
        my_apps = cache.get(self.key)
        if my_apps:
            for id in my_apps.split(','):
                valid = cache.get(
                    '%s:review_viewing:%s' % (settings.CACHE_PREFIX, id))
                if valid and valid == self.user_id:
                    ids.append(id)

        apps = []
        for app in Webapp.objects.filter(id__in=ids):
            apps.append({
                'app': app,
                'app_attrs': json.dumps(
                    product_as_dict(self.request, app, False, 'reviewer'),
                    cls=JSONEncoder),
            })
        return apps

    def add(self, addon_id):
        my_apps = cache.get(self.key)
        if my_apps:
            apps = my_apps.split(',')
        else:
            apps = []
        apps.append(addon_id)
        cache.set(self.key, ','.join(map(str, set(apps))),
                  amo.EDITOR_VIEWING_INTERVAL * 2)


def device_queue_search(request):
    """
    Returns a queryset that can be used as a base for searching the device
    specific queue.
    """
    filters = {
        'type': amo.ADDON_WEBAPP,
        'status': amo.STATUS_PENDING,
        'disabled_by_user': False,
    }
    sig = request.GET.get('pro')
    if sig:
        profile = FeatureProfile.from_signature(sig)
        filters.update(dict(
            **profile.to_kwargs(prefix='_current_version__features__has_')
        ))
    return Webapp.version_and_file_transformer(
        Webapp.objects.filter(**filters))
