# -*- coding: utf-8 -*-
# Generated by Django 1.11 on 2019-01-02 18:20
from __future__ import unicode_literals

from django.conf import settings
import django.contrib.postgres.fields.jsonb
from django.db import migrations, models
import django.db.models.deletion
import django.utils.timezone


class Migration(migrations.Migration):

    dependencies = [
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
        ('seqr', '0048_auto_20181106_1639'),
    ]

    operations = [
        migrations.CreateModel(
            name='VariantSearch',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('guid', models.CharField(db_index=True, max_length=30, unique=True)),
                ('created_date', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('last_modified_date', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('name', models.CharField(max_length=200, null=True)),
                ('search', django.contrib.postgres.fields.jsonb.JSONField()),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
        ),
        migrations.CreateModel(
            name='VariantSearchResults',
            fields=[
                ('id', models.AutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('guid', models.CharField(db_index=True, max_length=30, unique=True)),
                ('created_date', models.DateTimeField(db_index=True, default=django.utils.timezone.now)),
                ('last_modified_date', models.DateTimeField(blank=True, db_index=True, null=True)),
                ('search_hash', models.CharField(max_length=50)),
                ('sort', models.CharField(max_length=50, null=True)),
                ('es_index', models.TextField(null=True)),
                ('results', django.contrib.postgres.fields.jsonb.JSONField(null=True)),
                ('total_results', models.IntegerField(null=True)),
                ('created_by', models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('variant_search', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='seqr.VariantSearch')),
            ],
        ),
        migrations.AddField(
            model_name='varianttag',
            name='variant_search',
            field=models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, to='seqr.VariantSearchResults'),
        ),
        migrations.AlterUniqueTogether(
            name='variantsearchresults',
            unique_together=set([('search_hash', 'sort')]),
        ),
        migrations.AlterUniqueTogether(
            name='variantsearch',
            unique_together=set([('created_by', 'name')]),
        ),
    ]