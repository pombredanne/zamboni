import logging
from operator import itemgetter

from django.conf import settings as dj_settings

from django_statsd.clients import statsd
from elasticutils import S as EU_S
from elasticutils.contrib.django import get_es as eu_get_es
from pyes import ES as pyes_ES
from pyes import VERSION as PYES_VERSION


log = logging.getLogger('z.es')


DEFAULT_HOSTS = ['localhost:9200']
DEFAULT_TIMEOUT = 5
DEFAULT_INDEXES = ['default']
DEFAULT_DUMP_CURL = None


# Pulled from elasticutils 0.5 so we can upgrade elasticutils to a newer
# version which is based on pyelasticsearch and not break AMO.
def get_es(hosts=None, default_indexes=None, timeout=None, dump_curl=None,
           **settings):
    """Create an ES object and return it.

    :arg hosts: list of uris; ES hosts to connect to, defaults to
        ``['localhost:9200']``
    :arg default_indexes: list of strings; the default indexes to use,
        defaults to 'default'
    :arg timeout: int; the timeout in seconds, defaults to 5
    :arg dump_curl: function or None; function that dumps curl output,
        see docs, defaults to None
    :arg settings: other settings to pass into `pyes.es.ES`

    Examples:

    >>> es = get_es()

    >>> es = get_es(hosts=['localhost:9200'])

    >>> es = get_es(timeout=30)  # good for indexing

    >>> es = get_es(default_indexes=['sumo_prod_20120627']

    >>> class CurlDumper(object):
    ...     def write(self, text):
    ...         print text
    ...
    >>> es = get_es(dump_curl=CurlDumper())

    """
    # Cheap way of de-None-ifying things
    hosts = hosts or getattr(dj_settings, 'ES_HOSTS', DEFAULT_HOSTS)
    default_indexes = default_indexes or [dj_settings.ES_INDEXES['default']]
    timeout = (timeout if timeout is not None else
               getattr(dj_settings, 'ES_TIMEOUT', DEFAULT_TIMEOUT))
    dump_curl = dump_curl or getattr(dj_settings, 'DEFAULT_DUMP_CURL', False)

    if not isinstance(default_indexes, list):
        default_indexes = [default_indexes]

    es = pyes_ES(hosts, default_indexes=default_indexes, timeout=timeout,
                 dump_curl=dump_curl, **settings)

    # pyes 0.15 does this lame thing where it ignores dump_curl in
    # the ES constructor and always sets it to None. So what we do
    # is set it manually after the ES has been created and
    # defaults['dump_curl'] is truthy. This might not work for all
    # values of dump_curl.
    if PYES_VERSION[0:2] == (0, 15) and dump_curl is not None:
        es.dump_curl = dump_curl

    return es


