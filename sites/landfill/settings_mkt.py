"""private_mkt will be populated from puppet and placed in this directory"""

from lib.settings_base import *
from mkt.settings import *
from settings_base import *

import private_mkt

SERVER_EMAIL = 'zmktlandfill@addons.mozilla.org'

DOMAIN = "landfill.marketplace.allizom.org"
SITE_URL = 'https://landfill.marketplace.allizom.org'
SERVICES_URL = SITE_URL
STATIC_URL = SITE_URL + '/'
LOCAL_MIRROR_URL = '%s_files' % STATIC_URL
MIRROR_URL = LOCAL_MIRROR_URL

CSP_STATIC_URL = STATIC_URL[:-1]
CSP_IMG_SRC = CSP_IMG_SRC + (CSP_STATIC_URL,)
CSP_SCRIPT_SRC = CSP_SCRIPT_SRC + (CSP_STATIC_URL,)
CSP_STYLE_SRC = CSP_STYLE_SRC + (CSP_STATIC_URL,)

ADDON_ICON_URL = "%s/%s/%s/images/addon_icon/%%d-%%d.png?modified=%%s" % (STATIC_URL, LANGUAGE_CODE, DEFAULT_APP)
ADDON_ICON_URL = STATIC_URL + 'img/uploads/addon_icons/%s/%s-%s.png?modified=%s'
PREVIEW_THUMBNAIL_URL = (STATIC_URL +
        'img/uploads/previews/thumbs/%s/%d.png?modified=%d')
PREVIEW_FULL_URL = (STATIC_URL +
        'img/uploads/previews/full/%s/%d.%s?modified=%d')
# paths for uploaded extensions
FILES_URL = STATIC_URL + "%s/%s/downloads/file/%d/%s?src=%s"

SESSION_COOKIE_DOMAIN = ".%s" % DOMAIN

# paths for uploaded extensions
USERPICS_URL = STATIC_URL + 'img/uploads/userpics/%s/%s/%s.png?modified=%d'
COLLECTION_ICON_URL = STATIC_URL + '/img/uploads/collection_icons/%s/%s.png?m=%s'

MEDIA_URL = STATIC_URL + 'media/'
ADDON_ICONS_DEFAULT_URL = MEDIA_URL + 'img/hub'
ADDON_ICON_BASE_URL = MEDIA_URL + 'img/icons/'

PRODUCT_ICON_URL = STATIC_URL + 'product-icons'

CACHE_PREFIX = 'landfill.mkt.%s' % CACHE_PREFIX
CACHE_MIDDLEWARE_KEY_PREFIX = CACHE_PREFIX
CACHES['default']['KEY_PREFIX'] = CACHE_PREFIX

SYSLOG_TAG = "http_app_addons_marketplacelandfill"
SYSLOG_TAG2 = "http_app_addons_marketplacelandfill_timer"
SYSLOG_CSP = "http_app_addons_marketplacelandfill_csp"

STATSD_PREFIX = 'marketplace-landfill'

## Celery
BROKER_URL = private_mkt.BROKER_URL

CELERY_IGNORE_RESULT = True
CELERY_DISABLE_RATE_LIMITS = True
CELERYD_PREFETCH_MULTIPLIER = 1

# sandbox
PAYPAL_PAY_URL = 'https://svcs.sandbox.paypal.com/AdaptivePayments/'
PAYPAL_FLOW_URL = 'https://www.sandbox.paypal.com/webapps/adaptivepayment/flow/pay'
PAYPAL_API_URL = 'https://api-3t.sandbox.paypal.com/nvp'
PAYPAL_EMAIL = private_mkt.PAYPAL_EMAIL
PAYPAL_APP_ID = private_mkt.PAYPAL_APP_ID
PAYPAL_PERMISSIONS_URL = 'https://svcs.sandbox.paypal.com/Permissions/'
PAYPAL_CGI_URL = 'https://www.sandbox.paypal.com/cgi-bin/webscr'
PAYPAL_EMBEDDED_AUTH = {
    'USER': private_mkt.PAYPAL_EMBEDDED_AUTH_USER,
    'PASSWORD': private_mkt.PAYPAL_EMBEDDED_AUTH_PASSWORD,
    'SIGNATURE': private_mkt.PAYPAL_EMBEDDED_AUTH_SIGNATURE,
}

PAYPAL_CGI_AUTH = { 'USER': private_mkt.PAYPAL_CGI_AUTH_USER,
                    'PASSWORD': private_mkt.PAYPAL_CGI_AUTH_PASSWORD,
                    'SIGNATURE': private_mkt.PAYPAL_CGI_AUTH_SIGNATURE,
}

PAYPAL_CHAINS = (
    (30, private_mkt.PAYPAL_CHAINS_EMAIL),
)

WEBAPPS_RECEIPT_KEY = private_mkt.WEBAPPS_RECEIPT_KEY
WEBAPPS_RECEIPT_URL = private_mkt.WEBAPPS_RECEIPT_URL

APP_PREVIEW = True

WEBAPPS_UNIQUE_BY_DOMAIN = False

SENTRY_DSN = private_mkt.SENTRY_DSN

WEBAPPS_PUBLIC_KEY_DIRECTORY = NETAPP_STORAGE + '/public_keys'
PRODUCT_ICON_PATH = NETAPP_STORAGE + '/product-icons'

SOLITUDE_HOSTS = ('https://payments-dev.allizom.org',)

PAYPAL_LIMIT_PREAPPROVAL = False

VALIDATOR_IAF_URLS = ['https://marketplace.firefox.com',
                      'https://marketplace.allizom.org',
                      'https://marketplace-dev.allizom.org',
                      'https://landfill.addons.allizom.org']

AMO_LANGUAGES = AMO_LANGUAGES + ('dbg',)
LANGUAGES = lazy(lazy_langs, dict)(AMO_LANGUAGES)
LANGUAGE_URL_MAP = dict([(i.lower(), i) for i in AMO_LANGUAGES])

BLUEVIA_SECRET = private_mkt.BLUEVIA_SECRET

#Bug 748403
SIGNING_SERVER = private_mkt.SIGNING_SERVER
SIGNING_SERVER_ACTIVE = True
SIGNING_VALID_ISSUERS = ['marketplace-dev-cdn.allizom.org']

#Bug 793876
SIGNED_APPS_KEY = private_mkt.SIGNED_APPS_KEY
SIGNED_APPS_SERVER_ACTIVE = False

METLOG_CONF['logger'] = 'addons-marketplace-landfill'
METLOG_CONF['plugins']['raven'] = ('metlog_raven.raven_plugin:config_plugin', {'dsn': private_mkt.SENTRY_DSN})
METLOG = client_from_dict_config(METLOG_CONF)
