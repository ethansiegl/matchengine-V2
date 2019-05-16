from typing import Any, Tuple, Union, NewType, List, Dict, AsyncGenerator, Generator
from MatchCriteriaTransform import MatchCriteriaTransform
from MongoDBConnection import MongoDBConnection
from collections import deque, defaultdict
from bson import ObjectId

import pymongo.database
import networkx as nx
import json
import logging

from settings import CLINICAL_PROJECTION, GENOMIC_PROJECTION

logging.basicConfig(level=logging.INFO)
log = logging.getLogger('matchengine')

Trial = NewType("Trial", dict)
ParentPath = NewType("ParentPath", Tuple[Union[str, int]])
MatchClause = NewType("MatchClause", List[Dict[str, Any]])
MatchTree = NewType("MatchTree", nx.DiGraph)
MatchCriterion = NewType("MatchPath", List[Dict[str, Any]])
MultiCollectionQuery = NewType("MongoQuery", dict)
NodeID = NewType("NodeID", int)
MongoQueryResult = NewType("MongoQueryResult", Dict[str, Any])
MongoQuery = NewType("MongoQuery", Dict[str, Any])
GenomicID = NewType("GenomicID", ObjectId)
ClinicalID = NewType("ClinicalID", ObjectId)
RawQueryResult = NewType("RawQueryResult",
                         Tuple[ClinicalID, Dict[GenomicID, Dict[str, Union[MongoQuery, MongoQueryResult]]]])
TrialMatch = NewType("TrialMatch", Dict[str, Any])


def find_matches(sample_ids: list = None, protocol_nos: list = None, debug=False):
    """
    Take a list of sample ids and trial protocol numbers, return a dict of trial matches
    :param sample_ids:
    :param protocol_nos:
    :param debug:
    :return:
    """
    log.info('Beginning trial matching.')

    with open("config/config.json") as config_file_handle:
        config = json.load(config_file_handle)
    match_criteria_transform = MatchCriteriaTransform(config)

    with MongoDBConnection(read_only=True) as db:
        for trial in get_trials(db, protocol_nos):
            log.info("Begin Protocol No: {}".format(trial["protocol_no"]))
            for parent_path, match_clause in extract_match_clauses_from_trial(trial):
                for match_path in get_match_paths(create_match_tree(match_clause)):
                    try:
                        translated_match_path = translate_match_path(parent_path, match_path, match_criteria_transform)
                        query = add_sample_ids_to_query(translated_match_path, sample_ids, match_criteria_transform)
                        results = [result for result in run_query(db, match_criteria_transform, query)]
                        log.info("Protocol No: {}".format(trial["protocol_no"]))
                        log.info("Parent_path: {}".format(parent_path))
                        log.info("Match_path: {}".format(match_path))
                        log.info("Results: {}".format(len(results)))
                        if debug:
                            log.info("Query: {}".format(query))
                        log.info("")

                        create_trial_match(db, results, parent_path, trial)
                    except Exception as e:
                        logging.error("ERROR: {}".format(e))
                        raise e


def get_trials(db: pymongo.database.Database, protocol_nos: list = None) -> Generator[Trial, None, None]:
    trial_find_query = dict()
    projection = {'protocol_no': 1, 'nct_id': 1, 'treatment_list': 1, '_summary': 1, 'status': 1}
    if protocol_nos is not None:
        trial_find_query['protocol_no'] = {"$in": [protocol_no for protocol_no in protocol_nos]}

    for trial in db.trial.find(trial_find_query, projection):
        # TODO toggle with flag
        if trial['status'].lower().strip() in {"open to accrual"}:
            yield Trial(trial)
        else:
            logging.info('Trial %s is closed, skipping' % trial['protocol_no'])


