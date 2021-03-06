# -*- coding: utf-8 -*-
import datetime
import json
import os
import urlparse
import uuid
from operator import attrgetter

from django.conf import settings
from django.core.cache import cache
from django.core.exceptions import ObjectDoesNotExist
from django.core.files.storage import default_storage as storage
from django.core.urlresolvers import NoReverseMatch
from django.db import models
from django.db.models import signals as dbsignals
from django.dispatch import receiver

import commonware.log
import waffle
from elasticutils.contrib.django import F, Indexable, MappingType
from tower import ugettext as _

import amo
import amo.models
from access.acl import action_allowed, check_reviewer
from addons import query
from addons.models import (Addon, AddonDeviceType, attach_categories,
                           attach_devices, attach_prices, attach_tags,
                           attach_translations, Category)
from addons.signals import version_changed
from amo.decorators import skip_cache
from amo.helpers import absolutify
from amo.storage_utils import copy_stored_file
from amo.urlresolvers import reverse
from amo.utils import JSONEncoder, memoize, memoize_key, smart_path
from constants.applications import DEVICE_TYPES
from files.models import File, nfd_str, Platform
from files.utils import parse_addon, WebAppParser
from lib.crypto import packaged
from market.models import AddonPremium
from stats.models import ClientData
from translations.fields import save_signal
from versions.models import Version

import mkt
from mkt.constants import APP_FEATURES, apps
from mkt.search.utils import S
from mkt.site.models import DynamicBoolFieldsMixin
from mkt.webapps.utils import get_locale_properties, get_supported_locales


log = commonware.log.getLogger('z.addons')


def reverse_version(version):
    """
    The try/except AttributeError allows this to be used where the input is
    ambiguous, and could be either an already-reversed URL or a Version object.
    """
    if version:
        try:
            return reverse('version-detail', kwargs={'pk': version.pk})
        except AttributeError:
            return version
    return


class WebappManager(amo.models.ManagerBase):

    def __init__(self, include_deleted=False):
        amo.models.ManagerBase.__init__(self)
        self.include_deleted = include_deleted

    def get_query_set(self):
        qs = super(WebappManager, self).get_query_set()
        qs = qs._clone(klass=query.IndexQuerySet).filter(
            type=amo.ADDON_WEBAPP)
        if not self.include_deleted:
            qs = qs.exclude(status=amo.STATUS_DELETED)
        return qs.transform(Webapp.transformer)

    def valid(self):
        return self.filter(status__in=amo.LISTED_STATUSES,
                           disabled_by_user=False)

    def reviewed(self):
        return self.filter(status__in=amo.REVIEWED_STATUSES)

    def visible(self):
        return self.filter(status=amo.STATUS_PUBLIC, disabled_by_user=False)

    def top_free(self, listed=True):
        qs = self.visible() if listed else self
        return (qs.filter(premium_type__in=amo.ADDON_FREES)
                .order_by('-weekly_downloads')
                .with_index(addons='downloads_type_idx'))

    def top_paid(self, listed=True):
        qs = self.visible() if listed else self
        return (qs.filter(premium_type__in=amo.ADDON_PREMIUMS)
                .order_by('-weekly_downloads')
                .with_index(addons='downloads_type_idx'))

    @skip_cache
    def pending(self):
        # - Holding
        # ** Approved   -- PUBLIC
        # ** Unapproved -- PENDING
        # - Open
        # ** Reviewed   -- PUBLIC
        # ** Unreviewed -- LITE
        # ** Rejected   -- REJECTED
        return self.filter(status=amo.WEBAPPS_UNREVIEWED_STATUS)

    def rated(self):
        """IARC."""
        if waffle.switch_is_active('iarc'):
            return self.exclude(content_ratings__isnull=True)
        return self

    def by_identifier(self, identifier):
        """
        Look up a single app by its `id` or `app_slug`.

        If the identifier is coercable into an integer, we first check for an
        ID match, falling back to a slug check (probably not necessary, as
        there is validation preventing numeric slugs). Otherwise, we only look
        for a slug match.
        """
        try:
            return self.get(id=identifier)
        except (ObjectDoesNotExist, ValueError):
            return self.get(app_slug=identifier)


