# -*- coding: utf-8 -*-
# Generated by Django 1.11 on 2019-04-04 19:53
from __future__ import unicode_literals

import django.contrib.postgres.fields
from django.db import migrations, models
import django.db.models.deletion


class Migration(migrations.Migration):

    dependencies = [
        ('reference_data', '0012_auto_20190321_1656'),
    ]

    operations = [
        migrations.AddField(
            model_name='geneexpression',
            name='tissue_type',
            field=models.CharField(choices=[(b'heart', b'heart'), (b'nerve', b'nerve'), (b'brain', b'brain'), (b'lung', b'lung'), (b'skin', b'skin'), (b'esophagus', b'esophagus'), (b'kidney', b'kidney'), (b'thyroid', b'thyroid'), (b'testis', b'testis'), (b'adipose_tissue', b'adipose_tissue'), (b'blood_vessel', b'blood_vessel'), (b'vagina', b'vagina'), (b'cells_-_ebv-transformed_lymphocytes', b'cells_-_ebv-transformed_lymphocytes'), (b'stomach', b'stomach'), (b'liver', b'liver'), (b'whole_blood', b'whole_blood'), (b'prostate', b'prostate'), (b'ovary', b'ovary'), (b'pancreas', b'pancreas'), (b'cells_-_transformed_fibroblasts', b'cells_-_transformed_fibroblasts'), (b'adrenal_gland', b'adrenal_gland'), (b'salivary_gland', b'salivary_gland'), (b'spleen', b'spleen'), (b'small_intestine', b'small_intestine'), (b'uterus', b'uterus'), (b'colon', b'colon'), (b'pituitary', b'pituitary'), (b'muscle', b'muscle'), (b'breast', b'breast')], default='a', max_length=40),
            preserve_default=False,
        ),
        migrations.RemoveField(
            model_name='geneexpression',
            name='expression_values',
        ),
        migrations.AddField(
            model_name='geneexpression',
            name='expression_values',
            field=django.contrib.postgres.fields.ArrayField(base_field=models.FloatField(), size=None),
        ),
        migrations.AlterField(
            model_name='geneexpression',
            name='gene',
            field=models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, to='reference_data.GeneInfo'),
        ),
        migrations.AlterUniqueTogether(
            name='geneexpression',
            unique_together=set([('gene', 'tissue_type')]),
        ),
    ]
