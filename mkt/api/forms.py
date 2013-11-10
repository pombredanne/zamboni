import base64
import json
import StringIO

from django import forms

import happyforms
from tastypie.validation import CleanedDataFormValidation
from tower import ugettext_lazy as _lazy

import amo
from addons.models import Addon, AddonDeviceType, Category
from files.models import FileUpload
from mkt.developers.forms import NewPackagedAppForm
from mkt.developers.utils import check_upload
from mkt.submit.forms import mark_for_rereview


class DeviceTypeForm(forms.Form):
    device_types = forms.TypedMultipleChoiceField(
        choices=[(k, v.name) for k, v in amo.DEVICE_TYPES.items()],
        coerce=int,
        initial=amo.DEVICE_TYPES.keys(), widget=forms.CheckboxSelectMultiple)

    def __init__(self, *args, **kwargs):
        self.addon = kwargs.pop('addon')
        super(DeviceTypeForm, self).__init__(*args, **kwargs)
        device_types = AddonDeviceType.objects.filter(
            addon=self.addon).values_list('device_type', flat=True)
        if device_types:
            self.initial['device_types'] = device_types

    def save(self, addon):
        new_types = self.cleaned_data['device_types']
        old_types = [x.id for x in addon.device_types]

        added_devices = set(new_types) - set(old_types)
        removed_devices = set(old_types) - set(new_types)

        for d in added_devices:
            addon.addondevicetype_set.create(device_type=d)
        for d in removed_devices:
            addon.addondevicetype_set.filter(device_type=d).delete()

        # Send app to re-review queue if public and new devices are added.
        if added_devices and self.addon.status in amo.WEBAPPS_APPROVED_STATUSES:
            mark_for_rereview(self.addon, added_devices, removed_devices)


class UploadForm(happyforms.Form):
    is_packaged = False
    manifest = forms.CharField(max_length=32, min_length=32, required=False)
    upload = forms.CharField(max_length=32, min_length=32, required=False)

    def clean(self):
        uuid = self.cleaned_data.get('upload', '')
        if uuid:
            self.is_packaged = True
        else:
            uuid = self.cleaned_data.get('manifest', '')
        if not uuid:
            raise forms.ValidationError('No upload or manifest specified.')

        try:
            upload = FileUpload.objects.get(uuid=uuid)
        except FileUpload.DoesNotExist:
            raise forms.ValidationError('No upload found.')
        if not upload.valid:
            raise forms.ValidationError('Upload not valid.')

        self.obj = upload
        return uuid


class JSONField(forms.Field):
    def to_python(self, value):
        if value == '':
            return None

        try:
            if isinstance(value, basestring):
                return json.loads(value)
        except ValueError:
            pass
        return value


class SluggableModelChoiceField(forms.ModelChoiceField):
    """
    A model choice field that can accept either a slug or a pk and adapts
    itself based on that. Requries: `sluggable_to_field_name` to be set as
    the field that we will base the slug on.
    """

    def __init__(self, *args, **kw):
        if 'sluggable_to_field_name' not in kw:
            raise ValueError('sluggable_to_field_name is required.')
        self.sluggable_to_field_name = kw.pop('sluggable_to_field_name')
        return super(SluggableModelChoiceField, self).__init__(*args, **kw)

    def to_python(self, value):
        try:
            if not value.isdigit():
                self.to_field_name = self.sluggable_to_field_name
        except AttributeError:
            pass
        return super(SluggableModelChoiceField, self).to_python(value)


class RequestFormValidation(CleanedDataFormValidation):
    """
    A sub class of CleanedDataFormValidation that passes request through to
    the form.
    """
    def is_valid(self, bundle, request=None):
        data = bundle.data
        if data is None:
            data = {}

        form = self.form_class(data, request=request)

        if form.is_valid():
            bundle.data = form.cleaned_data
            return {}

        return form.errors