class ES(object):

    def __init__(self, type_, index):
        self.type = type_
        self.index = index
        self.steps = []
        self.start = 0
        self.stop = None
        self.as_list = self.as_dict = False
        self._results_cache = None

    def _clone(self, next_step=None):
        new = self.__class__(self.type, self.index)
        new.steps = list(self.steps)
        if next_step:
            new.steps.append(next_step)
        new.start = self.start
        new.stop = self.stop
        return new

    def values(self, *fields):
        return self._clone(next_step=('values', fields))

    def values_dict(self, *fields):
        return self._clone(next_step=('values_dict', fields))

    def order_by(self, *fields):
        return self._clone(next_step=('order_by', fields))

    def query(self, **kw):
        return self._clone(next_step=('query', kw.items()))

    def filter(self, **kw):
        return self._clone(next_step=('filter', kw.items()))

    def facet(self, **kw):
        return self._clone(next_step=('facet', kw.items()))

    def extra(self, **kw):
        new = self._clone()
        actions = 'values values_dict order_by query filter facet'.split()
        for key, vals in kw.items():
            assert key in actions
            if hasattr(vals, 'items'):
                new.steps.append((key, vals.items()))
            else:
                new.steps.append((key, vals))
        return new

    def count(self):
        if self._results_cache:
            return self._results_cache.count
        else:
            return self[:0].raw()['hits']['total']

    def __len__(self):
        return len(self._do_search())

    def __getitem__(self, k):
        new = self._clone()
        # TODO: validate numbers and ranges
        if isinstance(k, slice):
            new.start, new.stop = k.start or 0, k.stop
            return new
        else:
            new.start, new.stop = k, k + 1
            return list(new)[0]

    def _build_query(self):
        filters = []
        queries = []
        sort = []
        fields = ['id']
        facets = {}
        as_list = as_dict = False
        for action, value in self.steps:
            if action == 'order_by':
                for key in value:
                    if key.startswith('-'):
                        sort.append({key[1:]: 'desc'})
                    else:
                        sort.append(key)
            elif action == 'values':
                fields.extend(value)
                as_list, as_dict = True, False
            elif action == 'values_dict':
                if not value:
                    fields = []
                else:
                    fields.extend(value)
                as_list, as_dict = False, True
            elif action == 'query':
                queries.extend(self._process_queries(value))
            elif action == 'filter':
                filters.extend(self._process_filters(value))
            elif action == 'facet':
                facets.update(value)
            else:
                raise NotImplementedError(action)

        qs = {}
        if len(filters) > 1:
            qs['filter'] = {'and': filters}
        elif filters:
            qs['filter'] = filters[0]

        if len(queries) > 1:
            qs['query'] = {'bool': {'must': queries}}
        elif queries:
            qs['query'] = queries[0]

        if fields:
            qs['fields'] = fields
        if facets:
            qs['facets'] = facets
        if sort:
            qs['sort'] = sort
        if self.start:
            qs['from'] = self.start
        if self.stop is not None:
            qs['size'] = self.stop - self.start

        self.fields, self.as_list, self.as_dict = fields, as_list, as_dict
        return qs

    def _split(self, string):
        if '__' in string:
            return string.rsplit('__', 1)
        else:
            return string, None

    def _process_filters(self, value):
        rv = []
        value = dict(value)
        or_ = value.pop('or_', [])
        for key, val in value.items():
            key, field_action = self._split(key)
            if field_action is None:
                rv.append({'term': {key: val}})
            if field_action == 'in':
                rv.append({'in': {key: val}})
            elif field_action in ('gt', 'gte', 'lt', 'lte'):
                rv.append({'range': {key: {field_action: val}}})
            elif field_action == 'range':
                from_, to = val
                rv.append({'range': {key: {'gte': from_, 'lte': to}}})
        if or_:
            rv.append({'or': self._process_filters(or_.items())})
        return rv

    def _process_queries(self, value):
        rv = []
        value = dict(value)
        or_ = value.pop('or_', [])
        for key, val in value.items():
            key, field_action = self._split(key)
            if field_action is None:
                rv.append({'term': {key: val}})
            elif field_action in ('text', 'match'):
                rv.append({'match': {key: val}})
            elif field_action == 'startswith':
                rv.append({'prefix': {key: val}})
            elif field_action in ('gt', 'gte', 'lt', 'lte'):
                rv.append({'range': {key: {field_action: val}}})
            elif field_action == 'fuzzy':
                rv.append({'fuzzy': {key: val}})
        if or_:
            rv.append({'bool': {'should': self._process_queries(or_.items())}})
        return rv

    def _do_search(self):
        if not self._results_cache:
            hits = self.raw()
            if self.as_dict:
                ResultClass = DictSearchResults
            elif self.as_list:
                ResultClass = ListSearchResults
            else:
                ResultClass = ObjectSearchResults
            self._results_cache = ResultClass(self.type, hits, self.fields)
        return self._results_cache

    def raw(self):
        qs = self._build_query()
        es = get_es()
        try:
            with statsd.timer('search.es.timer') as timer:
                hits = es.search(qs, self.index, self.type._meta.db_table)
        except Exception:
            log.error(qs)
            raise
        statsd.timing('search.es.took', hits['took'])
        log.debug('[%s] [%s] %s' % (hits['took'], timer.ms, qs))
        return hits

    def __iter__(self):
        return iter(self._do_search())

    def raw_facets(self):
        return self._do_search().results.get('facets', {})

    @property
    def facets(self):
        facets = {}
        for key, val in self.raw_facets().items():
            if val['_type'] == 'terms':
                facets[key] = [v for v in val['terms']]
            elif val['_type'] == 'range':
                facets[key] = [v for v in val['ranges']]
        return facets


class SearchResults(object):

    def __init__(self, type, results, fields):
        self.type = type
        self.took = results['took']
        self.count = results['hits']['total']
        self.results = results
        self.fields = fields
        self.set_objects(results['hits']['hits'])

    def set_objects(self, hits):
        raise NotImplementedError()

    def __iter__(self):
        return iter(self.objects)

    def __len__(self):
        return len(self.objects)


class DictSearchResults(SearchResults):

    def set_objects(self, hits):
        key = 'fields' if self.fields else '_source'
        self.objects = [r[key] for r in hits]


class ListSearchResults(SearchResults):

    def set_objects(self, hits):
        if self.fields:
            getter = itemgetter(*self.fields)
            objs = [getter(r['fields']) for r in hits]
        else:
            objs = [r['_source'].values() for r in hits]
        self.objects = objs


class ObjectSearchResults(SearchResults):

    def set_objects(self, hits):
        self.ids = [int(r['_id']) for r in hits]
        self.objects = self.type.objects.filter(id__in=self.ids)

    def __iter__(self):
        objs = dict((obj.id, obj) for obj in self.objects)
        return (objs[id] for id in self.ids if id in objs)


class TempS(EU_S):
    # Temporary class override to mimic ElasticUtils v0.5 behavior.
    # TODO: Remove this when we've moved mkt to its own index.

    def get_es(self, **kwargs):
        """Returns the pyelasticsearch ElasticSearch object to use.

        This uses the django get_es builder by default which takes
        into account settings in ``settings.py``.

        """
        return super(TempS, self).get_es(default_builder=eu_get_es)

    def _do_search(self):
        """
        Perform the search, then convert that raw format into a SearchResults
        instance and return it.
        """
        if not self._results_cache:
            hits = self.raw()
            if self.as_list:
                ResultClass = ListSearchResults
            elif self.as_dict or self.type is None:
                ResultClass = DictSearchResults
            else:
                ResultClass = ObjectSearchResults
            self._results_cache = ResultClass(self.type, hits, self.fields)
        return self._results_cache

    def _build_query(self):
        query = super(TempS, self)._build_query()
        if 'fields' in query:
            if 'id' not in query['fields']:
                query['fields'].append('id')
        else:
            query['fields'] = ['id']

        return query
