# Generated by Django 2.2 on 2019-05-01 23:10

import django.contrib.postgres.fields.jsonb
from django.db import migrations


class Migration(migrations.Migration):

    dependencies = [
        ('post_office', '0010_auto_20190502_0108'),
    ]

    operations = [
        migrations.AlterField(
            model_name='email',
            name='context',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True, verbose_name='Context'),
        ),
        migrations.AlterField(
            model_name='emailtemplate',
            name='example_context',
            field=django.contrib.postgres.fields.jsonb.JSONField(blank=True, null=True, verbose_name='Context'),
        ),
    ]
