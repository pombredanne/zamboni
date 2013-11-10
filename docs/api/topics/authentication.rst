.. _authentication:

==============
Authentication
==============

Not all APIs require authentication. Each API will note if it needs
authentication.

Two options for authentication are available: shared-secret and OAuth.

.. _sharedsecret:

Shared Secret
=============

The Marketplace frontend uses a server-supplied token for authentication,
stored as a cookie.

.. http:post:: /api/v1/account/login/

    **Request**

    :param assertion: the Persona assertion.
    :type assertion: string
    :param audience: the Persona audience.
    :type audience: string

    Example:

    .. code-block:: json

        {
            "assertion": "1234",
            "audience": "some.site.com"
        }

    **Response**

    :param error: any error that occurred.
    :type error: string
    :param token: a shared secret to be used on later requests. It should be
        sent with authorized requests as a query string parameter named
        ``_user``.
    :type token: string
    :param permissions: :ref:`user permissions <permission-get-label>`.
    :type permissions: object
    :param settings: user account settings.
    :type settings: object

    Example:

    .. code-block:: json

        {
            "error": null,
            "token": "ffoob@example.com,95c9063d9f249aacfe5697fc83192e...",
            "settings": {
                "display_name": "fred foobar",
                "email": "ffoob@example.com",
                "region": "appistan"
            },
            "permissions": {
                "reviewer": false,
                "admin": false,
                "localizer": false,
                "lookup": true,
                "developer": true
            }
        }

    :status 201: successfully completed, a new profile might have been created
        in the marketplace if the account was new.

OAuth
=====

Marketplace provides OAuth 1.0a, allowing third-party apps to interact with its
API.


See the `OAuth Guide <http://hueniverse.com/oauth/guide/>`_ and this `authentication flow diagram <http://oauth.net/core/diagram.png>`_ for an overview of OAuth concepts.
The "Application Name" and "Redirect URI" fields are used by Marketplace when prompting users for authorization, allowing your application to make API requests on their behalf.
"Application Name" should contain the name of your app, for Marketplace to show users when asking them for authorization.
"Redirect URI" should contain the URI to redirect the user to, after the user grants access to your app (step D in the diagram linked above).
These fields can be left blank if this key will only be used to access your own Marketplace account.
When you are first developing your API to communicate with the Marketplace, you
should use the development server to test your API.

OAuth URLs
----------

 * The Temporary Credential Request URL path is `/oauth/register/`.
 * The Resource Owner Authorization URL path is `/oauth/authorize/`.
 * The Token Request URL path is `/oauth/token/`.


Production server
=================

The production server is at https://marketplace.firefox.com.

1. Log in using Persona:
   https://marketplace.firefox.com/login

2. At https://marketplace.firefox.com/developers/api provide the name of
   the app that will use the key, and the URI that Marketplace's OAuth provide
   will redirect to after the user grants permission to your app. You may then
   generate a key pair for use in your application.

3. (Optional) If you are planning on submitting an app, you must accept the
   terms of service: https://marketplace.firefox.com/developers/terms

Development server
==================

The development server is at https://marketplace-dev.allizom.org.

We make no guarantees on the uptime of the development server. Data is
regularly purged, causing the deletion of apps and tokens.

Using OAuth Tokens
==================

Once you've got your token, you will need to ensure that the OAuth token is
sent correctly in each request.

To correctly sign an OAuth request, you'll need the OAuth consumer key and
secret and then sign the request using your favourite OAuth library. An example
of this can be found in the `example marketplace client`_.

Example headers (new lines added for clarity)::

        Content-type: application/json
        Authorization: OAuth realm="",
                       oauth_body_hash="2jm...",
                       oauth_nonce="06731830",
                       oauth_timestamp="1344897064",
                       oauth_consumer_key="some-consumer-key",
                       oauth_signature_method="HMAC-SHA1",
                       oauth_version="1.0",
                       oauth_signature="Nb8..."

If requests are failing and returning a 401 response, then there will likely be
a reason contained in the response. For example:

        .. code-block:: json

            {"reason": "Terms of service not accepted."}

.. _`example marketplace client`: https://github.com/mozilla/Marketplace.Python
