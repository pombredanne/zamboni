from django.contrib.syndication.views import Feed
from django.shortcuts import get_object_or_404

from tower import ugettext as _

import amo
from amo.urlresolvers import reverse
from amo.helpers import absolutify, url, page_name
from addons.models import Addon, Category
from .views import addon_listing, SearchToolsFilter


class AddonFeedMixin(object):
    """Common pieces for add-ons in a feed."""

    def item_link(self, addon):
        """Link for a particular addon (<item><link>...</)"""
        return absolutify(reverse('addons.detail', args=[addon.slug]))

    def item_title(self, addon):
        version = ''
        if addon.current_version:
            version = u' %s' % addon.current_version
        return u'%s%s' % (addon.name, version)

    def item_description(self, addon):
        """Description for particular add-on (<item><description>)"""
        return unicode(addon.description) or ''

    def item_author_name(self, addon):
        """Author for a particuar add-on (<item><dc:creator>)"""
        if addon.listed_authors:
            return addon.listed_authors[0].name
        else:
            return ''

    def item_pubdate(self, addon):
        """Pubdate for a particuar add-on (<item><pubDate>)"""
        sort = self.request.GET.get('sort')
        return addon.created if sort == 'created' else addon.last_updated

    def item_guid(self, addon):
        """Guid for a particuar version (<item><guid>)"""
        url_ = reverse('addons.versions',
                       args=[addon.slug, addon.current_version])
        return absolutify(url_)


class CategoriesRss(AddonFeedMixin, Feed):

    def get_object(self, request, category_name=None):
        """
        Get the Category for which we are about to output
        the RSS feed of its Addons
        """
        self.request = request
        if category_name is None:
            return None
        q = Category.objects.filter(application=request.APP.id, type=self.TYPE)
        self.category = get_object_or_404(q, slug=category_name)
        return self.category

    def title(self, category):
        """Title for the feed as a whole"""
        name = category.name if category else _('Extensions')
        return u'%s :: %s' % (name, page_name(self.request.APP))

    def link(self, category):
        """Link for the feed as a whole"""
        return absolutify(url('home'))

    def description(self, category):
        """Description for the feed as a whole"""
        if category:
            # L10n: %s is a category name.
            return _(u'%s Add-ons') % category.name
        else:
            return _('Extensions')

    def items(self, category):
        """Return the Addons for this Category to be output as RSS <item>'s"""
        addons, _ = addon_listing(self.request, [self.TYPE], default='updated')
        if category:
            addons = addons.filter(categories__id=category.id)
        return addons[:20]


class ExtensionCategoriesRss(CategoriesRss):
    category = None
    request = None
    TYPE = amo.ADDON_EXTENSION
    title = _('Extensions')

    def description(self, category):
        """Description for the feed as a whole."""
        if category:
            # L10n: %s is a category name.
            return _(u'%s Add-ons') % category.name
        else:
            return _('Extensions')


class ThemeCategoriesRss(CategoriesRss):
    category = None
    request = None
    TYPE = amo.ADDON_THEME
    title = _('Themes')

    def description(self, category):
        """Description for the feed as a whole."""
        if category:
            # L10n: %s is a category name.
            return _(u'%s Themes') % category.name
        else:
            return self.title


class FeaturedRss(AddonFeedMixin, Feed):

    request = None

    def get_object(self, request):
        self.request = request
        self.app = request.APP
        self.appname = unicode(request.APP.pretty)

    def title(self):
        """Title for the feed"""
        return _('Featured Add-ons :: %s') % page_name(self.app)

    def link(self):
        """Link for the feed"""
        return absolutify(url('home'))

    def description(self):
        """Description for the feed"""
        # L10n: %s is an app name.
        return _("Here's a few of our favorite add-ons to help you get"
                 " started customizing %s.") % self.appname

    def items(self):
        """Return the Addons to be output as RSS <item>'s"""
        return Addon.objects.featured(self.app)[:20]


class SearchToolsRss(AddonFeedMixin, Feed):

    category = None
    request = None
    TYPES = None
    sort = ''

    def description(self):
        """Description of this feed."""
        if self.category:
            # L10n: %s is a category name.
            return _(u'Search tools relating to %s') % self.category.name
        elif self.show_featured:
            return _('Search tools and search-related extensions')
        else:
            return _('Search tools')

    def get_object(self, request, category=None):
        if category:
            # Note that we don't need to include extensions
            # when looking up a category
            qs = Category.objects.filter(application=request.APP.id,
                                         type=amo.ADDON_SEARCH)
            self.category = get_object_or_404(qs, slug=category)
        else:
            self.category = None
        self.request = request
        self.sort = self.request.GET.get('sort', 'popular')
        self.show_featured = self.sort == 'featured'
        self.TYPES = [amo.ADDON_SEARCH]
        if not self.category and self.show_featured:
            self.TYPES.append(amo.ADDON_EXTENSION)

        # We don't actually need to return anything, just hijacking the hook.
        return None

    def items(self):
        """Return search related Add-ons to be output as RSS <item>'s

        Just like on the landing page, the following rules apply:
        - when viewing featured search tools, include
          extensions in the search category
        - when viewing categories or any other sorting, do not
          include extensions.
        """
        addons, filter = addon_listing(self.request, self.TYPES,
                                       SearchToolsFilter, default='popular')
        if self.category:
            addons = addons.filter(categories__id=self.category.id)
        return addons[:30]

    def link(self, category):
        """Link for the feed as a whole"""
        if self.category:
            base = url('browse.search-tools.rss', self.category.slug)
        else:
            base = url('browse.search-tools.rss')
        return absolutify(base + '?sort=' + self.sort)

    def title(self):
        """Title for the feed as a whole"""
        base = _('Search Tools')
        if self.category:
            base = u'%s :: %s' % (self.category.name, base)
        return u'%s :: %s' % (base, page_name(self.request.APP))
