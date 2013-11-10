from django.core.exceptions import ObjectDoesNotExist
from django.db import transaction
from django.shortcuts import redirect
from django.utils.translation.trans_real import to_language

import commonware.log
import jingo
import waffle

import amo
from amo.decorators import login_required
from amo.urlresolvers import reverse
from addons.models import Addon, AddonUser
from files.models import Platform
from lib.metrics import record_action
from users.models import UserProfile

from mkt.constants import DEVICE_LOOKUP
from mkt.developers import tasks
from mkt.developers.decorators import dev_required
from mkt.developers.forms import AppFormMedia, CategoryForm, PreviewFormSet
from mkt.submit.forms import AppDetailsBasicForm
from mkt.submit.models import AppSubmissionChecklist

from . import forms
from .decorators import read_dev_agreement_required, submit_step


log = commonware.log.getLogger('z.submit')


def submit(request):
    """Determine which step to redirect user to."""
    if not request.user.is_authenticated():
        return proceed(request)
    # If dev has already agreed, continue to next step.
    user = UserProfile.objects.get(pk=request.user.id)
    if not user.read_dev_agreement:
        return redirect('submit.app.terms')
    return manifest(request)


def proceed(request):
    """
    This is a fake "Terms" view that we overlay the login.
    We link here from the Developer Hub landing page.
    """
    if request.user.is_authenticated():
        return submit(request)
    agreement_form = forms.DevAgreementForm({'read_dev_agreement': True},
                                            instance=None)
    return jingo.render(request, 'submit/terms.html', {
        'step': 'terms',
        'agreement_form': agreement_form,
        'proceed': True,
    })


@login_required
@submit_step('terms')
def terms(request):
    # If dev has already agreed, continue to next step.
    if (getattr(request, 'amo_user', None) and
            request.amo_user.read_dev_agreement):
        return manifest(request)

    agreement_form = forms.DevAgreementForm(
        request.POST or {'read_dev_agreement': True},
        instance=request.amo_user)
    if request.POST and agreement_form.is_valid():
        agreement_form.save()
        return redirect('submit.app')
    return jingo.render(request, 'submit/terms.html', {
        'step': 'terms',
        'agreement_form': agreement_form,
    })


@login_required
@read_dev_agreement_required
@submit_step('manifest')
def manifest(request):

    form = forms.NewWebappForm(request.POST or None, request=request)

    features_form = forms.AppFeaturesForm(request.POST or None)
    features_form_valid = (True if not waffle.switch_is_active('buchets')
                           else features_form.is_valid())

    if (request.method == 'POST' and form.is_valid()
        and features_form_valid):

        with transaction.commit_on_success():

            addon = Addon.from_upload(
                form.cleaned_data['upload'],
                [Platform.objects.get(id=amo.PLATFORM_ALL.id)],
                is_packaged=form.is_packaged())

            # Set the device type.
            for device in form.get_devices():
                addon.addondevicetype_set.get_or_create(
                    device_type=device.id)

            # Set the premium type, only bother if it's not free.
            premium = form.get_paid()
            if premium:
                addon.update(premium_type=premium)

            if addon.has_icon_in_manifest():
                # Fetch the icon, do polling.
                addon.update(icon_type='image/png')
            else:
                # In this case there is no need to do any polling.
                addon.update(icon_type='')

            AddonUser(addon=addon, user=request.amo_user).save()
            # Checking it once. Checking it twice.
            AppSubmissionChecklist.objects.create(addon=addon, terms=True,
                                                  manifest=True)

            # Create feature profile.
            if waffle.switch_is_active('buchets'):
                addon.current_version.features.update(
                    **features_form.cleaned_data)

        # Call task outside of `commit_on_success` to avoid it running before
        # the transaction is committed and not finding the app.
        tasks.fetch_icon.delay(addon)

        return redirect('submit.app.details', addon.app_slug)

    return jingo.render(request, 'submit/manifest.html', {
        'step': 'manifest',
        'features_form': features_form,
        'form': form,
        'DEVICE_LOOKUP': DEVICE_LOOKUP
    })


