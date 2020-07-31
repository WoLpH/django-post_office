# -*- coding: utf-8 -*-
from __future__ import unicode_literals
from django import forms, http
import re

from django import forms
from django.db import models
from django.contrib import admin
from django.conf import settings
from django.conf.urls import re_path
from django.core.exceptions import ValidationError
from django.core.mail.message import SafeMIMEText
from django.forms import BaseInlineFormSet
from django.forms.widgets import TextInput
from django.utils import six, html
from django.http.response import HttpResponse, HttpResponseNotFound
from django.template import Context, Template
from django.urls import reverse
from django.utils.html import format_html
from django.utils.text import Truncator
from django.utils.translation import gettext_lazy as _

from .fields import CommaSeparatedEmailField
from .models import Attachment, Log, Email, EmailTemplate, STATUS
from . import settings as post_office_settings
from .sanitizer import clean_html


def get_message_preview(instance):
    return ('{0}...'.format(instance.message[:25]) if len(instance.message) > 25
            else instance.message)

get_message_preview.short_description = 'Message'


class AttachmentInline(admin.StackedInline):
    model = Attachment.emails.through
    extra = 0

    def get_queryset(self, request):
        """
        Exclude inlined attachments from queryset, because they usually have meaningless names and
        are displayed anyway.
        """
        queryset = super().get_queryset(request)
        inlined_attachments = [
            a.id for a in queryset if a.attachment.headers.get('Content-Disposition').startswith('inline')
        ]
        return queryset.exclude(id__in=inlined_attachments)


class LogInline(admin.TabularInline):
    model = Log
    readonly_fields = fields = ['date', 'status', 'exception_type', 'message']
    can_delete = False

    def has_add_permission(self, request, obj=None):
        return False

    def has_change_permission(self, request, obj=None):
        return False


class CommaSeparatedEmailWidget(TextInput):

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs.update({'class': 'vTextField'})

    def format_value(self, value):
        # If the value is a string wrap it in a list so it does not get sliced.
        if not value:
            return ''
        if isinstance(value, str):
            value = [value, ]
        return ','.join([item for item in value])


def requeue(modeladmin, request, queryset):
    """An admin action to requeue emails."""
    queryset.update(status=STATUS.queued)

requeue.short_description = 'Requeue selected emails'