# We use super(Addon, self) on purpose to override expectations in Addon that
# are not true for Webapp. Webapp is just inheriting so it can share the db
# table.
class Webapp(Addon):

    objects = WebappManager()
    with_deleted = WebappManager(include_deleted=True)

    class Meta:
        proxy = True

    def save(self, **kw):
        # Make sure we have the right type.
        self.type = amo.ADDON_WEBAPP
        self.clean_slug(slug_field='app_slug')
        self.assign_uuid()
        creating = not self.id
        super(Addon, self).save(**kw)
        if creating:
            # Set the slug once we have an id to keep things in order.
            self.update(slug='app-%s' % self.id)

            # Create Geodata object (a 1-to-1 relationship).
            if not hasattr(self, '_geodata'):
                Geodata.objects.create(addon=self)

    @staticmethod
    def transformer(apps):
        # I think we can do less than the Addon transformer, so at some point
        # we'll want to copy that over.
        apps_dict = Addon.transformer(apps)
        if not apps_dict:
            return

        for adt in AddonDeviceType.objects.filter(addon__in=apps_dict):
            if not getattr(apps_dict[adt.addon_id], '_device_types', None):
                apps_dict[adt.addon_id]._device_types = []
            apps_dict[adt.addon_id]._device_types.append(
                DEVICE_TYPES[adt.device_type])

    @staticmethod
    def version_and_file_transformer(apps):
        """Attach all the versions and files to the apps."""
        if not apps:
            return []

        ids = set(app.id for app in apps)
        versions = (Version.objects.no_cache().filter(addon__in=ids)
                                    .select_related('addon'))
        vids = [v.id for v in versions]
        files = (File.objects.no_cache().filter(version__in=vids)
                             .select_related('version'))

        # Attach the files to the versions.
        f_dict = dict((k, list(vs)) for k, vs in
                      amo.utils.sorted_groupby(files, 'version_id'))
        for version in versions:
            version.all_files = f_dict.get(version.id, [])
        # Attach the versions to the apps.
        v_dict = dict((k, list(vs)) for k, vs in
                      amo.utils.sorted_groupby(versions, 'addon_id'))
        for app in apps:
            app.all_versions = v_dict.get(app.id, [])

        return apps

    @staticmethod
    def indexing_transformer(apps):
        """Attach everything we need to index apps."""
        transforms = (attach_categories, attach_devices, attach_prices,
                      attach_tags, attach_translations)
        for t in transforms:
            qs = apps.transform(t)
        return qs

    @property
    def geodata(self):
        if hasattr(self, '_geodata'):
            return self._geodata
        return Geodata.objects.create(addon=self)

    def get_api_url(self, action=None, api=None, resource=None, pk=False):
        """Reverse a URL for the API."""
        kwargs = {'api_name': api or 'apps',
                  'resource_name': resource or 'app'}
        if pk:
            kwargs['pk'] = self.pk
        else:
            kwargs['app_slug'] = self.app_slug
        return reverse('api_dispatch_%s' % (action or 'detail'), kwargs=kwargs)

    def get_url_path(self, more=False, add_prefix=True):
        # We won't have to do this when Marketplace absorbs all apps views,
        # but for now pretend you didn't see this.
        try:
            return reverse('detail', args=[self.app_slug],
                           add_prefix=add_prefix)
        except NoReverseMatch:
            # Fall back to old details page until the views get ported.
            return super(Webapp, self).get_url_path(more=more,
                                                    add_prefix=add_prefix)

    def get_detail_url(self, action=None):
        """Reverse URLs for 'detail', 'details.record', etc."""
        return reverse(('detail.%s' % action) if action else 'detail',
                       args=[self.app_slug])

    def get_purchase_url(self, action=None, args=None):
        """Reverse URLs for 'purchase', 'purchase.done', etc."""
        return reverse(('purchase.%s' % action) if action else 'purchase',
                       args=[self.app_slug] + (args or []))

    def get_dev_url(self, action='edit', args=None, prefix_only=False):
        # Either link to the "new" Marketplace Developer Hub or the old one.
        args = args or []
        prefix = ('mkt.developers' if getattr(settings, 'MARKETPLACE', False)
                  else 'devhub')
        view_name = ('%s.%s' if prefix_only else '%s.apps.%s')
        return reverse(view_name % (prefix, action),
                       args=[self.app_slug] + args)

    def get_ratings_url(self, action='list', args=None, add_prefix=True):
        """Reverse URLs for 'ratings.list', 'ratings.add', etc."""
        return reverse(('ratings.%s' % action),
                       args=[self.app_slug] + (args or []),
                       add_prefix=add_prefix)

    def get_stats_url(self, action='overview', args=None):
        """Reverse URLs for 'stats', 'stats.overview', etc."""
        # Simplifies the templates to not have to choose whether to call
        # get_stats_url.
        return reverse(('mkt.stats.%s' % action),
                       args=[self.app_slug] + (args or []))

    def get_comm_thread_url(self):
        threads = self.threads.order_by('-created')
        if threads.exists():
            return reverse('commonplace.commbadge.show_thread',
                           args=[threads[0].id])
        return reverse('commonplace.commbadge')

    @staticmethod
    def domain_from_url(url, allow_none=False):
        if not url:
            if allow_none:
                return
            raise ValueError('URL was empty')
        pieces = urlparse.urlparse(url)
        return '%s://%s' % (pieces.scheme, pieces.netloc.lower())

    @property
    def punycode_app_domain(self):
        return self.app_domain.encode('idna')

    @property
    def parsed_app_domain(self):
        if self.is_packaged:
            raise ValueError('Packaged apps do not have a domain')
        return urlparse.urlparse(self.app_domain)

    @property
    def device_types(self):
        # If the transformer attached something, use it.
        if hasattr(self, '_device_types'):
            return self._device_types
        return [DEVICE_TYPES[d.device_type] for d in
                self.addondevicetype_set.order_by('device_type')]

    @property
    def origin(self):
        if self.is_packaged:
            return self.app_domain

        parsed = urlparse.urlparse(self.get_manifest_url())
        return '%s://%s' % (parsed.scheme, parsed.netloc)

    def get_manifest_url(self, reviewer=False):
        """
        Hosted apps: a URI to an external manifest.
        Packaged apps: a URI to a mini manifest on m.m.o. If reviewer, the
        mini-manifest behind reviewer auth pointing to the reviewer-signed
        package.
        """
        if self.is_packaged:
            if reviewer and self.latest_version:
                # Get latest version and return reviewer manifest URL.
                version = self.latest_version
                return absolutify(reverse('reviewers.mini_manifest',
                                          args=[self.id, version.id]))
            elif self.current_version:
                return absolutify(reverse('detail.manifest', args=[self.guid]))
            else:
                return ''  # No valid version.
        else:
            return self.manifest_url

    def has_icon_in_manifest(self):
        data = self.get_manifest_json()
        return 'icons' in data

    def get_manifest_json(self, file_obj=None):
        file_ = file_obj or self.get_latest_file()
        if not file_:
            return

        try:
            return file_.version.manifest
        except AppManifest.DoesNotExist:
            # TODO: Remove this when we're satisified the above is working.
            log.info('Falling back to loading manifest from file system. '
                     'Webapp:%s File:%s' % (self.id, file_.id))
            if file_.status == amo.STATUS_OBSOLETE:
                file_path = file_.guarded_file_path
            else:
                file_path = file_.file_path

            return WebAppParser().get_json_data(file_path)

    def share_url(self):
        return reverse('apps.share', args=[self.app_slug])

    def manifest_updated(self, manifest, upload):
        """The manifest has updated, update the version and file.

        This is intended to be used for hosted apps only, which have only a
        single version and a single file.
        """
        data = parse_addon(upload, self)
        manifest = WebAppParser().get_json_data(upload)
        version = self.versions.latest()
        max_ = Version._meta.get_field_by_name('_developer_name')[0].max_length
        version.update(version=data['version'],
                       _developer_name=data['developer_name'][:max_])
        try:
            version.manifest_json.update(manifest=json.dumps(manifest))
        except AppManifest.DoesNotExist:
            AppManifest.objects.create(version=version,
                                       manifest=json.dumps(manifest))
        path = smart_path(nfd_str(upload.path))
        file = version.files.latest()
        file.filename = file.generate_filename(extension='.webapp')
        file.size = storage.size(path)
        file.hash = file.generate_hash(path)
        log.info('Updated file hash to %s' % file.hash)
        file.save()

        # Move the uploaded file from the temp location.
        copy_stored_file(path, os.path.join(version.path_prefix,
                                            nfd_str(file.filename)))
        log.info('[Webapp:%s] Copied updated manifest to %s' % (
            self, version.path_prefix))

        amo.log(amo.LOG.MANIFEST_UPDATED, self)

    def is_complete(self):
        """See if the app is complete. If not, return why. This function does
        not consider or include payments-related or IARC information.

        """
        reasons = []

        if not self.support_email:
            reasons.append(_('You must provide a support email.'))
        if not self.name:
            reasons.append(_('You must provide an app name.'))
        if not self.device_types:
            reasons.append(_('You must provide at least one device type.'))

        if not self.categories.count():
            reasons.append(_('You must provide at least one category.'))
        if not self.previews.count():
            reasons.append(_('You must upload at least one screenshot or '
                             'video.'))

        return not bool(reasons), reasons

    def is_rated(self):
        return self.content_ratings.exists()

    def has_payment_account(self):
        """App doesn't have a payment account set up yet."""
        try:
            self.app_payment_account
        except ObjectDoesNotExist:
            return False
        return True

    def mark_done(self):
        """When the submission process is done, update status accordingly."""
        self.update(status=amo.WEBAPPS_UNREVIEWED_STATUS)

    def update_status(self, using=None):
        if (self.is_deleted or self.is_disabled or
            self.status == amo.STATUS_BLOCKED):
            return

        def _log(reason, old=self.status):
            log.info(u'Update app status [%s]: %s => %s (%s).' % (
                self.id, old, self.status, reason))
            amo.log(amo.LOG.CHANGE_STATUS, self.get_status_display(), self)

        # Handle the case of no versions.
        if not self.versions.exists():
            self.update(status=amo.STATUS_NULL)
            _log('no versions')
            return

        # Handle the case of versions with no files.
        if not self.versions.filter(files__isnull=False).exists():
            self.update(status=amo.STATUS_NULL)
            _log('no versions with files')
            return

        # If there are no public versions and at least one pending, set status
        # to pending.
        public_statuses = amo.WEBAPPS_APPROVED_STATUSES
        has_public = (
            self.versions.filter(files__status__in=public_statuses).exists()
        )
        has_pending = (
            self.versions.filter(files__status=amo.STATUS_PENDING).exists())
        if not has_public and has_pending:
            self.update(status=amo.STATUS_PENDING)
            _log('has pending but no public files')
            return

    def authors_other_addons(self, app=None):
        """Return other apps by the same author."""
        return (self.__class__.objects.visible()
                              .filter(type=amo.ADDON_WEBAPP)
                              .exclude(id=self.id).distinct()
                              .filter(addonuser__listed=True,
                                      authors__in=self.listed_authors))

    def can_purchase(self):
        return self.is_premium() and self.premium and self.is_public()

    def is_purchased(self, user):
        return user and self.id in user.purchase_ids()

    def is_pending(self):
        return self.status == amo.STATUS_PENDING

    def is_visible(self, request):
        """Returns whether the app has a visible search result listing. Its
        detail page will always be there.

        This does not consider whether an app is excluded in the current region
        by the developer.
        """

        user_region = getattr(request, 'REGION', mkt.regions.WORLDWIDE)

        # See if it's a game without a content rating.
        for region in mkt.regions.ALL_REGIONS_WITH_CONTENT_RATINGS:
            if (user_region == region and self.listed_in(category='games') and
                not self.content_ratings_in(region, 'games')):
                unrated_game = True
            else:
                unrated_game = False

        # Let developers see it always.
        can_see = (self.has_author(request.amo_user) or
                   action_allowed(request, 'Apps', 'Edit'))

        # Let app reviewers see it only when it's pending.
        if check_reviewer(request, only='app') and self.is_pending():
            can_see = True

        visible = False

        if can_see:
            # Developers and reviewers should see it always.
            visible = True
        elif self.is_public() and not unrated_game:
            # Everyone else can see it only if it's public -
            # and if it's a game, it must have a content rating.
            visible = True

        return visible

    def has_premium(self):
        """If the app is premium status and has a premium object."""
        return bool(self.is_premium() and self.premium)

    def get_price(self, carrier=None, region=None, provider=None):
        """
        A shortcut to get the price as decimal. Returns None if their is no
        price for the app.

        :param optional carrier: an int for the carrier.
        :param optional region: an int for the region. Defaults to worldwide.
        :param optional provider: an int for the provider. Defaults to bango.
        """
        if self.has_premium() and self.premium.price:
            return self.premium.price.get_price(carrier=carrier,
                region=region, provider=provider)

    def get_price_locale(self, carrier=None, region=None, provider=None):
        """
        A shortcut to get the localised price with currency. Returns None if
        their is no price for the app.

        :param optional carrier: an int for the carrier.
        :param optional region: an int for the region. Defaults to worldwide.
        :param optional provider: an int for the provider. Defaults to bango.
        """
        if self.has_premium() and self.premium.price:
            return self.premium.price.get_price_locale(carrier=carrier,
                region=region, provider=provider)

    def get_tier(self):
        """
        Returns the price tier object.
        """
        if self.has_premium():
            return self.premium.price

    def get_tier_name(self):
        """
        Returns the price tier for showing prices in the reviewer
        tools and developer hub.
        """
        tier = self.get_tier()
        if tier:
            return tier.tier_locale()

    @amo.cached_property
    def promo(self):
        return self.get_promo()

    def get_promo(self):
        try:
            return self.previews.filter(position=-1)[0]
        except IndexError:
            pass

    def get_region_ids(self, worldwide=False, excluded=None):
        """
        Return IDs of regions in which this app is listed.

        If `excluded` is provided we'll use that instead of doing our own
        excluded lookup.
        """
        if worldwide:
            all_ids = mkt.regions.ALL_REGION_IDS
        else:
            all_ids = mkt.regions.REGION_IDS
        if excluded is None:
            excluded = list(self.addonexcludedregion
                                .values_list('region', flat=True))

        return sorted(set(all_ids) - set(excluded or []))

    def get_excluded_region_ids(self):
        """
        Return IDs of regions for which this app is excluded.

        This will be all the addon excluded regions. If the app is premium,
        this will also exclude any region that does not have the price tier
        set.

        Note: free and in-app are not included in this.
        """
        excluded = set(self.addonexcludedregion
                           .values_list('region', flat=True))

        if self.is_premium():
            all_regions = set(mkt.regions.ALL_REGION_IDS)
            tier = self.get_tier()
            if tier:
                # Find every region that does not have payments supported
                # and add that into the exclusions.
                return excluded.union(
                    all_regions.difference(self.get_price_region_ids()))

        return sorted(list(excluded))

    def get_price_region_ids(self):
        tier = self.get_tier()
        if tier:
            return sorted(p['region'] for p in tier.prices() if p['paid'])
        return []

    def get_regions(self):
        """
        Return regions, e.g.:
            [<class 'mkt.constants.regions.BR'>,
             <class 'mkt.constants.regions.CA'>,
             <class 'mkt.constants.regions.UK'>,
             <class 'mkt.constants.regions.US'>,
             <class 'mkt.constants.regions.WORLDWIDE'>]
        """
        _regions = map(mkt.regions.REGIONS_CHOICES_ID_DICT.get,
                       self.get_region_ids(worldwide=True))
        return sorted(_regions, key=lambda x: x.slug)

    def listed_in(self, region=None, category=None):
        listed = []
        if region:
            listed.append(region.id in self.get_region_ids(worldwide=True))
        if category:
            if isinstance(category, basestring):
                filters = {'slug': category}
            else:
                filters = {'id': category.id}
            listed.append(self.category_set.filter(**filters).exists())
        return all(listed or [False])

    def content_ratings_in(self, region, category=None):
        """
        Get all content ratings for this app in REGION for CATEGORY.
        (e.g. give me the content ratings for a game listed in a Brazil.)
        """

        # If we want to find games in Brazil with content ratings, then
        # make sure it's actually listed in Brazil and it's a game.
        if category and not self.listed_in(region, category):
            return []

        rb = []
        if not region.ratingsbodies:
            # If a region doesn't specify a ratings body, default to GENERIC.
            rb = [mkt.ratingsbodies.GENERIC.id]
        else:
            rb = [x.id for x in region.ratingsbodies]

        return list(self.content_ratings.filter(ratings_body__in=rb)
                        .order_by('rating'))

    @classmethod
    def now(cls):
        return datetime.date.today()

    @classmethod
    def from_search(cls, request, cat=None, region=None, gaia=False,
                    mobile=False, tablet=False, filter_overrides=None):

        filters = {
            'type': amo.ADDON_WEBAPP,
            'status': amo.STATUS_PUBLIC,
            'is_disabled': False,
        }

        # Special handling if status is 'any' to remove status filter.
        if filter_overrides and 'status' in filter_overrides:
            if filter_overrides['status'] is 'any':
                del filters['status']
                del filter_overrides['status']

        if filter_overrides:
            filters.update(filter_overrides)

        if cat:
            filters.update(category=cat.slug)

        srch = S(WebappIndexer).filter(**filters)

        if (region and not
            waffle.flag_is_active(request, 'override-region-exclusion')):
            srch = srch.filter(~F(region_exclusions=region.id))

        if mobile or gaia:
            srch = srch.filter(uses_flash=False)

        return srch

    @classmethod
    def category(cls, slug):
        try:
            return (Category.objects
                    .filter(type=amo.ADDON_WEBAPP, slug=slug))[0]
        except IndexError:
            return None

    def in_rereview_queue(self):
        return self.rereviewqueue_set.exists()

    def get_cached_manifest(self, force=False):
        """
        Creates the "mini" manifest for packaged apps and caches it.

        Call this with `force=True` whenever we need to update the cached
        version of this manifest, e.g., when a new version of the packaged app
        is approved.

        If the addon is not a packaged app, this will not cache anything.

        """
        if not self.is_packaged:
            return

        key = 'webapp:{0}:manifest'.format(self.pk)

        if not force:
            data = cache.get(key)
            if data:
                return data

        version = self.current_version
        if not version:
            data = {}
        else:
            file_obj = version.all_files[0]
            manifest = self.get_manifest_json(file_obj)
            package_path = absolutify(
                os.path.join(reverse('downloads.file', args=[file_obj.id]),
                             file_obj.filename))

            data = {
                'name': manifest['name'],
                'version': version.version,
                'size': storage.size(file_obj.signed_file_path),
                'release_notes': version.releasenotes,
                'package_path': package_path,
            }
            for key in ['developer', 'icons', 'locales']:
                if key in manifest:
                    data[key] = manifest[key]

        data = json.dumps(data, cls=JSONEncoder)

        cache.set(key, data, 0)

        return data

    def sign_if_packaged(self, version_pk, reviewer=False):
        if not self.is_packaged:
            return
        return packaged.sign(version_pk, reviewer=reviewer)

    def assign_uuid(self):
        """Generates a UUID if self.guid is not already set."""
        if not self.guid:
            max_tries = 10
            tried = 1
            guid = str(uuid.uuid4())
            while tried <= max_tries:
                if not Webapp.objects.filter(guid=guid).exists():
                    self.guid = guid
                    break
                else:
                    guid = str(uuid.uuid4())
                    tried += 1
            else:
                raise ValueError('Could not auto-generate a unique UUID')

    def is_premium_type_upgrade(self, premium_type):
        """
        Returns True if changing self.premium_type from current value to passed
        in value is considered an upgrade that should trigger a re-review.
        """
        ALL = set(amo.ADDON_FREES + amo.ADDON_PREMIUMS)
        free_upgrade = ALL - set([amo.ADDON_FREE])
        free_inapp_upgrade = ALL - set([amo.ADDON_FREE, amo.ADDON_FREE_INAPP])

        if (self.premium_type == amo.ADDON_FREE and
            premium_type in free_upgrade):
            return True
        if (self.premium_type == amo.ADDON_FREE_INAPP and
            premium_type in free_inapp_upgrade):
            return True
        return False

    def create_blocklisted_version(self):
        """
        Creates a new version who's file is the blocklisted app found in /media
        and sets status to STATUS_BLOCKLISTED.

        """
        blocklisted_path = os.path.join(settings.MEDIA_ROOT, 'packaged-apps',
                                        'blocklisted.zip')
        last_version = self.current_version.version
        v = Version.objects.create(
            addon=self, version='blocklisted-%s' % last_version)
        f = File(version=v, status=amo.STATUS_BLOCKED,
                 platform=Platform.objects.get(id=amo.PLATFORM_ALL.id))
        f.filename = f.generate_filename()
        copy_stored_file(blocklisted_path, f.file_path)
        log.info(u'[Webapp:%s] Copied blocklisted app from %s to %s' % (
            self.id, blocklisted_path, f.file_path))
        f.size = storage.size(f.file_path)
        f.hash = f.generate_hash(f.file_path)
        f.save()
        mf = WebAppParser().get_json_data(f.file_path)
        AppManifest.objects.create(version=v, manifest=json.dumps(mf))
        self.sign_if_packaged(v.pk)
        self.status = amo.STATUS_BLOCKED
        self._current_version = v
        self.save()

    def update_name_from_package_manifest(self):
        """
        Looks at the manifest.webapp inside the current version's file and
        updates the app's name and translated names.

        Note: Make sure the correct version is in place before calling this.
        """
        if not self.is_packaged:
            return None

        file_ = self.current_version.all_files[0]
        mf = self.get_manifest_json(file_)

        # Get names in "locales" as {locale: name}.
        locale_names = get_locale_properties(mf, 'name', self.default_locale)

        # Check changes to default_locale.
        locale_changed = self.update_default_locale(mf.get('default_locale'))
        if locale_changed:
            log.info(u'[Webapp:%s] Default locale changed from "%s" to "%s".'
                     % (self.pk, locale_changed[0], locale_changed[1]))

        # Update names
        crud = self.update_names(locale_names)
        if any(crud.values()):
            self.save()

    def update_supported_locales(self, latest=False, manifest=None):
        """
        Loads the manifest (for either hosted or packaged) and updates
        Version.supported_locales for the current version or latest version if
        latest=True.
        """
        version = self.versions.latest() if latest else self.current_version

        if not manifest:
            file_ = version.all_files[0]
            manifest = self.get_manifest_json(file_)

        updated = False

        supported_locales = ','.join(get_supported_locales(manifest))
        if version.supported_locales != supported_locales:
            updated = True
            version.update(supported_locales=supported_locales, _signal=False)

        return updated

    @property
    def app_type_id(self):
        """
        Returns int of `1` (hosted), `2` (packaged), or `3` (privileged).
        Used by ES.
        """
        if self.latest_version and self.latest_version.is_privileged:
            return amo.ADDON_WEBAPP_PRIVILEGED
        elif self.is_packaged:
            return amo.ADDON_WEBAPP_PACKAGED
        return amo.ADDON_WEBAPP_HOSTED

    @property
    def app_type(self):
        """
        Returns string of 'hosted', 'packaged', or 'privileged'.
        Used in the API.
        """
        return amo.ADDON_WEBAPP_TYPES[self.app_type_id]

    @property
    def supported_locales(self):
        """
        Returns a tuple of the form:

            (localized default_locale, list of localized supported locales)

        for the current public version.

        """
        languages = []
        version = self.current_version

        if version:
            for locale in version.supported_locales.split(','):
                if locale:
                    language = settings.LANGUAGES.get(locale.lower())
                    if language:
                        languages.append(language)

        return (
            settings.LANGUAGES.get(self.default_locale.lower()),
            sorted(languages)
        )

    @property
    def developer_name(self):
        """This is the developer name extracted from the manifest."""
        if self.current_version:
            return self.current_version.developer_name

    def get_trending(self, region=None):
        """
        Returns trending value.

        If no region, uses global value.
        If region and region is not mature, uses global value.
        Otherwise uses regional trending value.

        """
        if region and not region.adolescent:
            by_region = region.id
        else:
            by_region = 0

        try:
            return self.trending.get(region=by_region).value
        except ObjectDoesNotExist:
            return 0

    def set_content_ratings(self, data):
        """
        Sets content ratings on this app.

        This overwrites or creates ratings, it doesn't delete and expects data
        of the form::

            {<ratingsbodies class>: <rating class>, ...}


        """
        for ratings_body, rating in data.items():
            cr, created = self.content_ratings.safer_get_or_create(
                ratings_body=ratings_body.id, defaults={'rating': rating.id})
            if not created:
                cr.update(rating=rating.id)

    def set_interactives(self, data):
        """
        Sets IARC interactive elements on this app.

        This overwrites or creates elements, it doesn't delete and expects data
        of the form:

            [<interactive name 1>, <interactive name 2>]


        """
        create_kwargs = {}
        for interactive in mkt.ratinginteractives.RATING_INTERACTIVES.keys():
            create_kwargs['has_%s' % interactive.lower()] = (
                interactive.lower() in map(str.lower, data))

        ri, created = RatingInteractives.objects.get_or_create(
            addon=self, defaults=create_kwargs)
        if not created:
            ri.update(**create_kwargs)