@dev_required
@submit_step('details')
def details(request, addon_id, addon):
    # Name, Slug, Description, Privacy Policy, Homepage URL, Support URL,
    # Support Email.
    form_basic = AppDetailsBasicForm(request.POST or None, instance=addon,
                                     request=request)
    form_cats = CategoryForm(request.POST or None, product=addon,
                             request=request)
    form_icon = AppFormMedia(request.POST or None, request.FILES or None,
                             instance=addon, request=request)
    form_previews = PreviewFormSet(request.POST or None, prefix='files',
                                   queryset=addon.get_previews())

    # For empty webapp-locale (or no-locale) fields that have
    # form-locale values, duplicate them to satisfy the requirement.
    form_locale = request.COOKIES.get('current_locale', '')
    app_locale = to_language(addon.default_locale)
    for name, value in request.POST.items():
        if value:
            if name.endswith(form_locale):
                basename = name[:-len(form_locale)]
            else:
                basename = name + '_'
            othername = basename + app_locale
            if not request.POST.get(othername, None):
                request.POST[othername] = value
    forms = {
        'form_basic': form_basic,
        'form_cats': form_cats,
        'form_icon': form_icon,
        'form_previews': form_previews,
    }

    if request.POST and all(f.is_valid() for f in forms.itervalues()):
        addon = form_basic.save(addon)
        form_cats.save()
        form_icon.save(addon)
        for preview in form_previews.forms:
            preview.save(addon)

        # If this is an incomplete app from the legacy submission flow, it may
        # not have device types set yet - so assume it works everywhere.
        if not addon.device_types:
            for device in amo.DEVICE_TYPES:
                addon.addondevicetype_set.create(device_type=device)

        AppSubmissionChecklist.objects.get(addon=addon).update(details=True)

        # `make_public` if the developer doesn't want the app published
        # immediately upon review.
        make_public = (amo.PUBLIC_IMMEDIATELY
                       if form_basic.cleaned_data.get('publish')
                       else amo.PUBLIC_WAIT)

        if addon.premium_type == amo.ADDON_FREE:
            if waffle.switch_is_active('iarc'):
                # Free apps get STATUS_NULL until content ratings has been
                # entered.
                # TODO: set to STATUS_PENDING once app gets an IARC rating.
                addon.update(make_public=make_public)
            else:
                addon.update(status=amo.STATUS_PENDING,
                             make_public=make_public)
        else:
            # Paid apps get STATUS_NULL until payment information and content
            # ratings has been entered.
            addon.update(status=amo.STATUS_NULL, make_public=make_public,
                         highest_status=amo.STATUS_PENDING)

        record_action('app-submitted', request, {'app-id': addon.pk})

        return redirect('submit.app.done', addon.app_slug)

    ctx = {
        'step': 'details',
        'addon': addon,
    }
    ctx.update(forms)
    return jingo.render(request, 'submit/details.html', ctx)


@dev_required
def done(request, addon_id, addon):
    # No submit step forced on this page, we don't really care.
    if waffle.switch_is_active('iarc'):
        return jingo.render(request, 'submit/next_steps.html',
                            {'step': 'next_steps', 'addon': addon})
    else:
        return jingo.render(request, 'submit/done.html',
                            {'step': 'done', 'addon': addon})


@dev_required
def resume(request, addon_id, addon):
    try:
        # If it didn't go through the app submission
        # checklist. Don't die. This will be useful for
        # creating apps with an API later.
        step = addon.appsubmissionchecklist.get_next()
    except ObjectDoesNotExist:
        step = None

    return _resume(addon, step)


def _resume(addon, step):
    if step:
        if step in ['terms', 'manifest']:
            return redirect('submit.app.%s' % step)
        return redirect(reverse('submit.app.%s' % step,
                                args=[addon.app_slug]))

    return redirect(addon.get_dev_url('edit'))