@admin.register(Email)
class EmailAdmin(admin.ModelAdmin):
    list_display = ['id', 'to_display', 'shortened_subject', 'status', 'last_updated', 'scheduled_time', 'use_template']
    search_fields = ['to', 'subject']
    readonly_fields = ['render_subject', 'render_plaintext_body',  'render_html_body']
    date_hierarchy = 'last_updated'
    inlines = [AttachmentInline, LogInline]
    list_filter = ['status', 'template__language', 'template__name']
    formfield_overrides = {
        CommaSeparatedEmailField: {'widget': CommaSeparatedEmailWidget}
    }
    actions = [requeue]
    save_as = True

    def get_urls(self):
        urls = [
            re_path(r'^(?P<pk>\d+)/image/(?P<content_id>[0-9a-f]{32})$', self.fetch_email_image, name='post_office_email_image'),
        ]
        urls.extend(super().get_urls())
        return urls

    def get_queryset(self, request):
        return super().get_queryset(request).select_related('template')

    def to_display(self, instance):
        return ', '.join(instance.to)

    to_display.short_description = _("To")
    to_display.admin_order_field = 'to'

    def has_add_permission(self, request):
        return False

    def shortened_subject(self, instance):
        if instance.template:
            template_cache_key = '_subject_template_' + str(instance.template_id)
            template = getattr(self, template_cache_key, None)
            if template is None:
                # cache compiled template to speed up rendering of list view
                template = Template(instance.template.subject)
                setattr(self, template_cache_key, template)
            subject = template.render(Context(instance.context))
        else:
            subject = instance.subject
        return Truncator(subject).chars(100)

    shortened_subject.short_description = _("Subject")
    shortened_subject.admin_order_field = 'subject'

    def use_template(self, instance):
        return bool(instance.template_id)

    use_template.short_description = _("Use Template")
    use_template.boolean = True

    def get_fieldsets(self, request, obj=None):
        fieldsets = [
            (None, {
                'fields': ['from_email', 'to', 'cc', 'bcc',
                           'priority', ('status', 'scheduled_time')],
            }),
        ]
        has_plaintext_content, has_html_content = False, False
        for part in obj.email_message().message().walk():
            if not isinstance(part, SafeMIMEText):
                continue
            content_type = part.get_content_type()
            if content_type == 'text/plain':
                has_plaintext_content = True
            elif content_type == 'text/html':
                has_html_content = True

        if has_html_content:
            fieldsets.append(
                (_("HTML Email"), {'fields': ['render_subject', 'render_html_body']})
            )
            if has_plaintext_content:
                fieldsets.append(
                    (_("Text Email"), {'classes': ['collapse'], 'fields': ['render_plaintext_body']})
                )
        elif has_plaintext_content:
            fieldsets.append(
                (_("Text Email"), {'fields': ['render_subject', 'render_plaintext_body']})
             )

        return fieldsets

    def render_subject(self, instance):
        message = instance.email_message()
        return message.subject

    render_subject.short_description = _("Subject")

    def render_plaintext_body(self, instance):
        for message in instance.email_message().message().walk():
            if isinstance(message, SafeMIMEText) and message.get_content_type() == 'text/plain':
                return format_html('<pre>{}</pre>', message.get_payload())

    render_plaintext_body.short_description = _("Mail Body")

    def render_html_body(self, instance):
        pattern = re.compile('cid:([0-9a-f]{32})')
        url = reverse('admin:post_office_email_image', kwargs={'pk': instance.id, 'content_id': 32 * '0'})
        url = url.replace(32 * '0', r'\1')
        for message in instance.email_message().message().walk():
            if isinstance(message, SafeMIMEText) and message.get_content_type() == 'text/html':
                payload = message.get_payload(decode=True).decode('utf-8')
                return clean_html(pattern.sub(url, payload))

    render_html_body.short_description = _("HTML Body")

    def fetch_email_image(self, request, pk, content_id):
        instance = self.get_object(request, pk)
        for message in instance.email_message().message().walk():
            if message.get_content_maintype() == 'image' and message.get('Content-Id')[1:33] == content_id:
                return HttpResponse(message.get_payload(decode=True), content_type=message.get_content_type())
        return HttpResponseNotFound()


@admin.register(Log)
class LogAdmin(admin.ModelAdmin):
    list_display = ('date', 'email', 'status', get_message_preview)


class SubjectField(TextInput):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.attrs.update({'style': 'width: 610px;'})


class EmailTemplateAdminFormSet(BaseInlineFormSet):
    def clean(self):
        """
        Check that no two Email templates have the same default_template and language.
        """
        super().clean()
        data = set()
        for form in self.forms:
            default_template = form.cleaned_data['default_template']
            language = form.cleaned_data['language']
            if (default_template.id, language) in data:
                msg = _("Duplicate template for language '{language}'.")
                language = dict(form.fields['language'].choices)[language]
                raise ValidationError(msg.format(language=language))
            data.add((default_template.id, language))


class EmailTemplateAdminForm(forms.ModelForm):
    language = forms.ChoiceField(
        choices=settings.LANGUAGES,
        required=False,
        label=_("Language"),
        help_text=_("Render template in alternative language"),
    )

    class Meta:
        model = EmailTemplate
        fields = ['name', 'description', 'subject', 'content', 'html_content', 'language',
                  'default_template']

    def __init__(self, *args, **kwargs):
        instance = kwargs.get('instance')
        super().__init__(*args, **kwargs)
        if instance and instance.language:
            self.fields['language'].disabled = True