class Trending(amo.models.ModelBase):
    addon = models.ForeignKey(Addon, related_name='trending')
    value = models.FloatField(default=0.0)
    # When region=0, it's trending using install counts across all regions.
    region = models.PositiveIntegerField(null=False, default=0, db_index=True)

    class Meta:
        db_table = 'addons_trending'
        unique_together = ('addon', 'region')


class WebappIndexer(MappingType, Indexable):
    """
    Mapping type for Webapp models.

    By default we will return these objects rather than hit the database so
    include here all the things we need to avoid hitting the database.
    """

    @classmethod
    def get_mapping_type_name(cls):
        """
        Returns mapping type name which is used as the key in ES_INDEXES to
        determine which index to use.

        We override this because Webapp is a proxy model to Addon.
        """
        return 'webapp'

    @classmethod
    def get_index(cls):
        return settings.ES_INDEXES[cls.get_mapping_type_name()]

    @classmethod
    def get_model(cls):
        return Webapp

    @classmethod
    def get_settings(cls, settings_override=None):
        """
        Returns settings to be passed to ES create_index.

        If `settings_override` is provided, this will use `settings_override`
        to override the defaults defined here.

        """
        default_settings = {
            'number_of_replicas': settings.ES_DEFAULT_NUM_REPLICAS,
            'number_of_shards': settings.ES_DEFAULT_NUM_SHARDS,
            'refresh_interval': '5s',
            'store.compress.tv': True,
            'store.compress.stored': True,
            'analysis': cls.get_analysis(),
        }
        if settings_override:
            default_settings.update(settings_override)

        return default_settings

    @classmethod
    def get_analysis(cls):
        """
        Returns the analysis dict to be used in settings for create_index.

        For languages that ES supports we define either the minimal or light
        stemming, which isn't as aggresive as the snowball stemmer. We also
        define the stopwords for that language.

        For all languages we've customized we're using the ICU plugin.

        """
        analyzers = {}
        filters = {}

        # The default is used for fields that need ICU but are composed of
        # many languages.
        analyzers['default_icu'] = {
            'type': 'custom',
            'tokenizer': 'icu_tokenizer',
            'filter': ['word_delimiter', 'icu_folding', 'icu_normalizer'],
        }

        # Customize the word_delimiter filter to set various options.
        filters['custom_word_delimiter'] = {
            'type': 'word_delimiter',
            'preserve_original': True,
        }

        for lang, stemmer in amo.STEMMER_MAP.items():
            filters['%s_stem_filter' % lang] = {
                'type': 'stemmer',
                'name': stemmer,
            }
            filters['%s_stop_filter' % lang] = {
                'type': 'stop',
                'stopwords': ['_%s_' % lang],
            }

        for lang in amo.STEMMER_MAP:
            analyzers['%s_analyzer' % lang] = {
                'type': 'custom',
                'tokenizer': 'icu_tokenizer',
                'filter': [
                    'custom_word_delimiter', 'icu_folding', 'icu_normalizer',
                    '%s_stop_filter' % lang, '%s_stem_filter' % lang
                ],
            }

        return {
            'analyzer': analyzers,
            'filter': filters,
        }

    @classmethod
    def setup_mapping(cls):
        """Creates the ES index/mapping."""
        cls.get_es().create_index(cls.get_index(),
                                  {'mappings': cls.get_mapping(),
                                   'settings': cls.get_settings()})

    @classmethod
    def get_mapping(cls):

        doc_type = cls.get_mapping_type_name()

        def _locale_field_mapping(field, analyzer):
            get_analyzer = lambda a: (
                '%s_analyzer' % a if a in amo.STEMMER_MAP else a)
            return {'%s_%s' % (field, analyzer): {
                'type': 'string', 'analyzer': get_analyzer(analyzer)}}

        mapping = {
            doc_type: {
                # Disable _all field to reduce index size.
                '_all': {'enabled': False},
                # Add a boost field to enhance relevancy of a document.
                '_boost': {'name': '_boost', 'null_value': 1.0},
                'properties': {
                    'id': {'type': 'long'},
                    'app_slug': {'type': 'string'},
                    'app_type': {'type': 'byte'},
                    'author': {'type': 'string'},
                    'average_daily_users': {'type': 'long'},
                    'bayesian_rating': {'type': 'float'},
                    'category': {
                        'type': 'string',
                        'index': 'not_analyzed'
                    },
                    'collection': {
                        'type': 'object',
                        'properties': {
                            'id': {'type': 'long'},
                            'order': {'type': 'short'}
                        }
                    },
                    'content_ratings': {
                        'type': 'object',
                        'dynamic': 'true',
                    },
                    'created': {'format': 'dateOptionalTime', 'type': 'date'},
                    'current_version': {'type': 'string',
                                        'index': 'not_analyzed'},
                    'default_locale': {'type': 'string',
                                       'index': 'not_analyzed'},
                    'description': {'type': 'string',
                                    'analyzer': 'default_icu'},
                    'device': {'type': 'byte'},
                    'features': {
                        'type': 'object',
                        'properties': dict(
                            ('has_%s' % f.lower(), {'type': 'boolean'})
                            for f in APP_FEATURES)
                    },
                    'has_public_stats': {'type': 'boolean'},
                    'homepage': {'type': 'string', 'index': 'not_analyzed'},
                    'icons': {
                        'type': 'object',
                        'properties': {
                            'size': {'type': 'short'},
                            'url': {'type': 'string', 'index': 'not_analyzed'},
                        }
                    },
                    'is_disabled': {'type': 'boolean'},
                    'is_escalated': {'type': 'boolean'},
                    'last_updated': {'format': 'dateOptionalTime',
                                     'type': 'date'},
                    'latest_version': {
                        'type': 'object',
                        'properties': {
                            'status': {'type': 'byte'},
                            'is_privileged': {'type': 'boolean'},
                            'has_editor_comment': {'type': 'boolean'},
                            'has_info_request': {'type': 'boolean'},
                        },
                    },
                    'manifest_url': {'type': 'string',
                                     'index': 'not_analyzed'},
                    'name': {'type': 'string', 'analyzer': 'default_icu'},
                    # Turn off analysis on name so we can sort by it.
                    'name_sort': {'type': 'string', 'index': 'not_analyzed'},
                    'owners': {'type': 'long'},
                    'popularity': {'type': 'long'},
                    'premium_type': {'type': 'byte'},
                    'previews': {
                        'type': 'object',
                        'dynamic': 'true',
                    },
                    'price_tier': {'type': 'string',
                                   'index': 'not_analyzed'},
                    'ratings': {
                        'type': 'object',
                        'properties': {
                            'average': {'type': 'float'},
                            'count': {'type': 'short'},
                        }
                    },
                    'region_exclusions': {'type': 'short'},
                    'status': {'type': 'byte'},
                    'support_email': {'type': 'string',
                                      'index': 'not_analyzed'},
                    'support_url': {'type': 'string',
                                    'index': 'not_analyzed'},
                    'supported_locales': {'type': 'string',
                                          'index': 'not_analyzed'},
                    'tags': {'type': 'string', 'analyzer': 'simple'},
                    'type': {'type': 'byte'},
                    'upsell': {
                        'type': 'object',
                        'properties': {
                            'id': {'type': 'long'},
                            'app_slug': {'type': 'string',
                                         'index': 'not_analyzed'},
                            'icon_url': {'type': 'string',
                                         'index': 'not_analyzed'},
                            'name': {'type': 'string',
                                     'index': 'not_analyzed'},
                            'region_exclusions': {'type': 'short'},
                        }
                    },
                    'uses_flash': {'type': 'boolean'},
                    'versions': {
                        'type': 'object',
                        'properties': {
                            'version': {'type': 'string',
                                        'index': 'not_analyzed'},
                            'resource_uri': {'type': 'string',
                                             'index': 'not_analyzed'},
                        }
                    },
                    'weekly_downloads': {'type': 'long'},
                }
            }
        }

        # Add popularity by region.
        for region in mkt.regions.ALL_REGION_IDS:
            mapping[doc_type]['properties'].update(
                {'popularity_%s' % region: {'type': 'long'}})

        # Add room for language-specific indexes.
        for analyzer in amo.SEARCH_ANALYZER_MAP:

            if (not settings.ES_USE_PLUGINS and
                analyzer in amo.SEARCH_ANALYZER_PLUGINS):
                log.info('While creating mapping, skipping the %s analyzer'
                         % analyzer)
                continue

            mapping[doc_type]['properties'].update(
                _locale_field_mapping('name', analyzer))
            mapping[doc_type]['properties'].update(
                _locale_field_mapping('description', analyzer))

        # TODO: reviewer flags (bug 848446)

        return mapping

    @classmethod
    def extract_document(cls, pk, obj=None):
        """Extracts the ElasticSearch index document for this instance."""
        if obj is None:
            obj = cls.get_model().objects.no_cache().get(pk=pk)

        latest_version = obj.latest_version
        version = obj.current_version
        features = (version.features.to_dict()
                    if version else AppFeatures().to_dict())
        is_escalated = obj.escalationqueue_set.exists()

        try:
            status = latest_version.statuses[0][1] if latest_version else None
        except IndexError:
            status = None

        translations = obj.translations
        installed_ids = list(Installed.objects.filter(addon=obj)
                             .values_list('id', flat=True))

        # IARC.
        content_ratings = {}
        for cr in obj.content_ratings.all():
            for region in cr.get_region_slugs():
                body = cr.get_body()
                rating = cr.get_rating()
                content_ratings.setdefault(region, []).append({
                    'body': unicode(body.name),
                    'body_slug': unicode(body.slug),
                    'name': unicode(rating.name),
                    'slug': unicode(rating.slug),
                    'description': unicode(rating.description),
                })

        attrs = ('app_slug', 'average_daily_users', 'bayesian_rating',
                 'created', 'id', 'is_disabled', 'last_updated',
                 'premium_type', 'status', 'type', 'uses_flash',
                 'weekly_downloads')
        d = dict(zip(attrs, attrgetter(*attrs)(obj)))

        d['app_type'] = obj.app_type_id
        d['author'] = obj.developer_name
        d['category'] = list(obj.categories.values_list('slug', flat=True))
        d['collection'] = [{'id': cms.collection_id, 'order': cms.order}
                           for cms in obj.collectionmembership_set.all()]
        d['content_ratings'] = content_ratings if content_ratings else None
        d['current_version'] = version.version if version else None
        d['default_locale'] = obj.default_locale
        d['description'] = list(set(s for _, s
                                    in translations[obj.description_id]))
        d['device'] = getattr(obj, 'device_ids', [])
        d['features'] = features
        d['has_public_stats'] = obj.public_stats
        # TODO: Store all localizations of homepage.
        d['homepage'] = unicode(obj.homepage) if obj.homepage else ''
        d['icons'] = [{'size': icon_size, 'url': obj.get_icon_url(icon_size)}
                      for icon_size in (16, 48, 64, 128)]
        d['is_escalated'] = is_escalated
        if latest_version:
            d['latest_version'] = {
                'status': status,
                'is_privileged': latest_version.is_privileged,
                'has_editor_comment': latest_version.has_editor_comment,
                'has_info_request': latest_version.has_info_request,
            }
        else:
            d['latest_version'] = {
                'status': None,
                'is_privileged': None,
                'has_editor_comment': None,
                'has_info_request': None,
            }
        d['manifest_url'] = obj.get_manifest_url()
        d['name'] = list(set(string for _, string
                             in translations[obj.name_id]))
        d['name_sort'] = unicode(obj.name).lower()
        d['owners'] = [au.user.id for au in
                       obj.addonuser_set.filter(role=amo.AUTHOR_ROLE_OWNER)]
        d['popularity'] = d['_boost'] = len(installed_ids)
        d['previews'] = [{'filetype': p.filetype,
                          'image_url': p.image_url,
                          'thumbnail_url': p.thumbnail_url}
                         for p in obj.previews.all()]
        try:
            p = obj.addonpremium.price
            d['price_tier'] = p.name
        except AddonPremium.DoesNotExist:
            d['price_tier'] = None

        d['ratings'] = {
            'average': obj.average_rating,
            'count': obj.total_reviews,
        }
        d['region_exclusions'] = obj.get_excluded_region_ids()
        d['support_email'] = (unicode(obj.support_email)
                              if obj.support_email else None)
        d['support_url'] = (unicode(obj.support_url)
                            if obj.support_url else None)
        if version:
            d['supported_locales'] = filter(
                None, version.supported_locales.split(','))
        else:
            d['supported_locales'] = []

        d['tags'] = getattr(obj, 'tag_list', [])
        if obj.upsell:
            upsell_obj = obj.upsell.premium
            d['upsell'] = {
                'id': upsell_obj.id,
                'app_slug': upsell_obj.app_slug,
                'icon_url': upsell_obj.get_icon_url(128),
                # TODO: Store all localizations of upsell.name.
                'name': unicode(upsell_obj.name),
                'region_exclusions': upsell_obj.get_excluded_region_ids()
            }

        d['versions'] = [dict(version=v.version,
                              resource_uri=reverse_version(v))
                         for v in obj.versions.all()]

        # Calculate regional popularity for "mature regions"
        # (installs + reviews/installs from that region).
        installs = dict(ClientData.objects.filter(installed__in=installed_ids)
                        .annotate(region_counts=models.Count('region'))
                        .values_list('region', 'region_counts').distinct())
        for region in mkt.regions.ALL_REGION_IDS:
            cnt = installs.get(region, 0)
            if cnt:
                # Magic number (like all other scores up in this piece).
                d['popularity_%s' % region] = d['popularity'] + cnt * 10
            else:
                d['popularity_%s' % region] = len(installed_ids)
            d['_boost'] += cnt * 10

        # Bump the boost if the add-on is public.
        if obj.status == amo.STATUS_PUBLIC:
            d['_boost'] = max(d['_boost'], 1) * 4

        # Indices for each language. languages is a list of locales we want to
        # index with analyzer if the string's locale matches.
        for analyzer, languages in amo.SEARCH_ANALYZER_MAP.iteritems():
            if (not settings.ES_USE_PLUGINS and
                analyzer in amo.SEARCH_ANALYZER_PLUGINS):
                continue

            d['name_' + analyzer] = list(
                set(string for locale, string in translations[obj.name_id]
                    if locale.lower() in languages))
            d['description_' + analyzer] = list(
                set(string for locale, string
                    in translations[obj.description_id]
                    if locale.lower() in languages))

        return d

    @classmethod
    def get_indexable(cls):
        """Returns the queryset of ids of all things to be indexed."""
        return (Webapp.with_deleted.all()
                .order_by('-id').values_list('id', flat=True))


