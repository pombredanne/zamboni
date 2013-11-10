from django import forms
from django.template import Context, loader
from django.utils.safestring import mark_safe

from tower import ugettext as _

import users.notifications as email


class NotificationsSelectMultiple(forms.CheckboxSelectMultiple):
    """Widget that formats the notification checkboxes."""

    def __init__(self, **kwargs):
        super(self.__class__, self).__init__(**kwargs)

    def render(self, name, value, attrs=None):
        str_values = [int(v) for v in value] or []
        final_attrs = self.build_attrs(attrs, name=name)
        groups = {}

        # Mark the mandatory fields.
        mandatory = [k for k, v in
                     email.ALL_NOTIFICATIONS_BY_ID.iteritems() if v.mandatory]
        str_values = set(mandatory + str_values)

        for idx, label in sorted(self.choices):
            notification = email.ALL_NOTIFICATIONS_BY_ID[idx]
            cb_attrs = dict(final_attrs, id='%s_%s' % (attrs['id'], idx))
            notes = []

            if notification.mandatory:
                cb_attrs = dict(cb_attrs, disabled=1)
                notes.append(u'<span title="required" class="req">*</span>')

            if (hasattr(self.form_instance, 'choices_status') and
                self.form_instance.choices_status.get(idx)):
                notes.append(u'<sup class="msg">%s</sup>' % _('new'))

            cb = forms.CheckboxInput(
                cb_attrs, check_test=lambda value: value in str_values)

            rendered_cb = cb.render(name, idx)
            label_for = u' for="%s"' % cb_attrs['id']

            groups.setdefault(notification.group, []).append(
                    u'<li><label class="check" %s>%s %s %s</label></li>' % (
                    label_for, rendered_cb, label, ''.join(notes)
                ))

        output = []
        template_url = 'users/edit_notification_checkboxes.html'
        for e, name in email.NOTIFICATION_GROUPS.items():
            if e in groups:
                context = {'title': name, 'options': groups[e]}
                output.append(loader.get_template(template_url).render(
                    Context(context)))

        return mark_safe(u'<ol class="complex">%s</ol>' % u'\n'.join(output))