class EmailTemplateInline(admin.StackedInline):
    form = EmailTemplateAdminForm
    formset = EmailTemplateAdminFormSet
    model = EmailTemplate
    extra = 0
    fields = ('language', 'subject', 'content', 'html_content',
              'rendered_content', 'rendered_html_content',)
    readonly_fields = 'rendered_content', 'rendered_html_content'
    formfield_overrides = {
        models.CharField: {'widget': SubjectField}
    }

    def rendered_content(self, instance):
        if instance.content:
            height = instance.content.count('\n') * 25
            return html.mark_safe(
                '<iframe '
                'style="width: 80%; height: {}px;"'
                ' src="?preview=text&language={}">'
                '</iframe>'.format(height, instance.language))
        else:
            return ''

    def rendered_html_content(self, instance):
        if instance.html_content:
            return html.mark_safe('<iframe '
                                  'style="width: 80%; height: 800px;" '
                                  'src="?preview=html&language={}"></iframe>'
                                  .format(instance.language))
        else:
            return ''

    def get_max_num(self, request, obj=None, **kwargs):
        return len(settings.LANGUAGES)


@admin.register(EmailTemplate)
class EmailTemplateAdmin(admin.ModelAdmin):
    form = EmailTemplateAdminForm
    list_display = ('name', 'description_shortened', 'subject', 'languages_compact', 'created')
    search_fields = ('name', 'description', 'subject')
    readonly_fields = 'rendered_content', 'rendered_html_content'
    fieldsets = [
        (None, {
            'fields': ('name', 'description'),
        }),
        (_("Default Content"), {
            'fields': ('subject', 'content', 'html_content'),
        }),
        (_("Preview"), {
            'fields': ('example_context', 'rendered_content',
                       'rendered_html_content'),
        }),
    ]
    inlines = (EmailTemplateInline,) if settings.USE_I18N else ()
    formfield_overrides = {
        models.CharField: {'widget': SubjectField}
    }
    change_form_template = 'admin/post_office/EmailTemplate/change_form.html'

    def change_view(self, request, object_id, form_url='', extra_context=None):
        if request.GET.get('preview'):
            instance = self.model.objects.get(id=object_id)
            engine = post_office_settings.get_template_engine()

            if request.GET.get('language'):
                template_instance = instance.translated_templates.filter(
                    language=request.GET.get('language'),
                ).first()
            else:
                template_instance = instance

            if request.GET.get('preview') == 'html':
                template = engine.from_string(
                    template_instance.html_content
                    .replace('inline_image', 'static')
                    .replace(' post_office ', ' static '))
            else:
                template = engine.from_string(
                    '<pre>%s</pre>' % template_instance.content)

            return http.HttpResponse(template.render(instance.example_context))

        return super(EmailTemplateAdmin, self).change_view(
            request, object_id, form_url=form_url, extra_context=extra_context)

    def rendered_content(self, instance):
        if instance.content:
            height = instance.content.count('\n') * 25
            return html.mark_safe(
                '<iframe '
                'style="width: 80%; height: {}px;"'
                ' src="?preview=text">'
                '</iframe>'.format(height))
        else:
            return ''

    def rendered_html_content(self, instance):
        if instance.html_content:
            return html.mark_safe('<iframe '
                                  'style="width: 80%; height: 800px;" '
                                  'src="?preview=html"></iframe>')
        else:
            return ''

    def get_queryset(self, request):
        return self.model.objects.filter(default_template__isnull=True)

    def description_shortened(self, instance):
        return Truncator(instance.description.split('\n')[0]).chars(200)
    description_shortened.short_description = _("Description")
    description_shortened.admin_order_field = 'description'

    def languages_compact(self, instance):
        languages = [tt.language for tt in instance.translated_templates.order_by('language')]
        return ', '.join(languages)
    languages_compact.short_description = _("Languages")

    def save_model(self, request, obj, form, change):
        obj.save()

        # if the name got changed, also change the translated templates to match again
        if 'name' in form.changed_data:
            obj.translated_templates.update(name=obj.name)


@admin.register(Attachment)
class AttachmentAdmin(admin.ModelAdmin):
    list_display = ['name', 'file']
    filter_horizontal = ['emails']