# Pull all translated_fields from Addon over to Webapp.
Webapp._meta.translated_fields = Addon._meta.translated_fields


@receiver(dbsignals.post_save, sender=Webapp,
          dispatch_uid='webapp.search.index')
def update_search_index(sender, instance, **kw):
    from . import tasks
    if not kw.get('raw'):
        tasks.index_webapps.delay([instance.id])


models.signals.pre_save.connect(save_signal, sender=Webapp,
                                dispatch_uid='webapp_translations')


@receiver(version_changed, dispatch_uid='update_cached_manifests')
def update_cached_manifests(sender, **kw):
    if not kw.get('raw'):
        from mkt.webapps.tasks import update_cached_manifests
        update_cached_manifests.delay(sender.id)


@Webapp.on_change
def watch_status(old_attr={}, new_attr={}, instance=None, sender=None, **kw):
    """Set nomination date when app is pending review."""
    new_status = new_attr.get('status')
    if not new_status:
        return
    addon = instance
    if new_status == amo.STATUS_PENDING and old_attr['status'] != new_status:
        # We always set nomination date when app switches to PENDING, even if
        # previously rejected.
        try:
            latest = addon.versions.latest()
            log.debug('[Webapp:%s] Setting nomination date to now.' % addon.id)
            latest.update(nomination=datetime.datetime.now())
        except Version.DoesNotExist:
            log.debug('[Webapp:%s] Missing version, no nomination set.'
                      % addon.id)
            pass