def extract_match_clauses_from_trial(trial: Trial) -> Generator[List[Tuple[ParentPath, MatchClause]], None, None]:
    """
    Pull out all of the matches from a trial curation.
    Return the parent path and the values of that match clause
    :param trial:
    :return:
    """

    # find all match clauses. place everything else (nested dicts/lists) on a queue
    process_q = deque()
    for key, val in trial.items():

        # include top level match clauses
        if key == 'match':
            # TODO uncomment, for now don't match on top level match clauses
            continue
        #     parent_path = ParentPath(tuple())
        #     yield parent_path, val
        else:
            process_q.append((tuple(), key, val))

    # process nested dicts to find more match clauses
    while process_q:
        path, parent_key, parent_value = process_q.pop()
        if isinstance(parent_value, dict):
            for inner_key, inner_value in parent_value.items():
                if inner_key == 'match':
                    # TODO toggle with flag
                    # skip closed dose and arm levels
                    if 'dose' in path and 'arm' in path and 'step' in path \
                            and parent_value['level_suspended'].lower().strip() == 'y':
                        log.info('Dose level suspended {0}'.format(path))
                        continue

                    # TODO don't match on open arms inside closed steps
                    elif 'arm' in path and 'step' in path \
                            and parent_value['arm_suspended'].lower().strip() == 'y':
                        log.info('Arm suspended {0}'.format(path))
                        continue
                    else:
                        parent_path = ParentPath(path + (parent_key, inner_key))
                        yield parent_path, inner_value
                else:
                    process_q.append((path + (parent_key,), inner_key, inner_value))
        elif isinstance(parent_value, list):
            for index, item in enumerate(parent_value):
                process_q.append((path + (parent_key,), index, item))


def create_match_tree(match_clause: MatchClause) -> MatchTree:
    process_q: deque[Tuple[NodeID, Dict[str, Any]]] = deque()
    graph = nx.DiGraph()
    node_id: NodeID = NodeID(1)
    graph.add_node(0)  # root node is 0
    graph.nodes[0]['criteria_list'] = list()
    for item in match_clause:
        process_q.append((NodeID(0), item))
    while process_q:
        parent_id, values = process_q.pop()
        parent_is_or = True if graph.nodes[parent_id].setdefault('is_or', False) else False
        for label, value in values.items():  # label is 'and', 'or', 'genomic' or 'clinical'
            if label == 'and':
                for item in value:
                    process_q.append((parent_id, item))
            elif label == "or":
                graph.add_edges_from([(parent_id, node_id)])
                graph.nodes[node_id]['criteria_list'] = list()
                graph.nodes[node_id]['is_or'] = True
                for item in value:
                    process_q.append((node_id, item))
                node_id += 1
            elif parent_is_or:
                graph.add_edges_from([(parent_id, node_id)])
                graph.nodes[node_id]['criteria_list'] = [values]
                node_id += 1
            else:
                graph.nodes[parent_id]['criteria_list'].append({label: value})
    return MatchTree(graph)


def get_match_paths(match_tree: MatchTree) -> Generator[MatchCriterion, None, None]:
    leaves = list()
    for node in match_tree.nodes:
        if match_tree.out_degree(node) == 0:
            leaves.append(node)
    for leaf in leaves:
        path = nx.shortest_path(match_tree, 0, leaf) if leaf != 0 else [leaf]
        match_path = MatchCriterion(list())
        for node in path:
            match_path.extend(match_tree.nodes[node]['criteria_list'])
        yield match_path


def translate_match_path(path: ParentPath,
                         match_criterion: MatchCriterion,
                         match_criteria_transformer: MatchCriteriaTransform) -> MultiCollectionQuery:
    """
    Translate the keys/values from the trial curation into keys/values used in a genomic/clinical document.
    Uses an external config file ./config/config.json
    :param path:
    :param match_criterion:
    :param match_criteria_transformer:
    :return:
    """
    categories = defaultdict(list)
    for criteria in match_criterion:
        for genomic_or_clinical, values in criteria.items():
            and_query = dict()
            for trial_key, trial_value in values.items():
                trial_key_settings = match_criteria_transformer.trial_key_mappings[genomic_or_clinical].setdefault(
                    trial_key.upper(),
                    dict())

                if 'ignore' in trial_key_settings and trial_key_settings['ignore']:
                    continue

                sample_value_function_name = trial_key_settings.setdefault('sample_value', 'nomap')
                sample_function = MatchCriteriaTransform.__dict__[sample_value_function_name]
                args = dict(sample_key=trial_key.upper(),
                            trial_value=trial_value,
                            parent_path=path,
                            trial_path=genomic_or_clinical,
                            trial_key=trial_key)
                args.update(trial_key_settings)
                and_query.update(sample_function(match_criteria_transformer, **args))
            categories[genomic_or_clinical].append(and_query)
    return MultiCollectionQuery(categories)


