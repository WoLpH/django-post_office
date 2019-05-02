from __future__ import absolute_import, unicode_literals

from celery import shared_task
from django.core import management

from . import settings


@shared_task
def send_queued_mail(processes=settings.get_threads_per_process()):
    return management.call_command('send_queued_mail', processes=processes)


@shared_task
def cleanup_mail(days=90, delete_attachments=False):
    return management.call_command('cleanup_mail', days=days,
                                   delete_attachments=delete_attachments)
