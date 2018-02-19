import copy
import json
import logging
import sys

import pymongo

from xbrowse.core.constants import GENOME_VERSION_GRCh37, GENOME_VERSION_GRCh38
from xbrowse.core.genomeloc import get_chr_pos
import settings

from xbrowse import genomeloc
from xbrowse.core.variant_filters import VariantFilter, passes_variant_filter
from xbrowse import Variant

import datastore
from pprint import pprint, pformat

import StringIO
import elasticsearch
import elasticsearch_dsl
from elasticsearch_dsl import Q

logger = logging.getLogger()

# make encoded values as human-readable as possible
ES_FIELD_NAME_ESCAPE_CHAR = '$'
ES_FIELD_NAME_BAD_LEADING_CHARS = set(['_', '-', '+', ES_FIELD_NAME_ESCAPE_CHAR])
ES_FIELD_NAME_SPECIAL_CHAR_MAP = {
    '.': '_$dot$_',
    ',': '_$comma$_',
    '#': '_$hash$_',
    '*': '_$star$_',
    '(': '_$lp$_',
    ')': '_$rp$_',
    '[': '_$lsb$_',
    ']': '_$rsb$_',
    '{': '_$lcb$_',
    '}': '_$rcb$_',
}


def _encode_field_name(s):
    """Converts the given string into a valid elasticsearch index field name by encoding any special
    chars.

    See:
    https://discuss.elastic.co/t/special-characters-in-field-names/10658/2
    https://discuss.elastic.co/t/illegal-characters-in-elasticsearch-field-names/17196/2
    """
    field_name = StringIO.StringIO()
    for i, c in enumerate(s):
        if c == ES_FIELD_NAME_ESCAPE_CHAR:
            field_name.write(2*ES_FIELD_NAME_ESCAPE_CHAR)
        elif c in ES_FIELD_NAME_SPECIAL_CHAR_MAP:
            field_name.write(ES_FIELD_NAME_SPECIAL_CHAR_MAP[c])  # encode the char
        else:
            field_name.write(c)  # write out the char as is

    field_name = field_name.getvalue()

    # escape 1st char if necessary
    if any(field_name.startswith(c) for c in ES_FIELD_NAME_BAD_LEADING_CHARS):
        return ES_FIELD_NAME_ESCAPE_CHAR + field_name
    else:
        return field_name


def _decode_field_name(field_name):
    """Converts an elasticsearch field name back to the original string"""

    if field_name.startswith(ES_FIELD_NAME_ESCAPE_CHAR):
        field_name = field_name[1:]

    i = 0
    original_string = StringIO.StringIO()
    while i < len(field_name):
        current_string = field_name[i:]
        if current_string.startswith(2*ES_FIELD_NAME_ESCAPE_CHAR):
            original_string.write(ES_FIELD_NAME_ESCAPE_CHAR)
            i += 2
        else:
            for original_value, encoded_value in ES_FIELD_NAME_SPECIAL_CHAR_MAP.items():
                if current_string.startswith(encoded_value):
                    original_string.write(original_value)
                    i += len(encoded_value)
                    break
            else:
                original_string.write(field_name[i])
                i += 1

    return original_string.getvalue()


GENOTYPE_QUERY_MAP = {

    'ref_ref': 0,
    'ref_alt': 1,
    'alt_alt': 2,

    'has_alt': {'$gte': 1},
    'has_ref': {'$in': [0,1]},

    'not_missing': {'$gte': 0},
    'missing': -1,
}


def _add_genotype_filter_to_variant_query(db_query, genotype_filter):
    """
    Add conditions to db_query from the genotype filter
    Edits in place, returns True if successful
    """
    for indiv_id, genotype in genotype_filter.items():
        key = 'genotypes.%s.num_alt' % indiv_id
        db_query[key] = GENOTYPE_QUERY_MAP[genotype]
    return True


def _add_index_fields_to_variant(variant_dict, annotation=None):
    """
    Add fields to the vairant dictionary that you want to index on before load it
    """
    if annotation:
        variant_dict['db_freqs'] = annotation['freqs']
        variant_dict['db_tags'] = annotation['annotation_tags']
        variant_dict['db_gene_ids'] = annotation['gene_ids']