def parse(file_, require_name=False, require_type=None):
    try:
        if not set(['data', 'type']).issubset(set(file_.keys())):
            raise forms.ValidationError('Type and data are required.')
    except AttributeError:
        raise forms.ValidationError('File must be a dictionary.')
    try:
        data = base64.b64decode(file_['data'])
    except TypeError:
        raise forms.ValidationError('File must be base64 encoded.')

    result = StringIO.StringIO(data)
    result.size = len(data)

    if require_type and file_.get('type', '') != require_type:
        raise forms.ValidationError('Type must be %s.' % require_type)
    if require_name and not file_.get('name', ''):
        raise forms.ValidationError('Name not specified.')

    result.name = file_.get('name', '')
    return result


class NewPackagedForm(NewPackagedAppForm):
    upload = JSONField()

    def clean_upload(self):
        self.cleaned_data['upload'] = parse(self.cleaned_data
                                                .get('upload', {}),
                                            require_name=True,
                                            require_type='application/zip')
        return super(NewPackagedForm, self).clean_upload()


class PreviewArgsForm(happyforms.Form):
    app = forms.IntegerField(required=False)


class PreviewJSONForm(happyforms.Form):
    file = JSONField(required=True)
    position = forms.IntegerField(required=True)

    def clean_file(self):
        file_ = self.cleaned_data.get('file', {})
        file_obj = parse(file_)
        errors, hash_ = check_upload(file_obj, 'preview', file_['type'])
        if errors:
            raise forms.ValidationError(errors)

        self.hash_ = hash_
        return file_

    def clean(self):
        self.cleaned_data['upload_hash'] = getattr(self, 'hash_', None)
        return self.cleaned_data


class CategoryForm(happyforms.Form):
    # The CategoryFormSet is far too complicated, I don't follow it.
    # Hopefully this is easier.
    categories = forms.ModelMultipleChoiceField(
        queryset=Category.objects.filter(type=amo.ADDON_WEBAPP))


class StatusForm(happyforms.ModelForm):
    status = forms.ChoiceField(choices=(), required=False)

    lookup = {
        # You can push to the pending queue.
        amo.STATUS_NULL: amo.STATUS_PENDING,
        # You can push to public if you've been reviewed.
        amo.STATUS_PUBLIC_WAITING: amo.STATUS_PUBLIC,
    }

    class Meta:
        model = Addon
        fields = ['status', 'disabled_by_user']

    def __init__(self, *args, **kw):
        super(StatusForm, self).__init__(*args, **kw)
        choice = self.lookup.get(self.instance.status)
        choices = []
        if self.instance.status in self.lookup:
            choices.append(amo.STATUS_CHOICES_API[choice])
        choices.append(amo.STATUS_CHOICES_API[self.instance.status])
        self.fields['status'].choices = [(k, k) for k in sorted(choices)]

    def clean_status(self):
        requested = self.cleaned_data['status']
        if requested == amo.STATUS_CHOICES_API[amo.STATUS_PENDING]:
            valid, reasons = self.instance.is_complete()
            if not valid:
                raise forms.ValidationError(reasons)
        return amo.STATUS_CHOICES_API_LOOKUP[requested]


class CustomNullBooleanSelect(forms.Select):
    """A custom NullBooleanSelect, that uses true/false/'' values instead of
    1/2/3. See also https://code.djangoproject.com/ticket/17210."""

    def __init__(self, attrs=None):
        choices = ((u'', _lazy('Unknown')),
                   (u'true', _lazy('Yes')),
                   (u'false', _lazy('No')))
        super(CustomNullBooleanSelect, self).__init__(attrs, choices)

    def render(self, name, value, attrs=None, choices=()):
        try:
            value = {
                True: u'true',
                False: u'false',
                u'true': u'true',
                u'false': u'false'
            }[value]
        except KeyError:
            value = u''
        return super(CustomNullBooleanSelect, self).render(name, value, attrs,
                                                           choices)

    def value_from_datadict(self, data, files, name):
        value = data.get(name, None)
        return {
            u'true': True,
            True: True,
            'True': True,
            u'false': False,
            'False': False,
            False: False
        }.get(value, None)

    def _has_changed(self, initial, data):
        # For a CustomNullBooleanSelect, None (unknown) and False (No)
        # are *not* the same.
        if initial is not None:
            initial = bool(initial)
        if data is not None:
            data = bool(data)
        return initial != data
