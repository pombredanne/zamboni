import hashlib
import hmac
import uuid

from django.conf import settings
from django.contrib.auth.signals import user_logged_in

import basket
import commonware.log
from django_statsd.clients import statsd
from rest_framework import status
from rest_framework.exceptions import AuthenticationFailed
from rest_framework.generics import CreateAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.throttling import UserRateThrottle

from amo.utils import send_mail_jinja
from users.models import UserProfile
from users.views import browserid_authenticate

from mkt.account.serializers import (FeedbackSerializer, LoginSerializer,
                                     NewsletterSerializer,
                                     PermissionsSerializer)
from mkt.api.authentication import (RestAnonymousAuthentication,
                                    RestOAuthAuthentication,
                                    RestSharedSecretAuthentication)
from mkt.api.authorization import AllowSelf
from mkt.api.base import CORSMixin


log = commonware.log.getLogger('z.account')


class MineMixin(object):
    def get_object(self, queryset=None):
        pk = self.kwargs.get('pk')
        if pk == 'mine':
            self.kwargs['pk'] = self.request.amo_user.pk
        return super(MineMixin, self).get_object(queryset)


class CreateAPIViewWithoutModel(CreateAPIView):
    """
    A base class for APIs that need to support a create-like action, but
    without being tied to a Django Model.
    """
    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication,
                              RestAnonymousAuthentication]
    cors_allowed_methods = ['post']
    permission_classes = (AllowAny,)

    def response_success(self, request, serializer, data=None):
        if data is None:
            data = serializer.data
        return Response(data, status=status.HTTP_201_CREATED)

    def response_error(self, request, serializer):
        return Response(serializer.errors, status=status.HTTP_400_BAD_REQUEST)

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.DATA)
        if serializer.is_valid():
            data = self.create_action(request, serializer)
            return self.response_success(request, serializer, data=data)
        return self.response_error(request, serializer)


class FeedbackView(CORSMixin, CreateAPIViewWithoutModel):
    class FeedbackThrottle(UserRateThrottle):
        THROTTLE_RATES = {
            'user': '30/hour',
        }

    serializer_class = FeedbackSerializer
    throttle_classes = (FeedbackThrottle,)
    throttle_scope = 'user'

    def create_action(self, request, serializer):
        context_data = self.get_context_data(request, serializer)
        self.send_email(request, context_data)

    def send_email(self, request, context_data):
        sender = getattr(request.amo_user, 'email', settings.NOBODY_EMAIL)
        send_mail_jinja(u'Marketplace Feedback', 'account/email/feedback.txt',
                        context_data, from_email=sender,
                        recipient_list=[settings.MKT_FEEDBACK_EMAIL])

    def get_context_data(self, request, serializer):
        context_data = {
            'user_agent': request.META.get('HTTP_USER_AGENT', ''),
            'ip_address': request.META.get('REMOTE_ADDR', '')
        }
        context_data.update(serializer.data)
        context_data['user'] = request.amo_user
        return context_data


class LoginView(CORSMixin, CreateAPIViewWithoutModel):
    authentication_classes = []
    serializer_class = LoginSerializer

    def get_token(self, email):
        unique_id = uuid.uuid4().hex

        consumer_id = hashlib.sha1(
            email + settings.SECRET_KEY).hexdigest()

        hm = hmac.new(
            unique_id + settings.SECRET_KEY,
            consumer_id, hashlib.sha512)

        return ','.join((email, hm.hexdigest(), unique_id))

    def create_action(self, request, serializer):
        with statsd.timer('auth.browserid.verify'):
            profile, msg = browserid_authenticate(
                request, serializer.data['assertion'],
                browserid_audience=serializer.data['audience'],
                is_mobile=serializer.data['is_mobile'],
            )
        if profile is None:
            # Authentication failure.
            log.info('No profile: %s' % (msg or ''))
            raise AuthenticationFailed('No profile.')

        request.user, request.amo_user = profile.user, profile
        request.groups = profile.groups.all()

        # TODO: move this to the signal.
        profile.log_login_attempt(True)
        user_logged_in.send(sender=profile.user.__class__, request=request,
                            user=profile.user)

        # We want to return completely custom data, not the serializer's.
        data = {
            'error': None,
            'token': self.get_token(request.amo_user.email),
            'settings': {
                'display_name': request.amo_user.display_name,
                'email': request.amo_user.email,
            }
        }
        permissions = PermissionsSerializer(context={'request': request})
        data.update(permissions.data)
        return data


class NewsletterView(CORSMixin, CreateAPIViewWithoutModel):
    permission_classes = (IsAuthenticated,)
    serializer_class = NewsletterSerializer

    def response_success(self, request, serializer, data=None):
        return Response({}, status=status.HTTP_204_NO_CONTENT)

    def create_action(self, request, serializer):
        email = serializer.data['email']
        basket.subscribe(email, 'marketplace',
                         format='H', country=request.REGION.slug,
                         lang=request.LANG, optin='Y',
                         trigger_welcome='Y')


class PermissionsView(CORSMixin, MineMixin, RetrieveAPIView):

    authentication_classes = [RestOAuthAuthentication,
                              RestSharedSecretAuthentication]
    cors_allowed_methods = ['get']
    permission_classes = (AllowSelf,)
    model = UserProfile
    serializer_class = PermissionsSerializer