class Installed(amo.models.ModelBase):
    """Track WebApp installations."""
    addon = models.ForeignKey('addons.Addon', related_name='installed')
    user = models.ForeignKey('users.UserProfile')
    uuid = models.CharField(max_length=255, db_index=True, unique=True)
    client_data = models.ForeignKey('stats.ClientData', null=True)
    # Because the addon could change between free and premium,
    # we need to store the state at time of install here.
    premium_type = models.PositiveIntegerField(
        null=True, default=None, choices=amo.ADDON_PREMIUM_TYPES.items())
    install_type = models.PositiveIntegerField(
        db_index=True, default=apps.INSTALL_TYPE_USER,
        choices=apps.INSTALL_TYPES.items())

    class Meta:
        db_table = 'users_install'
        unique_together = ('addon', 'user', 'install_type', 'client_data')


@receiver(models.signals.post_save, sender=Installed)
def add_uuid(sender, **kw):
    if not kw.get('raw'):
        install = kw['instance']
        if not install.uuid and install.premium_type is None:
            install.uuid = ('%s-%s' % (install.pk, str(uuid.uuid4())))
            install.premium_type = install.addon.premium_type
            install.save()


class AddonExcludedRegion(amo.models.ModelBase):
    """
    Apps are listed in all regions by default.
    When regions are unchecked, we remember those excluded regions.
    """
    addon = models.ForeignKey('addons.Addon',
                              related_name='addonexcludedregion')
    region = models.PositiveIntegerField(
        choices=mkt.regions.REGIONS_CHOICES_ID)

    class Meta:
        db_table = 'addons_excluded_regions'
        unique_together = ('addon', 'region')

    def __unicode__(self):
        region = self.get_region()
        return u'%s: %s' % (self.addon, region.slug if region else None)

    def get_region(self):
        return mkt.regions.REGIONS_CHOICES_ID_DICT.get(self.region)