def add_disease_genes_to_variants(gene_list_map, variants):
    """
    Take a list of variants and annotate them with disease genes
    """
    error_counter = 0
    by_gene = gene_list_map
    for variant in variants:
        gene_lists = []
        try:
            for gene_id in variant.coding_gene_ids:
                for g in by_gene[gene_id]:
                    gene_lists.append(g.name)
            variant.set_extra('disease_genes', gene_lists)
        except Exception as e:
            exc_type, exc_obj, exc_tb = sys.exc_info()
            error_counter += 1
            if error_counter > 10:
                break


class ElasticsearchDatastore(datastore.Datastore):

    def __init__(self, annotator):
        self.liftover_grch38_to_grch37 = None
        self.liftover_grch37_to_grch38 = None

        self._annotator = annotator

        self._es_client = elasticsearch.Elasticsearch(host=settings.ELASTICSEARCH_SERVICE_HOSTNAME)

    def get_elasticsearch_variants(self, query_json, elasticsearch_dataset, project_id, family_id=None, variant_id_filter=None):

        try:
            from pyliftover import LiftOver
            if self.liftover_grch38_to_grch37 is None or self.liftover_grch37_to_grch38 is None:
                self.liftover_grch38_to_grch37 = LiftOver('hg38', 'hg19')
                self.liftover_grch37_to_grch38 = LiftOver('hg19', 'hg38')
        except Exception as e:
            logger.info("WARNING: Unable to set up liftover. Is there a working internet connection? " + str(e))


        elasticsearch_index = elasticsearch_dataset.dataset_id

        s = elasticsearch_dsl.Search(using=self._es_client, index=str(elasticsearch_index)+"*") #",".join(indices))

        print("===> QUERY: ")
        pprint(query_json)

        if variant_id_filter is not None:
            s = s.filter('term', **{"variantId": variant_id_filter})

        # parse variant query
        for key, value in query_json.items():
            if key == 'db_tags':
                vep_consequences = query_json.get('db_tags', {}).get('$in', [])

                consequences_filter = Q("terms", transcriptConsequenceTerms=vep_consequences)
                if 'intergenic_variant' in vep_consequences:
                    # for many intergenic variants VEP doesn't add any annotations, so if user selected 'intergenic_variant', also match variants where transcriptConsequenceTerms is emtpy
                    consequences_filter = consequences_filter | ~Q('exists', field='transcriptConsequenceTerms')

                s = s.filter(consequences_filter)
                print("==> transcriptConsequenceTerms: %s" % str(vep_consequences))

            if key.startswith("genotypes"):
                sample_id = ".".join(key.split(".")[1:-1])
                encoded_sample_id = _encode_field_name(sample_id)
                genotype_filter = value
                print("==> genotype filter: " + str(genotype_filter))
                if type(genotype_filter) == int or type(genotype_filter) == basestring:
                    print("==> genotypes: %s" % str({encoded_sample_id+"_num_alt": genotype_filter}))
                    s = s.filter('term', **{encoded_sample_id+"_num_alt": genotype_filter})

                elif '$gte' in genotype_filter:
                    genotype_filter = {k.replace("$", ""): v for k, v in genotype_filter.items()}
                    s = s.filter('range', **{encoded_sample_id+"_num_alt": genotype_filter})
                    print("==> genotypes: %s" % str({encoded_sample_id+"_num_alt": genotype_filter}))
                elif "$in" in genotype_filter:
                    num_alt_values = genotype_filter['$in']
                    q = Q('term', **{encoded_sample_id+"_num_alt": num_alt_values[0]})
                    print("==> genotypes: %s" % str({encoded_sample_id+"_num_alt": num_alt_values[0]}))
                    for num_alt_value in num_alt_values[1:]:
                        q = q | Q('term', **{encoded_sample_id+"_num_alt": num_alt_value})
                        print("==> genotypes: %s" % str({encoded_sample_id+"_num_alt": num_alt_value}))
                    s = s.filter(q)

            if key == "db_gene_ids":
                db_gene_ids = query_json.get('db_gene_ids', {})

                exclude_genes = db_gene_ids.get('$nin', [])
                gene_ids = exclude_genes or db_gene_ids.get('$in', [])

                if exclude_genes:
                    s = s.exclude("terms", geneIds=gene_ids)
                else:
                    s = s.filter("terms",  geneIds=gene_ids)
                print("==> %s %s" % ("exclude" if exclude_genes else "include", "geneIds: " + str(gene_ids)))

            if key == "$or" and type(value) == list:
                xpos_filters = value[0].get("$and", {})

                # for example: $or : [{'$and': [{'xpos': {'$gte': 12345}}, {'xpos': {'$lte': 54321}}]}]
                xpos_filters_dict = {}
                for xpos_filter in xpos_filters:
                    xpos_filter_setting = xpos_filter["xpos"]  # for example {'$gte': 12345} or {'$lte': 54321}
                    xpos_filters_dict.update(xpos_filter_setting)
                xpos_filter_setting = {k.replace("$", ""): v for k, v in xpos_filters_dict.items()}
                s = s.filter('range', **{"xpos": xpos_filter_setting})
                print("==> xpos range: " + str({"xpos": xpos_filter_setting}))


            af_key_map = {
                "db_freqs.1kg_wgs_phase3": "g1k_AF",
                "db_freqs.1kg_wgs_phase3_popmax": "g1k_POPMAX_AF",
                "db_freqs.exac_v3": "exac_AF",
                "db_freqs.exac_v3_popmax": "exac_AF_POPMAX",
                "db_freqs.topmed": "topmed_AF",
                "db_freqs.gnomad_exomes": "gnomad_exomes_AF",
                "db_freqs.gnomad_exomes_popmax": "gnomad_exomes_AF_POPMAX",
                "db_freqs.gnomad_genomes": "gnomad_genomes_AF",
                "db_freqs.gnomad_genomes_popmax": "gnomad_genomes_AF_POPMAX",
                "db_freqs.gnomad-exomes2": "gnomad_exomes_AF",
                "db_freqs.gnomad-exomes2_popmax": "gnomad_exomes_AF_POPMAX",
                "db_freqs.gnomad-genomes2": "gnomad_genomes_AF",
                "db_freqs.gnomad-genomes2_popmax": "gnomad_genomes_AF_POPMAX",
            }

            if key in af_key_map:
                filter_key = af_key_map[key]
                af_filter_setting = {k.replace("$", ""): v for k, v in value.items()}
                s = s.filter(Q('range', **{filter_key: af_filter_setting}) | ~Q('exists', field=filter_key))
                print("==> %s: %s" % (filter_key, af_filter_setting))

            s.sort("xpos")

        print("=====")
        print("FULL QUERY OBJ: " + pformat(s.__dict__))
        print("FILTERS: " + pformat(s.to_dict()))
        print("=====")
        print("Hits: ")
        # https://elasticsearch-py.readthedocs.io/en/master/helpers.html#elasticsearch.helpers.scan

        response = s.execute()
        print("TOTAL: " + str(response.hits.total))
        #print(pformat(response.to_dict()))
        from xbrowse_server.base.models import Project, Family, Individual, VariantNote, VariantTag
        from xbrowse_server.mall import get_reference
        from xbrowse_server.api.utils import add_gene_databases_to_variants


        if family_id is not None:
            family_individual_ids = [i.individual_id for i in Individual.objects.filter(family__family_id=family_id)]
        else:
            family_individual_ids = [i.individual_id for i in Individual.objects.filter(family__project__project_id=project_id)]

        project = Project.objects.get(project_id=project_id)
        gene_list_map = project.get_gene_list_map()

        reference = get_reference()

        for i, hit in enumerate(s.scan()):  # preserve_order=True
            filters = ",".join(hit["filters"]) if "filters" in hit else ""
            genotypes = {}
            all_num_alt = []
            for individual_id in family_individual_ids:
                encoded_individual_id = _encode_field_name(individual_id)
                num_alt =  int(hit["%s_num_alt" % encoded_individual_id]) if ("%s_num_alt" % encoded_individual_id) in hit else -1
                if num_alt is not None:
                    all_num_alt.append(num_alt)

                alleles = []
                if num_alt == 0:
                    alleles = [hit["ref"], hit["ref"]]
                elif num_alt == 1:
                    alleles = [hit["ref"], hit["alt"]]
                elif num_alt == 2:
                    alleles = [hit["alt"], hit["alt"]]
                elif num_alt == -1 or num_alt == None:
                    alleles = []
                else:
                    raise ValueError("Invalid num_alt: " + str(num_alt))

                genotypes[individual_id] = {
                    'ab': hit["%s_ab" % encoded_individual_id] if ("%s_ab" % encoded_individual_id) in hit else '',
                    'alleles': map(str, alleles),
                    'extras': {
                        'ad': hit["%s_ab" % encoded_individual_id]  if ("%s_ad" % encoded_individual_id) in hit else '',
                        'dp': hit["%s_dp" % encoded_individual_id]  if ("%s_dp" % encoded_individual_id) in hit else '',
                        'pl': '',
                    },
                    'filter': filters or "pass",
                    'gq': hit["%s_gq" % encoded_individual_id] if ("%s_gq" % encoded_individual_id in hit and hit["%s_gq" % encoded_individual_id] is not None) else '',
                    'num_alt': num_alt,
                }

            if all([num_alt <= 0 for num_alt in all_num_alt]):
                #print("Filtered out due to genotype: " + str(genotypes))
                #print("Filtered all_num_alt <= 0 - Result %s: GRCh38: %s:%s,  cadd: %s  %s - %s" % (i, hit["contig"], hit["start"], hit["cadd_PHRED"] if "cadd_PHRED" in hit else "", hit["transcriptConsequenceTerms"], all_num_alt))
                continue

            vep_annotation = json.loads(str(hit['sortedTranscriptConsequences']))

            if elasticsearch_dataset.genome_version == GENOME_VERSION_GRCh37:
                grch38_coord = None
                if self.liftover_grch37_to_grch38:
                    grch38_coord = self.liftover_grch37_to_grch38.convert_coordinate("chr%s" % hit["contig"].replace("chr", ""), int(hit["start"]))
                    if grch38_coord and grch38_coord[0]:
                        grch38_coord = "%s-%s-%s-%s "% (grch38_coord[0][0], grch38_coord[0][1], hit["ref"], hit["alt"])
                    else:
                        grch38_coord = ""
            else:
                grch38_coord = hit["variantId"]

            if elasticsearch_dataset.genome_version == GENOME_VERSION_GRCh38:
                grch37_coord = None
                if self.liftover_grch38_to_grch37:
                    grch37_coord = self.liftover_grch38_to_grch37.convert_coordinate("chr%s" % hit["contig"].replace("chr", ""), int(hit["start"]))
                    if grch37_coord and grch37_coord[0]:
                        grch37_coord = "%s-%s-%s-%s "% (grch37_coord[0][0], grch37_coord[0][1], hit["ref"], hit["alt"])
                    else:
                        grch37_coord = ""
            else:
                grch37_coord = hit["variantId"]

            result = {
                #u'_id': ObjectId('596d2207ff66f729285ca588'),
                'alt': str(hit["alt"]) if "alt" in hit else None,
                'annotation': {
                    'fathmm': None,
                    'metasvm': None,
                    'muttaster': None,
                    'polyphen': None,
                    'sift': None,

                    'cadd_phred': hit["cadd_PHRED"] if "cadd_PHRED" in hit else None,
                    'dann_score': hit["dbnsfp_DANN_score"] if "dbnsfp_DANN_score" in hit else None,
                    'revel_score': hit["dbnsfp_REVEL_score"] if "dbnsfp_REVEL_score" in hit else None,
                    'mpc_score': hit["mpc_MPC"] if "mpc_MPC" in hit else None,

                    'annotation_tags': list(hit["transcriptConsequenceTerms"] or []) if "transcriptConsequenceTerms" in hit else None,
                    'coding_gene_ids': list(hit['codingGeneIds'] or []),
                    'gene_ids': list(hit['geneIds'] or []),
                    'vep_annotation': vep_annotation,
                    'vep_group': str(hit['mainTranscript_major_consequence'] or ""),
                    'vep_consequence': str(hit['mainTranscript_major_consequence'] or ""),
                    'worst_vep_annotation_index': 0,
                    'worst_vep_index_per_gene': {str(hit['mainTranscript_gene_id']): 0},
                },
                'chr': hit["contig"],
                'coding_gene_ids': list(hit['codingGeneIds'] or []),
                'gene_ids': list(hit['geneIds'] or []),
                'db_freqs': {
                    '1kg_wgs_AF': float(hit["g1k_AF"] or 0.0),
                    '1kg_wgs_popmax_AF': float(hit["g1k_POPMAX_AF"] or 0.0),
                    'exac_v3_AC': float(hit["exac_AC_Adj"] or 0.0) if "exac_AC_Adj" in hit else 0.0,
                    'exac_v3_AF': float(hit["exac_AF"] or 0.0) if "exac_AF" in hit else (hit["exac_AC_Adj"]/float(hit["exac_AN_Adj"]) if int(hit["exac_AN_Adj"] or 0) > 0 else 0.0),
                    'exac_v3_popmax_AF': float(hit["exac_AF_POPMAX"] or 0.0) if "exac_AF_POPMAX" in hit else 0.0,

                    'topmed_AF': float(hit["topmed_AF"] or 0.0) if "topmed_AF" in hit else 0.0,

                    'gnomad_exomes_AC': float(hit["gnomad_exomes_AC"] or 0.0) if "gnomad_exomes_AC" in hit else 0.0,
                    'gnomad_exomes_Hom': float(hit["gnomad_exomes_HOM"] or 0.0) if "gnomad_exomes_HOM" in hit else 0.0,
                    'gnomad_exomes_AF': float(hit["gnomad_exomes_AF"] or 0.0) if "gnomad_exomes_AF" in hit else 0.0,
                    'gnomad_exomes_popmax_AF': float(hit["gnomad_exomes_AF_POPMAX"] or 0.0) if "gnomad_exomes_AF_POPMAX" in hit else 0.0,
                    'gnomad_genomes_AC': float(hit["gnomad_genomes_AC"] or 0.0) if "gnomad_genomes_AC" in hit else 0.0,
                    'gnomad_genomes_Hom': float(hit["gnomad_genomes_HOM"] or 0.0) if "gnomad_genomes_HOM" in hit else 0.0,
                    'gnomad_genomes_AF': float(hit["gnomad_genomes_AF"] or 0.0) if "gnomad_genomes_AF" in hit else 0.0,
                    'gnomad_genomes_popmax_AF': float(hit["gnomad_genomes_AF_POPMAX"] or 0.0) if "gnomad_genomes_AF_POPMAX" in hit else 0.0,
                    'gnomad_exome_coverage': float(hit["gnomad_exome_coverage"] or -1) if "gnomad_exome_coverage" in hit else -1,
                    'gnomad_genome_coverage': float(hit["gnomad_genome_coverage"] or -1) if "gnomad_genome_coverage" in hit else -1,
                },
                'db_gene_ids': list(hit["geneIds"] or []),
                'db_tags': str(hit["transcriptConsequenceTerms"] or "") if "transcriptConsequenceTerms" in hit else None,
                'extras': {
                    'genome_version': elasticsearch_dataset.genome_version,
                    'grch37_coords': grch37_coord,
                    'grch38_coords': grch38_coord,
                    'alt_allele_pos': 0,
                    'orig_alt_alleles': map(str, [a.split("-")[-1] for a in hit["originalAltAlleles"]]) if "originalAltAlleles" in hit else []},
                'genotypes': genotypes,
                'pos': long(hit['start']),
                'pos_end': str(hit['end']),
                'ref': str(hit['ref']),
                'vartype': 'snp' if len(hit['ref']) == len(hit['alt']) else "indel",
                'vcf_id': None,
                'xpos': long(hit["xpos"]),
                'xposx': long(hit["xpos"]),
            }
            result["annotation"]["freqs"] = result["db_freqs"]

            #print("\n\nConverted result: " + str(i))
            print("Result %s: GRCh37: %s GRCh38: %s:,  cadd: %s  %s - gene ids: %s, coding gene_ids: %s" % (i, grch37_coord, grch37_coord, hit["cadd_PHRED"] if "cadd_PHRED" in hit else "", hit["transcriptConsequenceTerms"], result["gene_ids"], result["coding_gene_ids"]))
            #pprint(result["db_freqs"])

            variant = Variant.fromJSON(result)

            # add gene info
            gene_names = {}
            if vep_annotation is not None:
                gene_names = {vep_anno["gene_id"]: vep_anno.get("gene_symbol") for vep_anno in vep_annotation if vep_anno.get("gene_symbol")}
            variant.set_extra('gene_names', gene_names)

            try:
                genes = {}
                for gene_id in variant.coding_gene_ids:
                    if gene_id:
                        genes[gene_id] = reference.get_gene_summary(gene_id)

                if not genes:
                    for gene_id in variant.gene_ids:
                        if gene_id:
                            genes[gene_id] = reference.get_gene_summary(gene_id)

                #if not genes:
                #    genes =  {vep_anno["gene_id"]: {"symbol": vep_anno["gene_symbol"]} for vep_anno in vep_annotation}

                variant.set_extra('genes', genes)
            except Exception as e:
                exc_type, exc_obj, exc_tb = sys.exc_info()
                print("WARNING: got unexpected error in add_gene_names_to_variants: %s : line %s" % (e, exc_tb.tb_lineno))


            add_disease_genes_to_variants(gene_list_map, [variant])
            add_gene_databases_to_variants([variant])
            #add_gene_info_to_variants([variant])
            #add_notes_to_variants_family(family, [variant])
            if family_id:
                family = Family.objects.get(project__project_id=project_id, family_id=family_id)
                try:
                    notes = list(VariantNote.objects.filter(family=family, xpos=variant.xpos, ref=variant.ref, alt=variant.alt).order_by('-date_saved'))
                    variant.set_extra('family_notes', [n.toJSON() for n in notes])
                    tags = list(VariantTag.objects.filter(family=family, xpos=variant.xpos, ref=variant.ref, alt=variant.alt))
                    variant.set_extra('family_tags', [t.toJSON() for t in tags])
                except Exception, e:
                    print("WARNING: got unexpected error in add_notes_to_variants_family for family %s %s" % (family, e))

            yield variant

            if i > settings.VARIANT_QUERY_RESULTS_LIMIT:
                break


    def get_variants(self, project_id, family_id, genotype_filter=None, variant_filter=None):
        db_query = self._make_db_query(genotype_filter, variant_filter)
        for i, variant in enumerate(self.get_elasticsearch_variants(db_query, elasticsearch_dataset, project_id, family_id)):
            yield variant


    def get_variants_in_gene(self, project_id, family_id, gene_id, genotype_filter=None, variant_filter=None):

        if variant_filter is None:
            modified_variant_filter = VariantFilter()
        else:
            modified_variant_filter = copy.deepcopy(variant_filter)
        modified_variant_filter.add_gene(gene_id)

        db_query = self._make_db_query(genotype_filter, modified_variant_filter)
        collection = self._get_family_collection(project_id, family_id)
        if not collection:
            return

        # we have to collect list in memory here because mongo can't sort on xpos,
        # as result size can get too big.
        # need to find a better way to do this.

        variants = []
        for variant_dict in collection.find(db_query).hint([('db_gene_ids', pymongo.ASCENDING), ('xpos', pymongo.ASCENDING)]):
            variant = Variant.fromJSON(variant_dict)
            self.add_annotations_to_variant(variant, project_id)
            if passes_variant_filter(variant, modified_variant_filter):
                variants.append(variant)
        variants = sorted(variants, key=lambda v: v.unique_tuple())
        for v in variants:
            yield v

    def get_single_variant(self, project_id, family_id, xpos, ref, alt):

        elasticsearch_dataset = get_elasticsearch_dataset(project_id, family_id)
        if elasticsearch_dataset is not None:

            chrom, pos = get_chr_pos(xpos)

            variant_id = "%s-%s-%s-%s" % (chrom, pos, ref, alt)
            results = list(self.get_elasticsearch_variants({}, elasticsearch_dataset, project_id, family_id=family_id, variant_id_filter=variant_id))
            if not results:
                return None

            variant = results[0]
            return variant

    def get_variants_cohort(self, project_id, cohort_id, variant_filter=None):

        raise ValueError("Not implemented")

        db_query = self._make_db_query(None, variant_filter)
        collection = self._get_family_collection(project_id, cohort_id)
        for i, variant in enumerate(collection.find(db_query).sort('xpos').limit(settings.VARIANT_QUERY_RESULTS_LIMIT+5)):
            if i > settings.VARIANT_QUERY_RESULTS_LIMIT:
                raise Exception("ERROR: this search exceeded the %s variant result size limit. Please set additional filters and try again." % settings.VARIANT_QUERY_RESULTS_LIMIT)

            yield Variant.fromJSON(variant)

    def get_single_variant_cohort(self, project_id, cohort_id, xpos, ref, alt):

        raise ValueError("Not implemented")

        collection = self._get_family_collection(project_id, cohort_id)
        variant = collection.find_one({'xpos': xpos, 'ref': ref, 'alt': alt})
        return Variant.fromJSON(variant)

    def get_project_variants_in_gene(self, project_id, gene_id, variant_filter=None):

        if variant_filter is None:
            modified_variant_filter = VariantFilter()
        else:
            modified_variant_filter = copy.deepcopy(variant_filter)
        modified_variant_filter.add_gene(gene_id)

        elasticsearch_dataset = get_elasticsearch_dataset(project_id, family_id=None)

        db_query = self._make_db_query(None, modified_variant_filter)
        sys.stderr.write("Project Gene Search: " + str(project_id) + " all variants query: " + str(db_query))

        if elasticsearch_dataset is not None:
            variants = [variant for variant in self.get_elasticsearch_variants(db_query, elasticsearch_dataset, project_id)]
            #variants = sorted(variants, key=lambda v: v.unique_tuple())
            return variants

    def _make_db_query(self, genotype_filter=None, variant_filter=None):
        """
        Caller specifies filters to get_variants, but they are evaluated later.
        Here, we just inspect those filters and see what heuristics we can apply to avoid a full table scan,
        Query here must return a superset of the true get_variants results
        Note that the full annotation isn't stored, so use the fields added by _add_index_fields_to_variant
        """
        db_query = {}

        # genotype filter
        if genotype_filter is not None:
            _add_genotype_filter_to_variant_query(db_query, genotype_filter)

        if variant_filter:
            if variant_filter.locations:
                location_ranges = []
                for i, location in enumerate(variant_filter.locations):
                    if isinstance(location, basestring):
                        chrom, pos_range = location.split(":")
                        start, end = pos_range.split("-")
                        xstart = genomeloc.get_xpos(chrom, int(start))
                        xend = genomeloc.get_xpos(chrom, int(end))
                        variant_filter.locations[i] = (xstart, xend)
                    else:
                        xstart, xend = location

                    location_ranges.append({'$and' : [ {'xpos' : {'$gte': xstart }}, {'xpos' : {'$lte': xend }}] })
                db_query['$or'] = location_ranges

            if variant_filter.so_annotations:
                db_query['db_tags'] = {'$in': variant_filter.so_annotations}
            if variant_filter.genes:
                if getattr(variant_filter, 'exclude_genes'):
                    db_query['db_gene_ids'] = {'$nin': variant_filter.genes}
                else:
                    db_query['db_gene_ids'] = {'$in': variant_filter.genes}
            if variant_filter.ref_freqs:
                for population, freq in variant_filter.ref_freqs:
                    #if population in self._annotator.reference_population_slugs:
                    db_query['db_freqs.' + population] = {'$lte': freq}

        return db_query

    def family_exists(self, project_id, family_id):
        from xbrowse_server.base.models import Family
        family = Family.objects.get(project__project_id=project_id, family_id=family_id)
        return family.has_variant_data()

    def get_family_status(self, project_id, family_id):
        if self.family_exists(project_id, family_id):
            return 'loaded'
        else:
            return 'not_loaded'

    def project_collection_is_loaded(self, project):
        """Returns true if the project collection is fully loaded (this is the
        collection that stores the project-wide set of variants used for gene
        search)."""

        return project.get_elasticsearch_index() is not None