def add_sample_ids_to_query(query: MultiCollectionQuery,
                            sample_ids: List[str],
                            match_criteria_transformer: MatchCriteriaTransform) -> MultiCollectionQuery:
    if sample_ids is not None:
        query[match_criteria_transformer.CLINICAL].append({
            "SAMPLE_ID": {
                "$in": sample_ids
            },
        })
    else:
        # TODO add flag
        # default to matching on alive patients only
        query[match_criteria_transformer.CLINICAL].append({
            "VITAL_STATUS": "alive",
        })
    return query


def run_query(db: pymongo.database.Database,
              match_criteria_transformer: MatchCriteriaTransform,
              multi_collection_query: MultiCollectionQuery) -> Generator[RawQueryResult, None, RawQueryResult]:
    """
    Execute mongo query
    :param db:
    :param match_criteria_transformer:
    :param multi_collection_query:
    :return:
    """
    # TODO refactor into smaller functions
    all_results = defaultdict(lambda: defaultdict(dict))

    # get clinical docs first
    clinical_docs, clinical_ids = execute_clinical_query(db, match_criteria_transformer, multi_collection_query)

    for doc in clinical_docs:
        all_results[doc['_id']][match_criteria_transformer.CLINICAL][doc['_id']] = doc

    # If no clinical docs are returned, skip executing genomic portion of the query
    if not clinical_docs:
        return RawQueryResult(tuple())

    # iterate over all queries
    for items in multi_collection_query.items():
        genomic_or_clinical, queries = items

        # skip clinical queries as they've already been executed
        if genomic_or_clinical == match_criteria_transformer.CLINICAL and clinical_docs:
            continue

        join_field = match_criteria_transformer.collection_mappings[genomic_or_clinical]['join_field']
        projection = {join_field: 1}
        if genomic_or_clinical == 'genomic':
            projection.update(GENOMIC_PROJECTION)

        for query in queries:
            if clinical_docs:
                query.update({join_field: {"$in": list(clinical_ids)}})

            results = [result for result in db[genomic_or_clinical].find(query, projection)]
            result_ids = {result[join_field] for result in results}

            # short circuit if no values are returned
            if not result_ids:
                return RawQueryResult(tuple())

            results_to_remove = clinical_ids - result_ids
            for result_to_remove in results_to_remove:
                if result_to_remove in all_results:
                    del all_results[result_to_remove]
            clinical_ids.intersection_update(result_ids)

            if not clinical_docs:
                return RawQueryResult(tuple())
            else:
                for doc in results:
                    if doc[join_field] in clinical_ids:
                        all_results[doc[join_field]][genomic_or_clinical][doc['_id']] = {
                            "result": doc,
                            "query": query
                        }

    for clinical_id, doc in all_results.items():
        yield RawQueryResult((clinical_id, doc))


def execute_clinical_query(db: pymongo.database.Database,
                           match_criteria_transformer: MatchCriteriaTransform,
                           multi_collection_query: MultiCollectionQuery):
    clinical_docs = dict()
    clinical_ids = set()
    if match_criteria_transformer.CLINICAL in multi_collection_query:
        collection = match_criteria_transformer.CLINICAL
        join_field = match_criteria_transformer.primary_collection_unique_field
        projection = {join_field: 1}
        projection.update(CLINICAL_PROJECTION)
        query = {"$and": multi_collection_query[collection]}
        clinical_docs = [doc for doc in db[collection].find(query, projection)]
        clinical_ids = set([doc['_id'] for doc in clinical_docs])

    return clinical_docs, clinical_ids


def create_trial_match(db: pymongo.database.Database,
                       raw_query_result: List[RawQueryResult],
                       parent_path: ParentPath,
                       trial: Trial):

    for result in raw_query_result:
        clinical_id = result[0]
        clinical_doc = result[1]['clinical'][clinical_id]

        genomic_id = None
        genomic_doc = dict()
        query = dict()
        if len(result[1]['genomic']) > 0:
            for key in result[1]['genomic']:
                genomic_id = key

            genomic_doc = result[1]['genomic'][genomic_id]['result']
            query = result[1]['genomic'][genomic_id]['query']
            if 'CLINICAL_ID' in query:
                del query['CLINICAL_ID']

        genomic_details = get_genomic_details(format_details(genomic_doc), query)

        trial_match = {
            **get_trial_details(parent_path, trial),
            **format_details(clinical_doc),
            **genomic_details,
            'clinical_id': clinical_id,
            'genomic_id': genomic_id,
            'sort_order': '',
            'query': query
        }
        db.trial_match_test.insert(trial_match)