@memoize(prefix='get_excluded_in')
def get_excluded_in(region_id):
    """Return IDs of Webapp objects excluded from a particular region."""
    return list(AddonExcludedRegion.objects.filter(region=region_id)
                .values_list('addon', flat=True))


@receiver(models.signals.post_save, sender=AddonExcludedRegion,
          dispatch_uid='clean_memoized_exclusions')
def clean_memoized_exclusions(sender, **kw):
    if not kw.get('raw'):
        for k in mkt.regions.ALL_REGION_IDS:
            cache.delete_many([memoize_key('get_excluded_in', k)
                               for k in mkt.regions.ALL_REGION_IDS])


class ContentRating(amo.models.ModelBase):
    """
    Ratings body information about an app.
    """
    addon = models.ForeignKey('addons.Addon', related_name='content_ratings')
    ratings_body = models.PositiveIntegerField(
        choices=[(k, rb.name) for k, rb in
                 mkt.ratingsbodies.RATINGS_BODIES.items()],
        null=False)
    rating = models.PositiveIntegerField(null=False)

    class Meta:
        db_table = 'webapps_contentrating'

    def __unicode__(self):
        return u'%s: %s' % (self.addon, self.get_label())

    def get_regions(self):
        """Gives us the region classes that use this rating body."""
        if self.get_body() == mkt.ratingsbodies.GENERIC:
            # All regions w/o specified ratings bodies use GENERIC.
            return mkt.regions.ALL_REGIONS_WO_CONTENT_RATINGS

        return [x for x in mkt.regions.ALL_REGIONS_WITH_CONTENT_RATINGS
                if self.get_body() in x.ratingsbodies]

    def get_region_slugs(self):
        """Gives us the region slugs that use this rating body."""
        if self.get_body() == mkt.ratingsbodies.GENERIC:
            # For the generic rating body, we just pigeonhole all of the misc.
            # regions into one region slug, GENERIC.
            return [mkt.regions.GENERIC_RATING_REGION_SLUG]
        return [x.slug for x in self.get_regions()]

    def get_body(self):
        """Gives us something like DEJUS."""
        body = mkt.ratingsbodies.RATINGS_BODIES[self.ratings_body]
        body.slug = unicode(body.name).lower()
        return body

    def get_rating(self):
        """Gives us the rating class (containing the name and description)."""
        rating = self.get_body().ratings[self.rating]
        if not hasattr(rating, 'slug'):
            rating.slug = unicode(rating.name).lower().replace('+', '')
        return rating

    def get_label(self):
        """Gives us the name to be used for the form options."""
        return u'%s - %s' % (self.get_body().name, self.get_rating().name)


# The RatingDescriptors table is created with dynamic fields based on
# mkt.constants.ratingdescriptors.
class RatingDescriptors(amo.models.ModelBase, DynamicBoolFieldsMixin):
    """
    A dynamically generated model that contains a set of boolean values
    stating if an app is rated with a particular descriptor.
    """
    addon = models.OneToOneField(Addon, related_name='rating_descriptors')
    field_source = mkt.ratingdescriptors.RATING_DESCS

    class Meta:
        db_table = 'webapps_rating_descriptors'

    def __unicode__(self):
        return u'%s: %s' % (self.id, self.addon.name)


# Add a dynamic field to `RatingDescriptors` model for each rating descriptor.
for k, v in mkt.ratingdescriptors.RATING_DESCS.iteritems():
    field = models.BooleanField(default=False, help_text=v['name'])
    field.contribute_to_class(RatingDescriptors, 'has_%s' % k.lower())


# The RatingInteractives table is created with dynamic fields based on
# mkt.constants.ratinginteractives.
class RatingInteractives(amo.models.ModelBase, DynamicBoolFieldsMixin):
    """
    A dynamically generated model that contains a set of boolean values
    stating if an app features a particular interactive element.
    """
    addon = models.OneToOneField(Addon, related_name='rating_interactives')
    field_source = mkt.ratinginteractives.RATING_INTERACTIVES

    class Meta:
        db_table = 'webapps_rating_interactives'

    def __unicode__(self):
        return u'%s: %s' % (self.id, self.addon.name)