def get_genomic_details(genomic_doc, query):
    if genomic_doc is None:
        return {}

    mmr_map_rev = {
        'Proficient (MMR-P / MSS)': 'MMR-P/MSS',
        'Deficient (MMR-D / MSI-H)': 'MMR-D/MSI-H'
    }

    # for clarity
    hugo_symbol = 'TRUE_HUGO_SYMBOL'
    true_protein = 'TRUE_PROTEIN_CHANGE'
    cnv = 'CNV_CALL'
    variant_classification = 'TRUE_VARIANT_CLASSIFICATION'
    variant_category = 'VARIANT_CATEGORY'
    wildtype = 'WILDTYPE'
    mmr_status = 'MMR_STATUS'

    alteration = ''
    is_variant = 'gene'

    # determine if match was gene- or variant-level
    if true_protein in query and query[true_protein] is not None:
        is_variant = 'variant'

    # add wildtype calls
    if wildtype in genomic_doc and genomic_doc[wildtype] is True:
        alteration += 'wt '

    # add gene
    if hugo_symbol in genomic_doc and genomic_doc[hugo_symbol] is not None:
        alteration += genomic_doc[hugo_symbol]

    # add mutation
    if true_protein in genomic_doc and genomic_doc[true_protein] is not None:
        alteration += ' %s' % genomic_doc[true_protein]

    # add cnv call
    elif cnv in genomic_doc and genomic_doc[cnv] is not None:
        alteration += ' %s' % genomic_doc[cnv]

    # add variant classification
    elif variant_classification in genomic_doc and genomic_doc[variant_classification] is not None:
        alteration += ' %s' % genomic_doc[variant_classification]

    # add structural variation
    elif variant_category in genomic_doc and genomic_doc[variant_category] == 'SV':
        alteration += ' Structural Variation'

    # add mutational signtature
    elif variant_category in genomic_doc \
            and genomic_doc[variant_category] == 'SIGNATURE' \
            and mmr_status in genomic_doc \
            and genomic_doc[mmr_status] is not None:
        alteration += mmr_map_rev[genomic_doc[mmr_status]]

    return {
        'match_type': is_variant,
        'genomic_alteration': alteration,
        **genomic_doc
    }


def format_details(clinical_doc):
    return {key.lower(): val for key, val in clinical_doc.items() if key != "_id"}


def get_trial_details(parent_path: ParentPath, trial: Trial) -> TrialMatch:
    """
    Extract relevant details from a trial curation to include in the trial_match document
    :param parent_path:
    :param trial:
    :return:
    """
    treatment_list = parent_path[0] if 'treatment_list' in parent_path else None
    step = parent_path[1] if 'step' in parent_path else None
    step_no = parent_path[2] if 'step' in parent_path else None
    arm = parent_path[3] if 'arm' in parent_path else None
    arm_no = parent_path[4] if 'arm' in parent_path else None
    dose = parent_path[5] if 'dose' in parent_path else None

    trial_match = dict()
    trial_match['protocol_no'] = trial['protocol_no']
    trial_match['coordinating_center'] = trial['_summary']['coordinating_center']
    trial_match['nct_id'] = trial['nct_id']

    if 'step' in parent_path and 'arm' in parent_path and 'dose' in parent_path:
        trial_match['code'] = trial[treatment_list][step][step_no][dose]['level_code']
        trial_match['internal_id'] = trial[treatment_list][step][step_no][dose]['level_internal_id']
    elif 'step' in parent_path and 'arm' in parent_path:
        trial_match['code'] = trial[treatment_list][step][step_no][arm][arm_no]['arm_code']
        trial_match['internal_id'] = trial[treatment_list][step][step_no][arm][arm_no]['arm_internal_id']
    elif 'step' in parent_path:
        trial_match['code'] = trial[treatment_list][step][step_no]['step_code']
        trial_match['internal_id'] = trial[treatment_list][step][step_no][dose]['step_internal_id']

    return TrialMatch(trial_match)


if __name__ == "__main__":
    # find_matches(protocol_nos=['***REMOVED***'])
    # find_matches(sample_ids=["***REMOVED***"], protocol_nos=None)
    find_matches(protocol_nos=['***REMOVED***'])
    # find_matches(sample_ids=None, protocol_nos=None)