# Add a dynamic field to `RatingInteractives` model for each rating descriptor.
for k, v in mkt.ratinginteractives.RATING_INTERACTIVES.iteritems():
    field = models.BooleanField(default=False, help_text=v['name'])
    field.contribute_to_class(RatingInteractives, 'has_%s' % k.lower())


# The AppFeatures table is created with dynamic fields based on
# mkt.constants.features, which requires some setup work before we call `type`.
class AppFeatures(amo.models.ModelBase, DynamicBoolFieldsMixin):
    """
    A dynamically generated model that contains a set of boolean values
    stating if an app requires a particular feature.
    """
    version = models.OneToOneField(Version, related_name='features')
    field_source = APP_FEATURES

    class Meta:
        db_table = 'addons_features'

    def __unicode__(self):
        return u'Version: %s: %s' % (self.version.id, self.to_signature())

    def set_flags(self, signature):
        """
        Sets flags given the signature.

        This takes the reverse steps in `to_signature` to set the various flags
        given a signature. Boolean math is used since "0.23.1" is a valid
        signature but does not produce a string of required length when doing
        string indexing.
        """
        fields = self._fields()
        # Grab the profile part of the signature and convert to binary string.
        try:
            profile = bin(int(signature.split('.')[0], 16)).lstrip('0b')
            n = len(fields) - 1
            for i, f in enumerate(fields):
                setattr(self, f, bool(int(profile, 2) & 2 ** (n - i)))
        except ValueError as e:
            log.error(u'ValueError converting %s. %s' % (signature, e))

    def to_signature(self):
        """
        This converts the boolean values of the flags to a signature string.

        For example, all the flags in APP_FEATURES order produce a string of
        binary digits that is then converted to a hexadecimal string with the
        length of the features list plus a version appended. E.g.::

            >>> profile = '10001010111111010101011'
            >>> int(profile, 2)
            4554411
            >>> '%x' % int(profile, 2)
            '457eab'
            >>> '%x.%s.%s' % (int(profile, 2), len(profile), 1)
            '457eab.23.1'

        """
        profile = ''.join('1' if getattr(self, f) else '0'
                          for f in self._fields())
        return '%x.%s.%s' % (int(profile, 2), len(profile),
                             settings.APP_FEATURES_VERSION)


# Add a dynamic field to `AppFeatures` model for each buchet feature.
for k, v in APP_FEATURES.iteritems():
    field = models.BooleanField(default=False, help_text=v['name'])
    field.contribute_to_class(AppFeatures, 'has_%s' % k.lower())


class AppManifest(amo.models.ModelBase):
    """
    Storage for manifests.

    Tied to version since they change between versions. This stores both hosted
    and packaged apps manifests for easy access.
    """
    version = models.OneToOneField(Version, related_name='manifest_json')
    manifest = models.TextField()

    class Meta:
        db_table = 'app_manifest'


class Geodata(amo.models.ModelBase):
    """TODO: Forgo AER and use bool columns for every region and carrier."""
    addon = models.OneToOneField('addons.Addon', related_name='_geodata')
    restricted = models.BooleanField(default=False)
    popular_region = models.CharField(max_length=10, null=True)

    class Meta:
        db_table = 'webapps_geodata'

    def __unicode__(self):
        return u'%s (%s): <Webapp %s>' % (self.id,
            'restricted' if self.restricted else 'unrestricted',
            self.addon.id)
